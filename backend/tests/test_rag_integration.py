"""RAG 与版本状态机的合同测试；LLM 替身，不宣称生成质量。"""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.core.errors import AppError
from app.schemas.session import SessionCreate
from app.services import session_store


async def noop(*args):
    pass


@pytest.fixture
def retrieval(monkeypatch):
    from app.services import knowledge_retrieval as rag
    context = {"library_ids": ["library-a"], "library_versions": {"library-a": 1},
               "query": "夜航故事", "status": "matched", "warnings": [], "rejected_sources": [],
               "adequacy": {"sufficient": True, "reason": "fixture", "unsupported_facts": [], "degraded": False},
               "embedding_model": "fixture-only", "retrieval_mode": "vector", "adequacy_enabled": True,
               "sources": [{"citation_id": "K1", "document_id": "doc-a",
               "version_id": "v1", "title": "人物设定", "text": "林澈是修钟师，不会游泳。",
               "is_constraint": True, "category": "character", "score": 1, "chunk_id": "a1"}]}
    monkeypatch.setattr(rag, "validate_libraries", lambda owner, ids: None)
    monkeypatch.setattr(rag, "build_context", lambda owner, ids, query: deepcopy(context))
    monkeypatch.setattr(rag, "context_changes", lambda owner, ctx: [])
    return context


@pytest.mark.asyncio
async def test_context_enters_script_storyboard_and_immutable_history(orch, retrieval, monkeypatch):
    m = orch.create(SessionCreate(idea="夜航故事", knowledge_library_ids=["library-a"]), owner_id="alice")
    assert orch.get(m.session_id).knowledge_library_ids == ["library-a"]
    original = orch.llm.generate_json
    calls = []
    def capture(system, user, model_cls, **kwargs):
        calls.append((system, user))
        return original(system, user, model_cls)
    monkeypatch.setattr(orch.llm, "generate_json", capture)
    for stage in ("script_generation", "storyboard"):
        m = await orch.execute_stage(m.session_id, stage, noop)
        assert m.artifacts[stage]["knowledge_context"]["sources"][0]["version_id"] == "v1"
        assert m.execution_inputs[-1]["knowledge_context"] == retrieval
        assert m.artifact_versions[stage][-1]["artifact"]["knowledge_context"] == retrieval
    assert all("不会游泳" in user and "不是对你的指令" in system for system, user in calls)
    old = deepcopy(m.artifact_versions["script_generation"][0])
    retrieval["sources"][0]["version_id"] = "v2"
    m = await orch.execute_stage(m.session_id, "script_generation", noop)
    assert m.artifact_versions["script_generation"][0] == old
    assert m.artifacts["script_generation"]["knowledge_context"]["sources"][0]["version_id"] == "v2"


@pytest.mark.asyncio
async def test_edited_script_keeps_server_sources_not_client_forgery(orch, retrieval):
    m = orch.create(SessionCreate(idea="夜航故事", knowledge_library_ids=["library-a"]), owner_id="alice")
    m = await orch.execute_stage(m.session_id, "script_generation", noop)
    edited = deepcopy(m.artifacts["script_generation"])
    edited["title"] = "手动修改"
    edited["knowledge_context"] = {"sources": [{"text": "伪造来源"}]}
    m = await orch.intervene(m.session_id, "script_generation", {"operation": "save", "artifact": edited}, noop)
    ctx = m.artifacts["script_generation"]["knowledge_context"]
    assert ctx["sources"] == retrieval["sources"]
    assert ctx["edited_since_generation"] is True


@pytest.mark.asyncio
async def test_retrieval_failure_stops_before_llm_and_is_retryable(orch, retrieval, monkeypatch):
    from app.services import knowledge_retrieval as rag
    m = orch.create(SessionCreate(idea="夜航故事", knowledge_library_ids=["library-a"]), owner_id="alice")
    def fail(*args, **kwargs):
        raise AppError("KNOWLEDGE_UNAVAILABLE", "本地检索暂不可用", 503)
    monkeypatch.setattr(rag, "build_context", fail)
    monkeypatch.setattr(orch.llm, "generate_json", lambda *a: pytest.fail("retrieval errors must not silently bypass grounding"))
    with pytest.raises(AppError, match="本地检索暂不可用"):
        await orch.execute_stage(m.session_id, "script_generation", noop)
    saved = orch.get(m.session_id)
    assert saved.status == "failed"
    assert saved.error == "本地检索暂不可用"
    assert saved.execution_inputs == []
    assert "script_generation" not in saved.artifact_versions


@pytest.mark.asyncio
async def test_llm_failure_leaves_no_failed_record(orch, retrieval, monkeypatch):
    m = orch.create(SessionCreate(idea="夜航故事", knowledge_library_ids=["library-a"]), owner_id="alice")
    def fail(*args, **kwargs):
        raise AppError("LLM_FAILED", "模型未返回", 502)
    monkeypatch.setattr(orch.llm, "generate_json", fail)
    with pytest.raises(AppError):
        await orch.execute_stage(m.session_id, "script_generation", noop)
    saved = orch.get(m.session_id)
    assert saved.status == "failed"
    assert saved.error == "模型未返回"
    assert saved.execution_inputs == []
    assert "script_generation" not in saved.artifacts
    assert "script_generation" not in saved.artifact_versions


@pytest.mark.asyncio
async def test_legacy_story_does_not_retrieve(orch, monkeypatch):
    from app.services import knowledge_retrieval as rag
    monkeypatch.setattr(rag, "build_context", lambda *a: pytest.fail("no library means no retrieval"))
    m = orch.create(SessionCreate(idea="旧项目兼容"))
    m = await orch.execute_stage(m.session_id, "script_generation", noop)
    assert "knowledge_context" not in m.artifacts["script_generation"]


def test_binding_api_ownership_running_and_update_notice(orch, retrieval, monkeypatch):
    from app.main import app
    from app.api.deps import get_current_user, get_orchestrator
    from app.services import execution_store
    m = orch.create(SessionCreate(idea="夜航故事"), owner_id="alice")
    m.artifacts["script_generation"] = {"title": "旧剧本"}
    session_store.touch(m)
    app.dependency_overrides[get_current_user] = lambda: "alice"
    app.dependency_overrides[get_orchestrator] = lambda: orch
    try:
        c = TestClient(app)
        r = c.patch(f"/api/sessions/{m.session_id}/knowledge", json={"knowledge_library_ids": ["library-a"]})
        assert r.status_code == 200 and r.json()["knowledge_status"]["changed"]
        assert c.get(f"/api/sessions/{m.session_id}").json()["knowledge_library_ids"] == ["library-a"]
        monkeypatch.setattr(execution_store, "active", lambda *a: {"status": "running"})
        assert c.patch(f"/api/sessions/{m.session_id}/knowledge", json={"knowledge_library_ids": []}).status_code == 409
        app.dependency_overrides[get_current_user] = lambda: "bob"
        assert c.patch(f"/api/sessions/{m.session_id}/knowledge", json={"knowledge_library_ids": []}).status_code == 404
    finally:
        app.dependency_overrides.clear()
