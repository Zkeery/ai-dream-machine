"""故事编辑、素材引用与失效传播（临时数据目录、模型替身）。"""
from copy import deepcopy
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.schemas.session import SessionCreate
from app.services import orchestrator as orch_mod


async def noop(*args):
    pass


@pytest.fixture(autouse=True)
def fake_ffmpeg(monkeypatch):
    async def concat(parts, out_path, audio_path=None):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"final")
        return out_path
    monkeypatch.setattr(orch_mod, "concat_videos", concat)


async def run_to(orch, stage):
    m = orch.create(SessionCreate(idea="最初创意", episodes=1))
    for s in orch_mod.STAGE_ORDER[:orch_mod.STAGE_ORDER.index(stage) + 1]:
        m = await orch.execute_stage(m.session_id, s, noop)
    return m


@pytest.mark.asyncio
async def test_expand_idea_calls_sync_llm_in_thread(orch):
    m = orch.create(SessionCreate(idea="创意", expand_idea=True))
    m = await orch.execute_stage(m.session_id, "script_generation", noop)
    assert m.status == "stage_completed"


@pytest.mark.asyncio
async def test_save_exact_script_no_model_and_invalidates_old_downstream(orch, monkeypatch):
    m = await run_to(orch, "post_production")
    final = m.artifacts["post_production"]["final_video"]
    old_version = m.selected_versions["script_generation"]
    old_snapshot = deepcopy(next(v for v in m.artifact_versions["script_generation"] if v["version_id"] == old_version))
    version_count = len(m.artifact_versions["script_generation"])
    monkeypatch.setattr(orch.llm, "generate_json", lambda *a: pytest.fail("save must not invoke a model"))
    edited = deepcopy(m.artifacts["script_generation"])
    edited["episodes"][0]["content"] = "用户精确修改的正文"
    m = await orch.intervene(m.session_id, "script_generation", {"operation": "save", "artifact": edited}, noop)
    assert m.artifacts["script_generation"]["episodes"][0]["content"] == "用户精确修改的正文"
    assert set(m.stale_stages) == set(orch_mod.STAGE_ORDER[2:])
    assert set(m.stages_completed) == {"script_generation", "character_design"}
    assert len(m.artifact_versions["script_generation"]) == version_count
    assert not any(v["reason"] == "save" for v in m.artifact_versions["script_generation"])
    assert next(v for v in m.artifact_versions["script_generation"] if v["version_id"] == old_version)["artifact"] == old_snapshot["artifact"]
    assert Path(final).read_bytes() == b"final"
    assert m.artifacts["post_production"]["final_video"] == final
    with pytest.raises(AppError, match="上游产物已失效"):
        await orch.execute_stage(m.session_id, "post_production", noop)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,collection,text_key", [("script_generation", "episodes", "content"), ("storyboard", "shots", "description")])
async def test_regeneration_contains_user_edits(orch, monkeypatch, stage, collection, text_key):
    m = await run_to(orch, stage)
    edited = deepcopy(m.artifacts[stage])
    edited[collection][0][text_key] = "用户编辑应成为生成输入"
    original = orch.llm.generate_json
    seen = []
    def capture(system, user, model_cls, **kwargs):
        seen.append(user)
        return original(system, user, model_cls)
    monkeypatch.setattr(orch.llm, "generate_json", capture)
    m = await orch.intervene(m.session_id, stage, {"artifact": edited}, noop)
    assert "用户编辑应成为生成输入" in seen[0]
    assert m.execution_inputs[-1]["input_artifact"][collection][0][text_key] == "用户编辑应成为生成输入"
    assert m.execution_inputs[-1]["input_artifact_version"] in {x["version_id"] for x in m.artifact_versions[stage]}
    if stage == "storyboard":
        assert m.execution_inputs[-1]["input_versions"]["script_generation"] == m.selected_versions["script_generation"]
    assert all(x["reason"] in orch_mod.STAGE_VERSION_REASONS for x in m.artifact_versions[stage])
    assert m.artifact_versions[stage][-1]["reason"] == "generated"


