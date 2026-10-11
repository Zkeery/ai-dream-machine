# -*- coding: utf-8 -*-
"""给 D7 成片配音并烧录字幕（只用 edge-tts，不调用 AIHubMix）。

用法：
  .venv/bin/python scripts/d7_voiceover.py --only S1,S2,S3,S4,S5
输出：output/<样本>/final-master-vo.mp4、final-vo.mp4；不覆盖 final-master.mp4 / final.mp4。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "backend"))

from app.models.tts_client import TTSClient  # noqa: E402
from app.services import d7_voiceover as vo  # noqa: E402

EVIDENCE = PROJECT / "docs" / "evidence" / "D7-角色一致性"
LINES = EVIDENCE / "配音台词-S1到S5.json"


async def run_sample(sid: str, force: bool) -> dict:
    out = EVIDENCE / "output" / sid
    master = out / "final-master.mp4"
    out_master, out_preview = out / "final-master-vo.mp4", out / "final-vo.mp4"
    if out_preview.exists() and not force:
        return {"id": sid, "skipped": True}
    lines = vo.load_lines(LINES, sid)
    shots = sorted((out / "shots").glob("s[0-9]*.mp4"))
    bounds = vo.shot_bounds(shots)
    work = out / "vo"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    synth = await vo.synthesize_lines(lines, work, lambda rate: TTSClient(rate=rate))
    total = vo.d7_post.probe(master)["duration"]
    starts = vo.line_starts(lines, bounds)
    for line, st, s, (_b0, b1) in zip(lines, starts, synth, bounds):
        if st + s["duration"] > b1 + 0.01:
            raise SystemExit(f"{sid}/{line['shot_id']} 语音 {st:.2f}+{s['duration']:.2f}s 超出该镜终点 {b1:.2f}s")
    wav = vo.mix_audio([s["path"] for s in synth], starts, total, work / "voice.wav")
    ass = work / "subs.ass"
    ass.write_text(vo.build_ass(lines, bounds), encoding="utf-8")
    res = vo.render(master, ass, wav, out_master, out_preview)
    res["lines"] = [{"shot": line["shot_id"], "start": round(st, 3), "end": round(st + s["duration"], 3),
                     "tts_s": round(s["duration"], 2), "rate": s["rate"], "over_4_8": s["over"]}
                    for line, st, s in zip(lines, starts, synth)]
    res["id"] = sid
    return res


async def main(args) -> int:
    for sid in [x.strip() for x in args.only.split(",") if x.strip()]:
        res = await run_sample(sid, args.force)
        print(json.dumps(res, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--only", default="S1,S2,S3,S4,S5")
    p.add_argument("--force", action="store_true")
    sys.exit(asyncio.run(main(p.parse_args())))
