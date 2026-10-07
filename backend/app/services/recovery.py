"""Recovery guards for immutable business input, separate from partial outputs."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import AppError
from app.services import db, execution_store, model_catalog, provider_jobs


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: str = Field(min_length=1, max_length=128)


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_recovery_inputs (
        execution_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL,
        snapshot TEXT NOT NULL, created_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_intermediates (
        scope_execution_id TEXT NOT NULL, item_key TEXT NOT NULL, input_hash TEXT NOT NULL,
        value TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(scope_execution_id,item_key))""")


def _root_execution(conn, execution_id: str) -> str:
    execution_store._ensure_requests(conn)
    seen = set()
    while execution_id not in seen:
        seen.add(execution_id)
        row = conn.execute("SELECT source_execution_id FROM execution_requests WHERE execution_id=?", (execution_id,)).fetchone()
        if not row or not row[0]:
            return execution_id
        execution_id = row[0]
    raise AppError("RESUME_INPUT_INVALID", "执行来源记录无效", 409)


def load_intermediate(key: str, inputs: dict, *, execution_id: str | None = None) -> str | None:
    current = execution_id or execution_store.current_execution_id()
    if not current:
        return None
    with db.connect() as conn:
        _ensure(conn)
        scope = _root_execution(conn, current)
        row = conn.execute("SELECT input_hash,value FROM execution_intermediates WHERE scope_execution_id=? AND item_key=?", (scope, key)).fetchone()
    if row:
        if row["input_hash"] != execution_store.fingerprint(inputs):
            raise AppError("RESUME_INPUT_CHANGED", "原任务的扩写输入或模型已变化，不能继续旧任务", 409)
        return row["value"]
    if scope != current:
        raise AppError("RESUME_INPUT_MISSING", "原任务缺少已完成的扩写结果，恢复已停止，未重新调用文本模型", 409)
    return None


def save_intermediate(key: str, inputs: dict, value: str):
    current = execution_store.current_execution_id()
    if not current:
        return
    with db.connect() as conn:
        _ensure(conn)
        scope = _root_execution(conn, current)
        conn.execute("INSERT OR IGNORE INTO execution_intermediates VALUES(?,?,?,?,?)",
                     (scope, key, execution_store.fingerprint(inputs), value, time.time()))


def literary_expansion_input(inputs: dict) -> dict:
    return {"text": (inputs.get("text") or "").strip(),
            "model": model_catalog.resolve(inputs.get("model_selection"), ["text"])["text"], "prompt_version": 1}


def _verify_intermediate(meta, execution_id: str):
    if meta.type == "literary_video" and len((meta.input.get("text") or "").strip()) <= 30:
        if load_intermediate("literary_expansion", literary_expansion_input(meta.input), execution_id=execution_id) is None:
            raise AppError("RESUME_INPUT_MISSING", "原任务没有保存扩写结果，请核对后重新生成", 409)


def execution(execution_id: str, owner_id: str, entity_type: str, entity_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM executions WHERE execution_id=? AND owner_id=? AND entity_type=? AND entity_id=?",
                           (execution_id, owner_id, entity_type, entity_id)).fetchone()
    if row is None:
        raise AppError("EXECUTION_NOT_FOUND", "执行记录不存在", 404)
    return dict(row)


def _file_hashes(value) -> dict:
    paths = set()
    def collect(item):
        if isinstance(item, dict):
            for nested in item.values():
                collect(nested)
        elif isinstance(item, list):
            for nested in item:
                collect(nested)
        elif isinstance(item, str) and item.startswith("/") and Path(item).suffix.lower() in {
            ".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".mp3", ".wav", ".m4a"
        }:
            paths.add(item)
    collect(value)
    result = {}
    for name in sorted(paths):
        path = Path(name)
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    digest.update(block)
            result[name] = digest.hexdigest()
        except OSError:
            result[name] = "missing"
    return result


