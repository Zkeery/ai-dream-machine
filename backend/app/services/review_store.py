"""Durable review evidence and explicitly approved, same-execution continuation.

Provider observations are immutable evidence. A human decision is separate and
never changes the provider's safe/accepted values. No function submits a model.
"""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from app.core import config
from app.core.errors import AppError
from app.services import db

_current_trace: ContextVar[str | None] = ContextVar("dream_review_trace", default=None)
_current_item: ContextVar[str | None] = ContextVar("dream_review_item", default=None)
MANUAL_REASON = "审查未给出明确理由，请人工确认"


class ReviewRequired(AppError):
    def __init__(self, review_id: str, reason: str):
        super().__init__("REVIEW_REQUIRED", reason, 409)
        self.review_id = review_id


def _ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS content_review_traces (
        review_id TEXT PRIMARY KEY, owner_id TEXT, session_id TEXT, execution_id TEXT,
        stage TEXT, image_path TEXT NOT NULL, image_sha256 TEXT, input_hash TEXT,
        instruction TEXT NOT NULL, model TEXT, protocol TEXT NOT NULL,
        status TEXT NOT NULL, route TEXT, payload TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_owner_session ON content_review_traces(owner_id,session_id,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_review_execution ON content_review_traces(execution_id,status)")


def file_sha(path: str) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _decode(row) -> dict:
    value = dict(row)
    value["evidence"] = json.loads(value.pop("payload"))
    return value


def _scope(conn):
    from app.services import execution_store
    eid = execution_store.current_execution_id()
    execution = conn.execute("SELECT * FROM executions WHERE execution_id=?", (eid,)).fetchone() if eid else None
    if not execution or execution["entity_type"] != "session":
        return None
    snapshot = conn.execute("SELECT input_hash FROM execution_recovery_inputs WHERE execution_id=?", (eid,)).fetchone()
    return execution, snapshot[0] if snapshot else None


def cached(path: str, instruction: str, protocol: str) -> dict | None:
    """Only reuse observations in the explicitly human-resumed execution."""
    from app.services import execution_store
    if not execution_store.current_execution_id():
        return None
    digest = file_sha(path)
    if not digest:
        return None
    with db.connect() as conn:
        _ensure(conn)
        scope = _scope(conn)
        if not scope:
            return None
        execution, input_hash = scope
        approved = conn.execute("SELECT 1 FROM content_review_traces WHERE execution_id=? AND status='approved'",
                                (execution["execution_id"],)).fetchone()
        if not approved:
            return None
        row = conn.execute("""SELECT * FROM content_review_traces WHERE execution_id=?
                            AND image_sha256=? AND instruction=? AND protocol=?
                            AND input_hash=? AND json_extract(payload,'$.item_id') IS ?
                            AND status IN ('passed','approved')
                            ORDER BY created_at DESC LIMIT 1""",
                           (execution["execution_id"], digest, instruction, protocol, input_hash, _current_item.get())).fetchone()
        return _decode(row) if row else None


def begin(path: str, instruction: str, model: str, protocol: str) -> str | None:
    from app.services import execution_store
    if not execution_store.current_execution_id():
        return None
    with db.connect() as conn:
        _ensure(conn)
        scope = _scope(conn)
        if not scope:
            # Standalone mock/schema fixtures have no paid execution to trace.
            # A real VLM call without a trace is rejected by the client itself.
            return None
        execution, input_hash = scope
        from app.services import f1_controls
        payload = {"raw_response": None, "provider_response": None, "parsed_result": None,
                   "error": None, "manual_decision": None,
                   "mode": "mock" if config.settings.mode_mock else "real", "item_id": _current_item.get(),
                   "f1_authorization_id": f1_controls.authorization_id() if f1_controls.enabled() else None}
        rid, now = uuid4().hex, time.time()
        conn.execute("""INSERT INTO content_review_traces
                        (review_id,owner_id,session_id,execution_id,stage,image_path,image_sha256,input_hash,
                         instruction,model,protocol,status,route,payload,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,'started',NULL,?,?,?)""",
                     (rid, execution["owner_id"], execution["entity_id"], execution["execution_id"],
                      execution["stage"], str(path), file_sha(path), input_hash, instruction, model,
                      protocol, json.dumps(payload, ensure_ascii=False), now, now))
        return rid


def bind(review_id: str | None):
    return _current_trace.set(review_id)


def bind_item(item_id: str | None):
    return _current_item.set(item_id)


def reset_item(token):
    _current_item.reset(token)


def reset(token):
    _current_trace.reset(token)


def current_id() -> str | None:
    return _current_trace.get()


def update(review_id: str | None, **fields):
    if not review_id:
        return
    with db.connect() as conn:
        _ensure(conn)
        row = conn.execute("SELECT payload FROM content_review_traces WHERE review_id=?", (review_id,)).fetchone()
        if row:
            payload = json.loads(row[0])
            payload.update(deepcopy(fields))
            conn.execute("UPDATE content_review_traces SET payload=?,updated_at=? WHERE review_id=?",
                         (json.dumps(payload, ensure_ascii=False), time.time(), review_id))


def response(raw, *, provider_response=None, response_id=None, model=None):
    """The VLM calls this BEFORE settlement checks, so uncertain cost retains raw."""
    parsed = None
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON field")
            value[key] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=unique)
        parsed = value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        pass
    fields = {"raw_response": raw, "parsed_response": parsed,
              "parsed_safe": parsed.get("safe") if parsed else None,
              "parsed_reason": parsed.get("reason") if parsed else None,
              "parse_status": "parsed_object" if parsed is not None else "unparseable"}
    if provider_response is not None:
        fields["provider_response"] = provider_response
    if response_id is not None:
        fields["provider_response_id"] = response_id
    if model is not None:
        fields["provider_model"] = model
    update(current_id(), **fields)


def error(review_id: str | None, exc):
    if not review_id:
        return
    update(review_id, error={"code": getattr(exc, "code", type(exc).__name__),
                             "message": getattr(exc, "message", "审查请求未能完成")})
    with db.connect() as conn:
        _ensure(conn)
        conn.execute("UPDATE content_review_traces SET status='failed',route='system_error',updated_at=? WHERE review_id=?",
                     (time.time(), review_id))


def result(review_id: str | None, value: dict, route: str):
    if not review_id:
        return
    update(review_id, parsed_result=value)
    status = "pending" if route == "manual" else "passed" if route == "accepted" else "blocked"
    with db.connect() as conn:
        _ensure(conn)
        conn.execute("UPDATE content_review_traces SET status=?,route=?,updated_at=? WHERE review_id=?",
                     (status, route, time.time(), review_id))


def reused(record: dict, new_path: str):
    update(record["review_id"], last_reused_path=str(new_path), last_reused_at=time.time())


def records(session_id: str, owner_id: str) -> list[dict]:
    with db.connect() as conn:
        _ensure(conn)
        return [_decode(row) for row in conn.execute("SELECT * FROM content_review_traces WHERE session_id=? AND owner_id=? ORDER BY created_at,review_id",
                                                   (session_id, owner_id))]


def manually_rejected(session_id: str, owner_id: str | None) -> bool:
    with db.connect() as conn:
        _ensure(conn)
        return conn.execute("SELECT 1 FROM content_review_traces WHERE session_id=? AND owner_id=? AND route='manual' AND status='rejected'",
                            (session_id, owner_id)).fetchone() is not None


def pending_from_row(row) -> dict:
    record = _decode(row) if not isinstance(row, dict) or "evidence" not in row else row
    parsed = record["evidence"].get("parsed_result") or {}
    reason = parsed.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = MANUAL_REASON
    path = Path(record["image_path"]).resolve()
    kind = "video" if path.is_relative_to(config.VIDEO_DIR.resolve()) else "image"
    return {"review_id": record["review_id"], "execution_id": record["execution_id"],
            "stage": record["stage"], "reason": reason,
            "image_url": f"/api/sessions/{record['session_id']}/media/{kind}/{path.name}",
            "status": "pending"}


def retained_paths(session_id: str, owner_id: str | None) -> set[str]:
    with db.connect() as conn:
        _ensure(conn)
        return {row[0] for row in conn.execute("SELECT image_path FROM content_review_traces WHERE session_id=? AND owner_id=?",
                                               (session_id, owner_id))}


def pending(session_id: str, owner_id: str | None) -> dict | None:
    with db.connect() as conn:
        _ensure(conn)
        row = conn.execute("""SELECT r.* FROM content_review_traces r JOIN executions e ON e.execution_id=r.execution_id
                              WHERE r.session_id=? AND r.owner_id=? AND r.route='manual'
                              AND (r.status='pending' OR (r.status='approved' AND e.status='awaiting_review'))
                              ORDER BY r.created_at DESC LIMIT 1""", (session_id, owner_id)).fetchone()
        return pending_from_row(row) if row else None


def resume_approved(execution_id: str | None) -> bool:
    if not execution_id:
        return False
    with db.connect() as conn:
        _ensure(conn)
        return conn.execute("SELECT 1 FROM content_review_traces WHERE execution_id=? AND route='manual' AND status='approved'",
                            (execution_id,)).fetchone() is not None


def confirm(session_id: str, owner_id: str, review_id: str, accepted: bool,
            current_input_hash: str) -> tuple[str, bool, int]:
    """Claim a same-execution continuation once. Return (eid, launch, cursor)."""
    from app.services import execution_store
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure(conn)
        row = conn.execute("SELECT * FROM content_review_traces WHERE review_id=? AND session_id=? AND owner_id=?",
                           (review_id, session_id, owner_id)).fetchone()
        if not row:
            raise AppError("REVIEW_NOT_FOUND", "审查记录不存在", 404)
        record = _decode(row)
        execution = conn.execute("SELECT * FROM executions WHERE execution_id=? AND entity_id=? AND owner_id=?",
                                 (record["execution_id"], session_id, owner_id)).fetchone()
        session = conn.execute("SELECT status,current_stage FROM sessions WHERE session_id=? AND owner_id=?",
                               (session_id, owner_id)).fetchone()
        if not execution or not session or record["route"] != "manual":
            raise AppError("REVIEW_NOT_APPROVABLE", "该审查不能人工批准继续", 409)
        evidence = record["evidence"]
        previous = evidence.get("manual_decision")
        cursor = int((previous or {}).get("resume_after_event_id", 0))
        if previous:
            if previous.get("accepted") is not accepted:
                raise AppError("REVIEW_DECISION_CONFLICT", "人工决定已保存，不能改变或重复启动", 409)
            if not accepted or execution["status"] != "awaiting_review":
                return record["execution_id"], False, cursor
            newer_pending = conn.execute("SELECT review_id FROM content_review_traces WHERE execution_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
                                         (record["execution_id"],)).fetchone()
            if newer_pending and newer_pending[0] != review_id:
                # Replaying the previous approval must not approve a new image.
                return record["execution_id"], False, cursor
        elif record["status"] != "pending":
            raise AppError("REVIEW_NOT_APPROVABLE", "该审查已处理，不能启动新的生成", 409)
        if execution["status"] != "awaiting_review" or session["status"] != "awaiting_review":
            raise AppError("REVIEW_EXECUTION_STOPPED", "本次执行已停止，不能批准继续", 409)
        if accepted:
            if session["current_stage"] != record["stage"] or not record["input_hash"] or current_input_hash != record["input_hash"]:
                raise AppError("REVIEW_INPUT_CHANGED", "创作设置或原始输入已变化，不能批准原执行", 409)
            if not record["image_sha256"] or file_sha(record["image_path"]) != record["image_sha256"]:
                raise AppError("REVIEW_IMAGE_CHANGED", "待确认图片已变化或不存在，不能批准原执行", 409)
        if accepted and evidence.get("mode") == "real":
            if config.settings.mode_mock or config.settings.disable_outbound_network or not config.settings.aihubmix_api_key:
                raise AppError("REVIEW_RUN_STOPPED", "本次真实运行已关闭，不能继续付费执行", 409)
            from app.services import f1_controls
            authorization = evidence.get("f1_authorization_id")
            if authorization and (not f1_controls.enabled() or f1_controls.authorization_id() != authorization):
                raise AppError("REVIEW_RUN_STOPPED", "本次单次授权已关闭或变化，不能继续", 409)
            calls = conn.execute("""SELECT l.call_id,l.status FROM model_cost_ledger l JOIN executions e ON e.execution_id=l.execution_id
                                    WHERE e.entity_type='session' AND e.entity_id=?""", (session_id,)).fetchall()
            if not calls or any(call["status"] != "completed" for call in calls):
                raise AppError("BUDGET_USAGE_UNCONFIRMED", "已有模型费用尚未确认，不能人工批准继续", 409)
            review_call = evidence.get("cost_call_id")
            if not review_call or not any(call["call_id"] == review_call for call in calls):
                raise AppError("BUDGET_USAGE_UNCONFIRMED", "本次审查费用记录缺失，不能人工批准继续", 409)
        if not previous:
            cursor = conn.execute("SELECT COALESCE(MAX(event_id),0) FROM execution_events WHERE execution_id=?",
                                  (record["execution_id"],)).fetchone()[0]
            evidence["manual_decision"] = {"accepted": accepted, "owner_id": owner_id, "confirmed_at": time.time(),
                                           "resume_after_event_id": cursor}
        elif accepted:
            # A restart may have appended a fresh waiting event. Keep the
            # original human decision, but start this continuation after it.
            cursor = conn.execute("SELECT COALESCE(MAX(event_id),0) FROM execution_events WHERE execution_id=?",
                                  (record["execution_id"],)).fetchone()[0]
            evidence["manual_decision"]["resume_after_event_id"] = cursor
        conn.execute("UPDATE content_review_traces SET status=?,payload=?,updated_at=? WHERE review_id=?",
                     ("approved" if accepted else "rejected", json.dumps(evidence, ensure_ascii=False), time.time(), review_id))
        if accepted:
            changed = conn.execute("UPDATE executions SET status='pending',worker_pid=?,updated_at=? WHERE execution_id=? AND status='awaiting_review'",
                                   (os.getpid(), time.time(), record["execution_id"])).rowcount
            return record["execution_id"], bool(changed), cursor
        return record["execution_id"], True, cursor
