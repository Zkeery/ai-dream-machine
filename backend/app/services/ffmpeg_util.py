# -*- coding: utf-8 -*-
"""FFmpeg 工具：兼容浏览器的视频输出与有界子进程生命周期。"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import tempfile
from pathlib import Path

from app.core.errors import AppError

logger = logging.getLogger(__name__)

# Padding preserves the image content's aspect ratio, including odd-sized uploads.
_VIDEO_OUTPUT = ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2,setsar=1",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


async def _run_ffmpeg(command: list[str], label: str, *, timeout: float) -> bytes:
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        raise AppError("FFMPEG_UNAVAILABLE", "本地视频处理需要安装 FFmpeg", 503) from None
    # Keep draining the pipes during cancellation so a killed process can be reaped.
    communication = asyncio.create_task(proc.communicate())
    try:
        _, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await communication
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise AppError("FFMPEG_TIMEOUT", f"{label}超时，请缩短素材后重试", 504) from None
    if proc.returncode != 0:
        logger.warning("%s: %s", label, (stderr or b"").decode("utf-8", "ignore")[-500:])
        raise AppError("FFMPEG_FAILED", f"{label}失败，请检查输入媒体是否完整", 422)
    return stderr or b""


async def concat_videos(parts: list[Path], out_path: Path, audio_path: Path | None = None) -> Path:
    """统一不同模型的尺寸、帧率及音轨后按顺序拼接，保留原素材。"""
    if not parts:
        raise AppError("NO_PARTS", "没有可拼接的视频片段")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    probes = [await probe_media(part) for part in parts]
    first = next((s for s in probes[0].get("streams", []) if s.get("codec_type") == "video"), {})
    width, height = first.get("width", 0), first.get("height", 0)
    if not width or not height:
        raise AppError("VIDEO_MEDIA_INVALID", "视频片段缺少有效画面尺寸", 422)
    width, height = math.ceil(width / 2) * 2, math.ceil(height / 2) * 2
    # Same directory ensures bounded temporary files are cleaned on failure/cancel.
    with tempfile.TemporaryDirectory(prefix="concat-", dir=out_path.parent) as folder:
        folder = Path(folder)
        for index, (part, probe) in enumerate(zip(parts, probes)):
            streams = probe.get("streams", [])
            video = next((s for s in streams if s.get("codec_type") == "video"), {})
            try:
                duration = float(video.get("duration") or probe.get("format", {}).get("duration") or 0)
                if not math.isfinite(duration) or duration <= 0 or not video:
                    raise ValueError
            except (TypeError, ValueError):
                raise AppError("VIDEO_MEDIA_INVALID", "视频片段时长无效", 422) from None
            has_audio = any(s.get("codec_type") == "audio" for s in streams)
            cmd = ["ffmpeg", "-nostdin", "-y", "-i", str(part)]
            if not has_audio:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
            cmd += ["-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0",
                    "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
                    "-af", "aresample=48000,apad", "-t", str(duration),
                    "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                    "-video_track_timescale", "15360", str(folder / f"{index}.mp4")]
            await _run_ffmpeg(cmd, "片段格式统一", timeout=300)
        list_file = folder / "parts.txt"
        # Generated numeric relative names avoid quote escaping of user paths.
        list_file.write_text("".join(f"file '{i}.mp4'\n" for i in range(len(parts))), encoding="utf-8")
        cmd = ["ffmpeg", "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file)]
        if audio_path:
            cmd += ["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-shortest"]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-movflags", "+faststart", str(out_path)]
        await _run_ffmpeg(cmd, "视频拼接", timeout=600)
        return out_path


async def image_audio_to_video(image_path: Path, audio_path: Path, out_path: Path) -> Path:
    """静态图片 + 音频合成视频（时长取音频长度）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-loop", "1", "-i", str(image_path),
        "-i", str(audio_path),
        *_VIDEO_OUTPUT, "-tune", "stillimage", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(out_path),
    ]
    await _run_ffmpeg(cmd, "图片配音合成", timeout=300)
    return out_path