@pytest.mark.asyncio
async def test_character_select_preserves_versions_and_passes_images_to_model(orch, monkeypatch):
    m = await run_to(orch, "storyboard")
    first = m.artifacts["character_design"]["characters"][0]["selected"]
    m = await orch.execute_stage(m.session_id, "character_design", noop)
    item = m.artifacts["character_design"]["characters"][0]
    second = item["selected"]
    assert first != second and Path(first).exists() and Path(second).exists()
    m = await orch.intervene(m.session_id, "character_design", {"operation": "select", "selections": [
        {"collection": "characters", "id": item["id"], "path": first}
    ]}, noop)
    assert m.artifacts["character_design"]["characters"][0]["selected"] == first
    assert "storyboard" not in m.stale_stages
    assert "reference_generation" in m.stale_stages
    seen = []
    original = orch.image.image_to_image
    def capture(paths, prompt, out, **kwargs):
        seen.append(([str(p) for p in paths], kwargs))
        return original(paths, prompt, out, **kwargs)
    monkeypatch.setattr(orch.image, "image_to_image", capture)
    m = await orch.execute_stage(m.session_id, "reference_generation", noop)
    assert first in seen[0][0]
    assert seen[0][1]["size"] == "1280x720"
    assert first in m.execution_inputs[-1]["input_paths"]
    assert m.execution_inputs[-1]["input_versions"]["character_design"] == m.selected_versions["character_design"]
    assert orch.continue_session(m.session_id).current_stage == "video_generation"


@pytest.mark.asyncio
async def test_reference_video_consumes_selected_role_and_scene_images(orch, monkeypatch):
    m = await run_to(orch, "reference_generation")
    seen = []
    original = orch.video.image_to_video
    def capture(path, prompt, out, mode, **kwargs):
        seen.append((path, mode, kwargs))
        return original(path, prompt, out, mode, **kwargs)
    monkeypatch.setattr(orch.video, "image_to_video", capture)
    m = await orch.intervene(m.session_id, "video_generation", {"video_generation_mode": "reference"}, noop)
    assert seen[0][1] == "reference"
    assert m.artifacts["character_design"]["characters"][0]["selected"] in seen[0][2]["reference_paths"]
    assert m.artifacts["character_design"]["settings"][0]["selected"] in seen[0][2]["reference_paths"]


@pytest.mark.asyncio
async def test_select_history_and_reject_unregistered_path(orch, tmp_path):
    m = await run_to(orch, "reference_generation")
    v = m.selected_versions["reference_generation"]
    old_path = m.artifacts["reference_generation"]["shots"][0]["path"]
    m = await orch.execute_stage(m.session_id, "reference_generation", noop)
    assert m.artifacts["reference_generation"]["shots"][0]["path"] != old_path
    m = await orch.intervene(m.session_id, "reference_generation", {"operation": "select", "stage_version_id": v}, noop)
    assert m.artifacts["reference_generation"]["shots"][0]["path"] == old_path
    foreign = tmp_path / "foreign.png"
    foreign.write_bytes(b"x")
    with pytest.raises(AppError) as e:
        await orch.intervene(m.session_id, "reference_generation", {"operation": "select", "selections": [
            {"collection": "shots", "id": "s1", "path": str(foreign)}]}, noop)
    assert e.value.code == "INVALID_ASSET_VERSION"


@pytest.mark.asyncio
async def test_save_cannot_inject_server_paths(orch):
    m = await run_to(orch, "storyboard")
    bad = deepcopy(m.artifacts["storyboard"])
    bad["shots"][0]["path"] = "/etc/passwd"
    with pytest.raises(AppError) as e:
        await orch.intervene(m.session_id, "storyboard", {"operation": "save", "artifact": bad}, noop)
    assert e.value.code == "INVALID_MODIFICATIONS"
    with pytest.raises(AppError):
        await orch.intervene(m.session_id, "character_design", {"artifact": {"characters": [{"selected": "/etc/passwd"}]}}, noop)


@pytest.mark.asyncio
async def test_registered_symlink_cannot_select_foreign_file(orch, tmp_path):
    m = await run_to(orch, "character_design")
    item = m.artifacts["character_design"]["characters"][0]
    foreign = tmp_path / "foreign.png"
    foreign.write_bytes(b"foreign-image")
    link = Path(item["selected"]).with_name("foreign-link.png")
    link.symlink_to(foreign)
    item["versions"].append(str(link))
    from app.services import session_store
    session_store.touch(m)
    with pytest.raises(AppError) as e:
        await orch.intervene(m.session_id, "character_design", {"operation": "select", "selections": [
            {"collection": "characters", "id": item["id"], "path": str(link)}]}, noop)
    assert e.value.code == "INVALID_PATH"


