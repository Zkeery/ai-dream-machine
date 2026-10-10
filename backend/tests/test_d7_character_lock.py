# -*- coding: utf-8 -*-
"""定妆图锁定后续镜头，负面提示词进入镜头生成，评测集不含原片。"""
import json
from pathlib import Path

import pytest

from app.core import config
from app.services import d7_runner, d7_set, prompts
from app.services.orchestrator import STAGE_ORDER


async def noop(*args):
    pass


async def run_to(orch, stage):
    from app.schemas.session import SessionCreate

    meta = orch.create(SessionCreate(idea="一只流浪猫在雨夜被好心人收留", episodes=1))
    for name in STAGE_ORDER[:STAGE_ORDER.index(stage) + 1]:
        meta = await orch.execute_stage(meta.session_id, name, noop)
    return meta


class RecordingMedia:
    def __init__(self):
        self.images = []
        self.videos = []

    def text_to_image(self, prompt, out_path, model=None, **kwargs):
        assert not self.videos, "定妆图必须先于镜头视频生成"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"sheet")
        self.images.append((prompt, model, kwargs.get("size")))
        return out_path

    def image_to_video(self, image_path, prompt, out_path, mode="first_frame", **kwargs):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"clip")
        self.videos.append((image_path, prompt, mode, list(kwargs.get("reference_paths") or [])))
        return out_path


def test_eval_set_has_rewritten_prompts_without_source_media():
    raw = d7_set.FIXTURE.read_text(encoding="utf-8")
    lowered = raw.lower()
    for banned in ("youtube", "youtu.be", "source.mp4", "kf0", "_keyframes", "扭打", "内衣", "killing"):
        assert banned not in lowered
    document = d7_set.load_document()
    assert document["generation"]["video_mode"] == "reference"
    samples = document["samples"]
    assert [item["id"] for item in samples] == ["S1", "S2", "S3", "S4", "S5"]
    for sample in samples:
        assert sample["negative_prompt_zh"] and sample["negative_prompt_en"]
        assert "三分屏" in sample["negative_prompt_zh"]
        plan = d7_set.generation_plan(sample)
        assert len(plan["assets"]) >= 1 and len(plan["shots"]) == 5
        for asset in plan["assets"]:
            assert asset["prompt"].startswith(next(
                item["prompt_zh"][:12] for item in sample["assets"] if item["id"] == asset["id"]))
            assert "避免出现：" in asset["prompt"]
            if asset["kind"] == "character":
                assert "三分屏" not in asset["prompt"]
        for shot in plan["shots"]:
            assert "避免出现：" in shot["prompt"] and "三分屏" in shot["prompt"]
            assert len(shot["reference_ids"]) <= 3
    robot = next(item for item in samples if item["id"] == "S2")
    assert "CL-01" in robot["assets"][0]["prompt_zh"]
    assert "清洁机器人" in robot["shots"][1]["prompt_zh"]
    assert "地球" in robot["shots"][4]["prompt_zh"]


def test_same_character_sheet_locks_later_shots(tmp_path):
    media = RecordingMedia()
    sample = next(item for item in d7_set.load_samples() if item["id"] == "S1")
    rendered = d7_runner.render_sample(
        sample, media, media, tmp_path, image_model="qwen-image-2.0", video_model="wan2.7-r2v")
    assert len(media.images) == len(sample["assets"])
    assert [call[2] for call in media.videos] == ["reference"] * 5
    cat = rendered["sheets"]["cat"]
    girl = rendered["sheets"]["girl"]
    by_shot = {item["shot_id"]: item for item in rendered["calls"]}
    assert by_shot["s1"]["image"] == by_shot["s2"]["image"] == by_shot["s5"]["image"] == cat
    assert by_shot["s4"]["image"] == girl
    assert cat in by_shot["s4"]["reference_paths"]
    assert all(call[1] == "qwen-image-2.0" and call[2] == "1280x720" for call in media.images)


def test_storyboard_and_pipeline_prompts_keep_identity_and_negative(orch):
    assert "3 到 4 个固定特征" in prompts.STORYBOARD_SYSTEM
    assert "三分屏" in prompts.STORYBOARD_SYSTEM


@pytest.mark.asyncio
async def test_pipeline_locks_sheets_and_appends_shot_negative(orch):
    meta = await run_to(orch, "video_generation")
    character = meta.artifacts["character_design"]["characters"][0]
    assert "角色定妆图" in character["prompt"]
    assert "三分屏" not in character["prompt"]
    reference = meta.artifacts["reference_generation"]["shots"][0]
    segment = meta.artifacts["video_generation"]["segments"][0]
    assert character["selected"] in reference["input_paths"]
    assert "避免出现：" in reference["prompt"] and "三分屏" in reference["prompt"]
    assert "避免出现：" in segment["prompt"] and "手部变形" in segment["prompt"]
    assert "猫躲雨" not in segment["prompt"]
    assert "雨夜街道" in segment["prompt"]


def test_score_rule_and_recorded_review_do_not_invent_agreement():
    assert d7_set.item_passed(None) is None
    assert d7_set.item_passed({"符合描述": 3, "质量可用": 2, "风格一致": None, "无违规内容": 3}) is None
    assert d7_set.item_passed({"符合描述": 3, "质量可用": 2, "风格一致": 1, "无违规内容": 3}) is False
    assert d7_set.item_passed({"符合描述": 2, "质量可用": 2, "风格一致": 2, "无违规内容": 2}) is False
    assert d7_set.item_passed({"符合描述": 2, "质量可用": 2, "风格一致": 2, "无违规内容": 3}) is True
    record_path = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "阶段2" / "样例打分" / "A评审-2026-10-10.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["reviewer_b"] is None and record["agreement"] is None and "kappa" not in record
    assert [item["passed"] for item in record["samples"]] == [False, False, False, True, True]
    for item in record["samples"]:
        assert d7_set.item_passed(item["scores"]) is item["passed"]
    pending = json.loads((Path(__file__).resolve().parents[2] / "docs" / "evidence" / "D7-角色一致性" / "待评分.json").read_text(encoding="utf-8"))
    assert pending["generated"] is False
    assert all(d7_set.item_passed(item["scores"]) is None for item in pending["samples"])


def test_missing_key_blocks_without_fake_output(monkeypatch):
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "")
    blocked = d7_runner.credential_block()
    assert blocked["generated"] is False
    assert "AIHUBMIX_API_KEY" in blocked["reason"]
    assert "final.mp4" in blocked["how_to_run"][-1]
