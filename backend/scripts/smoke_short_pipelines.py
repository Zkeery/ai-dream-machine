# -*- coding: utf-8 -*-
"""第二刀短管线真实冒烟：3 条管线各跑 1 条样例，真实模型调用，产物落盘到证据目录。"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core import config  # noqa: E402
from app.services.short_pipelines import ShortPipelines  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "阶段2" / "短管线冒烟"

# 动作迁移/数字人口播复用第一刀真实生成的素材
CHAR_IMG = str(config.IMAGE_DIR / "1790147269989" / "character_0.png")
MOTION_VID = str(config.VIDEO_DIR / "1790149290860" / "seg_0.mp4")

SAMPLES = [
    ("literary", "literary_video", {"text": "雨夜的老街，一盏孤灯下，一个人撑着伞慢慢走过。", "style": "realistic"}),
    ("motion", "motion_transfer", {"character_image": CHAR_IMG, "motion_video": MOTION_VID, "prompt": "人物微笑着向镜头挥手"}),
    ("talking", "talking_head", {"person_image": CHAR_IMG, "script": "欢迎来到AI造梦机。输入一句创意，就能生成属于你的短片。"}),
]


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    pipelines = ShortPipelines()
    results = []
    for name, ptype, inputs in SAMPLES:
        task_id = f"smoke-{name}"
        print(f"=== [{name}] {ptype} 开始 ===", flush=True)
        t0 = time.time()

        async def progress(stage: str, msg: str, pct: int) -> None:
            print(f"  [{stage}] {msg} ({pct}%)", flush=True)

        try:
            r = await pipelines.run(task_id, ptype, inputs, progress)
            dt = time.time() - t0
            print(f"  [{name}] 完成，耗时 {dt:.0f}s", flush=True)
            # 拷贝成片到证据目录
            final = Path(r["final_video"])
            dest = OUT_DIR / f"{name}-{final.name}"
            if final.exists():
                shutil.copy2(final, dest)
                r["evidence_video"] = str(dest)
            results.append({"name": name, "type": ptype, "ok": True, "elapsed_s": round(dt, 1), "result": r})
        except Exception as e:
            dt = time.time() - t0
            print(f"  [{name}] 失败: {type(e).__name__} {e}", flush=True)
            results.append({"name": name, "type": ptype, "ok": False, "elapsed_s": round(dt, 1), "error": str(e)})

    (OUT_DIR / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== 结果已写入", OUT_DIR / "result.json", "===", flush=True)
    for r in results:
        print(f"  {r['name']}: {'OK' if r['ok'] else 'FAIL'} ({r.get('elapsed_s')}s)", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
