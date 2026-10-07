"""分镜调用合同：原始数量约束和可用声线必须真正到达模型请求。"""
import json

import pytest

from app.models.tts_client import TTSClient
from app.schemas.session import COMIC_VOICES, ComicStoryboardArtifact, SessionCreate, StoryboardArtifact
from app.services import prompts


async def noop(*args):
    pass


async def capture_storyboard(orch, monkeypatch, project_type, idea):
    calls = []
    original = orch.llm.generate_json

    def generate(system, user, model_cls, **kwargs):
        if model_cls in (ComicStoryboardArtifact, StoryboardArtifact):
            calls.append((system, user))
        if model_cls is ComicStoryboardArtifact:
            return model_cls(shots=[{"shot_id": "s1", "description": "雨夜街道"}])
        return original(system, user, model_cls, **kwargs)

    monkeypatch.setattr(orch.llm, "generate_json", generate)
    meta = orch.create(SessionCreate(project_type=project_type, idea=idea, episodes=1))
    await orch.execute_stage(meta.session_id, "script_generation", noop)
    # Mock script intentionally omits the user's shot-count requirement.
    assert idea not in json.dumps(meta.artifacts, ensure_ascii=False)
    stage = "comic_storyboard" if project_type == "comic" else "storyboard"
    await orch.execute_stage(meta.session_id, stage, noop)
    assert len(calls) == 1
    return calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("project_type", ["story", "comic"])
@pytest.mark.parametrize("shot_count", [2, 7])
async def test_original_shot_count_reaches_storyboard_even_if_script_omits_it(orch, monkeypatch, project_type, shot_count):
    idea = f"守塔人看见一艘小船，全片合计只要 {shot_count} 个镜头。"
    system, user = await capture_storyboard(orch, monkeypatch, project_type, idea)
    assert idea in user
    assert prompts.SHOT_COUNT_RULE in system
    if project_type == "story":
        assert "只有用户未指定镜头数量时" in system
    else:
        assert "单集1–12镜头" in system


@pytest.mark.asyncio
async def test_comic_prompt_uses_live_voice_catalog_and_forbids_invented_ids(orch, monkeypatch):
    # A changed catalog must reach the request without editing the prompt template.
    catalog = [{"voice": "test-catalog-voice", "name": "测试声线", "gender": "male"}]
    monkeypatch.setattr(TTSClient, "list_voices", staticmethod(lambda: catalog))
    system, _ = await capture_storyboard(orch, monkeypatch, "comic", "雨夜灯塔")
    assert json.dumps(catalog, ensure_ascii=False) in system
    assert "voice_map 可以是空对象" in system
    assert "不得自造声线ID" in system
    assert "zh-CN-YunxiNeural" not in system


def test_schema_and_tts_supported_voices_stay_in_sync():
    assert set(COMIC_VOICES) == {voice["voice"] for voice in TTSClient.list_voices()}
