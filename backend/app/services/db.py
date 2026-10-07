# -*- coding: utf-8 -*-
"""SQLite 基础层：连接、建表（幂等）、WAL 模式。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.core import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS invite_codes (
    code TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    used_by TEXT,
    created_at REAL NOT NULL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    invite_code TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    filename TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    original_name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    owner_id TEXT,
    idea TEXT NOT NULL,
    project_type TEXT NOT NULL DEFAULT 'story',
    style TEXT NOT NULL DEFAULT 'realistic',
    episodes INTEGER NOT NULL DEFAULT 1,
    video_ratio TEXT NOT NULL DEFAULT '16:9',
    resolution TEXT NOT NULL DEFAULT '720P',
    expand_idea INTEGER NOT NULL DEFAULT 0,
    video_generation_mode TEXT NOT NULL DEFAULT 'first_frame',
    status TEXT NOT NULL DEFAULT 'idle',
    current_stage TEXT,
    stages_completed TEXT NOT NULL DEFAULT '[]',
    artifacts TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    input TEXT NOT NULL DEFAULT '{}',
    result TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_deletions (
    session_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    deleted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    owner_id TEXT,
    stage TEXT,
    operation TEXT NOT NULL,
    request_key TEXT,
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    worker_pid INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS executions_one_active
    ON executions(entity_type, entity_id) WHERE status IN ('pending', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS executions_request_key
    ON executions(owner_id, request_key) WHERE entity_type='task' AND request_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS executions_entity
    ON executions(entity_type, entity_id, created_at);
CREATE TABLE IF NOT EXISTS execution_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS execution_events_execution
    ON execution_events(execution_id, event_id);
CREATE TABLE IF NOT EXISTS task_requests (
    owner_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    task_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(owner_id, request_key)
);
CREATE TABLE IF NOT EXISTS session_requests (
    session_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(session_id, request_key)
);
"""


def db_path() -> Path:
    return config.DATA_DIR / "app.db"


@contextmanager
def connect():
    """独立连接（线程安全）+ 提交；异常回滚。"""
    conn = sqlite3.connect(str(db_path()), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Existing SQLite projects keep all rows/files. Add only the new metadata.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if "project_type" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN project_type TEXT NOT NULL DEFAULT 'story'")
        for name, default in (("stale_stages", "[]"), ("artifact_versions", "{}"),
                              ("selected_versions", "{}"), ("execution_inputs", "[]"),
                              ("knowledge_library_ids", "[]"), ("model_selection", "{}"),
                              ("orchestration_mode", "workflow"), ("agent_runs", "{}")):
            if name not in columns:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
