# -*- coding: utf-8 -*-
"""D7 故事主集：改写后的定妆、逐镜和负面提示词。"""
from __future__ import annotations

import json
from pathlib import Path

from app.services import prompts

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "d7_story_set.json"
DIMENSIONS = ("符合描述", "质量可用", "风格一致", "无违规内容")
SAMPLE_IDS = ("S1", "S2", "S3", "S4", "S5")


def load_document() -> dict:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    validate_document(data)
    return data


def load_samples() -> list[dict]:
    return load_document()["samples"]


def validate_document(data: dict) -> None:
    samples = data.get("samples")
    if not isinstance(samples, list) or [item.get("id") for item in samples] != list(SAMPLE_IDS):
        raise ValueError("D7 主集必须按 S1 到 S5 排列")
    limit = int(data.get("generation", {}).get("max_references_per_shot", 3))
    for sample in samples:
        assets = {}
        for asset in sample.get("assets") or []:
            if asset.get("id") in assets or asset.get("kind") not in {"character", "setting", "prop"}:
                raise ValueError(f"{sample.get('id')} 资产无效")
            if not str(asset.get("prompt_zh") or "").strip() or not str(asset.get("prompt_en") or "").strip():
                raise ValueError(f"{sample.get('id')} 缺少定妆提示词")
            assets[asset["id"]] = asset
        if not any(asset["kind"] == "character" for asset in assets.values()):
            raise ValueError(f"{sample.get('id')} 缺少角色定妆")
        shots = sample.get("shots") or []
        if len(shots) != 5 or [shot.get("shot_id") for shot in shots] != [f"s{i}" for i in range(1, 6)]:
            raise ValueError(f"{sample.get('id')} 必须是 s1 到 s5")
        if not str(sample.get("negative_prompt_zh") or "").strip():
            raise ValueError(f"{sample.get('id')} 缺少负面提示词")
        seen_characters: dict[str, int] = {}
        for shot in shots:
            refs = shot.get("reference_ids") or []
            if not refs or len(refs) > limit or any(ref not in assets for ref in refs):
                raise ValueError(f"{sample.get('id')} {shot.get('shot_id')} 的参考图无效")
            if not str(shot.get("prompt_zh") or "").strip() or not str(shot.get("prompt_en") or "").strip():
                raise ValueError(f"{sample.get('id')} {shot.get('shot_id')} 缺少逐镜提示词")
            for ref in refs:
                if assets[ref]["kind"] == "character":
                    seen_characters[ref] = seen_characters.get(ref, 0) + 1
        if not any(count >= 2 for count in seen_characters.values()):
            raise ValueError(f"{sample.get('id')} 没有跨镜头复用的角色")


def generation_plan(sample: dict) -> dict:
    """先排定妆，再让每个镜头引用这些定妆图。"""
    return {
        "id": sample["id"],
        "assets": [
            {
                "id": asset["id"],
                "kind": asset["kind"],
                "name": asset["name"],
                "prompt": prompts.compose_sheet_prompt(asset["prompt_zh"], kind=asset["kind"]),
            }
            for asset in sample["assets"]
        ],
        "shots": [
            {
                "shot_id": shot["shot_id"],
                "description": shot["description"],
                "prompt": prompts.compose_shot_prompt(
                    "使用所附参考图锁定角色与物体，不要换成另一个角色。\n" + shot["prompt_zh"],
                    sample["negative_prompt_zh"],
                ),
                "reference_ids": list(shot["reference_ids"]),
            }
            for shot in sample["shots"]
        ],
    }


def item_passed(scores: dict | None) -> bool | None:
    """四项都填、平均不低于 2、且没有 1 分。缺项返回 None，不记成通过或失败。"""
    if not isinstance(scores, dict):
        return None
    values = [scores.get(name) for name in DIMENSIONS]
    if any(value is None or value == "" for value in values):
        return None
    if any(type(value) is not int for value in values):
        return False
    match, quality, consistency, safety = values
    if safety not in (1, 3) or any(value not in (1, 2, 3) for value in (match, quality, consistency)):
        return False
    if 1 in values:
        return False
    return sum(values) / len(values) >= 2
