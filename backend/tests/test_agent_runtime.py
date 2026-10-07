import pytest
from pydantic import ValidationError

from app.core.errors import AppError
from app.schemas.session import SessionCreate
from app.services import session_store
from app.services.agent_runtime import AgentAction, AgentPlan, AgentRuntime, PlannerIntent, BRIEF, CONTROL_DECISION, MAX_DECISIONS


async def noop(*args):
    pass


class Decisions:
    def __init__(self, role="writer", research=False, revise=False):
        self.role, self.research, self.revise = role, research, revise
        self.actions = ([{"tool": "search_knowledge", "note": "检索设定", "query": "猫"},
                         {"tool": "finish", "note": "研究结论：保留雨夜场景"}] if research else []) + [
            {"tool": "set_brief", "note": "明确创作要求", "brief": "保留雨夜和一只猫"},
            {"tool": "generate_stage", "note": "生成本阶段"}]
        self.calls, self.reviews = [], 0

    def generate_json(self, system, user, schema, **kwargs):
        assert CONTROL_DECISION.get() is True
        self.calls.append((schema, user))
        assert "JSON Schema" in system
        if schema is PlannerIntent:
            return schema(summary="研究后创作", objective="生成本阶段",
                          preparation=["查证角色设定"] if self.research else [])
        if schema is AgentAction:
            return schema(**self.actions.pop(0))
        self.reviews += 1
        return schema(verdict="revise" if self.revise else "pass", summary="需完善冲突" if self.revise else "结构符合要求", revision_brief="加强剧情冲突")


def setup(orch, role="writer", research=False, revise=False, stage="script_generation"):
    meta = orch.create(SessionCreate(idea="雨夜的猫", orchestration_mode="multi_agent"))
    if research:
        meta.knowledge_library_ids = ["fixture-library"]
    orch.llm = Decisions(role, research, revise)
    generated = []

    async def generate():
        assert CONTROL_DECISION.get() is False
        generated.append(BRIEF.get())
        meta.artifacts[stage] = {"title": "猫", "episodes": [{"episode_number": 1, "content": "猫躲雨"}]}

    return meta, AgentRuntime(orch, meta, stage, noop, generate), generated


@pytest.mark.asyncio
async def test_autonomous_research_handoff_and_brief(orch, monkeypatch):
    meta, runtime, generated = setup(orch, research=True)
    searches = []

    async def search(*args):
        searches.append(args[2])
        return {"sources": [{"text": "雨夜"}]}

    monkeypatch.setattr(orch, "_knowledge_context", search)
    await runtime.run()
    assert searches == ["猫"]
    assert generated == ["保留雨夜和一只猫"]
    assert any("研究结论" in user for _, user in orch.llm.calls)
    assert runtime.state["status"] == "completed"
    assert BRIEF.get() == ""
    assert CONTROL_DECISION.get() is False
    saved = session_store.load_session(meta.session_id)
    assert saved.orchestration_mode == "multi_agent"
    assert saved.agent_runs["script_generation"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_text_revision_is_bounded_and_media_requires_human(orch):
    _, runtime, generated = setup(orch, revise=True)
    await runtime.run()
    assert len(generated) == 2
    assert "加强剧情冲突" in generated[1]
    assert runtime.state["status"] == "needs_review"
    _, media, rendered = setup(orch, role="designer", stage="character_design", revise=True)
    await media.run()
    assert len(rendered) == 1
    assert media.state["revision_count"] == 0
    assert media.state["status"] == "needs_review"


@pytest.mark.asyncio
async def test_resume_after_generation_does_not_render_twice(orch, monkeypatch):
    meta, runtime, generated = setup(orch, role="designer", stage="character_design")
    original = runtime.event

    async def crash(role, action, summary):
        await original(role, action, summary)
        if action == "generate_stage":
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(runtime, "event", crash)
    with pytest.raises(RuntimeError):
        await runtime.run()
    # Exercise the persisted checkpoint, not the in-memory artifacts.
    loaded = session_store.load_session(meta.session_id)
    loaded.artifacts = {}
    resumed = AgentRuntime(orch, loaded, "character_design", noop, runtime.generate)
    resumed.state = loaded.agent_runs["character_design"][0]
    await resumed.run()
    assert len(generated) == 1
    assert loaded.artifacts["character_design"]["title"] == "猫"


@pytest.mark.asyncio
async def test_unknown_decision_and_budget_never_replay(orch):
    _, runtime, generated = setup(orch)
    runtime.state["pending_decision"] = "planner"
    with pytest.raises(AppError, match="上次 Agent"):
        await runtime.run()
    assert not orch.llm.calls and not generated
    runtime.state.pop("pending_decision")
    runtime.state["decisions"] = MAX_DECISIONS
    with pytest.raises(AppError) as exc:
        await runtime.run()
    assert exc.value.code == "AGENT_BUDGET_EXCEEDED"
    assert not orch.llm.calls


@pytest.mark.asyncio
async def test_researcher_cannot_generate(orch):
    _, runtime, generated = setup(orch, research=True)
    orch.llm.actions[0] = {"tool": "generate_stage", "note": "越权生成"}
    with pytest.raises(AppError) as exc:
        await runtime.run()
    assert exc.value.code == "AGENT_TOOL_FORBIDDEN"
    assert generated == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["MODEL_OUTPUT_INVALID", "MODEL_REQUEST_INTERRUPTED"])
