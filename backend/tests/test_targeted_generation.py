"""逐项更新只调用选定素材，保留真实依赖和失败后的部分产物。"""
from copy import deepcopy
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.schemas.session import SessionCreate
from app.services import orchestrator as orch_mod, session_store


async def noop(*args):
    pass


async def run_to(orch, stage):
    meta = orch.create(SessionCreate(idea="逐项闭环测试", episodes=1))
    for name in orch_mod.STAGE_ORDER[:orch_mod.STAGE_ORDER.index(stage) + 1]:
        meta = await orch.execute_stage(meta.session_id, name, noop)
    return meta


@pytest.fixture(autouse=True)
def fake_ffmpeg(monkeypatch):
    async def concat(parts, out, audio_path=None):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"final")
        return out
    monkeypatch.setattr(orch_mod, "concat_videos", concat)


@pytest.mark.asyncio
async def test_character_prompt_and_description_reach_only_selected_request(orch, monkeypatch):
    meta = await run_to(orch, "character_design")
    original = deepcopy(meta.artifacts["character_design"])
    calls = []
    generate = orch.image.text_to_image
    def capture(prompt, out, **kwargs):
        calls.append(prompt)
        return generate(prompt, out, **kwargs)
    monkeypatch.setattr(orch.image, "text_to_image", capture)
    meta = await orch.intervene(meta.session_id, "character_design", {
        "operation": "regenerate", "target_ids": ["c1"],
        "prompts": {"c1": "exact custom visual prompt"}, "descriptions": {"c1": "红斗篷猫"}}, noop)
    assert calls == ["exact custom visual prompt"]
    current = meta.artifacts["character_design"]
    assert current["characters"][0]["description"] == "红斗篷猫"
    assert current["characters"][0]["selected"] != original["characters"][0]["selected"]
    assert current["settings"] == original["settings"]
    assert Path(original["characters"][0]["selected"]).exists()
    reloaded = session_store.load_session(meta.session_id)
    assert reloaded.execution_inputs[-1]["generation_request"]["prompts"]["c1"] == calls[0]
    assert reloaded.artifacts["character_design"]["stale_items"] == []


@pytest.mark.asyncio
async def test_setting_description_changes_actual_prompt(orch, monkeypatch):
    meta = await run_to(orch, "character_design")
    calls = []
    generate = orch.image.text_to_image
    def capture(prompt, out, **kwargs):
        calls.append(prompt)
        return generate(prompt, out, **kwargs)
    monkeypatch.setattr(orch.image, "text_to_image", capture)
    await orch.intervene(meta.session_id, "character_design", {
        "target_ids": ["l1"], "descriptions": {"l1": "极简雪原与远山"}}, noop)
    assert len(calls) == 1 and "极简雪原与远山" in calls[0]
    assert "纯环境设定图" in calls[0]
    assert "角色名：" not in calls[0]
    assert "正面全身/半身设定图" not in calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("project_type", ["story", "comic"])
async def test_character_and_setting_use_distinct_generation_prompts(orch, monkeypatch, project_type):
    meta = orch.create(SessionCreate(idea="守塔人与海边灯塔", project_type=project_type, episodes=1))
    await orch.execute_stage(meta.session_id, "script_generation", noop)
    calls = []
    generate = orch.image.text_to_image
    def capture(prompt, out, **kwargs):
        calls.append(prompt)
        return generate(prompt, out, **kwargs)
    monkeypatch.setattr(orch.image, "text_to_image", capture)
    meta = await orch.execute_stage(meta.session_id, "character_design", noop)
    assert len(calls) == 2
    assert "角色名：" in calls[0] and "正面全身/半身设定图" in calls[0]
    assert "场景名：" in calls[1] and "纯环境设定图" in calls[1]
    assert "不添加人物" in calls[1] and "不额外添加人形雕塑" in calls[1]
    assert "角色名：" not in calls[1] and "正面全身/半身设定图" not in calls[1]
    assert meta.artifacts["character_design"]["characters"][0]["prompt"] == calls[0]
    assert meta.artifacts["character_design"]["settings"][0]["prompt"] == calls[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("project_type", ["story", "comic"])
async def test_setting_targeted_override_is_exact_and_keeps_character(orch, monkeypatch, project_type):
    meta = orch.create(SessionCreate(idea="守塔人与海边灯塔", project_type=project_type, episodes=1))
    for stage in ("script_generation", "character_design"):
        meta = await orch.execute_stage(meta.session_id, stage, noop)
    before = deepcopy(meta.artifacts["character_design"])
    calls = []
    generate = orch.image.text_to_image
    def capture(prompt, out, **kwargs):
        calls.append(prompt)
        return generate(prompt, out, **kwargs)
    monkeypatch.setattr(orch.image, "text_to_image", capture)
    override = "An empty lighthouse on a rocky coast at dawn, no people."
    updated = await orch.intervene(meta.session_id, "character_design", {
        "operation": "regenerate", "target_ids": ["l1"], "prompts": {"l1": override}}, noop)
    assert calls == [override]
    after = updated.artifacts["character_design"]
    assert after["characters"] == before["characters"]
    assert after["settings"][0]["selected"] != before["settings"][0]["selected"]
    assert after["settings"][0]["prompt"] == override


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,collection,method", [
    ("reference_generation", "shots", "image_to_image"),
    ("video_generation", "segments", "image_to_video")])
