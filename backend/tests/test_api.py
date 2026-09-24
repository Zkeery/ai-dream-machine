# -*- coding: utf-8 -*-
"""API 参数校验、错误结构、健康检查、鉴权（第三刀）。"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.services import auth  # noqa: E402


def _client():
    from app.main import app

    return TestClient(app)


def _auth_headers(client) -> dict:
    code = auth.generate_invite_codes(1)[0]
    r = client.post("/api/auth/login", json={"invite_code": code})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_health():
    c = _client()
    r = c.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_unauthenticated_session_401():
    c = _client()
    assert c.post("/api/sessions", json={"idea": "测试"}).status_code == 401
    assert c.get("/api/sessions").status_code == 401


def test_create_session_empty_idea_unified_error(data_dirs):
    c = _client()
    h = _auth_headers(c)
    r = c.post("/api/sessions", json={"idea": ""}, headers=h)
    assert r.status_code == 422
    body = r.json()
    assert "error" in body and "code" in body["error"] and "message" in body["error"]


def test_create_session_episodes_out_of_range(data_dirs):
    c = _client()
    h = _auth_headers(c)
    r = c.post("/api/sessions", json={"idea": "测试", "episodes": 99}, headers=h)
    assert r.status_code == 422


def test_create_session_ok(data_dirs):
    c = _client()
    h = _auth_headers(c)
    r = c.post("/api/sessions", json={"idea": "一只猫的故事", "episodes": 2}, headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"]
    assert body["status"] == "idle"
    assert body["owner_id"]


def test_get_session_not_found_unified_error(data_dirs):
    c = _client()
    h = _auth_headers(c)
    r = c.get("/api/sessions/nonexist123", headers=h)
    assert r.status_code == 404
    body = r.json()
    assert body["error"]["code"] == "SESSION_NOT_FOUND"
    # 不泄露堆栈
    assert "Traceback" not in str(body)
