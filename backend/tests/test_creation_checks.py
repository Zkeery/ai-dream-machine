from copy import deepcopy

import pytest

from app.schemas.session import SessionCreate
from app.services import creation_checks as checks, session_store


async def progress(*args):
    pass


@pytest.mark.asyncio
async def test_generation_edit_restore_and_durable_history(orch):
    meta = orch.create(SessionCreate(idea="离线故事"))
    meta = await orch.execute_stage(meta.session_id, "script_generation", progress)
    first_version = meta.selected_versions["script_generation"]
    assert len(meta.creation_checks) == 1
    original = deepcopy(meta.artifacts["script_generation"])
    edited = deepcopy(original)
    edited["episodes"][0]["content"] = "待在室内就绝对安全，保证不会受到任何伤害。"
    meta = await orch.intervene(meta.session_id, "script_generation", {"operation": "save", "artifact": edited}, progress)
    assert len(meta.creation_checks) == 2
    assert meta.creation_checks[-1]["status"] == "review"
    assert meta.creation_checks[-1]["version_id"] is None
    assert meta.creation_checks[-1]["findings"][0]["location"] == "剧本 · 第1集 · 剧情"
    assert meta.artifact_versions["script_generation"][0]["artifact"] == original
    meta = await orch.intervene(meta.session_id, "script_generation", {"operation": "select", "stage_version_id": first_version}, progress)
    assert meta.artifacts["script_generation"] == original
    assert len(meta.creation_checks) == 2
    assert checks.public_checks(meta)[0]["matches_current"]
    assert not checks.public_checks(meta)[1]["matches_current"]
    saved = session_store.load_session(meta.session_id)
    assert saved.creation_checks == meta.creation_checks
    assert "checks" not in checks.public_checks(saved)[0]


def test_check_outage_keeps_content_and_can_retry(orch, monkeypatch):
    meta = orch.create(SessionCreate(idea="离线故障"))
    meta.artifacts["storyboard"] = {"shots": [{"description": "保证绝对安全"}]}
    before = deepcopy(meta.artifacts)
    with monkeypatch.context() as patch:
        patch.setattr(checks, "check_answer_grounding", lambda *args: (_ for _ in ()).throw(RuntimeError("故障")))
        assert checks.record_check(meta, "storyboard")["status"] == "unavailable"
    assert checks.record_check(meta, "storyboard")["status"] == "review"
    assert meta.artifacts == before and len(meta.creation_checks) == 2


def test_no_silent_pass_no_real_mode_and_source_binding(orch, monkeypatch):
    meta = orch.create(SessionCreate(idea="测试"))
    meta.artifacts["script_generation"] = {"logline": "猫经过街道。"}
    assert checks.record_check(meta, "script_generation")["status"] == "limited"
    meta.artifacts["script_generation"]["knowledge_context"] = {"sources": [{"citation_id": "K1", "text": "新资料"}]}
    checks.record_check(meta, "script_generation")
    assert not checks.public_checks(meta)[0]["matches_current"]
    monkeypatch.setattr(checks.config.settings, "mode_mock", False)
    assert checks.record_check(meta, "script_generation", force=True) is None
    assert len(meta.creation_checks) == 2
