"""AIHubMix reviewed native schema contract tests; HTTP is entirely replaced."""
from pathlib import Path
import base64
import struct
import zlib

import httpx
import pytest

from app.core import config
from app.core.errors import AppError
from app.models.image_client import ImageClient
from app.models.video_client import VideoClient


def uri(path):
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def png(width, height, color):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = (b"\0" + bytes([color, color, color]) * width) * height
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def downloaded_file(self, url, out):
    """Match the download contract without network or actual generation."""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"offline-contract-media")
    return out


@pytest.fixture
def media(monkeypatch, tmp_path):
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "offline-test-key")
    monkeypatch.setattr(config.settings, "image_t2i_model", "qwen-image-2.0")
    monkeypatch.setattr(config.settings, "video_reference_model", "wan2.7-r2v")
    monkeypatch.setattr(config.settings, "video_first_frame_model", "wan2.7-i2v")
    paths = [tmp_path / f"ref{i}.png" for i in range(3)]
    for i, p in enumerate(paths):
        p.write_bytes(png(720, 1280, i))
    return paths


def test_qwen_receives_actual_image_bytes_and_portrait_size(media, monkeypatch, tmp_path):
    seen = []
    def post(url, **kwargs):
        seen.append((url, kwargs["json"]))
        return httpx.Response(200, json={"status": "completed", "output": [{"content_url": "https://example.test/generated.png"}]})
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(ImageClient, "_download", downloaded_file)
    ImageClient().image_to_image(media[:2], "保持角色与场景", tmp_path / "out.png", size="720x1280")
    assert seen[0][0].endswith("/ai/v1/images/generations")
    payload = seen[0][1]
    assert payload["images"] == [uri(media[0]), uri(media[1])]
    assert payload["size"] == "720x1280" and payload["model"] == "qwen-image-2.0"


def test_qwen_rejects_unsupported_reference_model(media, tmp_path):
    with pytest.raises(AppError) as e:
        ImageClient().image_to_image(media[0], "p", tmp_path / "o.png", model="unknown-image-model")
    assert e.value.code == "IMAGE_REFERENCE_UNSUPPORTED"


@pytest.mark.parametrize("mode", ["reference", "start_end", "first_frame"])
def test_wan_native_modes_receive_distinct_actual_inputs(media, monkeypatch, tmp_path, mode):
    seen = []
    def request(method, url, **kwargs):
        if method == "POST":
            seen.append((url, kwargs["json"]))
            return httpx.Response(200, json={"id": "vid", "status": "in_progress"})
        return httpx.Response(200, json={"status": "completed", "output": [{"content_url": "https://example.test/v.mp4"}]})
    monkeypatch.setattr(httpx, "request", request)
    monkeypatch.setattr(VideoClient, "_download", downloaded_file)
    client = VideoClient()
    client.poll_interval = 0
    client.image_to_video(str(media[0]), "p", tmp_path / "out.mp4", mode,
                          reference_paths=[str(media[1])], end_image_path=str(media[2]) if mode == "start_end" else None,
                          video_ratio="9:16", resolution="1080P")
    assert seen[0][0].endswith("/ai/v1/videos")
    p = seen[0][1]
    assert p["resolution"] == "1080p" and p["duration"] == 5
    assert p["extra"] == {"prompt_extend": False}
    if mode == "reference":
        assert p["aspect_ratio"] == "9:16"
        assert [x["url"] for x in p["input_references"]] == [uri(media[0]), uri(media[1])]
        assert "frame_images" not in p
    else:
        assert p["frame_images"][0]["frame_type"] == "first_frame"
        assert len(p["frame_images"]) == (2 if mode == "start_end" else 1)
        if mode == "start_end":
            assert p["frame_images"][1] == {"frame_type": "last_frame", "image_url": {"url": uri(media[2])}}
        assert "input_references" not in p


def test_start_end_rejects_missing_real_tail_before_http(media, monkeypatch, tmp_path):
    monkeypatch.setattr(httpx, "request", lambda *a, **k: pytest.fail("missing tail must not submit a paid task"))
    with pytest.raises(AppError) as e:
        VideoClient().image_to_video(str(media[0]), "p", tmp_path / "o.mp4", "start_end")
    assert e.value.code == "END_FRAME_REQUIRED"


def test_first_frame_rejects_old_wrong_ratio_before_http(media, monkeypatch, tmp_path):
    media[0].write_bytes(png(1280, 720, 0))
    monkeypatch.setattr(httpx, "request", lambda *a, **k: pytest.fail("wrong ratio must not submit"))
    with pytest.raises(AppError) as e:
        VideoClient().image_to_video(str(media[0]), "p", tmp_path / "o.mp4", video_ratio="9:16")
    assert e.value.code == "REFERENCE_RATIO_MISMATCH"


@pytest.mark.parametrize("mode,model,ratio", [
    ("reference", "wan2.7-r2v", "9:16"),
    ("first_frame", "wan2.7-i2v", "9:16"),
    ("start_end", "wan2.7-i2v", "9:16"),
    ("first_frame", "wan2.6-i2v", "9:16"),
    ("first_frame", "wan2.7-i2v", None),
])
def test_story_clip_never_receives_other_shots_from_agent_brief(media, monkeypatch, tmp_path, mode, model, ratio):
    from app.services.agent_runtime import BRIEF
    from app.services.prompts import VIDEO_PROMPT

    seen = []
    monkeypatch.setattr(VideoClient, "_create_or_resume",
                        lambda self, endpoint, payload, out, **kw: seen.append(payload) or out)
    prompt = VIDEO_PROMPT.format(description="特写，手打开纸袋", prompt="Close-up of hands opening a paper bag")
    token = BRIEF.set("全片：警惕张望，然后开袋，吃炸鸡，最后坐在夜色中。其他镜头秘密标记。")
    try:
        VideoClient().image_to_video(str(media[0]), prompt, tmp_path / "clip.mp4", mode,
                                    model=model, video_ratio=ratio,
                                    end_image_path=str(media[2]) if mode == "start_end" else None)
    finally:
        BRIEF.reset(token)
    assert len(seen) == 1
    assert seen[0]["prompt"] == prompt
    assert "其他镜头秘密标记" not in seen[0]["prompt"]
    if model == "wan2.6-i2v":
        assert seen[0]["extra"] == {"prompt_extend": True, "shot_type": "single"}
    elif ratio:
        assert seen[0]["extra"] == {"prompt_extend": False}
