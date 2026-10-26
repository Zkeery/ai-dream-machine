# -*- coding: utf-8 -*-
"""D7 成片配音 + 烧录字幕（不调用任何生图/生视频模型）。

- 配音：项目自带 TTSClient（edge-tts，微软神经语音，免费）。每镜一句，从该镜起点开始播；
  单句超过 4.8 秒时按比例提高 TTS 语速重合成，不截断；每句 0.1s 淡入、0.15s 淡出；音轨补静音到与视频等长。
- 字幕：ASS，白字黑描边，底部居中，Noto Sans CJK SC；每句只在对应镜的时间段显示。
- 画面：烧字幕必须重编码。master 用 libx264 slow CRF16（与 d7_post 一致）；
  预览两遍编码，码率 = min(4650k, 按 15MB 上限扣除音频后的可用码率)。
"""
from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path

from app.core.errors import AppError
from app.core.offline import safe_media_command, safe_media_environment
from app.services import d7_post

MAX_LINE_S = 4.8
TARGET_LINE_S = 4.6
MAX_RATE_PCT = 60
BASE_RATE = "+0%"
FADE_IN_S = 0.10
FADE_OUT_S = 0.15
FONT_NAME = "Noto Sans CJK SC"
FONT_SIZE = 60
MASTER_AUDIO_KBPS = 192
PREVIEW_AUDIO_KBPS = 128
SAMPLE_RATE = 48000


def load_lines(path: Path, sample_id: str) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for sample in data["samples"]:
        if sample["id"] == sample_id:
            return sample["lines"]
    raise AppError("D7_VO_NO_LINES", f"台词文件里没有 {sample_id}")


def shot_bounds(shots: list[Path], fps: int = d7_post.FPS) -> list[tuple[float, float]]:
    """按各镜实际帧数算起止秒（S1–S5 后期都未去帧，与成片一一对应）。"""
    bounds, frame = [], 0
    for shot in shots:
        n = d7_post.probe(shot)["frames"]
        bounds.append((frame / fps, (frame + n) / fps))
        frame += n
    return bounds


def rate_for(duration: float, current_pct: int = 0) -> int:
    """时长超过 4.8s 时返回需要的语速百分比（相对 +0%），否则原样返回。"""
    if duration <= MAX_LINE_S:
        return current_pct
    speed = (1 + current_pct / 100) * duration / TARGET_LINE_S
    return min(MAX_RATE_PCT, int(math.ceil((speed - 1) * 100)))


def _pct(rate: str | None) -> int:
    return int(str(rate or "+0%").strip().rstrip("%") or 0)


def _rate_str(pct: int) -> str:
    return f"{pct:+d}%"


def line_starts(lines: list[dict], bounds: list[tuple[float, float]]) -> list[float]:
    """每句起点 = 该镜起点 + 可选 offset_s（用于对齐嘴动），且必须落在该镜内。"""
    starts = []
    for line, (start, end) in zip(lines, bounds):
        offset = float(line.get("offset_s") or 0)
        if not 0 <= offset < end - start:
            raise AppError("D7_VO_OFFSET", f"{line.get('shot_id')} offset_s={offset} 超出该镜时长", 422)
        starts.append(start + offset)
    return starts


def _ts(seconds: float) -> str:
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def build_ass(lines: list[dict], bounds: list[tuple[float, float]], width: int = 1920, height: int = 1080) -> str:
    if len(lines) != len(bounds):
        raise AppError("D7_VO_MISMATCH", f"台词 {len(lines)} 句与镜头 {len(bounds)} 个不一致")
    head = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\nPlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\n"
        f"Style: Default,{FONT_NAME},{FONT_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,"
        "1,4,1,2,80,80,70,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    starts = line_starts(lines, bounds)
    events = [f"Dialogue: 0,{_ts(st)},{_ts(end - 0.04)},Default,,0,0,0,,{_escape(line['text'])}"
              for line, st, (_start, end) in zip(lines, starts, bounds)]
    return head + "\n".join(events) + "\n"


