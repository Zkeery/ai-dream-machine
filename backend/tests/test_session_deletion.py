"""Owned-work logical deletion, execution races, and retained shared inputs.

Only temporary SQLite/files and in-process HTTP are used; no paid model calls.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_current_user, get_orchestrator
from app.core import config
from app.core.errors import AppError
from app.main import app
from app.schemas.session import SessionMeta
from app.services import auth, db, execution_store, knowledge_store, session_store


@pytest.fixture
def http_app(orch, monkeypatch):
    monkeypatch.setitem(app.dependency_overrides, get_current_user, lambda: "owner")
    monkeypatch.setitem(app.dependency_overrides, get_orchestrator, lambda: orch)
    return app


def create(sid="work", project_type="story", owner_id="owner", **kwargs):
    return session_store.create_session(SessionMeta(session_id=sid, owner_id=owner_id, idea="保留的作品",
                                                    project_type=project_type, **kwargs))


@pytest.mark.asyncio
@pytest.mark.parametrize("project_type", ["story", "comic"])
async def test_delete_owned_work_hides_every_entry_without_removing_shared_inputs(http_app, project_type):
    image = config.IMAGE_DIR / "work" / "owned.png"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b"registered owned image")
    artifacts = {"character_design": {"characters": [{"id": "c1", "selected": str(image), "versions": [str(image)]}]}}
    library = knowledge_store.create_library("owner", "保留知识库")
    meta = create(project_type=project_type, artifacts=artifacts, knowledge_library_ids=[library["library_id"]])
    keep = create("keep")
    create("foreign", owner_id="other")
    # A library and already reused upload belong to the account, not the source work.
    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as client:
        asset = (await client.get("/api/assets")).json()[0]
        reused = (await client.post("/api/assets/reuse", json={"asset_id": asset["asset_id"]})).json()
        copied = auth.require_upload_owner(reused["filename"], "owner")
        assert (await client.delete("/api/sessions/work")).status_code == 204
        assert (await client.get("/api/sessions")).json()[0]["session_id"] == keep.session_id
        assert (await client.get("/api/assets")).json() == []
        assert (await client.get(asset["url"])).status_code == 404
        assert (await client.post("/api/assets/reuse", json={"asset_id": asset["asset_id"]})).status_code == 404
        requests = [
            ("GET", "/api/sessions/work", None),
            ("GET", "/api/sessions/work/artifact/script_generation", None),
            ("GET", "/api/sessions/work/media/image/owned.png", None),
            ("GET", "/api/sessions/work/export", None),
            ("GET", "/api/sessions/work/stream", None),
            ("POST", "/api/sessions/work/continue", None),
            ("POST", "/api/sessions/work/execute/script_generation", None),
            ("POST", "/api/sessions/work/intervene", {"stage": "script_generation", "modifications": {"operation": "save"}}),
            ("PATCH", "/api/sessions/work/knowledge", {"knowledge_library_ids": []}),
            ("DELETE", "/api/sessions/work", None),
        ]
        for method, url, payload in requests:
            response = await client.request(method, url, json=payload)
            assert response.status_code == 404, (method, url, response.text)
            assert response.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert copied.read_bytes() == image.read_bytes()
    assert session_store.load_session("keep").model_dump() == keep.model_dump()
    assert session_store.load_session("foreign").owner_id == "other"
    with db.connect() as connection:
        assert connection.execute("SELECT 1 FROM sessions WHERE session_id=?", (meta.session_id,)).fetchone()
    assert knowledge_store.list_libraries("owner") == [library]
    db.init_db()  # Restart migration leaves the tombstone and historical files intact.
    with pytest.raises(AppError) as error:
        session_store.load_session(meta.session_id)
    assert error.value.status_code == 404 and image.exists()


@pytest.mark.asyncio
async def test_delete_foreign_or_missing_work_is_indistinguishable(http_app):
    create(owner_id="other")
    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as client:
        foreign = await client.delete("/api/sessions/work")
        missing = await client.delete("/api/sessions/missing")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert session_store.load_session("work").owner_id == "other"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "running"])
async def test_delete_rejects_active_execution_even_when_work_status_is_idle(http_app, status):
    create()
    execution_id, _ = execution_store.claim_session("work", "script_generation", "save", {})
    with db.connect() as connection:
        connection.execute("UPDATE executions SET status=? WHERE execution_id=?", (status, execution_id))
    async with AsyncClient(transport=ASGITransport(app=http_app), base_url="http://test") as client:
        response = await client.delete("/api/sessions/work")
    assert response.status_code == 409
    assert response.json()["error"] == {"code": "SESSION_RUNNING", "message": "当前作品正在生成，请等待完成后再删除"}
    assert session_store.load_session("work").status == "idle"
    assert execution_store.active("session", "work")


def test_legacy_running_status_also_prevents_deletion(data_dirs):
    create(status="running")
    with pytest.raises(AppError) as error:
        session_store.delete_session("work", "owner")
    assert error.value.code == "SESSION_RUNNING" and error.value.status_code == 409
    assert session_store.load_session("work").status == "running"


def test_deleted_work_cannot_be_resurrected_or_replay_an_old_execution(data_dirs):
    old = deepcopy(create())
    execution_id, _ = execution_store.claim_session("work", "script_generation", "save", {}, "previous-key")
    execution_store.finish(execution_id, {"type": "done"}, "completed")
    session_store.delete_session("work", "owner")
    for action in (lambda: session_store.save_session(old), lambda: session_store.touch(old),
                   lambda: execution_store.claim_session("work", "script_generation", "save", {}, "previous-key"),
                   lambda: execution_store.claim_session("work", "script_generation", "generate", {})):
        with pytest.raises(AppError) as error:
            action()
        assert error.value.code == "SESSION_NOT_FOUND" and error.value.status_code == 404
    assert session_store.list_sessions("owner") == []


def test_deletion_and_execution_claim_are_atomically_exclusive(data_dirs):
    def compete(sid, barrier, deleting):
        barrier.wait()
        try:
            if deleting:
                session_store.delete_session(sid, "owner")
                return "deleted"
            execution_store.claim_session(sid, "script_generation", "generate", {})
            return "claimed"
        except AppError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as workers:
        for index in range(12):
            sid, barrier = f"race{index}", Barrier(2)
            create(sid)
            left = workers.submit(compete, sid, barrier, True)
            right = workers.submit(compete, sid, barrier, False)
            result = (left.result(), right.result())
            assert result in (("deleted", "SESSION_NOT_FOUND"), ("SESSION_RUNNING", "claimed"))
            if result[0] == "deleted":
                assert not execution_store.active("session", sid)
            else:
                assert session_store.load_session(sid).status == "running"