async def test_one_shot_regeneration_keeps_other_outputs(orch, monkeypatch, stage, collection, method):
    meta = await run_to(orch, stage)
    original = deepcopy(meta.artifacts[stage][collection])
    client = orch.image if stage == "reference_generation" else orch.video
    generate = getattr(client, method)
    calls = []
    def capture(*args, **kwargs):
        calls.append(args)
        return generate(*args, **kwargs)
    monkeypatch.setattr(client, method, capture)
    meta = await orch.intervene(meta.session_id, stage, {
        "target_ids": ["s1"], "prompts": {"s1": "selected cinematic shot"}}, noop)
    assert len(calls) == 1 and "selected cinematic shot" in calls[0][1]
    items = meta.artifacts[stage][collection]
    assert items[0]["path"] != original[0]["path"]
    assert items[1] == original[1]
    assert Path(original[0]["path"]).exists()
    assert meta.artifacts[stage]["stale_items"] == []


@pytest.mark.asyncio
async def test_partial_update_keeps_remaining_stale_after_reload_and_blocks_export(orch):
    from app.api.sessions import export_session
    meta = await run_to(orch, "post_production")
    meta.owner_id = "owner"
    session_store.touch(meta)
    old_ref = deepcopy(meta.artifacts["reference_generation"]["shots"])
    old_final = meta.artifacts["post_production"]["final_video"]
    meta = await orch.intervene(meta.session_id, "character_design", {"target_ids": ["c1"]}, noop)
    meta = await orch.intervene(meta.session_id, "reference_generation", {"target_ids": ["s1"]}, noop)
    assert meta.artifacts["reference_generation"]["shots"][1]["path"] == old_ref[1]["path"]
    assert meta.artifacts["reference_generation"]["stale_items"] == ["s2"]
    assert "reference_generation" in meta.stale_stages and meta.status == "idle"
    assert "reference_generation" not in meta.stages_completed
    assert session_store.load_session(meta.session_id).artifacts["reference_generation"]["stale_items"] == ["s2"]
    with pytest.raises(AppError, match="尚未完成"):
        orch.continue_session(meta.session_id)
    with pytest.raises(AppError) as blocked:
        await export_session(meta.session_id, None, "owner", orch)
    assert blocked.value.code == "STALE_EXPORT"
    meta = await orch.intervene(meta.session_id, "reference_generation", {"target_ids": ["s2"]}, noop)
    assert meta.artifacts["reference_generation"]["stale_items"] == []
    meta = await orch.intervene(meta.session_id, "video_generation", {"target_ids": ["s1"]}, noop)
    assert meta.artifacts["video_generation"]["stale_items"] == ["s2"]
    meta = await orch.intervene(meta.session_id, "video_generation", {"target_ids": ["s2"]}, noop)
    assert meta.artifacts["video_generation"]["stale_items"] == []
    with pytest.raises(AppError) as blocked:
        await export_session(meta.session_id, None, "owner", orch)
    assert blocked.value.code == "STALE_EXPORT"
    meta = await orch.execute_stage(meta.session_id, "post_production", noop)
    assert meta.artifacts["post_production"]["final_video"] != old_final
    assert str((await export_session(meta.session_id, None, "owner", orch)).path) == meta.artifacts["post_production"]["final_video"]