def audio_duration(path: Path) -> float:
    out = subprocess.run(safe_media_command(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, env=safe_media_environment()).stdout.decode().strip()
    return float(out or 0)


async def synthesize_lines(lines: list[dict], work: Path, tts_factory) -> list[dict]:
    """逐句合成；超 4.8s 提速重合成（最多两次）。tts_factory(rate) -> 具备 synthesize 的客户端。"""
    results = []
    for i, line in enumerate(lines):
        text = line.get("tts_text") or line["text"]
        pct, path = _pct(line.get("rate")), work / f"line{i + 1}.mp3"
        limit = MAX_LINE_S
        for _ in range(3):
            rate = _rate_str(pct) if pct else BASE_RATE
            await tts_factory(rate).synthesize(text, path, voice=line["voice"])
            dur = audio_duration(path)
            nxt = rate_for(dur, pct)
            if dur <= MAX_LINE_S or nxt == pct:
                break
            pct = nxt
        results.append({"path": path, "duration": dur, "rate": _rate_str(pct), "over": dur > limit})
    return results


def mix_filter(starts: list[float], durations: list[float], total: float) -> str:
    parts = []
    for i, (start, dur) in enumerate(zip(starts, durations)):
        ms = int(round(start * 1000))
        out_start = max(0.0, dur - FADE_OUT_S)
        parts.append(f"[{i}:a]aresample={SAMPLE_RATE},aformat=channel_layouts=stereo,"
                     f"afade=t=in:st=0:d={FADE_IN_S},afade=t=out:st={out_start:.3f}:d={FADE_OUT_S},"
                     f"adelay={ms}|{ms}[a{i}]")
    inputs = "".join(f"[a{i}]" for i in range(len(starts)))
    parts.append(f"{inputs}amix=inputs={len(starts)}:normalize=0:dropout_transition=0,"
                 f"apad=whole_dur={total:.3f},atrim=0:{total:.3f}[aout]")
    return ";".join(parts)


def _run(cmd: list[str], label: str) -> None:
    proc = subprocess.run(safe_media_command(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=safe_media_environment())
    if proc.returncode != 0:
        raise AppError("FFMPEG_FAILED", f"{label}失败，请检查本地输入媒体", 422)


def mix_audio(clips: list[Path], starts: list[float], total: float, out_wav: Path) -> Path:
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    for clip in clips:
        cmd += ["-i", str(clip)]
    cmd += ["-filter_complex", mix_filter(starts, [audio_duration(c) for c in clips], total),
            "-map", "[aout]", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(out_wav)]
    _run(cmd, "混音")
    return out_wav


def _ass_filter(ass: Path) -> str:
    fonts = "/usr/share/fonts/opentype/noto"
    return f"ass={ass.as_posix()}:fontsdir={fonts}"


def render(master: Path, ass: Path, audio: Path, out_master: Path, out_preview: Path,
           *, preview_limit: int = d7_post.PREVIEW_LIMIT_BYTES) -> dict:
    info = d7_post.probe(master)
    duration = info["duration"]
    vf = _ass_filter(ass)
    base = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(master), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0", "-vf", vf, "-r", str(d7_post.FPS), "-c:v", "libx264",
            "-pix_fmt", "yuv420p"]
    _run([*base, "-preset", d7_post.MASTER_PRESET, "-crf", str(d7_post.MASTER_CRF),
          "-c:a", "aac", "-b:a", f"{MASTER_AUDIO_KBPS}k", "-t", f"{duration:.6f}", "-movflags", "+faststart",
          str(out_master)], "master 烧字幕")
    kbps = min(d7_post.PREVIEW_KBPS, int(preview_limit * 0.96 * 8 / duration / 1000) - PREVIEW_AUDIO_KBPS)
    with tempfile.TemporaryDirectory(prefix="d7vo-") as tmp:
        log = str(Path(tmp) / "pass")
        for _ in range(2):
            rate = ["-preset", d7_post.PREVIEW_PRESET, "-b:v", f"{kbps}k",
                    "-maxrate", f"{d7_post.PREVIEW_MAXRATE_KBPS}k", "-bufsize", f"{d7_post.PREVIEW_BUFSIZE_KBPS}k"]
            _run([*base, *rate, "-pass", "1", "-passlogfile", log, "-an", "-f", "mp4", "/dev/null"], "预览第一遍")
            _run([*base, *rate, "-pass", "2", "-passlogfile", log, "-c:a", "aac", "-b:a", f"{PREVIEW_AUDIO_KBPS}k",
                  "-t", f"{duration:.6f}", "-movflags", "+faststart", str(out_preview)], "预览第二遍")
            if out_preview.stat().st_size < preview_limit:
                break
            kbps = int(kbps * preview_limit * 0.95 / out_preview.stat().st_size)
    if out_preview.stat().st_size >= preview_limit:
        raise AppError("D7_PREVIEW_TOO_LARGE", "配音预览版仍超过 15MB", 422)
    return {"master": str(out_master), "preview": str(out_preview), "preview_video_kbps": kbps}
