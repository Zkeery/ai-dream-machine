# -*- coding: utf-8 -*-
"""D7 成片后期：与 S1（2026-10-11，可灵 1080P）完全一致的处理。

参数来源：S1 成片 ffprobe 与 x264 编码头（output/S1/final-master.mp4、final.mp4）。
- 输入：本地原始单镜 shots/s1..s5.mp4（平台下载的原片，不再二次压缩后拼接）。
- 第 0 帧：只有在第 0 帧是「定妆图」时才去掉。判定：第 0→1 帧的平均像素差明显大于后续帧间差
  （> 3 倍中位数且 > 8）。S1 五镜都不满足，所以 S1 没有去帧（605 帧 = 5 × 121）。
- 不加锐化、不缩放：所有单镜必须已是同一分辨率（统一 1080P）；分辨率不一致直接报错，不混用。
- 不转帧率：输出 24fps（与原片一致）。无音轨。
- master：libx264 preset slow，CRF 16，yuv420p，SAR 1:1，+faststart。
- 预览：libx264 两遍编码 preset veryslow，-b:v 4650k -maxrate 6500k -bufsize 9000k，yuv420p，+faststart；
  若超过 15,000,000 字节，按时长下调码率重编一次。
"""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import tempfile
from pathlib import Path

from app.core.errors import AppError

FPS = 24
MASTER_CRF = 16
MASTER_PRESET = "slow"
PREVIEW_PRESET = "veryslow"
PREVIEW_KBPS = 4650
PREVIEW_MAXRATE_KBPS = 6500
PREVIEW_BUFSIZE_KBPS = 9000
PREVIEW_LIMIT_BYTES = 15_000_000
SHEET_DIFF_RATIO = 3.0
SHEET_DIFF_MIN = 8.0


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise AppError("FFMPEG_UNAVAILABLE", "本地视频处理需要安装 FFmpeg", 503)
    return path


