# -*- coding: utf-8 -*-
"""用定妆图锁定 D7 各镜头。没有密钥时不调用模型，也不写假成片。"""
from __future__ import annotations

import json
from pathlib import Path

from app.core import config
from app.services import d7_set

EVIDENCE = Path(__file__).resolve().parents[3] / "docs" / "evidence" / "D7-角色一致性"
OUTPUT = EVIDENCE / "output"
STATUS_PATH = EVIDENCE / "run-status.json"

HOW_TO_RUN = [
    "在仓库根目录把 .env.example 复制为 .env，填入 AIHUBMIX_API_KEY。",
    "安装后端依赖：python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt。",
    "确认本机有 ffmpeg。D7 默认：图片 Seedream 5.0 Pro、视频可灵 3.0 Omni 1080P（pro）、审查 qwen3-vl-plus，参考图模式，16:9，每镜 5 秒。",
    "先零费用预检：在 backend 目录执行 .venv/bin/python scripts/run_d7_round.py --dry-run --only S2。",
    "正式生成：.venv/bin/python scripts/run_d7_round.py --only S2。可用 --budget-usd 设硬上限。",
    "成片写到 docs/evidence/D7-角色一致性/output/<样本ID>/final.mp4。分数留在待评分表，由人工填写。",
]


def credential_block() -> dict | None:
    if config.settings.aihubmix_api_key.strip():
        return None
    return {
        "generated": False,
        "reason": "未配置 AIHUBMIX_API_KEY，未调用模型，没有成片，也没有新的分数。",
        "output_dir": "docs/evidence/D7-角色一致性/output",
        "how_to_run": HOW_TO_RUN,
    }


def write_status(payload: dict, path: Path = STATUS_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def render_sample(sample: dict, image, video, out_dir: Path, *, image_model: str, video_model: str,
                  review_image=None, resolution: str = "1080P", video_ratio: str = "16:9",
                  image_size: str = "1280x720") -> dict:
    """先生成全部定妆图，再把同一张图传给该角色出现的每个镜头。

    默认分辨率 1080P，与 S1 可灵实测及 fixture.generation 一致。
    """
    plan = d7_set.generation_plan(sample)
    sheet_dir = out_dir / "sheets"
    shot_dir = out_dir / "shots"
    sheet_dir.mkdir(parents=True, exist_ok=True)
    shot_dir.mkdir(parents=True, exist_ok=True)
    sheets: dict[str, Path] = {}
    for asset in plan["assets"]:
        path = sheet_dir / f"{asset['id']}.png"
        image.text_to_image(asset["prompt"], path, model=image_model, size=image_size)
        if review_image is not None:
            review_image(path)
        sheets[asset["id"]] = path
    clips = []
    calls = []
    for shot in plan["shots"]:
        primary = sheets[shot["reference_ids"][0]]
        extra = [str(sheets[item]) for item in shot["reference_ids"][1:]]
        clip = shot_dir / f"{shot['shot_id']}.mp4"
        video.image_to_video(
            str(primary), shot["prompt"], clip, "reference",
            reference_paths=extra, video_ratio=video_ratio, resolution=resolution, model=video_model,
        )
        clips.append(clip)
        calls.append({"shot_id": shot["shot_id"], "image": str(primary), "reference_paths": extra,
                      "reference_ids": list(shot["reference_ids"])})
    return {
        "id": sample["id"],
        "sheets": {key: str(path) for key, path in sheets.items()},
        "clips": [str(path) for path in clips],
        "calls": calls,
        "image_model": image_model,
        "video_model": video_model,
        "video_mode": "reference",
        "resolution": resolution,
        "video_ratio": video_ratio,
    }
