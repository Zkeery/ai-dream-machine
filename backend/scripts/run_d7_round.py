# -*- coding: utf-8 -*-
"""按 D7 改写提示词重跑 S1–S5。

先为每个角色、场景和道具生成定妆图，再用参考图模式把同一张图传给后续镜头。
没有 AIHUBMIX_API_KEY 时只记录原因，不调用模型，不写假成片或假分数。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
os.environ.setdefault("DATA_DIR", str(PROJECT / ".runtime" / "d7-round-20261010"))
sys.path.insert(0, str(PROJECT / "backend"))

from app.core import config  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.models.image_client import ImageClient  # noqa: E402
from app.models.video_client import VideoClient  # noqa: E402
from app.schemas.session import SessionCreate  # noqa: E402
from app.services import db, d7_runner, d7_set, execution_store, ffmpeg_util, session_store  # noqa: E402
from app.services.content_review import ContentReviewer  # noqa: E402
from app.services.orchestrator import Orchestrator  # noqa: E402

OWNER = "d7-round-20261010"


def _review(path: Path) -> None:
    reviewer = ContentReviewer()
    if not reviewer.enabled:
        return
    result = reviewer.review_image(str(path))
    if not result["safe"]:
        raise AppError("CONTENT_REVIEW_FAILED", f"内容安全拦截：{result['reason']}", 400)


async def _run_sample(sample: dict) -> dict:
    destination = d7_runner.OUTPUT / sample["id"]
    final = destination / "final.mp4"
    orch = Orchestrator()
    meta = orch.create(SessionCreate(
        idea=sample["idea"], style=sample["style"], episodes=1,
        video_ratio="16:9", resolution="720P", video_generation_mode="reference",
    ), owner_id=OWNER)
    execution_id, _created = execution_store.claim_session(
        meta.session_id, "video_generation", "generate",
        {"d7_sample": sample["id"], "fixture": "backend/tests/fixtures/d7_story_set.json"},
    )
    token = execution_store._current_execution.set(execution_id)
    try:
        rendered = d7_runner.render_sample(
            sample, ImageClient(), VideoClient(), destination,
            image_model=config.settings.image_t2i_model,
            video_model=config.settings.video_reference_model,
            review_image=_review,
        )
        await ffmpeg_util.concat_videos([Path(path) for path in rendered["clips"]], final)
        execution_store.finish(execution_id, {
            "type": "done", "stage": "video_generation", "message": f"{sample['id']} 已生成", "percent": 100,
        }, "completed")
        meta = orch.get(meta.session_id)
        meta.status = "session_completed"
        session_store.touch(meta)
        rendered["final"] = str(final)
        rendered["session_id"] = meta.session_id
        return rendered
    except AppError as error:
        execution_store.fail(execution_id, error.code, error.message)
        raise
    except Exception:
        execution_store.fail(execution_id, "INTERNAL_ERROR", "D7 样本生成失败")
        raise
    finally:
        execution_store._current_execution.reset(token)


async def _main(only: str | None, force: bool) -> int:
    blocked = d7_runner.credential_block()
    if blocked:
        d7_runner.write_status(blocked)
        print(blocked["reason"])
        print("本地运行步骤：")
        for step in blocked["how_to_run"]:
            print(f"- {step}")
        return 2
    config.settings.ensure_dirs()
    db.init_db()
    samples = d7_set.load_samples()
    if only:
        samples = [sample for sample in samples if sample["id"] == only]
        if not samples:
            print(f"未知样本 {only}")
            return 2
    results = []
    for sample in samples:
        final = d7_runner.OUTPUT / sample["id"] / "final.mp4"
        if final.exists() and not force:
            results.append({"id": sample["id"], "final": str(final), "skipped": True})
            print(f"{sample['id']} 已有成片，跳过。要重跑请加 --force。")
            continue
        print(f"生成 {sample['id']}：{sample['idea']}")
        try:
            rendered = await _run_sample(sample)
        except AppError as error:
            results.append({"id": sample["id"], "error": error.message, "code": error.code})
            print(f"  失败 {error.code}: {error.message}")
            continue
        results.append(rendered)
        print(f"  成片 {rendered['final']}")
    generated = [item for item in results if item.get("final")]
    d7_runner.write_status({
        "generated": bool(generated) and all(item.get("final") for item in results),
        "scores": None,
        "note": "有成片也不代表通过。四维分数仍为空，等待人工观看后填写待评分表。这里没有自动打分。",
        "image_model": config.settings.image_t2i_model,
        "video_model": config.settings.video_reference_model,
        "video_mode": "reference",
        "samples": [
            {"id": item["id"], "final": item.get("final"), "skipped": item.get("skipped", False),
             "error": item.get("error"), "code": item.get("code")}
            for item in results
        ],
    })
    return 0 if generated and all(item.get("final") for item in results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=list(d7_set.SAMPLE_IDS))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(_main(args.only, args.force)))
    except AppError as error:
        d7_runner.write_status({
            "generated": False,
            "reason": error.message,
            "code": error.code,
            "scores": None,
            "how_to_run": d7_runner.HOW_TO_RUN,
        })
        print(f"{error.code}: {error.message}")
        sys.exit(1)
