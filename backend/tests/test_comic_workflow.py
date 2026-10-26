"""独立漫剧主链路：模型/TTS/渲染替身，真实临时SQLite与文件归属。"""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.errors import AppError
from app.schemas.session import SessionCreate, SessionMeta, ComicStoryboardArtifact
from app.services import comic_renderer, session_store
from app.services.comic_workflow import COMIC_STAGES


async def noop(*args):
    pass


@pytest.fixture
def comic(orch, monkeypatch):
    original = orch.llm.generate_json
    plan = {"shots": [
        {"shot_id": "s1", "description": "漫画雨夜街道的猫", "prompt": "manga cat rain", "character_ids": ["c1"], "setting_ids": ["l1"],
         "motion": "push_in", "dialogues": [{"line_id": "line1", "speaker_id": "c1", "text": "雨终于停了。"},
                                            {"line_id": "line2", "speaker_id": "narrator", "text": "一封信改变了命运。"}]},
        {"shot_id": "s2", "description": "漫画灯塔和猫", "prompt": "manga lighthouse", "character_ids": ["c1"],
         "motion": "pan_left", "dialogues": [{"line_id": "line3", "speaker_id": "c1", "text": "我要去灯塔。"}]}
    ], "voice_map": {"c1": "zh-CN-YunxiNeural", "narrator": "zh-CN-XiaoxiaoNeural"}}
    captured = {"tts": [], "render": [], "llm": []}
    def generate(system, user, model_cls, **kwargs):
        captured["llm"].append((system, user, model_cls))
        if model_cls is ComicStoryboardArtifact:
            return model_cls.model_validate(plan)
        return original(system, user, model_cls, **kwargs)
    async def synthesize(text, output, *, voice=None):
        captured["tts"].append({"text": text, "voice": voice, "path": str(output)})
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-mp3")
        return output
    async def render(image, lines, output, **kwargs):
        captured["render"].append({"image": str(image), "lines": deepcopy(lines), **kwargs})
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-shot")
        srt = output.with_suffix(".srt")
        srt.write_text("1\n00:00:00,000 --> 00:00:03,000\n中文台词\n", encoding="utf-8")
        return {"path": str(output), "duration": 3.0, "srt_path": str(srt), "width": 720, "height": 1280, "fps": 25,
                "cues": [{"start": 0.0, "end": 3.0, **line} for line in lines]}
    async def concat(parts, output):
        output.write_bytes(b"fake-final")
        srt = output.with_suffix(".srt")
        srt.write_text("已拼接字幕", encoding="utf-8")
        return {"path": str(output), "srt_path": str(srt), "duration": sum(part["duration"] for part in parts),
                "width": 720, "height": 1280, "fps": 25, "cues": [cue for part in parts for cue in part["cues"]]}
    monkeypatch.setattr(orch.llm, "generate_json", generate)
    monkeypatch.setattr(orch.tts, "synthesize", synthesize)
    monkeypatch.setattr(comic_renderer, "render_comic_shot", render)
    monkeypatch.setattr(comic_renderer, "concat_comic_shots", concat)
    return orch, captured


async def run_to(comic, stage="comic_composition"):
    orch, captured = comic
    meta = orch.create(SessionCreate(project_type="comic", idea="猫收到一封神秘信"), owner_id="owner")
    for current in COMIC_STAGES[:COMIC_STAGES.index(stage) + 1]:
        meta = await orch.execute_stage(meta.session_id, current, noop)
    return meta


def test_project_type_defaults_and_comic_limits():
    assert SessionCreate(idea="旧故事").project_type == "story"
    assert SessionMeta(session_id="legacy", idea="旧故事").project_type == "story"
    assert SessionCreate(project_type="comic", idea="漫画").video_ratio == "9:16"
    with pytest.raises(ValidationError):
        SessionCreate(project_type="comic", idea="漫画", episodes=2)
    with pytest.raises(ValidationError):
        SessionCreate(project_type="invalid", idea="漫画")
    with pytest.raises(ValidationError):
        SessionCreate(project_type="comic", idea="漫画", resolution="4K")


