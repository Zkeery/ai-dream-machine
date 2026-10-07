"""Audio-timed comic motion and Chinese subtitles using local FFmpeg + Pillow.

Subtitle text is rasterized with a verified CJK font and never interpolated into
FFmpeg filters. This also works with FFmpeg builds without libass/drawtext.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from app.core.errors import AppError

MOTIONS = ("static", "push_in", "pan_left", "pan_right")
FPS = 24
MAX_SHOT_SECONDS = 120
MAX_LINES = 24
MAX_CUES = 80
MAX_EPISODE_SHOTS = 120
MAX_EPISODE_SECONDS = 1800
MAX_IMAGE_PIXELS = 25_000_000
CLOSING_PUNCTUATION = frozenset("，。！？：；、）》】”’〉〕」』﹚﹜)]},.!?:;%")


async def _run(command: list[str], label: str, timeout: float = 180) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise AppError("COMIC_RENDERER_UNAVAILABLE", "本地漫剧渲染需要安装 FFmpeg 和 ffprobe", 503) from None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        if asyncio.current_task() and asyncio.current_task().cancelling():
            raise
        raise AppError("COMIC_RENDER_TIMEOUT", f"{label}超时，请缩短镜头后重试", 504) from None
    if process.returncode:
        raise AppError("COMIC_RENDER_FAILED", f"{label}失败，请检查输入媒体是否完整", 422)
    return stdout


async def probe_media(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        raise AppError("COMIC_MEDIA_MISSING", "漫剧素材文件不存在", 404)
    raw = await _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path.resolve())], "读取素材", 30)
    try:
        info = json.loads(raw)
        streams = info.get("streams", [])
        duration = float(info.get("format", {}).get("duration", 0))
        if not math.isfinite(duration) or duration < 0:
            raise ValueError
        return {"duration": duration, "streams": streams}
    except (ValueError, TypeError):
        raise AppError("COMIC_MEDIA_INVALID", "无法读取素材的有效时长", 422) from None


def _canvas(ratio: str, resolution: str) -> tuple[int, int]:
    sizes = {"720P": 720, "1080P": 1080}
    if ratio not in {"9:16", "16:9", "1:1"} or resolution not in sizes:
        raise AppError("COMIC_FORMAT_INVALID", "漫剧支持 9:16、16:9、1:1 和 720P、1080P", 422)
    short = sizes[resolution]
    long = int(short * 16 / 9)
    return (short, long) if ratio == "9:16" else (long, short) if ratio == "16:9" else (short, short)


def _font(size: int) -> ImageFont.FreeTypeFont:
    configured = os.getenv("COMIC_FONT_PATH")
    candidates = [configured] if configured else [
        "/System/Library/Fonts/STHeiti Medium.ttc", "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "C:/Windows/Fonts/msyh.ttc",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            font = ImageFont.truetype(candidate, size)
            missing = bytes(font.getmask("\uffff"))
            if all(bytes(font.getmask(char)) != missing for char in "漫剧钟楼"):
                return font
        except (OSError, ValueError):
            continue
    raise AppError("COMIC_FONT_UNAVAILABLE", "未找到可用中文字体，请通过 COMIC_FONT_PATH 指向支持中文的字体文件", 503)


def _subtitle_pages(text: str, speaker: str, width: int) -> list[str]:
    text = " ".join(text.replace("\x00", "").split())
    if len(text) > 1200 or len(speaker) > 80:
        raise AppError("COMIC_SUBTITLE_LIMIT", "单句字幕最多 1200 字，角色名最多 80 字", 422)
    if not text:
        return []
    display = f"{speaker}：{text}" if speaker else text
    font = _font(max(24, round(width * 0.043)))
    available = int(width * 0.82)
    wrapped = []
    line = ""
    for char in display:
        if line and font.getlength(line + char) > available:
            # Do not strand closing Chinese punctuation on the next line.
            # Move the previous visible character with it, keeping both lines
            # within the measured width instead of allowing visual overflow.
            carry_index = len(line.rstrip()) - 1
            if char in CLOSING_PUNCTUATION and carry_index > 0 and font.getlength(line[carry_index:] + char) <= available:
                wrapped.append(line[:carry_index])
                line = line[carry_index:] + char
            else:
                wrapped.append(line)
                line = char
        else:
            line += char
    if line:
        wrapped.append(line)
    return ["\n".join(wrapped[index:index + 2]) for index in range(0, len(wrapped), 2)]


def _subtitle_png(text: str, width: int, target: Path) -> None:
    font = _font(max(24, round(width * 0.043)))
    padding = round(width * 0.018)
    spacing = round(width * 0.011)
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    bbox = measure.multiline_textbbox((0, 0), text, font=font, spacing=spacing, stroke_width=1, align="center")
    box_width = math.ceil(min(width - 2 * padding, bbox[2] - bbox[0] + 2 * padding))
    box_height = math.ceil(bbox[3] - bbox[1] + 2 * padding)
    image = Image.new("RGBA", (box_width, box_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, box_width - 1, box_height - 1), radius=padding, fill=(7, 10, 20, 195))
    draw.multiline_text((box_width / 2, padding - bbox[1]), text, font=font, anchor="ma", align="center", spacing=spacing, fill="white", stroke_width=1, stroke_fill=(0, 0, 0, 230))
    image.save(target)


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"


def write_srt(cues: list[dict], target: Path) -> Path:
    target.write_text("\n\n".join(f'{index}\n{_srt_time(cue["start"])} --> {_srt_time(cue["end"])}\n{cue["text"]}' for index, cue in enumerate(cues, 1)) + ("\n" if cues else ""), encoding="utf-8")
    return target


def _finite_duration(value, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise AppError("COMIC_DURATION_INVALID", f"{label}时长不合法", 422) from None
    if not math.isfinite(result) or not 0.05 <= result <= MAX_SHOT_SECONDS:
        raise AppError("COMIC_DURATION_INVALID", f"{label}时长需在 0.05 至 120 秒之间", 422)
    return result


async def render_comic_shot(image_path: Path, lines: list[dict], out_path: Path, *, ratio: str = "9:16",
                            motion: str = "static", silent_duration: float = 3.0, resolution: str = "720P") -> dict:
    """Render ordered dialogue audio; silence and long subtitle pagination supported.

    Each line: text, audio_path? (duration always probed), speaker?, line_id?,
    speaker_id?, duration? (seconds, only for lines with no audio).
    """
    if motion not in MOTIONS:
        raise AppError("COMIC_MOTION_INVALID", "镜头运动只支持静止、推近、向左平移、向右平移", 422)
    if len(lines) > MAX_LINES:
        raise AppError("COMIC_LINE_LIMIT", "每个镜头最多支持 24 句台词", 422)
    width, height = _canvas(ratio, resolution)
    image_path, out_path = Path(image_path), Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    audio_items, cues = [], []
    cursor = 0.0
    for line in lines:
        text = str(line.get("text", ""))
        audio_path = Path(line["audio_path"]) if line.get("audio_path") else None
        if audio_path:
            info = await probe_media(audio_path)
            if not any(stream.get("codec_type") == "audio" for stream in info["streams"]):
                raise AppError("COMIC_AUDIO_INVALID", "台词素材没有音轨", 422)
            duration = _finite_duration(info["duration"], "配音")
        else:
            duration = _finite_duration(line.get("duration", max(1.5, len(text) / 4)), "静音台词")
        audio_items.append({"path": audio_path, "duration": duration})
        pages = _subtitle_pages(text, str(line.get("speaker", "")), width)
        weights = [max(1, len(page.replace("\n", ""))) for page in pages]
        offset = cursor
        for page, weight in zip(pages, weights):
            end = offset + duration * weight / sum(weights)
            cues.append({"start": round(offset, 6), "end": round(end, 6), "text": page,
                         **{key: line[key] for key in ("speaker", "speaker_id", "line_id") if key in line}})
            offset = end
        cursor += duration
    if not audio_items:
        cursor = _finite_duration(silent_duration, "无台词镜头")
        audio_items = [{"path": None, "duration": cursor}]
    if cursor > MAX_SHOT_SECONDS or len(cues) > MAX_CUES:
        raise AppError("COMIC_SHOT_LIMIT", "镜头超过 120 秒或字幕分页过多，请拆分镜头", 422)
    frames = math.ceil(cursor * FPS)
    duration = frames / FPS
    with tempfile.TemporaryDirectory(prefix="comic-render-", dir=out_path.parent) as directory:
        temporary = Path(directory)
        still = temporary / "panel.png"
        try:
            with Image.open(image_path) as source:
                if source.width * source.height > MAX_IMAGE_PIXELS:
                    raise AppError("COMIC_IMAGE_TOO_LARGE", "漫画图片最多支持 2500 万像素，请缩小后重试", 422)
                ImageOps.fit(ImageOps.exif_transpose(source).convert("RGB"), (width * 2, height * 2), method=Image.Resampling.LANCZOS).save(still)
        except (OSError, ValueError, Image.DecompressionBombError):
            raise AppError("COMIC_IMAGE_INVALID", "无法读取漫画镜头图片", 422) from None
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(still)]
        filters = []
        for index, item in enumerate(audio_items, 1):
            if item["path"]:
                command += ["-i", str(item["path"].resolve())]
            else:
                command += ["-f", "lavfi", "-t", f'{item["duration"]:.6f}', "-i", "anullsrc=r=48000:cl=stereo"]
            filters.append(f'[{index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration={item["duration"]:.6f},asetpts=PTS-STARTPTS[a{index}]')
        audio_refs = "".join(f"[a{index}]" for index in range(1, len(audio_items) + 1))
        filters.append(f"{audio_refs}concat=n={len(audio_items)}:v=0:a=1,apad,atrim=duration={duration:.6f}[audio]")
        progress = f"on/{max(1, frames - 1)}"
        zoom = f"1+0.10*{progress}" if motion == "push_in" else "1.10" if motion.startswith("pan_") else "1"
        x = f"(iw-iw/zoom)*(1-{progress})" if motion == "pan_left" else f"(iw-iw/zoom)*{progress}" if motion == "pan_right" else "iw/2-iw/zoom/2"
        filters.append(f"[0:v]zoompan=z='{zoom}':x='{x}':y='ih/2-ih/zoom/2':d={frames}:s={width}x{height}:fps={FPS},setsar=1[v0]")
        for index, cue in enumerate(cues):
            subtitle = temporary / f"subtitle-{index:03}.png"
            _subtitle_png(cue["text"], width, subtitle)
            command += ["-i", str(subtitle)]
            input_index = 1 + len(audio_items) + index
            bottom = round(height * 0.055)
            filters.append(f'[v{index}][{input_index}:v]overlay=x=(W-w)/2:y=H-h-{bottom}:eof_action=repeat:enable=\'gte(t,{cue["start"]:.6f})*lt(t,{cue["end"]:.6f})\'[v{index + 1}]')
        command += ["-filter_complex_threads", "1", "-filter_complex", ";".join(filters), "-map", f"[v{len(cues)}]", "-map", "[audio]",
                    "-c:v", "libx264", "-threads", "2", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
                    "-c:a", "aac", "-b:a", "160k", "-t", f"{duration:.6f}", "-movflags", "+faststart", str(out_path.resolve())]
        try:
            await _run(command, "漫剧镜头合成", max(180, duration * 8))
        except BaseException:
            out_path.unlink(missing_ok=True)
            raise
    measured = await probe_media(out_path)
    srt_path = write_srt(cues, out_path.with_suffix(".srt"))
    return {"path": str(out_path), "duration": measured["duration"], "audio_duration": cursor, "width": width, "height": height,
            "fps": FPS, "cues": cues, "srt_path": str(srt_path), "motion": motion}


async def concat_comic_shots(parts: list[dict], out_path: Path) -> dict:
    """Concatenate normalized rendered shots and offset their subtitle cue clocks."""
    if not parts:
        raise AppError("COMIC_NO_PARTS", "没有可拼接的漫剧镜头", 422)
    if len(parts) > MAX_EPISODE_SHOTS:
        raise AppError("COMIC_EPISODE_LIMIT", "单集最多支持 120 个镜头，请拆成多集合成", 422)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cues, timings, media = [], [], []
    cursor, canvas = 0.0, None
    for index, part in enumerate(parts):
        path = Path(part["path"])
        info = await probe_media(path)
        video = next((stream for stream in info["streams"] if stream.get("codec_type") == "video"), None)
        if not video or not any(stream.get("codec_type") == "audio" for stream in info["streams"]):
            raise AppError("COMIC_PART_INVALID", "待拼接镜头必须同时包含画面和音轨", 422)
        size = (video["width"], video["height"])
        if canvas and canvas != size:
            raise AppError("COMIC_FORMAT_MISMATCH", "镜头画幅不一致，请重新合成对应镜头", 422)
        canvas = size
        duration = float(video.get("duration", info["duration"]))
        media.append((path.resolve(), duration))
        for cue in part.get("cues", []):
            cues.append({**cue, "start": round(cursor + cue["start"], 6), "end": round(cursor + cue["end"], 6)})
        timings.append({"index": index, "start": cursor, "end": cursor + duration, "path": str(path)})
        cursor += duration
        if cursor > MAX_EPISODE_SECONDS:
            raise AppError("COMIC_EPISODE_LIMIT", "单集最长支持 30 分钟，请拆成多集合成", 422)
    # The demuxer opens clips sequentially instead of keeping up to 120 HD
    # decoder inputs alive. Only generated ASCII basenames enter its manifest.
    with tempfile.TemporaryDirectory(prefix="comic-concat-", dir=out_path.parent) as directory:
        temporary = Path(directory)
        manifest = []
        for index, (source, duration) in enumerate(media):
            local = temporary / f"part{index:04}.mp4"
            try:
                os.link(source, local)
            except OSError:
                shutil.copyfile(source, local)
            manifest += [f"file '{local.name}'", f"duration {duration:.6f}"]
        listing = temporary / "parts.txt"
        listing.write_text("\n".join(manifest) + "\n", encoding="utf-8")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "1", "-i", str(listing),
                   "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-threads", "2", "-preset", "fast", "-crf", "20",
                   "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k", "-t", f"{cursor:.6f}",
                   "-movflags", "+faststart", str(out_path.resolve())]
        try:
            await _run(command, "漫剧整集合成", max(180, cursor * 8))
        except BaseException:
            out_path.unlink(missing_ok=True)
            raise
    measured = await probe_media(out_path)
    srt_path = write_srt(cues, out_path.with_suffix(".srt"))
    return {"path": str(out_path), "duration": measured["duration"], "width": canvas[0], "height": canvas[1], "fps": FPS,
            "cues": cues, "srt_path": str(srt_path), "shot_timings": timings}
