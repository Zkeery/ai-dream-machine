"""Settings/sandbox authorization from a server allowlist; isolated real tokens.

No real .env edits, model requests, or application server restarts.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_orchestrator
from app.core import config
from app.main import app
from app.schemas.session import SessionMeta
from app.schemas.task import TaskMeta
from app.services import auth, session_store, task_store


@pytest.fixture
def client(orch, monkeypatch):
    monkeypatch.setattr(config.settings, "admin_user_ids", frozenset())
    monkeypatch.setitem(app.dependency_overrides, get_orchestrator, lambda: orch)
    return TestClient(app)


def login(client, **extra):
    code = auth.generate_invite_codes(1)[0]
    response = client.post("/api/auth/login", json={"invite_code": code, **extra})
    assert response.status_code == 200
    return response.json(), code


def headers(account):
    return {"Authorization": "Bearer " + account["token"]}


@pytest.mark.parametrize("endpoint", ["/api/settings", "/api/admin/sandbox"])
def test_management_routes_require_valid_login_and_admin(client, endpoint):
    unauthenticated = client.get(endpoint)
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "AUTH_REQUIRED"
    forged_token = client.get(endpoint, headers={"Authorization": "Bearer forged-token", "X-Is-Admin": "true"})
    assert forged_token.status_code == 401
    ordinary, _ = login(client)
    forbidden = client.get(endpoint, headers={**headers(ordinary), "X-Role": "admin", "X-User-Id": "admin"}, params={"is_admin": "true"})
    assert forbidden.status_code == 403
    assert forbidden.json()["error"] == {"code": "ADMIN_REQUIRED", "message": "仅管理员可访问此功能"}


def test_first_signup_and_client_role_claims_do_not_grant_admin(client):
    first, _ = login(client, is_admin=True, role="admin", user_id="fake-admin")
    second, _ = login(client, is_admin=True)
    assert first["is_admin"] is False and second["is_admin"] is False
    for account in (first, second):
        me = client.get("/api/auth/me", headers=headers(account)).json()
        assert me["is_admin"] is False and me["user_id"] == account["user_id"]
    assert first["user_id"] != "fake-admin"


def test_allowlisted_user_role_is_returned_in_login_and_me_and_checked_each_request(client, monkeypatch):
    administrator, code = login(client)
    other, _ = login(client)
    monkeypatch.setattr(config.settings, "admin_user_ids", frozenset({administrator["user_id"]}))
    relogin = client.post("/api/auth/login", json={"invite_code": code}).json()
    assert relogin["user_id"] == administrator["user_id"] and relogin["is_admin"] is True
    assert client.get("/api/auth/me", headers=headers(administrator)).json()["is_admin"] is True
    assert client.get("/api/auth/me", headers=headers(other)).json()["is_admin"] is False
    for endpoint in ("/api/settings", "/api/admin/sandbox"):
        assert client.get(endpoint, headers=headers(administrator)).status_code == 200
        assert client.get(endpoint, headers=headers(other)).status_code == 403
    # Existing tokens contain no trusted role claim; revocation is server-side.
    monkeypatch.setattr(config.settings, "admin_user_ids", frozenset())
    assert client.get("/api/auth/me", headers=headers(administrator)).json()["is_admin"] is False
    assert client.get("/api/settings", headers=headers(administrator)).status_code == 403


def test_admin_sandbox_keeps_original_owner_counts_and_excludes_deleted_works(client, monkeypatch):
    administrator, _ = login(client)
    other, _ = login(client)
    monkeypatch.setattr(config.settings, "admin_user_ids", frozenset({administrator["user_id"]}))
    for sid, owner, project_type in (("story", administrator["user_id"], "story"),
                                     ("comic", administrator["user_id"], "comic"),
                                     ("gone", administrator["user_id"], "story"),
                                     ("foreign", other["user_id"], "story")):
        session_store.create_session(SessionMeta(session_id=sid, owner_id=owner, project_type=project_type, idea="隔离作品"))
    session_store.delete_session("gone", administrator["user_id"])
    task_store.create_task(TaskMeta(task_id="own-task", owner_id=administrator["user_id"], type="literary_video"))
    task_store.create_task(TaskMeta(task_id="other-task", owner_id=other["user_id"], type="literary_video"))
    response = client.get("/api/admin/sandbox", headers=headers(administrator))
    assert response.json() == {"status": "ok", "session_count": 2, "task_count": 1}


def test_ordinary_users_keep_business_creation_and_public_liveness(client):
    ordinary, _ = login(client)
    assert client.get("/api/health").json() == {"status": "ok"}
    for project_type in ("story", "comic"):
        response = client.post("/api/sessions", json={"project_type": project_type, "idea": "普通用户创意"}, headers=headers(ordinary))
        assert response.status_code == 200
        assert response.json()["owner_id"] == ordinary["user_id"]
    assert len(client.get("/api/sessions", headers=headers(ordinary)).json()) == 2
    assert client.get("/api/tasks", headers=headers(ordinary)).status_code == 200
    assert client.get("/api/knowledge/libraries", headers=headers(ordinary)).status_code == 200
    assert client.get("/api/comic/voices", headers=headers(ordinary)).status_code == 200