@pytest.mark.asyncio
async def test_unrelated_character_and_shot_are_reused_by_actual_dependencies(orch, monkeypatch):
    orch.llm.script["characters"].append({"character_id": "c2", "name": "狗", "description": "白狗", "role": "配角"})
    for i, shot in enumerate(orch.llm.storyboard["shots"], 1):
        shot["character_ids"] = [f"c{i}"]
        shot["setting_ids"] = ["l1"]
    meta = await run_to(orch, "post_production")
    old_ref = deepcopy(meta.artifacts["reference_generation"]["shots"][1])
    old_video = deepcopy(meta.artifacts["video_generation"]["segments"][1])
    meta = await orch.intervene(meta.session_id, "character_design", {
        "target_ids": ["c1"], "prompts": {"c1": "new cat only"}}, noop)
    assert meta.artifacts["reference_generation"]["stale_items"] == ["s1"]
    assert meta.artifacts["video_generation"]["stale_items"] == ["s1"]
    meta = await orch.intervene(meta.session_id, "reference_generation", {"target_ids": ["s1"]}, noop)
    assert meta.artifacts["reference_generation"]["stale_items"] == []
    meta = await orch.intervene(meta.session_id, "video_generation", {"target_ids": ["s1"]}, noop)
    assert meta.artifacts["video_generation"]["stale_items"] == []
    assert meta.artifacts["reference_generation"]["shots"][1] == old_ref
    assert meta.artifacts["video_generation"]["segments"][1] == old_video
    reloaded = session_store.load_session(meta.session_id)
    assert reloaded.artifacts["video_generation"]["segments"][1]["input_dependencies"] == old_video["input_dependencies"]
    meta = await orch.execute_stage(meta.session_id, "post_production", noop)
    assert old_video["path"] in meta.artifacts["post_production"]["parts"]


@pytest.mark.asyncio
async def test_failed_generation_leaves_no_record(orch, monkeypatch):
    meta = await run_to(orch, "reference_generation")
    old = deepcopy(meta.artifacts["reference_generation"])
    old_selected = meta.selected_versions["reference_generation"]
    old_inputs = len(meta.execution_inputs)
    old_versions = deepcopy(meta.artifact_versions["reference_generation"])
    generate = orch.image.image_to_image
    calls = []
    created = []
    def sometimes_fail(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            raise AppError("MODEL_TEST_FAILURE", "isolated failure")
        out = generate(*args, **kwargs)
        created.append(str(out))
        return out
    monkeypatch.setattr(orch.image, "image_to_image", sometimes_fail)
    with pytest.raises(AppError, match="isolated failure"):
        await orch.intervene(meta.session_id, "reference_generation", {
            "target_ids": ["s1", "s2"], "prompts": {"s1": "partial custom input"}}, noop)
    meta = session_store.load_session(meta.session_id)
    assert meta.status == "failed"
    assert meta.error == "isolated failure"
    assert meta.artifacts["reference_generation"] == old
    assert meta.selected_versions["reference_generation"] == old_selected
    assert meta.artifact_versions["reference_generation"] == old_versions
    assert len(meta.execution_inputs) == old_inputs
    assert not any(row.get("status") in {"failed", "interrupted"} for row in meta.execution_inputs)
    assert not any(v["reason"] == "generated_partial_failed" for v in meta.artifact_versions["reference_generation"])
    for path in created:
        assert not Path(path).exists()
    assert Path(old["shots"][0]["path"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("generation_request", [
    {"target_ids": []}, {"target_ids": ["missing"]}, {"target_ids": ["s1", "s1"]},
    {"target_ids": "s1"}, {"target_ids": ["s1"], "prompts": {"s2": "unselected"}},
    {"target_ids": ["s1"], "prompts": {"s1": " "}}])
async def test_invalid_target_or_prompt_fails_before_model(orch, monkeypatch, generation_request):
    meta = await run_to(orch, "reference_generation")
    monkeypatch.setattr(orch.image, "image_to_image", lambda *a, **kw: pytest.fail("invalid requests must not call models"))
    with pytest.raises(AppError):
        await orch.intervene(meta.session_id, "reference_generation", generation_request, noop)
    unchanged = session_store.load_session(meta.session_id)
    assert unchanged.artifacts == meta.artifacts
    assert len(unchanged.execution_inputs) == len(meta.execution_inputs)
