# -*- coding: utf-8 -*-
"""会话持久化：SQLite 存储（复杂字段 JSON 文本列），产物仍以文件存储。"""
from __future__ import annotations

import json
import time

from app.core.errors import AppError
from app.core.path_security import validate_session_id
from app.schemas.session import SessionMeta
from app.services import db

_COLS = ["session_id", "orchestration_mode", "agent_runs", "owner_id", "idea", "project_type", "style", "episodes", "video_ratio", "resolution", "expand_idea",
         "video_generation_mode", "status", "current_stage", "stages_completed", "artifacts", "error",
         "created_at", "updated_at", "stale_stages", "artifact_versions", "selected_versions", "execution_inputs", "knowledge_library_ids", "model_selection"]

_JSON_DEFAULTS = {"agent_runs": {}, "stages_completed": [], "artifacts": {}, "stale_stages": [],
                  "artifact_versions": {}, "selected_versions": {}, "execution_inputs": [], "knowledge_library_ids": [], "model_selection": {}}


def _to_row(meta: SessionMeta) -> dict:
    d = meta.model_dump()
    d["expand_idea"] = int(d["expand_idea"])
    for key, default in _JSON_DEFAULTS.items():
        d[key] = json.dumps(d.get(key, default), ensure_ascii=False)
    return d


def _from_row(row) -> SessionMeta:
    d = dict(row)
    d["expand_idea"] = bool(d["expand_idea"])
    for key, default in _JSON_DEFAULTS.items():
        d[key] = json.loads(d[key]) if d.get(key) else default
    return SessionMeta.model_validate(d)


def save_session(meta: SessionMeta) -> SessionMeta:
    r = _to_row(meta)
    cols = ",".join(_COLS)
    placeholders = ",".join("?" for _ in _COLS)
    updates = ",".join(f"{c}=excluded.{c}" for c in _COLS if c != "session_id")
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM session_deletions WHERE session_id=?", (meta.session_id,)).fetchone():
            raise AppError("SESSION_NOT_FOUND", "会话不存在", 404)
        conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({placeholders}) "
                     f"ON CONFLICT(session_id) DO UPDATE SET {updates}",
                     [r[c] for c in _COLS])
    return meta


def create_session(meta: SessionMeta) -> SessionMeta:
    meta.created_at = meta.updated_at = time.time()
    save_session(meta)
    return meta


def load_session(session_id: str) -> SessionMeta:
    validate_session_id(session_id)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE session_id=? AND NOT EXISTS "
                           "(SELECT 1 FROM session_deletions WHERE session_deletions.session_id=sessions.session_id)",
                           (session_id,)).fetchone()
    if row is None:
        raise AppError("SESSION_NOT_FOUND", "会话不存在", 404)
    return _from_row(row)


def touch(meta: SessionMeta) -> None:
    meta.updated_at = time.time()
    save_session(meta)


def list_sessions(owner_id: str) -> list[SessionMeta]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE owner_id=? AND NOT EXISTS "
                            "(SELECT 1 FROM session_deletions WHERE session_deletions.session_id=sessions.session_id) "
                            "ORDER BY updated_at DESC", (owner_id,)).fetchall()
    return [_from_row(r) for r in rows]


def delete_session(session_id: str, owner_id: str) -> None:
    """Hide an owned inactive work durably; retain media and reused inputs.

    The same SQLite write lock as execution claiming prevents a generation from
    starting between the active check and the tombstone insertion.
    """
    validate_session_id(session_id)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM sessions WHERE session_id=? AND owner_id=? AND NOT EXISTS "
                           "(SELECT 1 FROM session_deletions WHERE session_deletions.session_id=sessions.session_id)",
                           (session_id, owner_id)).fetchone()
        if row is None:
            raise AppError("SESSION_NOT_FOUND", "会话不存在", 404)
        active = conn.execute("SELECT 1 FROM executions WHERE entity_type='session' AND entity_id=? "
                              "AND status IN ('pending','running')", (session_id,)).fetchone()
        if active or row["status"] in ("pending", "running"):
            raise AppError("SESSION_RUNNING", "当前作品正在生成，请等待完成后再删除", 409)
        conn.execute("INSERT INTO session_deletions (session_id,owner_id,deleted_at) VALUES (?,?,?)",
                     (session_id, owner_id, time.time()))


def update_models(session_id: str, owner_id: str, selection) -> SessionMeta:
    """与执行占用共用写锁，只更新模型偏好，不重写产物或有效性。"""
    from app.services.model_catalog import validate_selection
    validate_session_id(session_id)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM sessions WHERE session_id=? AND owner_id=? AND NOT EXISTS "
                           "(SELECT 1 FROM session_deletions WHERE session_deletions.session_id=sessions.session_id)",
                           (session_id, owner_id)).fetchone()
        if row is None:
            raise AppError("SESSION_NOT_FOUND", "会话不存在", 404)
        if row["status"] in ("pending", "running") or conn.execute(
                "SELECT 1 FROM executions WHERE entity_type='session' AND entity_id=? AND status IN ('pending','running')",
                (session_id,)).fetchone():
            raise AppError("SESSION_RUNNING", "当前项目正在执行，请等待结束后再修改模型", 409)
        value = validate_selection(selection, project_type=row["project_type"])
        conn.execute("UPDATE sessions SET model_selection=?,updated_at=? WHERE session_id=?",
                     (json.dumps(value, ensure_ascii=False), time.time(), session_id))
        updated = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return _from_row(updated)