async def extract_first_frame(video_path: Path, out_path: Path) -> Path:
    """提取视频首帧为 jpg。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", str(video_path), "-frames:v", "1", "-q:v", "2", str(out_path)]
    await _run_ffmpeg(cmd, "首帧提取", timeout=60)
    return out_path


async def probe_media(path: Path) -> dict:
    """Inspect streams with a bounded process; no provider response is trusted as media."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,duration,width,height",
            "-of", "json", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise AppError("FFMPEG_UNAVAILABLE", "视频与声音检查需要安装 FFmpeg", 503) from None
    communication = asyncio.create_task(proc.communicate())
    try:
        stdout, _ = await asyncio.wait_for(asyncio.shield(communication), timeout=30)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await communication
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise AppError("MEDIA_PROBE_TIMEOUT", "视频与声音检查超时，已保留生成任务，可稍后继续", 504) from None
    try:
        data = json.loads(stdout)
        if proc.returncode != 0 or not isinstance(data, dict):
            raise ValueError
        return data
    except (ValueError, TypeError, UnicodeError):
        raise AppError("TALKING_MEDIA_INVALID", "生成结果不是可读取的视频，未标记为嘴型同步成功", 502) from None


async def verify_talking_video(path: Path, *, expected_duration: float = 10) -> dict:
    data = await probe_media(path)
    streams = data.get("streams") or []
    kinds = {stream.get("codec_type") for stream in streams if isinstance(stream, dict)}
    if not {"video", "audio"}.issubset(kinds):
        raise AppError("TALKING_AUDIO_MISSING", "生成结果缺少画面或声音，未标记为嘴型同步成功；请检查后重新生成", 502)
    try:
        duration = float((data.get("format") or {}).get("duration"))
        if not math.isfinite(duration) or abs(duration - expected_duration) > 1:
            raise ValueError
    except (ValueError, TypeError):
        raise AppError("TALKING_DURATION_INVALID", "生成视频时长不符合 10 秒要求，未标记为嘴型同步成功", 502) from None
    return {"duration": duration}


async def extract_audio(video_path: Path, out_path: Path) -> Path:
    """Create an audio preview while preserving the provider's original video track."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-nostdin", "-y", "-i", str(video_path), "-map", "0:a:0", "-vn",
           "-c:a", "aac", "-b:a", "128k", str(out_path)]
    try:
        await _run_ffmpeg(cmd, "口播声音提取", timeout=60)
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    return out_path


async def trim_talking_silence(video_path: Path, out_path: Path, *, duration: float) -> dict:
    """Trim only sustained trailing silence, preserving pauses and the provider file.

    This is an audio activity check, not a guarantee of verbatim speech or lip sync.
    A 350 ms tail protects quiet word endings. Isolated audible sounds are retained.
    """
    log = await _run_ffmpeg(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video_path), "-map", "0:a:0",
         "-af", "silencedetect=noise=-45dB:d=0.8", "-f", "null", "-"],
        "口播静音检查", timeout=60,
    )
    text = log.decode("utf-8", "replace")
    events = re.findall(r"silence_(start|end):\s*([\d.]+)", text)
    tail_start = None
    start = None
    for kind, value in events:
        if kind == "start":
            start = float(value)
        elif start is not None:
            if float(value) >= duration - 0.12:
                tail_start = start
            start = None
    if start is not None:
        tail_start = start
    if tail_start is not None and tail_start < 0.15:
        raise AppError("TALKING_AUDIO_SILENT", "生成视频的音轨没有有效声音，已保留原片；请检查后再决定是否重新生成", 502)
    end = min(duration, tail_start + 0.35) if tail_start is not None else duration
    if duration - end < 0.5:
        return {"path": video_path, "duration": duration, "trimmed_tail": 0.0}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _run_ffmpeg(
            ["ffmpeg", "-nostdin", "-y", "-i", str(video_path), "-t", f"{end:.3f}",
             "-map", "0:v:0", "-map", "0:a:0", *_VIDEO_OUTPUT,
             "-c:a", "aac", "-b:a", "128k", str(out_path)],
            "口播尾部静音处理", timeout=120,
        )
        info = await probe_media(out_path)
        actual = float(info["format"]["duration"])
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    return {"path": out_path, "duration": actual, "trimmed_tail": duration - actual}
