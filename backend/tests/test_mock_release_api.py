import json

import httpx
import pytest

from app.core import config
from app.core.errors import AppError
from app.services import auth, recovery, session_store
from app.schemas.session import SessionCreate


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["workflow", "multi_agent"])
async def test_mock_failure_resume_checks_and_owner_guard(orch, monkeypatch, mode):
    from app.main import app
    from app.api.deps import get_orchestrator
    monkeypatch.setenv("TRIAL_MODE_ENABLED", "false")
    user = auth.login(auth.generate_invite_codes(1)[0])
    other = auth.login(auth.generate_invite_codes(1)[0])
    headers = {"Authorization": "Bearer " + user["token"]}
    meta = orch.create(SessionCreate(idea="Mock 恢复", orchestration_mode=mode), owner_id=user["user_id"])
    if mode == "multi_agent":
        from app.models.llm_client import LLMClient
        orch.llm = LLMClient()
    original = orch.llm.generate_json
    calls = []
    def fail_once(*args, **kwargs):
        if args[2].__name__ != "ScriptArtifact":
            return original(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:
            raise AppError("MOCK_INTERRUPTED", "模拟中断", 503)
        return original(*args, **kwargs)
    monkeypatch.setattr(orch.llm, "generate_json", fail_once)
    app.dependency_overrides[get_orchestrator] = lambda: orch
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
            url = "/api/sessions/" + meta.session_id
            failed = await client.post(url + "/execute/script_generation")
            assert "模拟中断" in failed.text
            status = (await client.get(url + "/recovery")).json()
            assert status["can_resume"] and status["mock"] and status["jobs"] == []
            resumed = await client.post(url + "/resume", json={"execution_id": status["execution_id"]})
            assert '"type": "done"' in resumed.text and len(calls) == 2
            saved = (await client.get(url)).json()
            assert saved["status"] == "stage_completed" and len(saved["creation_checks"]) == 1
            assert (await client.post(url + "/checks/script_generation")).status_code == 200
            assert len((await client.get(url)).json()["creation_checks"]) == 2
            assert (await client.post(url + "/checks/script_generation", headers={"Authorization": "Bearer " + other["token"]})).status_code == 404
            assert (await client.post(url + "/checks/video_generation")).status_code == 409
            monkeypatch.setattr(config.settings, "mode_mock", False)
            assert (await client.post(url + "/checks/script_generation")).status_code == 409
    finally:
        app.dependency_overrides.pop(get_orchestrator, None)


def test_mock_cannot_resume_unmarked_or_real_execution(orch, monkeypatch):
    from app.services import execution_store
    meta = orch.create(SessionCreate(idea="保留原限制"), owner_id="owner")
    execution_id, _ = execution_store.claim_session(meta.session_id, "script_generation", "generate", {})
    assert not recovery._control_resume_allowed(meta, execution_id)
    recovery.capture(execution_id, {"test": True})
    assert recovery._control_resume_allowed(meta, execution_id)
    monkeypatch.setattr(config.settings, "mode_mock", False)
    assert not recovery._control_resume_allowed(meta, execution_id)