@pytest.mark.asyncio
async def test_reference_limit_fails_before_image_call(orch, monkeypatch):
    m = await run_to(orch, "storyboard")
    design = m.artifacts["character_design"]
    template = design["characters"][0]
    for i in range(3):
        item = deepcopy(template)
        item["id"] = f"extra{i}"
        p = Path(template["selected"]).with_name(f"extra{i}.png")
        p.write_bytes(b"image")
        item["selected"] = str(p)
        item["versions"] = [str(p)]
        design["characters"].append(item)
    # 显式引用超过 3 张时应在调用生图前失败；空列表不再被当成“引用全部”。
    m.artifacts["storyboard"]["shots"][0]["character_ids"] = [c["id"] for c in design["characters"]]
    m.artifacts["storyboard"]["shots"][0]["setting_ids"] = []
    from app.services import session_store
    session_store.touch(m)
    monkeypatch.setattr(orch.image, "image_to_image", lambda *a, **k: pytest.fail("unsupported count must fail first"))
    monkeypatch.setattr(orch.image, "text_to_image", lambda *a, **k: pytest.fail("unsupported count must fail first"))
    with pytest.raises(AppError) as e:
        await orch.execute_stage(m.session_id, "reference_generation", noop)
    assert e.value.code == "IMAGE_REFERENCE_LIMIT"


@pytest.mark.asyncio
async def test_empty_shot_refs_mean_none_not_all_designs(orch, monkeypatch):
    m = await run_to(orch, "storyboard")
    m.artifacts["storyboard"]["shots"][0]["character_ids"] = []
    m.artifacts["storyboard"]["shots"][0]["setting_ids"] = ["l1"] if any(
        x.get("id") == "l1" for x in m.artifacts["character_design"]["settings"]) else [
        m.artifacts["character_design"]["settings"][0]["id"]]
    from app.services import session_store
    session_store.touch(m)
    seen = []
    original = orch.image.image_to_image
    def capture(paths, prompt, out, **kwargs):
        seen.append([str(p) for p in paths])
        return original(paths, prompt, out, **kwargs)
    monkeypatch.setattr(orch.image, "image_to_image", capture)
    m = await orch.execute_stage(m.session_id, "reference_generation", noop)
    assert m.status == "stage_completed"
    assert len(seen[0]) == 1


@pytest.mark.asyncio
async def test_legacy_meta_bootstraps_versions_and_edit_after_completion(orch):
    m = await run_to(orch, "post_production")
    m = orch.continue_session(m.session_id)
    assert m.status == "session_completed"
    m.artifact_versions = {}
    m.selected_versions = {}
    for stage, collections in (("character_design", ("characters", "settings")),
                               ("reference_generation", ("shots",)), ("video_generation", ("segments",))):
        for collection in collections:
            for item in m.artifacts[stage][collection]:
                item.pop("input_dependencies", None)
                item.pop("input_versions", None)
    from app.services import session_store
    session_store.touch(m)
    m = await orch.intervene(m.session_id, "script_generation", {"operation": "save", "artifact": {"title": "新片名"}}, noop)
    assert m.status == "stage_completed"
    assert m.artifact_versions["post_production"][0]["reason"] == "legacy"
    assert orch.continue_session(m.session_id).current_stage == "character_design"


@pytest.mark.asyncio
async def test_failed_and_select_records_are_not_versions(orch):
    m = await run_to(orch, "character_design")
    good = m.selected_versions["character_design"]
    m.artifact_versions["character_design"] = [
        {"version_id": "fail1", "artifact": deepcopy(m.artifacts["character_design"]),
         "input_versions": {}, "created_at": 1, "reason": "generated_partial_failed"},
        next(v for v in m.artifact_versions["character_design"] if v["version_id"] == good),
        {"version_id": "sel1", "artifact": deepcopy(m.artifacts["character_design"]),
         "input_versions": {}, "created_at": 3, "reason": "select"},
    ]
    m.selected_versions["character_design"] = "sel1"
    from app.services import session_store
    session_store.touch(m)
    m = await orch.intervene(m.session_id, "character_design", {
        "operation": "select",
        "selections": [{"collection": "characters", "id": m.artifacts["character_design"]["characters"][0]["id"],
                        "path": m.artifacts["character_design"]["characters"][0]["selected"]}],
    }, noop)
    reasons = [v["reason"] for v in m.artifact_versions["character_design"]]
    assert reasons == ["generated"]
    assert m.selected_versions["character_design"] == good
    assert not any(v["reason"] in {"generated_partial_failed", "select", "save"} for v in m.artifact_versions["character_design"])