def session_input(meta, stage: str, request: dict) -> dict:
    from app.services.orchestrator import Orchestrator
    from app.services import knowledge_store
    order = Orchestrator.stage_order(meta)
    if stage not in order:
        raise AppError("UNKNOWN_STAGE", "未知阶段", 404)
    upstream = order[:order.index(stage)]
    effective = meta.model_copy(deep=True)
    if stage == "video_generation" and "video_generation_mode" in request:
        effective.video_generation_mode = request["video_generation_mode"]
    params = effective.model_dump(include={"project_type", "idea", "style", "episodes", "video_ratio", "resolution",
                                          "expand_idea", "video_generation_mode", "model_selection", "knowledge_library_ids"})
    artifacts = {key: meta.artifacts.get(key) for key in upstream}
    if meta.orchestration_mode == "multi_agent":
        params["orchestration_mode"] = "multi_agent"
    libraries, documents = knowledge_store.active_snapshot(meta.owner_id, meta.knowledge_library_ids) if meta.knowledge_library_ids else ([], [])
    # The active stage's own partial outputs/status/version are deliberately
    # excluded: writing a completed first image must not invalidate its retry.
    return {"version": 1, "stage": stage, "parameters": params, "request": request,
            "model_usage": model_catalog.stage_usage(effective, stage),
            "upstream_versions": {key: meta.selected_versions.get(key) for key in upstream},
            "upstream_artifacts": artifacts, "files": _file_hashes([artifacts, request]),
            "knowledge_versions": {row["library_id"]: row["revision"] for row in libraries},
            "knowledge_documents": sorted(row["version_id"] for row in documents)}


def task_input(meta, request: dict) -> dict:
    return {"version": 1, "type": meta.type, "input": meta.input, "request": request,
            "model_usage": model_catalog.task_usage(meta.type, meta.input), "files": _file_hashes(meta.input)}


def capture(execution_id: str, value: dict):
    with db.connect() as conn:
        _ensure(conn)
        conn.execute("INSERT OR IGNORE INTO execution_recovery_inputs VALUES(?,?,?,?)",
                     (execution_id, execution_store.fingerprint(value), json.dumps(value, ensure_ascii=False), time.time()))


def copy_snapshot(source_execution_id: str, target_execution_id: str):
    with db.connect() as conn:
        _ensure(conn)
        count = conn.execute("INSERT OR IGNORE INTO execution_recovery_inputs SELECT ?,input_hash,snapshot,? FROM execution_recovery_inputs WHERE execution_id=?",
                             (target_execution_id, time.time(), source_execution_id)).rowcount
        if not count and not conn.execute("SELECT 1 FROM execution_recovery_inputs WHERE execution_id=?", (target_execution_id,)).fetchone():
            raise AppError("RESUME_INPUT_MISSING", "原任务缺少恢复输入快照", 409)


