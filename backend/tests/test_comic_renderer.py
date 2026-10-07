"""Real local FFmpeg/ffprobe rendering; no remote model or TTS calls."""
from __future__ import annotations

import io
import math
import shutil
import struct
import sys
import wave
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from app.core.errors import AppError
from app.services import comic_renderer as renderer

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="local FFmpeg/ffprobe required")


def panel(path: Path):
    image = Image.new("RGB", (900, 900), "#172740")
    draw = ImageDraw.Draw(image)
    for x in range(0, 900, 40):
        draw.rectangle((x, 0, x + 20, 900), fill=(30 + x // 5, 80, 160))
    draw.ellipse((190, 110, 610, 740), fill="#edbb71")
    draw.rectangle((250, 400, 650, 460), fill="#eb5a6c")
    image.save(path)


def tone(path: Path, duration: float, frequency: float = 440):
    rate = 48000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"".join(struct.pack("<h", round(5000 * math.sin(2 * math.pi * frequency * i / rate))) for i in range(round(rate * duration))))


async def frame(path: Path, seconds: float) -> Image.Image:
    data = await renderer._run(["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], "test frame")
    return Image.open(io.BytesIO(data)).convert("RGB")


@pytest.mark.asyncio
async def test_real_dialogue_duration_chinese_burn_in_srt_and_concat(tmp_path):
    image = tmp_path / "panel 'quoted'.png"
    audio_a, audio_b = tmp_path / "a.wav", tmp_path / "b.wav"
    panel(image)
    tone(audio_a, 0.7)
    tone(audio_b, 0.8, 660)
    lines = [
        {"line_id": "l1", "speaker_id": "c1", "speaker": "林澈", "text": "她说：“现在几点？” [安全]：不执行 'filter'。", "audio_path": str(audio_a)},
        {"line_id": "l2", "speaker": "旁白", "text": "钟楼亮起，新的故事开始。", "audio_path": str(audio_b)},
    ]
    rendered = await renderer.render_comic_shot(image, lines, tmp_path / "spoken.mp4", ratio="16:9", motion="static")
    assert (rendered["width"], rendered["height"]) == (1280, 720)
    assert abs(rendered["duration"] - 1.5) <= 1 / renderer.FPS
    assert abs(rendered["audio_duration"] - 1.5) < 0.0001
    assert rendered["cues"][0]["start"] == 0
    assert rendered["cues"][-1]["end"] == 1.5
    assert rendered["cues"][0]["line_id"] == "l1"
    assert "林澈" in Path(rendered["srt_path"]).read_text()
    assert "filter" in Path(rendered["srt_path"]).read_text()
    first = await frame(Path(rendered["path"]), 0.3)
    # White glyph pixels exist only in subtitle overlay (source has no white).
    bottom = first.crop((0, 520, 1280, 710))
    white_pixels = sum(1 for r, g, b in bottom.getdata() if min(r, g, b) > 215)
    assert white_pixels > 100, "Chinese subtitle raster was not burned into the video"
    silent = await renderer.render_comic_shot(image, [], tmp_path / "silent.mp4", ratio="16:9", silent_duration=0.5)
    merged = await renderer.concat_comic_shots([rendered, silent], tmp_path / "episode.mp4")
    assert abs(merged["duration"] - 2.0) <= 1 / renderer.FPS
    assert merged["cues"] == rendered["cues"]
    assert len(merged["shot_timings"]) == 2
    assert abs(merged["shot_timings"][1]["start"] - 1.5) < 0.002
    assert not list(tmp_path.glob("comic-render-*")), "temporary subtitle/image files leaked"


@pytest.mark.asyncio
@pytest.mark.parametrize("motion", ["push_in", "pan_left", "pan_right"])
async def test_real_portrait_motion_changes_pixels(tmp_path, motion):
    image = tmp_path / "panel.png"
    panel(image)
    result = await renderer.render_comic_shot(image, [], tmp_path / f"{motion}.mp4", motion=motion, ratio="9:16", silent_duration=0.75)
    assert (result["width"], result["height"]) == (720, 1280)
    assert result["cues"] == []
    assert abs(result["duration"] - 0.75) < 0.045
    first, last = await frame(Path(result["path"]), 0), await frame(Path(result["path"]), 0.65)
    difference = ImageStat.Stat(ImageChops.difference(first, last)).mean
    assert sum(difference) > 2, "requested camera movement remained static"


@pytest.mark.asyncio
async def test_long_subtitle_paginates_and_silent_line_is_supported(tmp_path):
    image = tmp_path / "panel.png"
    panel(image)
    text = "钟楼的故事很长，林澈停下来整理思绪。" * 8
    result = await renderer.render_comic_shot(image, [{"text": text, "duration": 0.75}], tmp_path / "long.mp4", ratio="1:1")
    assert (result["width"], result["height"]) == (720, 720)
    assert len(result["cues"]) > 1
    assert all(len(cue["text"].splitlines()) <= 2 for cue in result["cues"])
    assert "".join(cue["text"].replace("\n", "") for cue in result["cues"]) == text
    assert result["cues"][-1]["end"] == 0.75


def test_missing_configured_cjk_font_fails_readably(monkeypatch):
    monkeypatch.setenv("COMIC_FONT_PATH", "/nonexistent/font.ttf")
    with pytest.raises(AppError) as error:
        renderer._subtitle_pages("中文字幕", "", 720)
    assert error.value.code == "COMIC_FONT_UNAVAILABLE"


def test_narrow_subtitles_keep_closing_punctuation_with_previous_character():
    width = 100
    font = renderer._font(24)
    for punctuation in "，。！？：；、）》】”’":
        text = "甲乙丙" + punctuation
        pages = renderer._subtitle_pages(text, "", width)
        lines = [line for page in pages for line in page.splitlines()]
        assert "".join(lines) == text
        assert all(font.getlength(line) <= int(width * 0.82) for line in lines)
        assert not any(line[0] in renderer.CLOSING_PUNCTUATION for line in lines)


@pytest.mark.asyncio
async def test_invalid_duration_and_motion_rejected(tmp_path):
    with pytest.raises(AppError) as error:
        await renderer.render_comic_shot(tmp_path / "absent.png", [], tmp_path / "bad.mp4", motion="zoom;inject")
    assert error.value.code == "COMIC_MOTION_INVALID"
    with pytest.raises(AppError) as error:
        await renderer.render_comic_shot(tmp_path / "absent.png", [], tmp_path / "bad.mp4", silent_duration=float("nan"))
    assert error.value.code == "COMIC_DURATION_INVALID"


def test_1080_canvas_contract():
    assert renderer._canvas("16:9", "1080P") == (1920, 1080)
    assert renderer._canvas("9:16", "1080P") == (1080, 1920)


@pytest.mark.asyncio
async def test_episode_resource_limit_is_checked_before_opening_inputs(tmp_path):
    with pytest.raises(AppError) as error:
        await renderer.concat_comic_shots([{"path": "absent.mp4"}] * 121, tmp_path / "episode.mp4")
    assert error.value.code == "COMIC_EPISODE_LIMIT"


@pytest.mark.asyncio
async def test_failed_render_removes_partial_output_and_subtitle_temporary_files(tmp_path, monkeypatch):
    image, output = tmp_path / "panel.png", tmp_path / "broken.mp4"
    panel(image)
    async def fail(*args, **kwargs):
        output.write_bytes(b"partial render")
        raise AppError("COMIC_RENDER_TIMEOUT", "timeout", 504)
    monkeypatch.setattr(renderer, "_run", fail)
    with pytest.raises(AppError):
        await renderer.render_comic_shot(image, [{"text": "中文字幕", "duration": 0.5}], output)
    assert not output.exists()
    assert not list(tmp_path.glob("comic-render-*"))


@pytest.mark.asyncio
async def test_subprocess_timeout_is_bounded_and_readable():
    with pytest.raises(AppError) as error:
        await renderer._run([sys.executable, "-c", "import time; time.sleep(5)"], "测试进程", timeout=0.02)
    assert error.value.code == "COMIC_RENDER_TIMEOUT"
