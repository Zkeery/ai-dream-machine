"""Native talking video protocol, billing duration and no duplicate submissions."""
import base64

import httpx
import pytest

from app.core import config
from app.core.errors import AppError
from app.models.video_client import VideoClient
from app.services import cost_control


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "offline-test-key")
    monkeypatch.setattr(config.settings, "aihubmix_base", "https://gateway.example.test")
    result = VideoClient()
    result.poll_interval = 0
    return result


def test_native_talking_payload_and_ten_second_reservation(client, tmp_path, monkeypatch):
    source = tmp_path / "portrait.png"
    source.write_bytes(b"offline-portrait")
    requests, reservations = [], []

    def request(method, url, **kwargs):
        requests.append((method, url, kwargs.get("json")))
        if method == "POST":
            return httpx.Response(200, json={"id": "talking-1", "status": "pending"})
        return httpx.Response(200, json={"status": "completed", "output": [{"content_url": "/result.mp4"}]})

    monkeypatch.setattr(httpx, "request", request)
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: httpx.Response(200, content=b"offline-video"))
    monkeypatch.setattr(cost_control, "reserve", lambda call_id, model, kind, units: reservations.append((model, kind, units)))
    monkeypatch.setattr(cost_control, "settle", lambda *a, **kw: None)
    script = "你好，欢迎来到我的故事。"
    out = client.talking_head(str(source), script, tmp_path / "out.mp4", model="wan2.6-i2v")
    assert out.read_bytes() == b"offline-video"
    assert [request[0] for request in requests] == ["POST", "GET"]
    assert requests[0][1] == "https://gateway.example.test/ai/v1/videos"
    payload = requests[0][2]
    assert payload["duration"] == 10 and payload["resolution"] == "720p"
    assert payload["model"] == "wan2.6-i2v"
    assert script in payload["prompt"]
    assert payload["extra"] == {"prompt_extend": True, "shot_type": "single"}
    assert set(payload) == {"model", "prompt", "duration", "resolution", "frame_images", "extra"}
    media = payload["frame_images"][0]
    assert media["frame_type"] == "first_frame"
    assert base64.b64decode(media["image_url"]["url"].split(",", 1)[1]) == source.read_bytes()
    assert reservations == [("wan2.6-i2v", "video", {"seconds": 10.0, "resolution": "720P"})]


@pytest.mark.parametrize("script,model,duration,code", [
    (" ", "wan2.6-i2v", 10, "VALIDATION_ERROR"),
    ("字" * 41, "wan2.6-i2v", 10, "TALKING_SCRIPT_TOO_LONG"),
    ("你好", "wan2.7-i2v", 10, "VIDEO_MODEL_UNSUPPORTED"),
    ("你好", "wan2.6-i2v", 5, "TALKING_DURATION_UNSUPPORTED"),
])
def test_invalid_talking_request_never_submits(client, monkeypatch, tmp_path, script, model, duration, code):
    monkeypatch.setattr(httpx, "request", lambda *a, **kw: pytest.fail("Invalid input must not reach paid HTTP"))
    with pytest.raises(AppError) as caught:
        client.talking_head("unused.png", script, tmp_path / "out.mp4", model=model, duration=duration)
    assert caught.value.code == code


@pytest.mark.parametrize("failure", ["timeout", "5xx"])
def test_unknown_talking_submit_never_repeats(client, monkeypatch, tmp_path, failure):
    source = tmp_path / "image.png"
    source.write_bytes(b"offline-image")
    requests = []

    def request(method, url, **kwargs):
        requests.append(method)
        if failure == "timeout":
            raise httpx.ReadTimeout("offline simulated failure")
        return httpx.Response(503)

    monkeypatch.setattr(httpx, "request", request)
    with pytest.raises(AppError) as caught:
        client.talking_head(str(source), "你好", tmp_path / "out.mp4", model="wan2.6-i2v")
    assert caught.value.code == "VIDEO_SUBMIT_UNCONFIRMED"
    assert requests == ["POST"]


def test_cancelled_talking_job_is_terminal(client, monkeypatch, tmp_path):
    source = tmp_path / "image.png"
    source.write_bytes(b"offline-image")
    requests = []

    def request(method, url, **kwargs):
        requests.append(method)
        return httpx.Response(200, json={"id": "cancelled-1", "status": "cancelled"})

    monkeypatch.setattr(httpx, "request", request)
    with pytest.raises(AppError) as caught:
        client.talking_head(str(source), "你好", tmp_path / "out.mp4", model="wan2.6-i2v")
    assert caught.value.code == "VIDEO_TASK_FAILED"
    assert requests == ["POST"]
