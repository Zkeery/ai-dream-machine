# -*- coding: utf-8 -*-
"""会话持久化：SQLite 存储（复杂字段 JSON 文本列），产物仍以文件存储。"""
from __future__ import annotations

import json
import time

from app.core.errors import AppError
from app.core.path_security import validate_session_id
from app.schemas.session import SessionMeta
from app.services import db

_COLS = ["session_id", "owner_id", "idea", "style", "episodes", "video_ratio", "resolution", "expand_idea",
         "video_generation_mode", "status", "current_stage", "stages_completed", "artifacts", "error",
         "created_at", "updated_at"]


def _to_row(meta: SessionMeta) -> dict:
    d = meta.model_dump()
    d["expand_idea"] = int(d["expand_idea"])
    d["stages_completed"] = json.dumps(d["stages_completed"], ensure_ascii=False)
    d["artifacts"] = json.dumps(d["artifacts"], ensure_ascii=False)
    return d


def _from_row(row) -> SessionMeta:
    d = dict(row)
    d["expand_idea"] = bool(d["expand_idea"])
    d["stages_completed"] = json.loads(d["stages_completed"] or "[]")
    d["artifacts"] = json.loads(d["artifacts"] or "{}")
    return SessionMeta.model_validate(d)


def save_session(meta: SessionMeta) -> SessionMeta:
    r = _to_row(meta)
    cols = ",".join(_COLS)
    placeholders = ",".join("?" for _ in _COLS)
    updates = ",".join(f"{c}=excluded.{c}" for c in _COLS if c != "session_id")
    with db.connect() as conn:
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
        row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        raise AppError("SESSION_NOT_FOUND", "会话不存在", 404)
    return _from_row(row)


def touch(meta: SessionMeta) -> None:
    meta.updated_at = time.time()
    save_session(meta)


def list_sessions(owner_id: str) -> list[SessionMeta]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM sessions WHERE owner_id=? ORDER BY updated_at DESC", (owner_id,)).fetchall()
    return [_from_row(r) for r in rows]