@pytest.mark.asyncio
async def test_full_comic_chain_is_separate_and_persisted(comic):
    from app.api.sessions import export_session
    orch, captured = comic
    meta = await run_to(comic)
    assert meta.project_type == "comic" and meta.video_ratio == "9:16"
    assert set(meta.stages_completed) == set(COMIC_STAGES) and not meta.stale_stages
    assert "video_generation" not in meta.artifacts and "post_production" not in meta.artifacts
    assert len(captured["tts"]) == 3 and len(captured["render"]) == 2
    assert captured["render"][0]["motion"] == "push_in"
    assert captured["render"][0]["lines"][0]["text"] == "雨终于停了。"
    assert captured["render"][0]["lines"][0]["audio_path"] == captured["tts"][0]["path"]
    reloaded = session_store.load_session(meta.session_id)
    assert reloaded.project_type == "comic" and reloaded.artifacts == meta.artifacts
    assert reloaded.execution_inputs[-1]["project_type"] == "comic"
    assert reloaded.execution_inputs[-1]["stage_inputs"]["shots"][0]["motion"] == "push_in"
    final = await export_session(meta.session_id, None, "owner", orch)
    assert str(final.path) == meta.artifacts["comic_composition"]["final_video"]
    assert orch.continue_session(meta.session_id).status == "session_completed"


@pytest.mark.asyncio
async def test_panels_use_actual_character_images(comic, monkeypatch):
    orch, captured = comic
    meta = await run_to(comic, "comic_storyboard")
    calls = []
    original = orch.image.image_to_image
    def generate(paths, prompt, output, **kwargs):
        calls.append((paths, prompt))
        return original(paths, prompt, output, **kwargs)
    monkeypatch.setattr(orch.image, "image_to_image", generate)
    meta = await orch.execute_stage(meta.session_id, "comic_panels", noop)
    selected = meta.artifacts["character_design"]["characters"][0]["selected"]
    assert str(calls[0][0][0]) == selected and Path(selected).exists()
    assert "不要在画面中绘制字幕" in calls[0][1]
    assert meta.artifacts["comic_panels"]["shots"][0]["input_paths"][0] == selected


def _capture_review_calls(orch, monkeypatch):
    calls = []
    def review(path, *, expected_person_count=None):
        calls.append({"path": path, "expected_person_count": expected_person_count})
        return {"safe": True, "accepted": True, "review_valid": True,
                "person_count": expected_person_count, "reason": "mock通过", "raw_response": "mock-evidence"}
    monkeypatch.setattr(orch.reviewer, "enabled", True)
    monkeypatch.setattr(orch.reviewer, "review_image", review)
    return calls


@pytest.mark.asyncio
async def test_comic_character_strict_review_excludes_setting_and_persists(comic, monkeypatch):
    orch, _ = comic
    orch.llm.script["characters"][0]["description"] = "纯黄色雨衣、纯黑靴"
    def forbidden_sheet_prompt(*args, **kwargs):
        pytest.fail("comic默认角色图不能调用多视图compose_sheet_prompt")
    monkeypatch.setattr("app.services.prompts.compose_sheet_prompt", forbidden_sheet_prompt)
    calls = _capture_review_calls(orch, monkeypatch)
    meta = await run_to(comic, "character_design")
    prompt = meta.artifacts["character_design"]["characters"][0]["prompt"]
    assert "纯黄色雨衣、纯黑靴" in prompt and "单一视角，全身正面" in prompt
    assert "正面、侧面和脸部放在同一张" not in prompt
    assert [call["expected_person_count"] for call in calls] == [1, None]
    record = session_store.load_session(meta.session_id).execution_inputs[-1]
    assert record["stage"] == "character_design"
    # Both the character strict review and ordinary setting review are retained.
    assert [review["item_id"] for review in record["content_reviews"]] == ["c1", "l1"]
    assert record["content_reviews"][0]["item_id"] == "c1"
    assert record["content_reviews"][0]["result"]["raw_response"] == "mock-evidence"


