# -*- coding: utf-8 -*-
"""第三刀：邀请码登录与账号隔离测试（mock，不调真实模型）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.services import auth  # noqa: E402


@pytest.fixture
def client(data_dirs):
    from app.main import app

    return TestClient(app)


def _login(client, code):
    r = client.post("/api/auth/login", json={"invite_code": code})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- 邀请码 ----------

def test_generate_invite_codes_format(data_dirs):
    codes = auth.generate_invite_codes(3)
    assert len(codes) == 3
    for c in codes:
        assert len(c) == 8
        assert all(ch not in "0O1I" for ch in c)
    # 重复生成不冲突
    codes2 = auth.generate_invite_codes(3)
    assert len(set(codes) & set(codes2)) == 0


def test_login_invalid_code(client, data_dirs):
    r = client.post("/api/auth/login", json={"invite_code": "00000000"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "INVITE_INVALID"


def test_login_revoked_code(client, data_dirs):
    code = auth.generate_invite_codes(1)[0]
    auth.revoke_invite_code(code)
    r = client.post("/api/auth/login", json={"invite_code": code})
    assert r.status_code == 403


def test_relogin_same_code_returns_same_user(client, data_dirs):
    code = auth.generate_invite_codes(1)[0]
    t1 = _login(client, code)
    me1 = client.get("/api/auth/me", headers=_headers(t1)).json()
    t2 = _login(client, code)
    me2 = client.get("/api/auth/me", headers=_headers(t2)).json()
    assert me1["user_id"] == me2["user_id"]


def test_logout_revokes_token(client, data_dirs):
    code = auth.generate_invite_codes(1)[0]
    token = _login(client, code)
    r = client.post("/api/auth/logout", headers=_headers(token))
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=_headers(token)).status_code == 401


# ---------- 认证 ----------

def test_protected_requires_auth(client, data_dirs):
    assert client.post("/api/sessions", json={"idea": "测试"}).status_code == 401
    assert client.get("/api/sessions").status_code == 401
    assert client.post("/api/upload").status_code == 401


def test_forged_token_401(client, data_dirs):
    r = client.get("/api/sessions", headers=_headers("deadbeef" * 8))
    assert r.status_code == 401


# ---------- 账号隔离 ----------

def test_isolation_list_and_access(client, data_dirs):
    code_a = auth.generate_invite_codes(1)[0]
    code_b = auth.generate_invite_codes(1)[0]
    ta = _login(client, code_a)
    tb = _login(client, code_b)
    ua = client.get("/api/auth/me", headers=_headers(ta)).json()["user_id"]
    ub = client.get("/api/auth/me", headers=_headers(tb)).json()["user_id"]
    assert ua != ub

    # A 创建会话
    r = client.post("/api/sessions", json={"idea": "A 的创意"}, headers=_headers(ta))
    assert r.status_code == 200
    sid_a = r.json()["session_id"]
    assert r.json()["owner_id"] == ua

    # B 创建会话
    r = client.post("/api/sessions", json={"idea": "B 的创意"}, headers=_headers(tb))
    sid_b = r.json()["session_id"]

    # A 的列表只含自己的会话
    list_a = client.get("/api/sessions", headers=_headers(ta)).json()
    ids_a = {s["session_id"] for s in list_a}
    assert sid_a in ids_a and sid_b not in ids_a

    # B 访问 A 的会话 → 404（不泄露）
    assert client.get(f"/api/sessions/{sid_a}", headers=_headers(tb)).status_code == 404
    assert client.post(f"/api/sessions/{sid_a}/continue", headers=_headers(tb)).status_code == 404
    assert client.get(f"/api/sessions/{sid_a}/export", headers=_headers(tb)).status_code == 404

    # A 能读自己的会话
    assert client.get(f"/api/sessions/{sid_a}", headers=_headers(ta)).status_code == 200


def test_upload_requires_auth_and_records_owner(client, data_dirs):
    code = auth.generate_invite_codes(1)[0]
    token = _login(client, code)
    uid = client.get("/api/auth/me", headers=_headers(token)).json()["user_id"]
    files = {"file": ("a.jpg", b"fake-image-bytes", "image/jpeg")}
    r = client.post("/api/upload", files=files, headers=_headers(token))
    assert r.status_code == 200
    # 上传索引（SQLite）记录了归属
    fname = Path(r.json()["file_path"]).name
    assert auth.get_upload(fname)["owner_id"] == uid
