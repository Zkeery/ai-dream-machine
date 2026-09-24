# -*- coding: utf-8 -*-
"""批量跑真实样例用于 D7 验收标准打分。每条创意跑完整 6 阶段，保存成片与摘要。"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.services.orchestrator import STAGE_ORDER, Orchestrator  # noqa: E402
from app.schemas.session import SessionCreate  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "阶段2" / "样例打分"

SAMPLES = [
    ("科幻", "在火星基地，一个清洁机器人第一次透过穹顶看到蓝色的地球"),
    ("悬疑", "深夜便利店里，一位浑身湿透的神秘顾客反复购买同一把黑伞"),
    ("古风", "竹林深处，一只会说话的狐狸守护着一座被遗忘的茶亭"),
    ("喜剧", "外卖小哥在送餐路上接连遇到各种离谱又好笑的事"),
]


async def progress(_stage: str, _msg: str, _pct: int) -> None:
    pass


async def run_one(idx: int, genre: str, idea: str) -> dict:
    orch = Orchestrator()
    meta = orch.create(SessionCreate(idea=idea, episodes=1))
    for stage in STAGE_ORDER:
        meta = await orch.execute_stage(meta.session_id, stage, progress)
        if stage != STAGE_ORDER[-1]:
            meta = orch.continue_session(meta.session_id)
    final = Path(meta.artifacts["post_production"]["final_video"])
    title = (meta.artifacts.get("script_generation") or {}).get("title", f"样例{idx}")
    dest = OUT_DIR / f"{idx}-{genre}-{title}.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(final.read_bytes())
    summary = {
        "序号": idx,
        "风格": genre,
        "创意": idea,
        "片名": title,
        "成片": str(dest),
        "角色数": len((meta.artifacts.get("script_generation") or {}).get("characters", [])),
        "镜头数": len((meta.artifacts.get("storyboard") or {}).get("shots", [])),
    }
    (OUT_DIR / f"{idx}-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for idx, (genre, idea) in enumerate(SAMPLES, start=2):  # 1 号是已跑过的雨夜微光
        t0 = time.time()
        print(f"[{idx}/5] {genre}：{idea}", flush=True)
        try:
            s = await run_one(idx, genre, idea)
            print(f"  完成《{s['片名']}》 镜头 {s['镜头数']}，耗时 {time.time()-t0:.0f}s", flush=True)
        except Exception as e:
            print(f"  失败: {type(e).__name__} {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
