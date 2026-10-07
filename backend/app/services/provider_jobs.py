"""Durable paid-media intents and explicit, owner-bound continuation scopes.

Only provider submission may create a job. A persisted job ID or completed URL
can be queried/downloaded again; an uncertain submission is never resubmitted.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import uuid4

from app.core.errors import AppError
from app.services import db, execution_store

@dataclass
class ResumePlan:
    source: str
    allow_new: bool
    steps: list[str]
    cursor: int = 0

    def __post_init__(self):
        self.lock = RLock()


_resume_plan: ContextVar[ResumePlan | None] = ContextVar("provider_resume_plan", default=None)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_jobs (
    id TEXT PRIMARY KEY, scope_execution_id TEXT NOT NULL, execution_id TEXT NOT NULL,
    owner_id TEXT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, stage TEXT,
    kind TEXT NOT NULL, model TEXT NOT NULL, endpoint TEXT NOT NULL,
    input_hash TEXT NOT NULL, status TEXT NOT NULL, vendor_job_id TEXT,
    result_url TEXT, local_path TEXT, result_sha256 TEXT, error_code TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(scope_execution_id, input_hash)
);
CREATE INDEX IF NOT EXISTS provider_jobs_owner ON provider_jobs(owner_id, entity_id);
CREATE TABLE IF NOT EXISTS provider_resume_links (
    execution_id TEXT PRIMARY KEY, source_execution_id TEXT NOT NULL,
    owner_id TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_job_steps (
    scope_execution_id TEXT NOT NULL, ordinal INTEGER NOT NULL, job_id TEXT NOT NULL,
    PRIMARY KEY(scope_execution_id, ordinal)
);
CREATE TABLE IF NOT EXISTS provider_job_attempts (
    job_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, retry_of TEXT,
    attempt INTEGER NOT NULL DEFAULT 1, http_status INTEGER, rejection_evidence TEXT
);
"""

# A received rejection for the CREATE request is distinct from an asynchronous
# failed job. 408/409 and all unclassified responses may describe accepted work.
REJECTED_HTTP_STATUSES = frozenset({400, 401, 402, 403, 404, 405, 413, 415, 422, 429})
_JOB_SELECT = """SELECT j.*, a.request_hash, a.retry_of, a.attempt,
    a.http_status, a.rejection_evidence FROM provider_jobs j
    LEFT JOIN provider_job_attempts a ON a.job_id=j.id """


def _definitely_rejected(row) -> bool:
    evidence = (row["http_status"] in REJECTED_HTTP_STATUSES
                and row["rejection_evidence"] in {"create_http_response", "verified_legacy_http_402"})
    local_gate = row["rejection_evidence"] == "local_pre_submit" and row["http_status"] is None
    return (row["status"] == "rejected" and (evidence or local_gate)
            and not row["vendor_job_id"] and not row["result_url"])


def _active_jobs(conn, scope: str):
    _steps(conn, scope)
    return conn.execute(_JOB_SELECT + """JOIN provider_job_steps s ON s.job_id=j.id
        WHERE s.scope_execution_id=? ORDER BY s.ordinal""", (scope,)).fetchall()


def _ensure(conn):
    # DDL must precede BEGIN IMMEDIATE; executescript commits any open transaction.
    conn.executescript(_SCHEMA)


def _root(conn, execution_id: str) -> str:
    row = conn.execute("SELECT source_execution_id FROM provider_resume_links WHERE execution_id=?", (execution_id,)).fetchone()
    return row[0] if row else execution_id


def _steps(conn, scope: str) -> list[str]:
    rows = conn.execute("SELECT job_id FROM provider_job_steps WHERE scope_execution_id=? ORDER BY ordinal", (scope,)).fetchall()
    if not rows:
        # Upgrade original intents created before ordered recovery was added.
        rows = conn.execute("SELECT id FROM provider_jobs WHERE scope_execution_id=? ORDER BY created_at,id", (scope,)).fetchall()
        for ordinal, row in enumerate(rows):
            conn.execute("INSERT INTO provider_job_steps VALUES(?,?,?)", (scope, ordinal, row[0]))
    return [row[0] for row in rows]


def _append_step(conn, scope: str, job_id: str):
    ordinal = conn.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM provider_job_steps WHERE scope_execution_id=?", (scope,)).fetchone()[0]
    conn.execute("INSERT INTO provider_job_steps VALUES(?,?,?)", (scope, ordinal, job_id))