@pytest.mark.asyncio
async def test_comic_panel_review_counts_unique_characters_per_shot(comic, monkeypatch):
    orch, _ = comic
    orch.llm.script["characters"].extend([
        {"name": "狗", "description": "小狗", "role": "伙伴"},
        {"name": "兔", "description": "小兔", "role": "伙伴"},
    ])
    meta = await run_to(comic, "comic_storyboard")
    board = deepcopy(meta.artifacts["comic_storyboard"])
    board["shots"][0]["character_ids"] = ["c1", "c1"]
    board["shots"][1]["character_ids"] = ["c1", "c2"]
    board["shots"].append({"shot_id": "s3", "description": "空街道", "prompt": "empty street",
                           "character_ids": [], "setting_ids": ["l1"], "dialogues": []})
    meta = await orch.intervene(meta.session_id, "comic_storyboard", {"operation": "save", "artifact": board}, noop)
    calls = _capture_review_calls(orch, monkeypatch)
    meta = await orch.execute_stage(meta.session_id, "comic_panels", noop)
    assert [call["expected_person_count"] for call in calls] == [1, 2, 0]
    reviews = session_store.load_session(meta.session_id).execution_inputs[-1]["content_reviews"]
    assert [review["item_id"] for review in reviews] == ["s1", "s2", "s3"]
    assert [review["result"]["person_count"] for review in reviews] == [1, 2, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["character_design", "comic_panels"])
async def test_comic_rejected_image_stops_generation_and_persists_raw_review(comic, monkeypatch, stage):
    from app.services.content_review import ContentReviewer
    orch, _ = comic
    meta = await run_to(comic, "script_generation" if stage == "character_design" else "comic_storyboard")
    raw = json.dumps({"safe": True, "person_count": 3, "multi_view_layout": True,
                      "floating_head": True, "detached_limbs": False, "text_or_signage": False,
                      "reason": "两个侧身加一个独立头像"}, ensure_ascii=False)
    class FakeVLM:
        def review(self, path, instruction):
            return raw
    orch.reviewer = ContentReviewer(vlm=FakeVLM())
    orch.reviewer.enabled = False  # A disabled general safety switch must not bypass this gate.
    generated = []
    for method_name in ("text_to_image", "image_to_image"):
        original = getattr(orch.image, method_name)
        def generate(*args, _original=original, **kwargs):
            result = _original(*args, **kwargs)
            generated.append(str(result))
            return result
        monkeypatch.setattr(orch.image, method_name, generate)
    with pytest.raises(AppError, match="图片结构审查拦截") as error:
        await orch.execute_stage(meta.session_id, stage, noop)
    assert error.value.code == "CONTENT_REVIEW_FAILED"
    assert len(set(generated)) == 1
    saved = session_store.load_session(meta.session_id)
    assert saved.status == "failed" and stage not in saved.stages_completed
    reviews = [review for review in saved.content_reviews if review["stage"] == stage]
    assert len(reviews) == 1
    assert reviews[0]["result"]["raw_response"] == raw
    assert reviews[0]["result"]["accepted"] is False
    assert reviews[0]["result"]["人物数量"] == 3


@pytest.mark.asyncio
async def test_story_character_design_retains_legacy_review_boundary(comic, monkeypatch):
    orch, _ = comic
    calls = _capture_review_calls(orch, monkeypatch)
    meta = orch.create(SessionCreate(project_type="story", idea="故事角色"), owner_id="owner")
    await orch.execute_stage(meta.session_id, "script_generation", noop)
    meta = await orch.execute_stage(meta.session_id, "character_design", noop)
    assert [call["expected_person_count"] for call in calls] == [None, None]
    assert "正面、侧面和脸部放在同一张" in meta.artifacts["character_design"]["characters"][0]["prompt"]


def test_review_log_migration_preserves_legacy_session(data_dirs):
    from app.services import db
    session_store.create_session(SessionMeta(session_id="legacy-review-log", idea="保留旧会话"))
    with db.connect() as connection:
        connection.execute("ALTER TABLE sessions DROP COLUMN content_reviews")
    db.init_db()
    legacy = session_store.load_session("legacy-review-log")
    assert legacy.idea == "保留旧会话" and legacy.content_reviews == []
    legacy.content_reviews.append({"stage": "comic_panels", "result": {"accepted": False}})
    session_store.touch(legacy)
    db.init_db()  # Repeated startup must not clear the audit log.
    assert session_store.load_session(legacy.session_id).content_reviews == legacy.content_reviews


