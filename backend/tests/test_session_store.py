# -*- coding: utf-8 -*-
"""会话持久化（SQLite）与重启恢复。"""
from __future__ import annotations

import pytest

from app.core.errors import AppError
from app.schemas.session import SessionMeta
from app.services import db, session_store


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


def test_comic_type_survives_reload_and_owner_listing(data_dirs):
    session_store.create_session(SessionMeta(session_id="comic1", owner_id="u1", idea="漫画冒险", project_type="comic"))
    session_store.create_session(SessionMeta(session_id="story1", owner_id="u1", idea="故事短片"))
    assert session_store.load_session("comic1").project_type == "comic"
    assert session_store.load_session("story1").project_type == "story"
    assert {s.project_type for s in session_store.list_sessions("u1")} == {"story", "comic"}
    assert session_store.list_sessions("u2") == []


def test_legacy_database_adds_story_type_without_losing_artifacts(data_dirs):
    # Recreate only the session table in its pre-comic shape, then apply startup migration twice.
    with db.connect() as conn:
        conn.execute("DROP TABLE sessions")
        conn.executescript(db.SCHEMA.replace("    project_type TEXT NOT NULL DEFAULT 'story',\n", ""))
        conn.execute(
            "INSERT INTO sessions (session_id, owner_id, idea, artifacts, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            ("legacy1", "u1", "旧作品", '{"script_generation":{"title":"保留剧本"}}', 1, 2),
        )
    db.init_db()
    db.init_db()
    loaded = session_store.load_session("legacy1")
    assert loaded.project_type == "story"
    assert loaded.owner_id == "u1"
    assert loaded.artifacts["script_generation"]["title"] == "保留剧本"
    assert loaded.created_at == 1