async def test_review_failure_preserves_output_without_repeating_generation(orch, monkeypatch, error_code):
    from app.services.agent_runtime import AgentReview
    meta, runtime, generated = setup(orch, role="designer", stage="character_design")
    original = orch.llm.generate_json

    def decision(system, user, schema, **kwargs):
        if schema is AgentReview:
            raise AppError(error_code, "审稿失败", 502)
        return original(system, user, schema, **kwargs)

    monkeypatch.setattr(orch.llm, "generate_json", decision)
    await runtime.run()
    assert len(generated) == 1
    assert runtime.state["status"] == "needs_review"
    assert runtime.state["reviews"][-1]["verdict"] == "manual"
    assert runtime.state["review_error"]["code"] == error_code
    assert runtime.state["output"] == meta.artifacts["character_design"]
    assert "pending_decision" not in runtime.state


@pytest.mark.asyncio
async def test_invalid_planner_uses_host_owned_plan_once(orch, monkeypatch):
    _, runtime, generated = setup(orch)
    original = orch.llm.generate_json

    def decision(system, user, schema, **kwargs):
        if schema is PlannerIntent:
            raise AppError("MODEL_OUTPUT_INVALID", "无效计划", 502)
        return original(system, user, schema, **kwargs)

    monkeypatch.setattr(orch.llm, "generate_json", decision)
    await runtime.run()
    assert runtime.state["planner_fallback"]["code"] == "MODEL_OUTPUT_INVALID"
    assert runtime.state["plan"]["tasks"][0]["id"] == "t1"
    assert runtime.state["status"] == "completed"
    assert len(generated) == 1


def test_host_controls_ids_roles_dependencies_and_omits_empty_research():
    intent = PlannerIntent(summary="设计角色", objective="保留原创设定", preparation=["外观", "服装"])
    plan = intent.compile("designer", has_knowledge=True)
    assert [(t.id, t.role, t.depends_on) for t in plan.tasks] == [
        ("t1", "researcher", []), ("t2", "researcher", []), ("t3", "designer", ["t1", "t2"])]
    assert len(intent.compile("designer", has_knowledge=False).tasks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code,allowed", [("MODEL_OUTPUT_INVALID", True), ("MODEL_REQUEST_INTERRUPTED", False)])
async def test_revision_resume_requires_known_response(orch, monkeypatch, error_code, allowed):
    from app.services import recovery
    _, runtime, generated = setup(orch, revise=True)
    original = runtime.generate

    async def generate():
        if generated:
            raise AppError(error_code, "修订失败", 502)
        await original()

    runtime.generate = generate
    with pytest.raises(AppError):
        await runtime.run()
    assert runtime.state["revision_error"] == error_code
    assert "output" in runtime.state
    monkeypatch.setattr(recovery, "_verify_agent_checkpoint", lambda *args: runtime.state)
    assert recovery._control_resume_allowed(runtime.meta, runtime.state["execution_id"]) is allowed


def test_plan_rejects_cycles_unknown_tools_and_legacy_default():
    with pytest.raises(ValidationError):
        AgentPlan(summary="bad", tasks=[{"id": "a", "role": "writer", "objective": "x", "depends_on": ["a"]}])
    with pytest.raises(ValidationError):
        AgentAction(tool="execute_shell", note="bad")
    assert SessionCreate(idea="old client").orchestration_mode == "workflow"


def test_plan_accepts_descriptive_ids_and_sorts_valid_forward_dependencies():
    plan = AgentPlan(summary="合法的任务图", tasks=[
        {"id": "design-character-and-setting-visuals", "role": "designer", "objective": "生成角色与场景", "depends_on": ["research-character-visual-reference"]},
        {"id": "research-character-visual-reference", "role": "researcher", "objective": "研究设定"},
    ])
    assert plan.tasks[0].role == "researcher"
    assert plan.tasks[1].depends_on == [plan.tasks[0].id]
    with pytest.raises(ValidationError):
        AgentPlan(summary="无效引用", tasks=[{"id": "t1", "role": "designer", "objective": "画图", "depends_on": ["script_generation"]}])
    with pytest.raises(ValidationError):
        AgentPlan(summary="重复ID", tasks=[{"id": "t1", "role": "designer", "objective": "画图"}] * 2)


@pytest.mark.asyncio
async def test_invalid_model_output_is_not_an_unknown_network_request(orch, monkeypatch):
    meta, runtime, generated = setup(orch)

    def invalid(*args, **kwargs):
        raise AppError("MODEL_OUTPUT_INVALID", "模型输出格式不合法，多次重试仍失败", 502,
                       details={"schema": "AgentPlan", "validation_errors": [{"field": "tasks.0.role", "type": "literal_error"}]})

    monkeypatch.setattr(orch.llm, "generate_json", invalid)
    with pytest.raises(AppError):
        await runtime.run()
    saved = session_store.load_session(meta.session_id).agent_runs["script_generation"][0]
    assert "pending_decision" not in saved
    assert saved["decision_error"]["code"] == "MODEL_OUTPUT_INVALID"
    assert saved["decision_error"]["details"]["validation_errors"][0]["field"] == "tasks.0.role"
    assert saved["events"][-1]["action"] == "validation_failed"
    assert CONTROL_DECISION.get() is False
    assert generated == []