@pytest.mark.asyncio
async def test_comic_model_preference_preserves_valid_art_and_old_panel_model(comic, monkeypatch):
    orch, captured = comic
    meta = await run_to(comic)
    original = deepcopy(meta.artifacts)
    panel_path = original["comic_panels"]["shots"][0]["path"]
    meta = session_store.update_models(meta.session_id, "owner", {"text": "qwen3.5-flash", "image": "qwen-image-2.0-pro"})
    from app.services.comic_workflow import ComicWorkflow
    ComicWorkflow(orch).refresh(meta)
    assert not meta.stale_stages and meta.artifacts == original
    calls = []
    generate = orch.image.image_to_image
    def image(paths, prompt, output, **kwargs):
        calls.append(kwargs["model"])
        return generate(paths, prompt, output, **kwargs)
    monkeypatch.setattr(orch.image, "image_to_image", image)
    meta = await orch.intervene(meta.session_id, "comic_panels", {"target_ids": ["s1"]}, noop)
    assert calls == ["qwen-image-2.0-pro"]
    assert meta.artifacts["comic_panels"]["shots"][1] == original["comic_panels"]["shots"][1]
    meta = await orch.intervene(meta.session_id, "comic_panels", {"operation": "select", "selections": [{"collection": "shots", "id": "s1", "path": panel_path}]}, noop)
    panel = meta.artifacts["comic_panels"]["shots"][0]
    assert panel["path"] == panel_path
    assert panel["model_usage"]["models"]["image"] == "qwen-image-2.0"


@pytest.mark.asyncio
async def test_dialogue_edit_reuses_art_and_unchanged_audio_lines(comic):
    orch, captured = comic
    meta = await run_to(comic)
    old_panels = deepcopy(meta.artifacts["comic_panels"])
    old_characters = deepcopy(meta.artifacts["character_design"])
    old_audio = deepcopy(meta.artifacts["comic_audio"])
    board = deepcopy(meta.artifacts["comic_storyboard"])
    board["shots"][0]["dialogues"][0]["text"] = "这句话已经被精确修改。"
    meta = await orch.intervene(meta.session_id, "comic_storyboard", {"operation": "save", "artifact": board}, noop)
    assert meta.artifacts["comic_panels"] == old_panels and meta.artifacts["character_design"] == old_characters
    assert set(meta.stale_stages) == {"comic_audio", "comic_composition"}
    assert meta.artifacts["comic_audio"]["stale_items"] == ["s1"]
    before = len(captured["tts"])
    meta = await orch.intervene(meta.session_id, "comic_audio", {"target_ids": ["s1"]}, noop)
    assert len(captured["tts"]) == before + 1
    assert meta.artifacts["comic_audio"]["shots"][0]["lines"][1] == old_audio["shots"][0]["lines"][1]
    assert meta.artifacts["comic_audio"]["shots"][1] == old_audio["shots"][1]
    assert "comic_audio" not in meta.stale_stages
    before_render = len(captured["render"])
    meta = await orch.execute_stage(meta.session_id, "comic_composition", noop)
    assert len(captured["render"]) == before_render + 1
    assert not meta.stale_stages


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["script_generation", "comic_storyboard"])
async def test_save_adjustment_clears_selected_version_without_creating_one(comic, stage):
    orch, captured = comic
    meta = await run_to(comic, "comic_storyboard")
    version_id = meta.selected_versions[stage]
    original = deepcopy(meta.artifacts[stage])
    versions = deepcopy(meta.artifact_versions[stage])
    # A no-op save still corresponds exactly to the selected generated version.
    meta = await orch.intervene(meta.session_id, stage, {"operation": "save", "artifact": original}, noop)
    assert meta.selected_versions[stage] == version_id
    edited = deepcopy(original)
    if stage == "script_generation":
        edited["title"] = "改过的标题"
    else:
        edited["shots"][0]["dialogues"][0]["text"] = "改过的对白"
    meta = await orch.intervene(meta.session_id, stage, {"operation": "save", "artifact": edited}, noop)
    assert stage not in meta.selected_versions
    assert meta.artifact_versions[stage] == versions
    assert stage not in session_store.load_session(meta.session_id).selected_versions
    # Even when there is only one generated version, it can restore the original.
    meta = await orch.intervene(meta.session_id, stage, {"operation": "select", "stage_version_id": version_id}, noop)
    assert meta.selected_versions[stage] == version_id
    assert meta.artifacts[stage] == original


