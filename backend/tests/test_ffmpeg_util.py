"""真实本地媒体与子进程生命周期回归；不调用任何模型服务。"""
from __future__ import annotations

import asyncio
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import wave

from PIL import Image
import pytest

from app.core.errors import AppError
from app.services import ffmpeg_util


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tail", "silent", "continuous"])
async def test_talking_tail_silence_preserves_pause_and_provider_file(tmp_path, kind):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("requires local FFmpeg and ffprobe")
    raw, final = tmp_path / "provider.mp4", tmp_path / "playback.mp4"
    signal = {"tail": "if(between(t,0.2,1)+between(t,2.5,3),0.2*sin(2*PI*440*t),0)",
              "silent": "0", "continuous": "0.2*sin(2*PI*440*t)"}[kind]
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=navy:s=64x64:r=25:d=5", "-f", "lavfi", "-i",
                    f"aevalsrc='{signal}':s=16000:d=5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(raw)], check=True)
    before = hashlib.sha256(raw.read_bytes()).hexdigest()
    if kind == "silent":
        with pytest.raises(AppError) as error:
            await ffmpeg_util.trim_talking_silence(raw, final, duration=5)
        assert error.value.code == "TALKING_AUDIO_SILENT"
        assert not final.exists()
    else:
        result = await ffmpeg_util.trim_talking_silence(raw, final, duration=5)
        if kind == "tail":
            assert result["path"] == final
            assert 3.3 <= result["duration"] <= 3.5  # the middle 1.5 second pause is retained
            assert result["trimmed_tail"] > 1.5
        else:
            assert result == {"path": raw, "duration": 5, "trimmed_tail": 0.0}
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == before


def _audio(path: Path) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"\x00\x00" * 8000)


def _probe(path: Path) -> dict:
    return json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)
    ]))


@pytest.mark.asyncio
async def test_concat_mixed_model_clips_normalizes_fps_size_and_missing_audio(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("requires local FFmpeg and ffprobe")
    first, second, final = (tmp_path / name for name in ("wan.mp4", "veo.mp4", "joined.mp4"))
    for path, size, fps, color in ((first, "160x90", 30, "red"), (second, "320x180", 24, "blue")):
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"color={color}:s={size}:r={fps}:d=1"]
        if path == second:
            cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1:sample_rate=44100", "-c:a", "aac"]
        subprocess.run([*cmd, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    originals = [p.read_bytes() for p in (first, second)]
    await ffmpeg_util.concat_videos([first, second], final)
    data = _probe(final)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next(s for s in data["streams"] if s["codec_type"] == "audio")
    assert (video["width"], video["height"], video["r_frame_rate"]) == (160, 90, "30/1")
    assert audio["sample_rate"] == "48000" and audio["channels"] == 2
    assert 1.9 <= float(data["format"]["duration"]) <= 2.2
    assert [p.read_bytes() for p in (first, second)] == originals
    assert not list(tmp_path.glob("concat-*"))
    # Decoding every frame catches timestamp/container failures missed by probing.
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(final), "-f", "null", "-"], check=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("size,expected", [((500, 700), (500, 700)), ((501, 701), (502, 702))])
async def test_photo_output_is_browser_compatible(tmp_path, size, expected):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("requires local FFmpeg and ffprobe")
    image, audio, output = tmp_path / "image.png", tmp_path / "voice.wav", tmp_path / "clip.mp4"
    Image.new("RGB", size, "navy").save(image)
    _audio(audio)
    await ffmpeg_util.image_audio_to_video(image, audio, output)
    probe = _probe(output)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == expected
    assert video["codec_name"] == "h264"
    assert video["pix_fmt"] == "yuv420p"
    assert video["sample_aspect_ratio"] == "1:1"
    assert any(s["codec_name"] == "aac" for s in probe["streams"])
    assert 0.9 <= float(probe["format"]["duration"]) <= 1.3
    data = output.read_bytes()
    assert data.find(b"moov") < data.find(b"mdat")
    joined = tmp_path / "joined.mp4"
    await ffmpeg_util.concat_videos([output, output], joined)
    assert 1.8 <= float(_probe(joined)["format"]["duration"]) <= 2.6
    frame = tmp_path / "first.jpg"
    await ffmpeg_util.extract_first_frame(joined, frame)
    with Image.open(frame) as first:
        assert first.size == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancellation_reaps_real_process(monkeypatch, cancel):
    started = asyncio.Event()
    children = []
    real_create = asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        process = await real_create(*args, **kwargs)
        children.append(process)
        started.set()
        return process

    monkeypatch.setattr(ffmpeg_util.asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(ffmpeg_util._run_ffmpeg(
        [sys.executable, "-c", "import time; time.sleep(60)"], "验收处理", timeout=0.1 if not cancel else 10
    ))
    await asyncio.wait_for(started.wait(), 2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(AppError, match="超时") as error:
            await task
        assert error.value.code == "FFMPEG_TIMEOUT"
    assert children[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)


@pytest.mark.asyncio
async def test_missing_ffmpeg_has_actionable_error(monkeypatch):
    async def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(ffmpeg_util.asyncio, "create_subprocess_exec", missing)
    with pytest.raises(AppError) as error:
        await ffmpeg_util._run_ffmpeg(["ffmpeg"], "视频处理", timeout=1)
    assert error.value.code == "FFMPEG_UNAVAILABLE"


@pytest.mark.asyncio
async def test_bad_media_has_sanitized_error():
    with pytest.raises(AppError) as error:
        await ffmpeg_util._run_ffmpeg([sys.executable, "-c", "import sys; sys.stderr.write('/private/input'); sys.exit(1)"],
                                     "视频处理", timeout=2)
    assert error.value.code == "FFMPEG_FAILED"
    assert "/private" not in error.value.message


@pytest.mark.asyncio
async def test_native_audio_probe_and_extraction_preserve_original_video(tmp_path):
    """Verify media plumbing only; a synthetic color clip is not lip-sync acceptance."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("requires local FFmpeg and ffprobe")
    video, audio = tmp_path / "native.mp4", tmp_path / "voice.m4a"
    await ffmpeg_util._run_ffmpeg([
        "ffmpeg", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=navy:s=64x64:r=10:d=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=10",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video),
    ], "合成媒体测试输入", timeout=30)
    original = video.read_bytes()
    result = await ffmpeg_util.verify_talking_video(video)
    assert 9.9 <= result["duration"] <= 10.1
    await ffmpeg_util.extract_audio(video, audio)
    assert video.read_bytes() == original
    streams = (await ffmpeg_util.probe_media(audio))["streams"]
    assert {stream["codec_type"] for stream in streams} == {"audio"}
