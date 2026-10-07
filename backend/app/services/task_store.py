# -*- coding: utf-8 -*-
"""短管线任务持久化（SQLite）；进度和订阅由 execution_store 持久事件负责。"""
from __future__ import annotations

import json
import time

from app.core.errors import AppError
from app.core.path_security import validate_session_id
from app.schemas.task import TaskMeta
from app.services import db

_COLS = ["task_id", "type", "owner_id", "status", "input", "result", "error", "created_at", "updated_at"]


def _to_row(meta: TaskMeta) -> dict:
    d = meta.model_dump()
    d["input"] = json.dumps(d["input"], ensure_ascii=False)
    d["result"] = json.dumps(d["result"], ensure_ascii=False) if d["result"] is not None else None
    return d


def _from_row(row) -> TaskMeta:
    d = dict(row)
    d["input"] = json.loads(d["input"] or "{}")
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return TaskMeta.model_validate(d)


def save_task(meta: TaskMeta) -> TaskMeta:
    r = _to_row(meta)
    cols = ",".join(_COLS)
    placeholders = ",".join("?" for _ in _COLS)
    updates = ",".join(f"{c}=excluded.{c}" for c in _COLS if c != "task_id")
    with db.connect() as conn:
        conn.execute(f"INSERT INTO tasks ({cols}) VALUES ({placeholders}) "
                     f"ON CONFLICT(task_id) DO UPDATE SET {updates}",
                     [r[c] for c in _COLS])
    return meta


def create_task(meta: TaskMeta) -> TaskMeta:
    meta.created_at = meta.updated_at = time.time()
    save_task(meta)
    return meta


def load_task(task_id: str) -> TaskMeta:
    validate_session_id(task_id)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if row is None:
        raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
    return _from_row(row)


def list_tasks(owner_id: str) -> list[TaskMeta]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM tasks WHERE owner_id=? ORDER BY created_at DESC", (owner_id,)).fetchall()
    return [_from_row(r) for r in rows]


def mark_running(meta: TaskMeta) -> None:
    meta.status = "running"
    meta.updated_at = time.time()
    save_task(meta)


def mark_completed(meta: TaskMeta, result: dict) -> None:
    meta.status = "completed"
    meta.result = result
    meta.updated_at = time.time()
    save_task(meta)


def save_partial_result(task_id: str, result: dict) -> None:
    """Preserve finished media without overwriting input or concurrent task status."""
    from app.services import execution_store
    execution_id = execution_store.current_execution_id()
    if not execution_id:
        return
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        execution = conn.execute("SELECT owner_id FROM executions WHERE execution_id=? AND entity_type='task' "
                                 "AND entity_id=? AND status IN ('pending','running')",
                                 (execution_id, task_id)).fetchone()
        if not execution:
            raise AppError("PROVIDER_EXECUTION_INACTIVE", "当前任务已停止", 409)
        row = conn.execute("SELECT result FROM tasks WHERE task_id=? AND owner_id=?",
                           (task_id, execution["owner_id"])).fetchone()
        if row is None:
            raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
        merged = {**(json.loads(row["result"]) if row["result"] else {}), **result}
        conn.execute("UPDATE tasks SET result=?,updated_at=? WHERE task_id=? AND owner_id=?",
                     (json.dumps(merged, ensure_ascii=False), time.time(), task_id, execution["owner_id"]))


def mark_failed(meta: TaskMeta, error: str) -> None:
    meta.status = "failed"
    meta.error = error
    meta.updated_at = time.time()
    save_task(meta)