@pytest.mark.asyncio
async def test_old_final_selection_preserves_inputs_and_cannot_export_as_current(orch):
    from app.api import sessions as sessions_api
    from app.services import session_store
    m = await run_to(orch, "post_production")
    m.owner_id = "owner"
    session_store.touch(m)
    old_post_id = m.selected_versions["post_production"]
    old_video_id = m.selected_versions["video_generation"]
    old_parts = deepcopy(m.artifacts["post_production"]["parts"])
    m = await orch.execute_stage(m.session_id, "video_generation", noop)
    new_video_id = m.selected_versions["video_generation"]
    assert new_video_id != old_video_id
    m = await orch.intervene(m.session_id, "post_production", {"operation": "select", "stage_version_id": old_post_id}, noop)
    assert m.selected_versions["post_production"] == old_post_id
    old_post = next(v for v in m.artifact_versions["post_production"] if v["version_id"] == old_post_id)
    assert old_post["input_versions"]["video_generation"] == old_video_id
    assert m.artifacts["post_production"]["parts"] == old_parts
    assert "post_production" in m.stale_stages and "post_production" not in m.stages_completed
    assert m.status == "idle"
    with pytest.raises(AppError) as e:
        await sessions_api.export_session(m.session_id, None, "owner", orch)
    assert e.value.code == "STALE_EXPORT"
    historical = await sessions_api.export_session(m.session_id, old_post_id, "owner", orch)
    assert str(historical.path) == old_post["artifact"]["final_video"]
    m = await orch.execute_stage(m.session_id, "post_production", noop)
    assert not m.stale_stages
    current = await sessions_api.export_session(m.session_id, None, "owner", orch)
    assert str(current.path) != str(historical.path)
    assert m.artifact_versions["post_production"][-1]["input_versions"]["video_generation"] == new_video_id


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,upstream", [("reference_generation", "character_design"), ("video_generation", "reference_generation")])
async def test_old_stage_selection_does_not_rebind_to_new_upstream(orch, stage, upstream):
    m = await run_to(orch, stage)
    old_id = m.selected_versions[stage]
    old_input = m.selected_versions[upstream]
    m = await orch.execute_stage(m.session_id, upstream, noop)
    new_input = m.selected_versions[upstream]
    assert old_input != new_input
    m = await orch.intervene(m.session_id, stage, {"operation": "select", "stage_version_id": old_id}, noop)
    selected = next(v for v in m.artifact_versions[stage] if v["version_id"] == m.selected_versions[stage])
    assert selected["input_versions"][upstream] == old_input
    assert stage in m.stale_stages and stage not in m.stages_completed
    m = await orch.execute_stage(m.session_id, stage, noop)
    assert stage not in m.stale_stages
    assert m.artifact_versions[stage][-1]["input_versions"][upstream] == new_input


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,upstream,collection,id_key", [
    ("reference_generation", "character_design", "shots", "shot_id"),
    ("video_generation", "reference_generation", "segments", "segment_id")])
async def test_old_item_selection_keeps_real_generation_lineage(orch, stage, upstream, collection, id_key):
    m = await run_to(orch, stage)
    old_input = m.selected_versions[upstream]
    old_item = deepcopy(m.artifacts[stage][collection][0])
    version_count = len(m.artifact_versions[stage])
    m = await orch.execute_stage(m.session_id, upstream, noop)
    m = await orch.execute_stage(m.session_id, stage, noop)
    new_input = m.selected_versions[upstream]
    assert len(m.artifact_versions[stage]) == version_count + 1
    m = await orch.intervene(m.session_id, stage, {"operation": "select", "selections": [
        {"collection": collection, "id": old_item[id_key], "path": old_item["path"]}]}, noop)
    assert len(m.artifact_versions[stage]) == version_count + 1
    assert not any(v["reason"] == "select" for v in m.artifact_versions[stage])
    record = m.execution_inputs[-1]
    assert record["input_versions"][upstream] == old_input
    lineage_id = old_item["shot_id"] if stage == "video_generation" else old_item[id_key]
    assert record["item_input_versions"][f"{collection}:{lineage_id}"][upstream] == old_input
    assert new_input != old_input
    assert stage in m.stale_stages and stage not in m.stages_completed