@pytest.mark.asyncio
async def test_character_rename_invalidates_burned_name_without_rerunning_art_or_audio(comic):
    orch, captured = comic
    meta = await run_to(comic, "comic_storyboard")
    board = deepcopy(meta.artifacts["comic_storyboard"])
    for shot in board["shots"]:
        shot["character_ids"] = []
        shot["setting_ids"] = []
    board["shots"][1]["dialogues"][0]["speaker_id"] = "narrator"
    meta = await orch.intervene(meta.session_id, "comic_storyboard", {"operation": "save", "artifact": board}, noop)
    for stage in ("comic_panels", "comic_audio", "comic_composition"):
        meta = await orch.execute_stage(meta.session_id, stage, noop)
    old_panels, old_audio = deepcopy(meta.artifacts["comic_panels"]), deepcopy(meta.artifacts["comic_audio"])
    old_segments = deepcopy(meta.artifacts["comic_composition"]["segments"])
    renders, voices = len(captured["render"]), len(captured["tts"])
    script = deepcopy(meta.artifacts["script_generation"])
    script["characters"][0]["name"] = "新名字"
    meta = await orch.intervene(meta.session_id, "script_generation", {"operation": "save", "artifact": script}, noop)
    # Explicitly keep the unchanged storyboard while binding it to the new script.
    meta = await orch.intervene(meta.session_id, "comic_storyboard", {"operation": "save", "artifact": board}, noop)
    assert meta.artifacts["comic_panels"] == old_panels and meta.artifacts["comic_audio"] == old_audio
    assert "comic_composition" in meta.stale_stages
    meta = await orch.execute_stage(meta.session_id, "comic_composition", noop)
    assert len(captured["render"]) == renders + 1 and len(captured["tts"]) == voices
    assert captured["render"][-1]["lines"][0]["speaker"] == "新名字"
    assert meta.artifacts["comic_composition"]["segments"][0]["path"] != old_segments[0]["path"]
    assert meta.artifacts["comic_composition"]["segments"][1]["path"] == old_segments[1]["path"]


@pytest.mark.asyncio
async def test_voice_and_motion_changes_have_distinct_dependencies(comic):
    orch, captured = comic
    meta = await run_to(comic)
    board = deepcopy(meta.artifacts["comic_storyboard"])
    board["voice_map"]["c1"] = "zh-CN-XiaoyiNeural"
    meta = await orch.intervene(meta.session_id, "comic_storyboard", {"operation": "save", "artifact": board}, noop)
    assert set(meta.stale_stages) == {"comic_audio", "comic_composition"}
    before = len(captured["tts"])
    meta = await orch.execute_stage(meta.session_id, "comic_audio", noop)
    assert len(captured["tts"]) == before + 2
    assert all(call["voice"] == "zh-CN-XiaoyiNeural" for call in captured["tts"][before:])
    meta = await orch.execute_stage(meta.session_id, "comic_composition", noop)
    board = deepcopy(meta.artifacts["comic_storyboard"])
    board["shots"][1]["motion"] = "pan_right"
    board["shots"][0]["dialogues"][0]["emotion"] = "生气（备注）"
    meta = await orch.intervene(meta.session_id, "comic_storyboard", {"operation": "save", "artifact": board}, noop)
    assert set(meta.stale_stages) == {"comic_composition"}


@pytest.mark.asyncio
async def test_targeted_panel_regeneration_preserves_other_panel_and_history(comic):
    orch, captured = comic
    meta = await run_to(comic)
    panels = deepcopy(meta.artifacts["comic_panels"]["shots"])
    board = deepcopy(meta.artifacts["comic_storyboard"])
    board["shots"][0]["prompt"] = "new close-up manga cat"
    board["shots"][1]["prompt"] = "new distant lighthouse"
    meta = await orch.intervene(meta.session_id, "comic_storyboard", {"operation": "save", "artifact": board}, noop)
    assert meta.artifacts["comic_panels"]["stale_items"] == ["s1", "s2"]
    meta = await orch.intervene(meta.session_id, "comic_panels", {"target_ids": ["s1"]}, noop)
    assert meta.artifacts["comic_panels"]["stale_items"] == ["s2"] and meta.status == "idle"
    assert meta.artifacts["comic_panels"]["shots"][1]["path"] == panels[1]["path"]
    assert Path(panels[0]["path"]).exists()
    with pytest.raises(AppError):
        orch.continue_session(meta.session_id)
    with pytest.raises(AppError):
        await orch.execute_stage(meta.session_id, "comic_composition", noop)