def _run(cmd: list[str], label: str, timeout: float = 1800) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "ignore")[-400:]
        raise AppError("FFMPEG_FAILED", f"{label}失败：{tail}", 422)


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,codec_name,width,height,r_frame_rate,nb_frames,bit_rate,pix_fmt",
         "-show_entries", "format=duration,bit_rate,size", "-of", "json", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout
    data = json.loads(out)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    num, den = (video.get("r_frame_rate") or "0/1").split("/")
    return {
        "width": int(video.get("width") or 0), "height": int(video.get("height") or 0),
        "fps": float(num) / float(den or 1), "frames": int(video.get("nb_frames") or 0),
        "pix_fmt": video.get("pix_fmt"),
        "duration": float(data.get("format", {}).get("duration") or 0),
        "bit_rate": int(data.get("format", {}).get("bit_rate") or 0),
        "size": int(data.get("format", {}).get("size") or path.stat().st_size),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def first_frame_is_sheet(clip: Path) -> dict:
    """判断第 0 帧是不是定妆图（与后续帧突变）。只读本地文件。"""
    w, h, n = 160, 90, 5
    raw = subprocess.run(
        [_ffmpeg(), "-v", "error", "-i", str(clip), "-frames:v", str(n),
         "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout
    size = w * h
    frames = [raw[i * size:(i + 1) * size] for i in range(len(raw) // size)]
    if len(frames) < 3:
        return {"trim": False, "diffs": [], "reason": "帧数不足，不去帧"}
    diffs = [sum(abs(a - b) for a, b in zip(frames[i], frames[i + 1])) / size for i in range(len(frames) - 1)]
    rest = statistics.median(diffs[1:])
    trim = diffs[0] > max(SHEET_DIFF_MIN, SHEET_DIFF_RATIO * rest)
    return {"trim": trim, "diffs": [round(d, 2) for d in diffs],
            "reason": "第0帧与后续画面突变，判定为定妆图" if trim else "第0帧是正常画面，不去帧"}


def _filter(n: int, trims: list[bool]) -> str:
    parts = []
    for i, trim in enumerate(trims):
        sel = f"[{i}:v]" + ("trim=start_frame=1,setpts=PTS-STARTPTS," if trim else "") + f"setsar=1[v{i}]"
        parts.append(sel)
    inputs = "".join(f"[v{i}]" for i in range(n))
    parts.append(f"{inputs}concat=n={n}:v=1:a=0,format=yuv420p[v]")
    return ";".join(parts)


def finalize(clips: list[Path], out_dir: Path, *, preview_limit: int = PREVIEW_LIMIT_BYTES) -> dict:
    """由本地单镜产出 final-master.mp4 与 final.mp4（预览 < 15MB）。"""
    if not clips:
        raise AppError("NO_PARTS", "没有可拼接的视频片段")
    out_dir.mkdir(parents=True, exist_ok=True)
    infos = [probe(c) for c in clips]
    dims = {(i["width"], i["height"]) for i in infos}
    if len(dims) != 1:
        raise AppError("D7_RESOLUTION_MIXED", f"单镜分辨率不一致 {sorted(dims)}；按生成规则不混用分辨率", 422)
    checks = [first_frame_is_sheet(c) for c in clips]
    trims = [c["trim"] for c in checks]
    inputs: list[str] = []
    for c in clips:
        inputs += ["-i", str(c)]
    graph = _filter(len(clips), trims)
    master = out_dir / "final-master.mp4"
    preview = out_dir / "final.mp4"
    ff = _ffmpeg()
    common = ["-filter_complex", graph, "-map", "[v]", "-an", "-r", str(FPS), "-c:v", "libx264"]
    _run([ff, "-nostdin", "-v", "error", "-y", *inputs, *common, "-preset", MASTER_PRESET,
          "-crf", str(MASTER_CRF), "-movflags", "+faststart", str(master)], "master 编码")

    kbps = PREVIEW_KBPS
    with tempfile.TemporaryDirectory(prefix="d7post-") as tmp:
        log = str(Path(tmp) / "pass")
        for attempt in (1, 2):
            rate = ["-preset", PREVIEW_PRESET, "-b:v", f"{kbps}k",
                    "-maxrate", f"{PREVIEW_MAXRATE_KBPS}k", "-bufsize", f"{PREVIEW_BUFSIZE_KBPS}k"]
            _run([ff, "-nostdin", "-v", "error", "-y", *inputs, *common, *rate,
                  "-pass", "1", "-passlogfile", log, "-f", "mp4", "/dev/null"], "预览第一遍")
            _run([ff, "-nostdin", "-v", "error", "-y", *inputs, *common, *rate,
                  "-pass", "2", "-passlogfile", log, "-movflags", "+faststart", str(preview)], "预览第二遍")
            size = preview.stat().st_size
            if size < preview_limit:
                break
            duration = probe(preview)["duration"] or 1
            kbps = int(preview_limit * 0.95 * 8 / duration / 1000)
    if preview.stat().st_size >= preview_limit:
        raise AppError("D7_PREVIEW_TOO_LARGE", "预览版仍超过 15MB", 422)
    return {"master": str(master), "preview": str(preview), "trims": checks,
            "preview_kbps": kbps, "master_info": probe(master), "preview_info": probe(preview)}


# ---------- 复用已有素材（--reuse-existing） ----------
EXPECTED_SIZE = {"1080P": (1920, 1080), "720P": (1280, 720)}
MIN_CLIP_FRAMES = 115  # 5 秒 × 24fps ≈ 121 帧，留少量余量
MIN_SHEET_BYTES = 10_000


def reusable_sheet(path: Path) -> bool:
    """定妆图可复用：存在、不是空文件、是 PNG/JPEG。"""
    try:
        if not path.is_file() or path.stat().st_size < MIN_SHEET_BYTES:
            return False
        head = path.read_bytes()[:8]
    except OSError:
        return False
    return head.startswith(b"\x89PNG\r\n\x1a\n") or head[:3] == b"\xff\xd8\xff"


def reusable_clip(path: Path, resolution: str = "1080P", fps: int = FPS) -> bool:
    """单镜可复用：分辨率与本轮一致、24fps、帧数正常（≈5 秒）。"""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        info = probe(path)
    except Exception:
        return False
    return ((info["width"], info["height"]) == EXPECTED_SIZE.get(resolution)
            and abs(info["fps"] - fps) < 0.01 and info["frames"] >= MIN_CLIP_FRAMES)


def reuse_plan(sample: dict, out_dir: Path, resolution: str = "1080P") -> dict:
    """返回每张定妆图 / 每个镜头是否可直接复用（True=复用，不调用模型）。"""
    return {
        "assets": {a["id"]: reusable_sheet(out_dir / "sheets" / f"{a['id']}.png") for a in sample["assets"]},
        "shots": {s["shot_id"]: reusable_clip(out_dir / "shots" / f"{s['shot_id']}.mp4", resolution)
                  for s in sample["shots"]},
    }
