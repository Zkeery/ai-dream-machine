# -*- coding: utf-8 -*-
"""Durable execution claims and events, independent of any SSE connection.

The current model clients do not persist a resumable provider job ID. On process
restart we therefore interrupt unfinished work instead of submitting it again.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from contextvars import ContextVar
from typing import AsyncIterator, Awaitable, Callable

from app.core.errors import AppError
from app.services import db

ACTIVE = ("pending", "running")
INTERRUPTED_MESSAGE = "服务重启或执行被中断，无法确认模型侧结果；已保留输入和已有产物，请核对后主动重试。"
_workers: dict[str, asyncio.Task] = {}
_current_execution: ContextVar[str | None] = ContextVar("dream_execution_id", default=None)


def current_execution_id() -> str | None:
    """Parent ID for a stage's persisted completion ledger, including partial runs."""
    return _current_execution.get()


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _ensure_requests(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_requests (
        execution_id TEXT PRIMARY KEY, payload TEXT NOT NULL, model_usage TEXT,
        source_execution_id TEXT, created_at REAL NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_execution_requests_source ON execution_requests(source_execution_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS execution_resume_keys (
        owner_id TEXT NOT NULL, entity_type TEXT NOT NULL, entity_key TEXT NOT NULL,
        request_key TEXT NOT NULL, source_execution_id TEXT NOT NULL, execution_id TEXT NOT NULL,
        PRIMARY KEY(owner_id,entity_type,entity_key,request_key))""")


def _save_request(conn, execution_id: str, payload: dict, usage: dict | None = None,
                  source_execution_id: str | None = None) -> None:
    _ensure_requests(conn)
    conn.execute("INSERT INTO execution_requests VALUES(?,?,?,?,?)",
                 (execution_id, json.dumps(payload, ensure_ascii=False),
                  json.dumps(usage, ensure_ascii=False) if usage is not None else None,
                  source_execution_id, time.time()))


def request_payload(execution_id: str) -> dict:
    """Original request only; never reconstruct a retry from current UI input."""
    with db.connect() as conn:
        _ensure_requests(conn)
        row = conn.execute("SELECT payload FROM execution_requests WHERE execution_id=?", (execution_id,)).fetchone()
    if row is None:
        raise AppError("RESUME_INPUT_MISSING", "该历史任务未保存可恢复的原始请求，请重新确认输入后生成", 409)
    return json.loads(row["payload"])


def claim_resume(source_execution_id: str, owner_id: str,
                 request_key: str | None = None) -> tuple[str, bool]:
    """Atomically resume a failed execution under original input, ownership and quotas."""
    from app.services.cost_control import check_execution_limits
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_requests(conn)
        source = conn.execute("SELECT * FROM executions WHERE execution_id=? AND owner_id=?",
                              (source_execution_id, owner_id)).fetchone()
        if source is None:
            raise AppError("EXECUTION_NOT_FOUND", "执行记录不存在", 404)
        if source["status"] not in ("failed", "interrupted"):
            raise AppError("RESUME_NOT_AVAILABLE", "只有失败或中断的任务可以恢复", 409)
        request = conn.execute("SELECT * FROM execution_requests WHERE execution_id=?", (source_execution_id,)).fetchone()
        if request is None:
            raise AppError("RESUME_INPUT_MISSING", "该历史任务未保存可恢复的原始请求，请重新确认输入后生成", 409)
        session = source["entity_type"] == "session"
        table, key = ("sessions", "session_id") if session else ("tasks", "task_id")
        meta = conn.execute(f"SELECT * FROM {table} WHERE {key}=? AND owner_id=?",
                            (source["entity_id"], owner_id)).fetchone()
        if meta is None or (session and conn.execute("SELECT 1 FROM session_deletions WHERE session_id=?", (source["entity_id"],)).fetchone()):
            raise AppError("SESSION_NOT_FOUND" if session else "TASK_NOT_FOUND", "作品或任务不存在", 404)
        if session and source["operation"] in ("save", "select"):
            raise AppError("RESUME_NOT_AVAILABLE", "编辑和选版操作不支持模型任务恢复", 409)
        if session and meta["current_stage"] != source["stage"]:
            raise AppError("RESUME_INPUT_CHANGED", "当前阶段已变化，请重新核对后生成", 409)
        entity_key = source["entity_id"] if session else ""

        def remember(execution_id):
            if request_key:
                conn.execute("INSERT OR IGNORE INTO execution_resume_keys VALUES(?,?,?,?,?,?)",
                             (owner_id, source["entity_type"], entity_key, request_key, source_execution_id, execution_id))

        if request_key:
            remembered = conn.execute("SELECT * FROM execution_resume_keys WHERE owner_id=? AND entity_type=? AND entity_key=? AND request_key=?",
                                      (owner_id, source["entity_type"], entity_key, request_key)).fetchone()
            if remembered:
                if remembered["source_execution_id"] != source_execution_id:
                    raise AppError("IDEMPOTENCY_CONFLICT", "重复提交标识对应其他恢复请求", 409)
                return remembered["execution_id"], False
            prior = conn.execute("SELECT e.*,r.source_execution_id FROM executions e LEFT JOIN execution_requests r "
                                 "ON r.execution_id=e.execution_id WHERE e.owner_id=? AND e.request_key=? "
                                 "AND e.entity_type=? AND e.entity_id=?",
                                 (owner_id, request_key, source["entity_type"], source["entity_id"])).fetchone()
            if prior:
                if prior["source_execution_id"] != source_execution_id:
                    raise AppError("IDEMPOTENCY_CONFLICT", "重复提交标识对应其他恢复请求", 409)
                remember(prior["execution_id"])
                return prior["execution_id"], False
        # An in-flight or successful recovery of this source is reused even if
        # another tab sends a new key. A failed recovery may be retried explicitly.
        prior = conn.execute("SELECT e.* FROM executions e JOIN execution_requests r ON r.execution_id=e.execution_id "
                             "WHERE r.source_execution_id=? AND e.status IN ('pending','running','completed') "
                             "ORDER BY e.created_at DESC LIMIT 1", (source_execution_id,)).fetchone()
        if prior:
            remember(prior["execution_id"])
            return prior["execution_id"], False
        if conn.execute("SELECT 1 FROM executions WHERE entity_type=? AND entity_id=? AND status IN ('pending','running')",
                        (source["entity_type"], source["entity_id"])).fetchone() or meta["status"] in ("pending", "running"):
            raise AppError("SESSION_RUNNING" if session else "TASK_RUNNING", "当前作品正在执行，请等待结束后再恢复", 409)
        if meta["status"] in ("session_completed", "completed"):
            raise AppError("RESUME_NOT_AVAILABLE", "该作品已完成，不能重新打开旧执行", 409)
        check_execution_limits(conn, owner_id)
        usage = json.loads(request["model_usage"]) if request["model_usage"] else None
        execution_id = _insert(conn, source["entity_type"], source["entity_id"], owner_id,
                               source["stage"], source["operation"], source["input_hash"], request_key, usage)
        _save_request(conn, execution_id, json.loads(request["payload"]), usage, source_execution_id)
        remember(execution_id)
        conn.execute(f"UPDATE {table} SET status='running',error=NULL,updated_at=? WHERE {key}=?",
                     (time.time(), source["entity_id"]))
        return execution_id, True


def _event(conn, execution_id: str, event: dict) -> None:
    conn.execute("INSERT INTO execution_events(execution_id,payload,created_at) VALUES(?,?,?)",
                 (execution_id, json.dumps(event, ensure_ascii=False), time.time()))


def _insert(conn, entity_type: str, entity_id: str, owner_id: str | None,
            stage: str | None, operation: str, input_hash: str, request_key: str | None = None,
            model_usage: dict | None = None) -> str:
    execution_id = uuid.uuid4().hex
    now = time.time()
    conn.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (execution_id, entity_type, entity_id, owner_id, stage, operation,
                  request_key, input_hash, "running", None, os.getpid(), now, now))
    _event(conn, execution_id, {"type": "progress", "stage": stage or operation,
                               "message": "已接收，正在执行", "percent": 0,
                               **({"model_usage": model_usage} if model_usage is not None else {})})
    return execution_id


def claim_session(session_id: str, stage: str, operation: str, payload: dict,
                  request_key: str | None = None) -> tuple[str, bool]:
    """Claim and set running in one transaction, before any model coroutine starts."""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        meta = conn.execute("SELECT * FROM sessions WHERE session_id=? AND NOT EXISTS "
                            "(SELECT 1 FROM session_deletions WHERE session_deletions.session_id=sessions.session_id)",
                            (session_id,)).fetchone()
        if meta is None:
            raise AppError("SESSION_NOT_FOUND", "会话不存在", 404)
        from app.services import model_catalog, session_store
        model_meta = session_store._from_row(meta)
        if stage == "video_generation" and "video_generation_mode" in payload:
            mode = payload["video_generation_mode"]
            if mode not in {"first_frame", "start_end", "reference"}:
                raise AppError("VIDEO_MODE_UNSUPPORTED", "不支持的视频生成模式", 422)
            model_meta.video_generation_mode = mode
        usage = model_catalog.stage_usage(model_meta, stage) if operation in ("generate", "regenerate") else None
        input_hash = fingerprint({"stage": stage, "operation": operation, "payload": payload,
                                  **({"model_usage": usage} if usage is not None else {})})
        if request_key:
            request = conn.execute("SELECT * FROM session_requests WHERE session_id=? AND request_key=?",
                                   (session_id, request_key)).fetchone()
            if request:
                if request["input_hash"] != input_hash:
                    raise AppError("IDEMPOTENCY_CONFLICT", "重复提交标识对应的输入不同，请重新提交", 409)
                return request["execution_id"], False
            old = conn.execute("SELECT * FROM executions WHERE entity_type='session' AND entity_id=? "
                               "AND request_key=?", (session_id, request_key)).fetchone()
            if old:
                if old["input_hash"] != input_hash:
                    raise AppError("IDEMPOTENCY_CONFLICT", "重复提交标识对应的输入不同，请重新提交", 409)
                return old["execution_id"], False
        active = conn.execute("SELECT * FROM executions WHERE entity_type='session' AND entity_id=? "
                              "AND status IN ('pending','running')", (session_id,)).fetchone()
        if active:
            if active["input_hash"] == input_hash:
                if request_key:
                    conn.execute("INSERT INTO session_requests VALUES(?,?,?,?,?)",
                                 (session_id, request_key, input_hash, active["execution_id"], time.time()))
                return active["execution_id"], False
            raise AppError("SESSION_RUNNING", "当前项目正在执行，请等待结束后再操作", 409)
        if meta["status"] == "running":
            raise AppError("SESSION_RUNNING", "当前项目仍在生成，请先查询任务状态", 409)
        if operation == "generate" and meta["status"] == "session_completed":
            raise AppError("SESSION_DONE", "会话已完成", 409)
        from app.services.cost_control import check_execution_limits
        if operation not in ("save", "select"):
            check_execution_limits(conn, meta["owner_id"])
        execution_id = _insert(conn, "session", session_id, meta["owner_id"], stage, operation, input_hash, request_key, usage)
        _save_request(conn, execution_id, payload, usage)
        if request_key:
            conn.execute("INSERT INTO session_requests VALUES(?,?,?,?,?)",
                         (session_id, request_key, input_hash, execution_id, time.time()))
        # Deterministic save/select keep the stage's existing lifecycle status.
        if operation not in ("save", "select"):
            conn.execute("UPDATE sessions SET status='running',error=NULL,updated_at=? WHERE session_id=?",
                         (time.time(), session_id))
        return execution_id, True


def create_task(meta, request_key: str | None = None) -> tuple[str, str, bool]:
    """Persist a new short task and its execution atomically; repeated keys reuse it."""
    input_hash = fingerprint({"type": meta.type, "input": meta.input})
    now = time.time()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if request_key:
            request = conn.execute("SELECT * FROM task_requests WHERE owner_id=? AND request_key=?",
                                   (meta.owner_id, request_key)).fetchone()
            if request:
                if request["input_hash"] != input_hash:
                    raise AppError("IDEMPOTENCY_CONFLICT", "重复提交标识对应的输入不同，请重新提交", 409)
                return request["task_id"], request["execution_id"], False
            old = conn.execute("SELECT * FROM executions WHERE entity_type='task' AND owner_id=? "
                               "AND request_key=?", (meta.owner_id, request_key)).fetchone()
            if old:
                if old["input_hash"] != input_hash:
                    raise AppError("IDEMPOTENCY_CONFLICT", "重复提交标识对应的输入不同，请重新提交", 409)
                return old["entity_id"], old["execution_id"], False
        # Legacy clients without a key still cannot double-submit an active input.
        old = conn.execute("SELECT * FROM executions WHERE entity_type='task' AND owner_id=? "
                           "AND input_hash=? AND status IN ('pending','running')",
                           (meta.owner_id, input_hash)).fetchone()
        if old:
            if request_key:
                conn.execute("INSERT INTO task_requests VALUES(?,?,?,?,?,?)",
                             (meta.owner_id, request_key, input_hash, old["entity_id"], old["execution_id"], now))
            return old["entity_id"], old["execution_id"], False
        from app.services.cost_control import check_execution_limits
        check_execution_limits(conn, meta.owner_id)
        meta.status = "running"
        meta.created_at = meta.updated_at = now
        conn.execute("INSERT INTO tasks(task_id,type,owner_id,status,input,result,error,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,NULL,NULL,?,?)", (meta.task_id, meta.type, meta.owner_id, "running",
                                                       json.dumps(meta.input, ensure_ascii=False), now, now))
        execution_id = _insert(conn, "task", meta.task_id, meta.owner_id, None, meta.type, input_hash, request_key, meta.input.get("model_usage"))
        _save_request(conn, execution_id, meta.input, meta.input.get("model_usage"))
        if request_key:
            conn.execute("INSERT INTO task_requests VALUES(?,?,?,?,?,?)",
                         (meta.owner_id, request_key, input_hash, meta.task_id, execution_id, now))
        return meta.task_id, execution_id, True


def active(entity_type: str, entity_id: str) -> bool:
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM executions WHERE entity_type=? AND entity_id=? "
                            "AND status IN ('pending','running')", (entity_type, entity_id)).fetchone() is not None


def snapshot(entity_type: str, entity_id: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM executions WHERE entity_type=? AND entity_id=? "
                           "ORDER BY created_at DESC LIMIT 1", (entity_type, entity_id)).fetchone()
        if row is None:
            return None
        last = conn.execute("SELECT payload FROM execution_events WHERE execution_id=? "
                            "AND json_extract(payload,'$.type')='progress' "
                            "ORDER BY event_id DESC LIMIT 1", (row["execution_id"],)).fetchone()
        first = conn.execute("SELECT payload FROM execution_events WHERE execution_id=? ORDER BY event_id LIMIT 1",
                             (row["execution_id"],)).fetchone()
        usage = json.loads(first["payload"]).get("model_usage") if first else None
    return {"execution_id": row["execution_id"], "status": row["status"], "stage": row["stage"],
            "last_event": json.loads(last["payload"]) if last else None,
            "started_at": row["created_at"], "updated_at": row["updated_at"], "error": row["error"],
            **({"model_usage": usage} if usage is not None else {})}


def progress(execution_id: str) -> Callable[[str, str, int], Awaitable[None]]:
    async def report(stage: str, message: str, percent: int) -> None:
        with db.connect() as conn:
            _event(conn, execution_id, {"type": "progress", "stage": stage,
                                       "message": message, "percent": percent})
            conn.execute("UPDATE executions SET updated_at=? WHERE execution_id=? AND status='running'",
                         (time.time(), execution_id))
    return report


def finish(execution_id: str, event: dict, status: str, error: str | None = None) -> None:
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute("UPDATE executions SET status=?,error=?,updated_at=? WHERE execution_id=? "
                               "AND status IN ('pending','running')",
                               (status, error, time.time(), execution_id)).rowcount
        if changed:
            _event(conn, execution_id, event)


def fail(execution_id: str, code: str, message: str, interrupted: bool = False) -> None:
    """Store a readable failure while preserving inputs, successful artifacts and history."""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
        if not row or row["status"] not in ACTIVE:
            return
        table, id_column = ("sessions", "session_id") if row["entity_type"] == "session" else ("tasks", "task_id")
        conn.execute(f"UPDATE {table} SET status='failed',error=?,updated_at=? WHERE {id_column}=?",
                     (message, time.time(), row["entity_id"]))
        conn.execute("UPDATE executions SET status=?,error=?,updated_at=? WHERE execution_id=?",
                     ("interrupted" if interrupted else "failed", message, time.time(), execution_id))
        _event(conn, execution_id, {"type": "error", "error": {"code": code, "message": message}})


def launch(execution_id: str, operation: Callable[[], Awaitable[dict]]) -> None:
    """Keep a strong reference until completion; subscribers never own this task."""
    async def worker() -> None:
        token = _current_execution.set(execution_id)
        try:
            event = await operation()
            finish(execution_id, event, "completed")
        except asyncio.CancelledError:
            fail(execution_id, "EXECUTION_INTERRUPTED", INTERRUPTED_MESSAGE, interrupted=True)
            raise
        except AppError as exc:
            fail(execution_id, exc.code, exc.message)
        except Exception:
            fail(execution_id, "INTERNAL_ERROR", "服务内部错误，请保留输入后重试")
        finally:
            _current_execution.reset(token)
            _workers.pop(execution_id, None)

    _workers[execution_id] = asyncio.create_task(worker())


async def stream(execution_id: str, after_event_id: int = 0) -> AsyncIterator[str]:
    """Replay stored events with each subscriber's own cursor, then follow new ones."""
    cursor = after_event_id
    while True:
        with db.connect() as conn:
            events = conn.execute("SELECT event_id,payload FROM execution_events WHERE execution_id=? "
                                  "AND event_id>? ORDER BY event_id LIMIT 200", (execution_id, cursor)).fetchall()
            state = conn.execute("SELECT status,error FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
        for ev in events:
            cursor = ev["event_id"]
            yield f"id: {cursor}\ndata: {ev['payload']}\n\n"
            if json.loads(ev["payload"]).get("type") in ("done", "error"):
                return
        if state is None:
            raise AppError("EXECUTION_NOT_FOUND", "执行记录不存在", 404)
        if state["status"] not in ACTIVE:
            if len(events) == 200:
                continue
            # A reconnect cursor may already include the terminal event.
            with db.connect() as conn:
                terminal = conn.execute("SELECT payload FROM execution_events WHERE execution_id=? "
                                        "ORDER BY event_id DESC LIMIT 1", (execution_id,)).fetchone()
            if terminal:
                yield f"data: {terminal['payload']}\n\n"
            return
        if not events:
            yield ": keep-alive\n\n"
        await asyncio.sleep(0.2)


def _process_alive(pid: int) -> bool:
    if pid == os.getpid():
        return False  # startup has no workers belonging to the previous lifespan
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _saved_session_completion(meta, execution) -> bool:
    """Accept only a finished ledger entry belonging to this exact parent request.

    A targeted run may successfully finish its selected items while leaving the
    stage idle with other stale items. Status alone cannot identify that result.
    """
    if meta["current_stage"] != execution["stage"] or meta["status"] not in ("idle", "stage_completed"):
        return False
    try:
        ledger = json.loads(meta["execution_inputs"] or "[]")
        for record in reversed(ledger):
            if (record.get("parent_execution_id") == execution["execution_id"]
                    and record.get("stage") == execution["stage"]
                    and record.get("status") == "completed"
                    and float(record.get("finished_at") or 0) >= execution["created_at"]):
                return True
    except (TypeError, ValueError, AttributeError):
        return False
    return False


def recover_unfinished() -> dict[str, int]:
    """Do not create model calls. Preserve live other workers, interrupt abandoned work."""
    recovered = {"session": 0, "task": 0}
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT * FROM executions WHERE status IN ('pending','running')").fetchall()
        for row in rows:
            if row["execution_id"] in _workers or _process_alive(row["worker_pid"]):
                continue
            table, id_column = ("sessions", "session_id") if row["entity_type"] == "session" else ("tasks", "task_id")
            meta = conn.execute(f"SELECT * FROM {table} WHERE {id_column}=?", (row["entity_id"],)).fetchone()
            # A crash after saving the result but before the terminal event is
            # recoverable without calling a provider again.
            completed = meta and ((row["entity_type"] == "task" and meta["status"] == "completed" and meta["result"])
                                  or (row["entity_type"] == "session" and row["operation"] not in ("save", "select")
                                      and meta["status"] == "stage_completed" and meta["current_stage"] == row["stage"])
                                  or (row["entity_type"] == "session" and _saved_session_completion(meta, row)))
            if completed:
                from app.services import session_store, task_store
                saved = session_store._from_row(meta) if row["entity_type"] == "session" else task_store._from_row(meta)
                conn.execute("UPDATE executions SET status='completed',updated_at=? WHERE execution_id=?",
                             (time.time(), row["execution_id"]))
                _event(conn, row["execution_id"], {"type": "done", row["entity_type"]: saved.model_dump()})
                continue
            conn.execute(f"UPDATE {table} SET status='failed',error=?,updated_at=? WHERE {id_column}=?",
                         (INTERRUPTED_MESSAGE, time.time(), row["entity_id"]))
            conn.execute("UPDATE executions SET status='interrupted',error=?,updated_at=? WHERE execution_id=?",
                         (INTERRUPTED_MESSAGE, time.time(), row["execution_id"]))
            _event(conn, row["execution_id"], {"type": "error", "error": {"code": "EXECUTION_INTERRUPTED",
                                                                        "message": INTERRUPTED_MESSAGE}})
            recovered[row["entity_type"]] += 1
        # Upgrade legacy rows stranded before durable execution records existed.
        for entity_type, table, id_column in (("session", "sessions", "session_id"), ("task", "tasks", "task_id")):
            legacy = conn.execute(f"SELECT * FROM {table} WHERE status IN ('running','pending') AND NOT EXISTS "
                                  f"(SELECT 1 FROM executions WHERE entity_type=? AND entity_id={table}.{id_column})",
                                  (entity_type,)).fetchall()
            for meta in legacy:
                eid = _insert(conn, entity_type, meta[id_column], meta["owner_id"],
                              meta["current_stage"] if entity_type == "session" else None,
                              "legacy_recovery", fingerprint({"legacy_id": meta[id_column]}))
                conn.execute(f"UPDATE {table} SET status='failed',error=?,updated_at=? WHERE {id_column}=?",
                             (INTERRUPTED_MESSAGE, time.time(), meta[id_column]))
                conn.execute("UPDATE executions SET status='interrupted',error=?,updated_at=? WHERE execution_id=?",
                             (INTERRUPTED_MESSAGE, time.time(), eid))
                _event(conn, eid, {"type": "error", "error": {"code": "EXECUTION_INTERRUPTED",
                                                            "message": INTERRUPTED_MESSAGE}})
                recovered[entity_type] += 1
    return recovered


async def shutdown() -> None:
    workers = list(_workers.items())
    for execution_id, worker in workers:
        # A task canceled before its coroutine starts does not reach its handler.
        fail(execution_id, "EXECUTION_INTERRUPTED", INTERRUPTED_MESSAGE, interrupted=True)
        worker.cancel()
    if workers:
        await asyncio.gather(*(worker for _, worker in workers), return_exceptions=True)
    for execution_id, _ in workers:
        _workers.pop(execution_id, None)
