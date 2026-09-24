# -*- coding: utf-8 -*-
"""FFmpeg 工具：视频拼接、音频替换。系统级依赖，已装 ffmpeg 9.0.2。"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from app.core.errors import AppError

logger = logging.getLogger(__name__)


async def concat_videos(parts: list[Path], out_path: Path, audio_path: Path | None = None) -> Path:
    """按顺序拼接视频片段；可选替换为指定音频。"""
    if not parts:
        raise AppError("NO_PARTS", "没有可拼接的视频片段")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # concat demuxer 列表文件
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p.resolve()}'\n")
        list_file = f.name

    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file]
    if audio_path:
        cmd += ["-i", str(audio_path), "-c:v", "copy", "-c:a", "aac", "-shortest"]
    else:
        cmd += ["-c", "copy"]
    cmd += [str(out_path)]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            tail = (stderr or b"").decode("utf-8", "ignore")[-500:]
            raise AppError("FFMPEG_FAILED", f"视频拼接失败：{tail}")
        return out_path
    finally:
        Path(list_file).unlink(missing_ok=True)


async def image_audio_to_video(image_path: Path, audio_path: Path, out_path: Path) -> Path:
    """静态图片 + 音频合成视频（时长取音频长度）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", str(image_path),
        "-i", str(audio_path),
        "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "192k",
        "-shortest", str(out_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "ignore")[-500:]
        raise AppError("FFMPEG_FAILED", f"图片配音合成失败：{tail}")
    return out_path


async def extract_first_frame(video_path: Path, out_path: Path) -> Path:
    """提取视频首帧为 jpg。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-frames:v", "1", "-q:v", "2", str(out_path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "ignore")[-500:]
        raise AppError("FFMPEG_FAILED", f"首帧提取失败：{tail}")
    return out_path
