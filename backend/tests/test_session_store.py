# -*- coding: utf-8 -*-
"""会话持久化（SQLite）与重启恢复。"""
from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.schemas.session import SessionMeta
from app.services import session_store


def _meta(sid: str = "abc123") -> SessionMeta:
    return SessionMeta(session_id=sid, idea="测试创意", status="idle")


def test_create_and_load_roundtrip(data_dirs):
    session_store.create_session(_meta())
    loaded = session_store.load_session("abc123")
    assert loaded.idea == "测试创意"
    assert loaded.status == "idle"


def test_recovery_after_restart(data_dirs):
    """模拟重启：写盘后重新 load，状态不丢。"""
    m = _meta("restart1")
    m.status = "stage_completed"
    m.current_stage = "script_generation"
    m.stages_completed = ["script_generation"]
    m.artifacts = {"script_generation": {"title": "测试片", "characters": [{"name": "猫"}]}}
    session_store.save_session(m)
    loaded = session_store.load_session("restart1")
    assert loaded.status == "stage_completed"
    assert loaded.current_stage == "script_generation"
    assert loaded.stages_completed == ["script_generation"]
    assert loaded.artifacts["script_generation"]["title"] == "测试片"


def test_not_found(data_dirs):
    with pytest.raises(AppError) as e:
        session_store.load_session("nonexist")
    assert e.value.code == "SESSION_NOT_FOUND"


def test_list_filters_by_owner(data_dirs):
    session_store.save_session(SessionMeta(session_id="a1", owner_id="u1", idea="A1"))
    session_store.save_session(SessionMeta(session_id="a2", owner_id="u1", idea="A2"))
    session_store.save_session(SessionMeta(session_id="b1", owner_id="u2", idea="B1"))
    own = session_store.list_sessions("u1")
    assert {s.session_id for s in own} == {"a1", "a2"}
    assert session_store.list_sessions("u2")[0].session_id == "b1"