def _execution(conn, execution_id: str, owner_id: str | None = None):
    row = conn.execute("SELECT * FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
    if row is None or (owner_id is not None and row["owner_id"] != owner_id):
        raise AppError("EXECUTION_NOT_FOUND", "执行记录不存在", 404)
    if row["entity_type"] == "session" and conn.execute(
        "SELECT 1 FROM session_deletions WHERE session_id=?", (row["entity_id"],)
    ).fetchone():
        raise AppError("SESSION_NOT_FOUND", "作品不存在", 404)
    return row


def _public(row) -> dict:
    uncertain = row["status"] in {"submitting", "unknown"} and not row["vendor_job_id"] and not row["result_url"]
    rejected = _definitely_rejected(row)
    resumable = rejected or (bool(row["vendor_job_id"] or row["result_url"]) and row["status"] != "failed")
    return {"id": row["id"], "kind": row["kind"], "model": row["model"], "status": row["status"],
            "resumable": resumable, "requires_reconciliation": uncertain, "error_code": row["error_code"],
            "retry_requires_submission": rejected, "attempt": row["attempt"] or 1,
            "http_status": row["http_status"]}


def public_status(execution_id: str, owner_id: str) -> dict:
    """Safe UI state: no input text, external identifiers, paths, URLs or secrets."""
    with db.connect() as conn:
        _ensure(conn)
        execution = _execution(conn, execution_id, owner_id)
        source = _root(conn, execution_id)
        jobs = [_public(row) for row in _active_jobs(conn, source) if row["owner_id"] == owner_id]
    uncertain = any(job["requires_reconciliation"] for job in jobs)
    return {"execution_id": execution_id, "source_execution_id": source, "jobs": jobs,
            "requires_reconciliation": uncertain,
            "can_resume": execution["status"] in {"failed", "interrupted"} and bool(jobs)
                          and all(job["resumable"] for job in jobs) and not uncertain}


def validate_resume(source_execution_id: str, owner_id: str, *, entity_type: str | None = None,
                    entity_id: str | None = None, allow_empty: bool = False) -> dict:
    with db.connect() as conn:
        _ensure(conn)
        row = _execution(conn, source_execution_id, owner_id)
        if (entity_type is not None and row["entity_type"] != entity_type) or (entity_id is not None and row["entity_id"] != entity_id):
            raise AppError("EXECUTION_NOT_FOUND", "执行记录不存在", 404)
    result = public_status(source_execution_id, owner_id)
    if result["requires_reconciliation"]:
        raise AppError("PROVIDER_SUBMIT_UNCONFIRMED", "上次提交结果未确认，无法自动续接；请先核对供应商任务状态", 409)
    if not result["can_resume"] and not (allow_empty and not result["jobs"] and row["status"] in {"failed", "interrupted"}):
        raise AppError("PROVIDER_RESUME_UNAVAILABLE", "此执行没有可续接的生成任务", 409)
    return result


@contextmanager
def resume_scope(source_execution_id: str, owner_id: str, *, allow_new: bool = False, allow_empty: bool = False):
    """Replay the frozen original sequence; optionally append unsubmitted work.

    Only trusted execution code that froze/checked the original stage inputs may
    opt into allow_new. Existing slots always require their original fingerprint.
    """
    current = execution_store.current_execution_id()
    if not current:
        raise AppError("PROVIDER_EXECUTION_REQUIRED", "续接必须关联有效执行", 409)
    result = validate_resume(source_execution_id, owner_id, allow_empty=allow_empty)
    with db.connect() as conn:
        _ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
        previous, target = _execution(conn, source_execution_id, owner_id), _execution(conn, current, owner_id)
        if target["status"] not in {"pending", "running"}:
            raise AppError("PROVIDER_EXECUTION_INACTIVE", "当前执行已结束", 409)
        if current == source_execution_id or any(previous[key] != target[key] for key in ("entity_type", "entity_id", "stage")):
            raise AppError("PROVIDER_RESUME_MISMATCH", "续接执行与原作品或阶段不一致", 409)
        source = result["source_execution_id"]
        prior = conn.execute("SELECT source_execution_id FROM provider_resume_links WHERE execution_id=?", (current,)).fetchone()
        if prior and prior[0] != source:
            raise AppError("PROVIDER_RESUME_MISMATCH", "当前执行已绑定另一续接来源", 409)
        conn.execute("INSERT OR IGNORE INTO provider_resume_links VALUES(?,?,?,?)", (current, source, owner_id, time.time()))
        steps = _steps(conn, source)
    token = _resume_plan.set(ResumePlan(source, allow_new, steps))
    try:
        yield result
    finally:
        _resume_plan.reset(token)


@dataclass
class Job:
    id: str
    new: bool
    persisted: bool
    row: dict


def begin(kind: str, endpoint: str, payload: dict) -> Job:
    """Persist an intent before any paid HTTP or reuse only its exact input."""
    now = time.time()
    execution_id = execution_store.current_execution_id()
    fingerprint = hashlib.sha256(json.dumps({"kind": kind, "endpoint": endpoint, "payload": payload},
                                           sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if not execution_id:
        # Direct offline client tests have no execution. Cost control rejects
        # these in production; no production database is touched by such tests.
        return Job(uuid4().hex, True, False, {"status": "submitting", "model": payload["model"], "kind": kind})
    plan = _resume_plan.get()
    # The same mutable plan is copied by asyncio.to_thread's context, so a
    # sequence counter is shared rather than reset for each worker thread.
    with plan.lock if plan else RLock(), db.connect() as conn:
        _ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
        execution = _execution(conn, execution_id)
        if execution["status"] not in {"pending", "running"}:
            raise AppError("PROVIDER_EXECUTION_INACTIVE", "执行已停止，未提交新的生成请求", 409)
        scope = _root(conn, execution_id)
        resuming = scope != execution_id
        if not resuming:
            _steps(conn, scope)
        if resuming and (plan is None or plan.source != scope):
            raise AppError("PROVIDER_RESUME_SCOPE_REQUIRED", "续接必须在原输入恢复范围内执行", 409)
        replaying = bool(resuming and plan.cursor < len(plan.steps))
        if replaying:
            row = conn.execute(_JOB_SELECT + "WHERE j.id=? AND j.scope_execution_id=?", (plan.steps[plan.cursor], scope)).fetchone()
            if row is None or (row["request_hash"] or row["input_hash"]) != fingerprint:
                raise AppError("PROVIDER_INPUT_CHANGED", "当前模型、提示词或参考图与原任务对应位置不一致；未提交新的生成请求", 409)
            active = conn.execute("SELECT job_id FROM provider_job_steps WHERE scope_execution_id=? AND ordinal=?", (scope, plan.cursor)).fetchone()
            if not active or active[0] != row["id"]:
                raise AppError("PROVIDER_RESUME_MISMATCH", "原任务已由另一次续接更新，请刷新任务状态", 409)
        else:
            if resuming:
                if not plan.allow_new:
                    raise AppError("PROVIDER_INPUT_CHANGED", "该输入没有匹配的原生成任务；当前续接不会新建付费任务", 409)
                if any(item["status"] != "completed" for item in _active_jobs(conn, scope)):
                    raise AppError("PROVIDER_SUBMIT_UNCONFIRMED", "原生成任务尚未确认完成，不能追加新的付费生成", 409)
            row = conn.execute(_JOB_SELECT + """WHERE j.scope_execution_id=?
                AND COALESCE(a.request_hash,j.input_hash)=?
                AND NOT EXISTS(SELECT 1 FROM provider_job_attempts newer WHERE newer.retry_of=j.id)
                ORDER BY j.created_at DESC LIMIT 1""", (scope, fingerprint)).fetchone()
        retry_of = None
        attempt = 1
        if row is not None:
            if any(row[key] != execution[key] for key in ("owner_id", "entity_type", "entity_id", "stage")):
                raise AppError("PROVIDER_RESUME_MISMATCH", "生成任务与当前作品不一致", 409)
            if row["status"] == "failed":
                raise AppError("PROVIDER_JOB_FAILED", "供应商已确认该生成失败，请明确选择重新生成", 409)
            if _definitely_rejected(row):
                if not replaying or not plan.allow_new:
                    raise AppError("PROVIDER_RETRY_REQUIRES_SUBMISSION", "原请求未被受理，需通过允许生成的人工续接重新提交", 409)
                retry_of, attempt = row["id"], (row["attempt"] or 1) + 1
            elif not row["vendor_job_id"] and not row["result_url"]:
                raise AppError("PROVIDER_SUBMIT_UNCONFIRMED", "上次提交结果未确认，已停止自动重提；请先核对供应商任务状态", 409)
            if not retry_of:
                if replaying:
                    plan.cursor += 1
                else:
                    _append_step(conn, scope, row["id"])
                    if resuming:
                        plan.cursor += 1
                return Job(row["id"], False, True, dict(row))
        job_id = uuid4().hex
        # Keep the original UNIQUE key intact for older databases. Attempt
        # identity is unique, while request_hash always binds the complete input.
        storage_hash = hashlib.sha256(f"{fingerprint}:{job_id}".encode()).hexdigest() if retry_of else fingerprint
        conn.execute("INSERT INTO provider_jobs(id,scope_execution_id,execution_id,owner_id,entity_type,entity_id,stage,kind,model,endpoint,input_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (job_id, scope, execution_id, execution["owner_id"], execution["entity_type"], execution["entity_id"], execution["stage"], kind, payload["model"], endpoint, storage_hash, "submitting", now, now))
        conn.execute("INSERT INTO provider_job_attempts(job_id,request_hash,retry_of,attempt) VALUES(?,?,?,?)", (job_id, fingerprint, retry_of, attempt))
        row = conn.execute("SELECT * FROM provider_jobs WHERE id=?", (job_id,)).fetchone()
        if retry_of:
            conn.execute("UPDATE provider_job_steps SET job_id=? WHERE scope_execution_id=? AND ordinal=?", (job_id, scope, plan.cursor))
            plan.steps[plan.cursor] = job_id
        else:
            _append_step(conn, scope, job_id)
        if resuming:
            plan.cursor += 1
        return Job(job_id, True, True, dict(row))


def _update(job: Job, **fields):
    fields["updated_at"] = time.time()
    job.row.update(fields)
    if job.persisted:
        with db.connect() as conn:
            conn.execute("UPDATE provider_jobs SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), job.id))


def check_active(job: Job):
    """Cooperative stop for model clients running inside asyncio.to_thread."""
    current = execution_store.current_execution_id()
    if job.persisted and current:
        with db.connect() as conn:
            row = _execution(conn, current)
            if row["status"] not in {"pending", "running"}:
                raise AppError("PROVIDER_EXECUTION_INACTIVE", "执行已停止；已保留供应商任务，可稍后续接", 409)


def submitted(job: Job, vendor_job_id: str):
    if not isinstance(vendor_job_id, str) or not vendor_job_id:
        raise AppError("PROVIDER_RESPONSE_INVALID", "供应商未返回有效任务编号", 502)
    _update(job, vendor_job_id=vendor_job_id, status="job_pending", error_code=None)


def ready(job: Job, url: str):
    if not isinstance(url, str) or not url:
        raise AppError("PROVIDER_RESPONSE_INVALID", "供应商未返回有效产物地址", 502)
    _update(job, result_url=url, status="ready", error_code=None)


def failed(job: Job, code: str, *, uncertain: bool = False):
    # Query/download failures must keep a known provider job available to resume.
    status = "ready" if job.row.get("result_url") else "job_pending" if job.row.get("vendor_job_id") else "unknown" if uncertain else "failed"
    if not uncertain:
        status = "failed"
    _update(job, status=status, error_code=code)


def rejected(job: Job, http_status: int):
    """Record a definite HTTP refusal of CREATE, never a failed remote job."""
    if http_status not in REJECTED_HTTP_STATUSES or job.row.get("vendor_job_id") or job.row.get("result_url"):
        raise AppError("PROVIDER_REJECTION_UNCONFIRMED", "无法确认原请求未被受理", 409)
    code = f"{job.row['kind'].upper()}_SUBMIT_REJECTED"
    if job.persisted:
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT INTO provider_job_attempts(job_id,request_hash,http_status,rejection_evidence)
                VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                http_status=excluded.http_status,rejection_evidence=excluded.rejection_evidence""",
                         (job.id, job.row["input_hash"], http_status, "create_http_response"))
            conn.execute("UPDATE provider_jobs SET status='rejected',error_code=?,updated_at=? WHERE id=?", (code, time.time(), job.id))
    job.row.update(status="rejected", error_code=code, http_status=http_status)


def not_submitted(job: Job, code: str):
    """A local budget/stop gate ran before HTTP; never infer this after POST."""
    if job.row.get("vendor_job_id") or job.row.get("result_url"):
        raise AppError("PROVIDER_REJECTION_UNCONFIRMED", "已有供应商任务，不能标记为未提交", 409)
    if job.persisted:
        with db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""INSERT INTO provider_job_attempts(job_id,request_hash,rejection_evidence)
                VALUES(?,?,'local_pre_submit') ON CONFLICT(job_id) DO UPDATE SET
                http_status=NULL,rejection_evidence='local_pre_submit'""", (job.id, job.row["input_hash"]))
            conn.execute("UPDATE provider_jobs SET status='rejected',error_code=?,updated_at=? WHERE id=?", (code, time.time(), job.id))
    job.row.update(status="rejected", error_code=code)


def reconcile_legacy_http_402(job_id: str, owner_id: str) -> dict:
    """Explicit operator repair only, requiring matching persisted 402 evidence.

    A legacy rejected cost ledger alone is insufficient (old clients classified
    all 4xx as rejected). This method is deliberately not called by any API or
    automatic migration, and cannot reconcile timeout/unknown/async failures.
    """
    with db.connect() as conn:
        _ensure(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(_JOB_SELECT + "WHERE j.id=? AND j.owner_id=?", (job_id, owner_id)).fetchone()
        if row is None:
            raise AppError("PROVIDER_JOB_NOT_FOUND", "生成任务不存在", 404)
        execution = _execution(conn, row["execution_id"], owner_id)
        ledger = conn.execute("SELECT * FROM model_cost_ledger WHERE call_id=?", (job_id,)).fetchone()
        expected = "图片接口返回 402" if row["kind"] == "image" else "视频接口返回 402"
        last_job = conn.execute("SELECT id FROM provider_jobs WHERE execution_id=? ORDER BY created_at DESC,id DESC LIMIT 1", (row["execution_id"],)).fetchone()
        if (row["status"] != "failed" or row["vendor_job_id"] or row["result_url"]
                or row["error_code"] not in {"IMAGE_MODEL_ERROR", "VIDEO_MODEL_ERROR"}
                or execution["status"] not in {"failed", "interrupted"} or execution["error"] != expected
                or ledger is None or ledger["status"] != "rejected" or ledger["owner_id"] != owner_id
                or ledger["execution_id"] != row["execution_id"]
                or last_job is None or last_job[0] != job_id
                or ledger["model"] != row["model"] or ledger["kind"] != row["kind"]):
            raise AppError("PROVIDER_REJECTION_UNCONFIRMED", "缺少与该请求对应的明确 HTTP 402 拒绝证据，不能恢复付费提交", 409)
        conn.execute("""INSERT INTO provider_job_attempts(job_id,request_hash,http_status,rejection_evidence)
            VALUES(?,?,402,'verified_legacy_http_402') ON CONFLICT(job_id) DO UPDATE SET
            http_status=402,rejection_evidence='verified_legacy_http_402'""", (job_id, row["request_hash"] or row["input_hash"]))
        conn.execute("UPDATE provider_jobs SET status='rejected',error_code=?,updated_at=? WHERE id=?",
                     (f"{row['kind'].upper()}_SUBMIT_REJECTED", time.time(), job_id))
        return _public(conn.execute(_JOB_SELECT + "WHERE j.id=?", (job_id,)).fetchone())


def completed(job: Job, path: Path):
    path = Path(path)
    _update(job, local_path=str(path.resolve()), result_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), status="completed", error_code=None)


def retained_local_paths(owner_id: str | None, entity_type: str, entity_id: str) -> set[str]:
    """Completed paid results are recovery assets, even if the stage rolls back."""
    if not owner_id:
        return set()
    with db.connect() as conn:
        _ensure(conn)
        return {row[0] for row in conn.execute(
            "SELECT local_path FROM provider_jobs WHERE owner_id=? AND entity_type=? AND entity_id=? AND local_path IS NOT NULL",
            (owner_id, entity_type, entity_id)) if row[0]}


def reuse_local(job: Job, out_path: Path) -> bool:
    cached = job.row.get("local_path")
    if not cached or job.row.get("status") != "completed":
        return False
    source = Path(cached)
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != job.row.get("result_sha256"):
        return False
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != out_path.resolve():
        shutil.copyfile(source, out_path)
    completed(job, out_path)
    return True