@pytest.mark.asyncio
async def test_old_final_remains_history_and_default_export_rejects(comic):
    from app.api.sessions import export_session
    orch, captured = comic
    meta = await run_to(comic)
    old_version = meta.selected_versions["comic_composition"]
    old_final = meta.artifacts["comic_composition"]["final_video"]
    board = deepcopy(meta.artifacts["comic_storyboard"])
    board["shots"][0]["dialogues"][0]["text"] = "新对白"
    meta = await orch.intervene(meta.session_id, "comic_storyboard", {"operation": "save", "artifact": board}, noop)
    await orch.execute_stage(meta.session_id, "comic_audio", noop)
    meta = await orch.intervene(meta.session_id, "comic_composition", {"operation": "select", "stage_version_id": old_version}, noop)
    assert "comic_composition" in meta.stale_stages
    with pytest.raises(AppError) as error:
        await export_session(meta.session_id, None, "owner", orch)
    assert error.value.code == "STALE_EXPORT"
    assert str((await export_session(meta.session_id, old_version, "owner", orch)).path) == old_final
    meta = await orch.execute_stage(meta.session_id, "comic_composition", noop)
    assert str((await export_session(meta.session_id, None, "owner", orch)).path) != old_final


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    lambda value: value["shots"][0].update(path="/private/file.png"),
    lambda value: value["shots"][0]["dialogues"][0].update(speaker_id="foreign"),
    lambda value: value["shots"][0]["dialogues"][0].update(text=" "),
    lambda value: value["shots"][0].update(motion="fly"),
    lambda value: value["voice_map"].update(c1="unsupported-voice"),
    lambda value: value.update(project_type="story"),
])
async def test_comic_schema_edits_cannot_inject_paths_or_invalid_inputs(comic, mutation):
    orch, captured = comic
    meta = await run_to(comic, "comic_storyboard")
    before = deepcopy(meta.artifacts)
    board = deepcopy(meta.artifacts["comic_storyboard"])
    mutation(board)
    with pytest.raises(AppError):
        await orch.intervene(meta.session_id, "comic_storyboard", {"operation": "save", "artifact": board}, noop)
    assert session_store.load_session(meta.session_id).artifacts == before


@pytest.mark.asyncio
async def test_project_stage_isolation_owner_audio_media_and_invalid_targets(comic):
    from app.api.sessions import get_artifact, get_media, export_session
    orch, captured = comic
    meta = await run_to(comic, "comic_audio")
    with pytest.raises(AppError) as error:
        await orch.execute_stage(meta.session_id, "video_generation", noop)
    assert error.value.code == "UNKNOWN_STAGE"
    with pytest.raises(AppError):
        await get_artifact(meta.session_id, "storyboard", "owner", orch)
    path = meta.artifacts["comic_audio"]["shots"][0]["lines"][0]["path"]
    response = await get_media(meta.session_id, "video", Path(path).name, "owner", orch)
    assert str(response.path) == path
    with pytest.raises(AppError) as error:
        await get_media(meta.session_id, "video", Path(path).name, "other", orch)
    assert error.value.status_code == 404
    before = len(captured["tts"])
    with pytest.raises(AppError):
        await orch.intervene(meta.session_id, "comic_audio", {"target_ids": []}, noop)
    assert len(captured["tts"]) == before


@pytest.mark.asyncio
async def test_tts_failure_rolls_back_without_publishing_failed_version(comic, monkeypatch):
    orch, captured = comic
    meta = await run_to(comic, "comic_audio")
    board = deepcopy(meta.artifacts["comic_storyboard"])
    board["shots"][0]["dialogues"][0]["text"] = "新配音"
    meta = await orch.intervene(meta.session_id, "comic_storyboard", {"operation": "save", "artifact": board}, noop)
    old_audio, versions = deepcopy(meta.artifacts["comic_audio"]), deepcopy(meta.artifact_versions["comic_audio"])
    async def failure(*args, **kwargs):
        raise AppError("TTS_FAILED", "mock voice failure", 503)
    monkeypatch.setattr(orch.tts, "synthesize", failure)
    with pytest.raises(AppError, match="mock voice failure"):
        await orch.execute_stage(meta.session_id, "comic_audio", noop)
    saved = session_store.load_session(meta.session_id)
    assert saved.status == "failed" and saved.artifacts["comic_audio"] == old_audio
    assert saved.artifact_versions["comic_audio"] == versions
    assert "comic_audio" in saved.stale_stages