def verify(execution_id: str, value: dict):
    with db.connect() as conn:
        _ensure(conn)
        row = conn.execute("SELECT input_hash FROM execution_recovery_inputs WHERE execution_id=?", (execution_id,)).fetchone()
        source = conn.execute("SELECT * FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
        if row is None:
            raise AppError("RESUME_INPUT_MISSING", "该历史任务未保存恢复输入快照，请重新确认后生成", 409)
        if row["input_hash"] != execution_store.fingerprint(value):
            raise AppError("RESUME_INPUT_CHANGED", "模型、创作设置或上游素材已变化，不能继续原任务；请核对后重新生成", 409)
        latest = conn.execute("SELECT execution_id FROM executions WHERE entity_type=? AND entity_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                              (source["entity_type"], source["entity_id"])).fetchone()
        # Another explicit save/select/generate makes an old recovery obsolete.
        cursor, visited = latest[0], set()
        execution_store._ensure_requests(conn)
        while cursor != execution_id and cursor not in visited:
            visited.add(cursor)
            parent = conn.execute("SELECT source_execution_id FROM execution_requests WHERE execution_id=?", (cursor,)).fetchone()
            cursor = parent[0] if parent and parent[0] else None
            if cursor is None:
                raise AppError("RESUME_INPUT_CHANGED", "原任务之后已有新的编辑或生成操作，不能继续旧任务", 409)
        if cursor != execution_id:
            raise AppError("RESUME_INPUT_INVALID", "执行来源记录无效，无法继续原任务", 409)


def agent_retry_known(meta, execution_id: str) -> bool:
    """Only received-invalid replies are eligible for a new explicit attempt."""
    with db.connect() as conn:
        root = _root_execution(conn, execution_id)
        row = conn.execute("SELECT error FROM executions WHERE execution_id=?", (root,)).fetchone()
    run = next((run for runs in meta.agent_runs.values() for run in runs if run["execution_id"] == root), None)
    return bool(run and (run.get("decision_error", {}).get("code") == "MODEL_OUTPUT_INVALID"
                        or (not run.get("legacy_invalid_acknowledged") and row and row["error"] == "模型输出格式不合法，多次重试仍失败")))


def _verify_agent_checkpoint(meta, execution_id: str):
    if getattr(meta, "orchestration_mode", "workflow") != "multi_agent":
        return
    with db.connect() as conn:
        root = _root_execution(conn, execution_id)
        original = conn.execute("SELECT error FROM executions WHERE execution_id=?", (root,)).fetchone()
        # Legacy checkpoints left pending_decision set even after a received
        # response failed validation. Classify that known server error read-only.
        legacy_invalid = bool(original and original["error"] == "模型输出格式不合法，多次重试仍失败")
    run = next((run for runs in meta.agent_runs.values() for run in runs if run["execution_id"] == root), None)
    if run is None:
        raise AppError("AGENT_CHECKPOINT_MISSING", "原任务没有 Agent 检查点，请核对后重新生成", 409)
    known = run.get("decision_error", {}).get("code") == "MODEL_OUTPUT_INVALID" or (legacy_invalid and not run.get("legacy_invalid_acknowledged"))
    if run.get("pending_decision") and not known and not (run.get("pending_decision") == "reviewer" and "output" in run):
        raise AppError("AGENT_DECISION_UNCERTAIN", "上次 Agent 请求结果未确认，请核对用量后重新生成；不会自动重复请求", 409)
    return run


def _control_resume_allowed(meta, execution_id: str) -> bool:
    run = _verify_agent_checkpoint(meta, execution_id)
    if not run:
        return False
    if run.get("revision_pending") and run.get("revision_error") != "MODEL_OUTPUT_INVALID":
        # Includes legacy revision checkpoints with no known response evidence.
        return False
    # No provider record means that an interrupted generation cannot be inferred
    # to have failed safely. Only a received-invalid text result may be repeated.
    for task in run.get("tasks", {}).values():
        if task.get("generation_status") in {"in_flight", "failed"}:
            if task.get("generation_error") != "MODEL_OUTPUT_INVALID":
                return False
    return bool("output" in run or agent_retry_known(meta, execution_id)
                or any(t.get("generation_error") == "MODEL_OUTPUT_INVALID" for t in run.get("tasks", {}).values()))


def status(meta, entity_type: str) -> dict:
    entity_id = meta.session_id if entity_type == "session" else meta.task_id
    latest = execution_store.snapshot(entity_type, entity_id)
    if not latest:
        return {"execution_id": None, "source_execution_id": None, "jobs": [], "can_resume": False,
                "requires_reconciliation": False, "reason": "尚无需要恢复的生成任务", "reason_code": None}
    result = provider_jobs.public_status(latest["execution_id"], meta.owner_id)
    result.update(reason=None, reason_code=None)
    if entity_type == "session" and latest["status"] in {"failed", "interrupted"}:
        try:
            _verify_agent_checkpoint(meta, latest["execution_id"])
            if not result["jobs"] and _control_resume_allowed(meta, latest["execution_id"]):
                result["can_resume"] = True
        except AppError as error:
            result.update(can_resume=False, reason=error.message, reason_code=error.code)
            return result
    if result["requires_reconciliation"]:
        result.update(reason="上次提交结果未确认，请先核对供应商任务，避免重复生成", reason_code="PROVIDER_SUBMIT_UNCONFIRMED")
    if result["can_resume"]:
        try:
            request = execution_store.request_payload(latest["execution_id"])
            source = execution(latest["execution_id"], meta.owner_id, entity_type, entity_id)
            if entity_type == "task":
                _verify_intermediate(meta, latest["execution_id"])
            value = session_input(meta, source["stage"], request) if entity_type == "session" else task_input(meta, request)
            verify(latest["execution_id"], value)
        except AppError as error:
            result.update(can_resume=False, reason=error.message, reason_code=error.code)
    return result


def prepare(meta, entity_type: str, source_execution_id: str) -> tuple[dict, dict]:
    entity_id = meta.session_id if entity_type == "session" else meta.task_id
    source = execution(source_execution_id, meta.owner_id, entity_type, entity_id)
    if entity_type == "session":
        _verify_agent_checkpoint(meta, source_execution_id)
    provider_jobs.validate_resume(source_execution_id, meta.owner_id, entity_type=entity_type, entity_id=entity_id,
                                  allow_empty=entity_type == "session" and _control_resume_allowed(meta, source_execution_id))
    request = execution_store.request_payload(source_execution_id)
    if entity_type == "task":
        _verify_intermediate(meta, source_execution_id)
    value = session_input(meta, source["stage"], request) if entity_type == "session" else task_input(meta, request)
    verify(source_execution_id, value)
    return source, request
