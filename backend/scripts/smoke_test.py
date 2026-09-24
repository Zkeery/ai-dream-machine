# -*- coding: utf-8 -*-
"""端到端真实冒烟：一句创意 → 6 阶段 → 成片（真实模型，会产生费用）。

用法：
  cd backend
  .venv/bin/python scripts/smoke_test.py "你的创意" [剧集数]
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# 保证能 import app（backend 目录加入 path）
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.orchestrator import STAGE_ORDER, Orchestrator
from app.schemas.session import SessionCreate

STAGE_CN = {
    "script_generation": "剧本",
    "character_design": "角色/场景",
    "storyboard": "分镜",
    "reference_generation": "参考图",
    "video_generation": "视频",
    "post_production": "成片",
}


async def progress(stage: str, msg: str, pct: int) -> None:
    print(f"  [{STAGE_CN.get(stage, stage)}] {pct}% {msg}")


async def main() -> None:
    idea = sys.argv[1] if len(sys.argv) > 1 else "一只流浪猫在雨夜被好心人收留"
    episodes = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    orch = Orchestrator()
    meta = orch.create(SessionCreate(idea=idea, episodes=episodes))
    print(f"会话: {meta.session_id}\n创意: {idea}\n")

    t_total = time.time()
    for stage in STAGE_ORDER:
        t0 = time.time()
        print(f"=== 阶段 {STAGE_CN[stage]} ===")
        meta = await orch.execute_stage(meta.session_id, stage, progress)
        print(f"  完成，耗时 {time.time()-t0:.1f}s")
        art = meta.artifacts.get(stage, {})
        if stage == "script_generation":
            print(f"  片名: {art.get('title')} | 角色 {len(art.get('characters',[]))} | 场景 {len(art.get('settings',[]))}")
        elif stage == "storyboard":
            print(f"  镜头数: {len(art.get('shots',[]))}")
        elif stage == "post_production":
            print(f"  成片: {art.get('final_video')}")
        if stage != STAGE_ORDER[-1]:
            meta = orch.continue_session(meta.session_id)

    print(f"\n✅ 全流程完成，总耗时 {time.time()-t_total:.1f}s，成片: {meta.artifacts['post_production']['final_video']}")


if __name__ == "__main__":
    asyncio.run(main())
