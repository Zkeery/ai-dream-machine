"""Per-request model selection contracts; all HTTP is replaced, no paid calls."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from PIL import Image
from pydantic import BaseModel

from app.core import config
from app.core.errors import AppError
from app.models.image_client import ImageClient
from app.models.llm_client import LLMClient
from app.models.video_client import VideoClient


@pytest.fixture(autouse=True)
def offline_clients(monkeypatch):
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "offline-test-key")
    monkeypatch.setattr(config.settings, "aihubmix_base", "https://gateway.example.test")
    monkeypatch.setattr(config.settings, "llm_model", "qwen3.5-plus")
    monkeypatch.setattr(config.settings, "image_t2i_model", "qwen-image-2.0")
    monkeypatch.setattr(config.settings, "video_first_frame_model", "wan2.7-i2v")
    monkeypatch.setattr(config.settings, "video_start_end_model", "wan2.7-i2v")
    monkeypatch.setattr(config.settings, "video_reference_model", "wan2.7-r2v")
    monkeypatch.setattr(config.settings, "max_retries", 1)

    def unexpected(*args, **kwargs):
        pytest.fail("Unexpected HTTP: model-selection tests must stay offline")

    for method in ("post", "request", "get"):
        monkeypatch.setattr(httpx, method, unexpected)


def image_file(tmp_path, ratio="16:9", name="source.png"):
    width, height = {"16:9": (160, 90), "9:16": (90, 160), "1:1": (90, 90)}[ratio]
    path = tmp_path / name
    Image.new("RGB", (width, height), "navy").save(path)
    return path


def test_shared_llm_uses_each_request_model_without_mutating_default(monkeypatch):
    barrier = Barrier(2)
    seen = {}

    def post(url, **kwargs):
        payload = kwargs["json"]
        user = payload["messages"][1]["content"]
        if user != "default":
            barrier.wait(timeout=5)
        seen[user] = payload["model"]
        assert url.endswith("/v1/chat/completions")
        assert payload["temperature"] == 0.7
        return httpx.Response(200, json={"choices": [{"message": {"content": user}}]})

    monkeypatch.setattr(httpx, "post", post)
    client = LLMClient()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.generate, "system", "plus", model="qwen3.5-plus")
        second = pool.submit(client.generate, "system", "flash", model="qwen3.5-flash")
        assert first.result() == "plus"
        assert second.result() == "flash"
    assert client.generate("system", "default") == "default"
    assert seen == {"plus": "qwen3.5-plus", "flash": "qwen3.5-flash", "default": "qwen3.5-plus"}
    assert client.model == config.settings.llm_model == "qwen3.5-plus"


def test_structured_output_retry_preserves_selected_model(monkeypatch):
    class Answer(BaseModel):
        title: str

    seen = []

    def post(url, **kwargs):
        seen.append(kwargs["json"]["model"])
        content = "not JSON" if len(seen) == 1 else '{"title":"重试成功"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(httpx, "post", post)
    client = LLMClient()
    result = client.generate_json("system", "user", Answer, model="qwen3.5-flash")
    assert result.title == "重试成功"
    assert seen == ["qwen3.5-flash", "qwen3.5-flash"]
    assert client.model == "qwen3.5-plus"


def test_structured_output_retry_receives_field_feedback(monkeypatch):
    from app.services.agent_runtime import AgentPlan
    seen = []

    def post(url, **kwargs):
        import json
        seen.append(kwargs["json"])
        # The second response repairs the field identified in the retry prompt.
        if len(seen) == 1:
            content = {"summary": "计划", "tasks": [{"id": "t1", "role": "unknown", "objective": "角色图"}]}
        else:
            assert "tasks.0.role" in seen[-1]["messages"][0]["content"]
            assert "literal_error" in seen[-1]["messages"][0]["content"]
            content = {"summary": "计划", "tasks": [{"id": "t1", "role": "designer", "objective": "角色图"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})

    monkeypatch.setattr(httpx, "post", post)
    result = LLMClient().generate_json("system", "user", AgentPlan)
    assert result.tasks[0].role == "designer"
    assert len(seen) == 2
    assert seen[0]["messages"][1] == seen[1]["messages"][1]


def test_exhausted_structured_output_preserves_safe_validation_details(monkeypatch):
    from app.services.agent_runtime import AgentPlan

    def post(url, **kwargs):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"summary":"plan","tasks":[]}'}}]})

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(AppError) as exc:
        LLMClient().generate_json("system", "user", AgentPlan)
    assert exc.value.code == "MODEL_OUTPUT_INVALID"
    assert exc.value.details["schema"] == "AgentPlan"
    assert exc.value.details["validation_errors"][0]["field"] == "tasks"
    assert "input" not in exc.value.details["validation_errors"][0]


@pytest.mark.parametrize("model", ["qwen3.5-plus", "qwen3.5-flash"])
def test_agent_control_has_bounded_output_without_changing_creative_calls(monkeypatch, model):
    from app.services.agent_runtime import CONTROL_DECISION
    seen, reservations = [], []
    from app.services import cost_control

    def post(url, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(cost_control, "reserve", lambda call_id, model, kind, units, **kwargs: reservations.append(units))
    monkeypatch.setattr(cost_control, "settle", lambda *args, **kwargs: None)
    client = LLMClient()
    token = CONTROL_DECISION.set(True)
    try:
        assert client.generate("system", "decision", model=model) == "ok"
    finally:
        CONTROL_DECISION.reset(token)
    assert client.generate("system", "creative", model=model) == "ok"
    assert seen[0]["enable_thinking"] is False
    assert seen[0]["max_tokens"] <= 2048
    assert reservations[0]["output_tokens"] == seen[0]["max_tokens"]
    assert seen[0]["temperature"] == 0.2
    assert "enable_thinking" not in seen[1]
    assert seen[1]["max_tokens"] == config.settings.llm_max_output_tokens
    assert seen[1]["temperature"] == 0.7


def test_agent_timeout_identifies_decision_and_does_not_repeat_request(monkeypatch):
    from app.services.agent_runtime import CONTROL_DECISION
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        raise httpx.ReadTimeout("offline timeout")

    monkeypatch.setattr(httpx, "post", post)
    token = CONTROL_DECISION.set(True)
    try:
        with pytest.raises(AppError) as exc:
            LLMClient().generate("system", "user")
        assert "Agent 决策请求超时" in exc.value.message
        assert exc.value.code == "MODEL_REQUEST_INTERRUPTED"
        assert len(calls) == 1
    finally:
        CONTROL_DECISION.reset(token)


@pytest.mark.parametrize("reference", [False, True])
def test_image_pro_and_default_reach_distinct_http_payloads(monkeypatch, tmp_path, reference):
    seen = []
    source = image_file(tmp_path)

    def post(url, **kwargs):
        payload = kwargs["json"]
        seen.append(payload)
        assert payload["size"] == "1920x1080"
        if reference:
            assert url.endswith("/ai/v1/images/generations")
            assert payload["async"] is False
            assert len(payload["images"]) == 1
            assert payload["images"][0].startswith("data:image/png;base64,")
            body = {"status": "completed", "output": [{"content_url": "/image.png"}]}
        else:
            assert url.endswith("/v1/images/generations")
            body = {"data": [{"url": "/image.png"}]}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, content=b"offline-image"))
    client = ImageClient()
    for index, model in enumerate(("qwen-image-2.0-pro", None)):
        out = tmp_path / f"image-{index}.png"
        if reference:
            result = client.image_to_image(source, "prompt", out, model=model, size="1920x1080")
        else:
            result = client.text_to_image("prompt", out, model=model, size="1920x1080")
        assert result.read_bytes() == b"offline-image"
    assert [payload["model"] for payload in seen] == ["qwen-image-2.0-pro", "qwen-image-2.0"]
    assert config.settings.image_t2i_model == "qwen-image-2.0"


def video_http(monkeypatch):
    requests = []

    def request(method, url, **kwargs):
        requests.append((method, url, kwargs.get("json")))
        if method == "POST":
            return httpx.Response(200, json={"id": "offline-video", "status": "in_progress"})
        return httpx.Response(200, json={
            "status": "completed", "output": [{"content_url": "/video.mp4"}], "url": "/video.mp4",
        })

    monkeypatch.setattr(httpx, "request", request)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, content=b"offline-video"))
    return requests


@pytest.mark.parametrize("ratio", ["16:9", "9:16", "1:1"])
@pytest.mark.parametrize("resolution", ["720P", "1080P"])
def test_wan26_first_frame_uses_only_reviewed_native_fields(monkeypatch, tmp_path, ratio, resolution):
    source = image_file(tmp_path, ratio)
    seen = video_http(monkeypatch)
    client = VideoClient()
    client.poll_interval = 0
    result = client.image_to_video(str(source), "镜头推进", tmp_path / "out.mp4",
                                   video_ratio=ratio, resolution=resolution, model="wan2.6-i2v")
    assert result.read_bytes() == b"offline-video"
    method, url, payload = seen[0]
    assert method == "POST" and url.endswith("/ai/v1/videos")
    assert set(payload) == {"model", "prompt", "duration", "resolution", "frame_images", "extra"}
    assert payload["extra"] == {"prompt_extend": True, "shot_type": "single"}
    assert payload["model"] == "wan2.6-i2v"
    assert payload["duration"] == 5 and payload["resolution"] == resolution.lower()
    assert len(payload["frame_images"]) == 1
    assert payload["frame_images"][0]["frame_type"] == "first_frame"
    assert payload["frame_images"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert seen[1][:2] == ("GET", "https://gateway.example.test/ai/v1/videos/offline-video")
    assert config.settings.video_first_frame_model == "wan2.7-i2v"


@pytest.mark.parametrize("mode,model", [
    ("first_frame", "wan2.7-r2v"),
    ("first_frame", "unsupported-video"),
    ("start_end", "wan2.6-i2v"),
    ("start_end", "wan2.7-r2v"),
    ("reference", "wan2.6-i2v"),
    ("reference", "wan2.7-i2v"),
])
@pytest.mark.parametrize("override", [True, False])
def test_incompatible_video_model_fails_before_http(monkeypatch, tmp_path, mode, model, override):
    source = image_file(tmp_path)
    if not override:
        monkeypatch.setattr(config.settings, f"video_{mode}_model", model)
    kwargs = {"end_image_path": str(source)} if mode == "start_end" else {}
    with pytest.raises(AppError) as error:
        VideoClient().image_to_video(str(source), "prompt", tmp_path / "out.mp4", mode,
                                      model=model if override else None, **kwargs)
    assert error.value.code == "VIDEO_MODEL_UNSUPPORTED"
    assert not (tmp_path / "out.mp4").exists()


def test_wan26_no_ratio_uses_native_and_start_end_uses_own_default(monkeypatch, tmp_path):
    monkeypatch.setattr(config.settings, "video_first_frame_model", "wan2.6-i2v")
    source = image_file(tmp_path)
    seen = video_http(monkeypatch)
    client = VideoClient()
    client.poll_interval = 0
    client.image_to_video(str(source), "首帧", tmp_path / "first.mp4")
    client.image_to_video(str(source), "首尾", tmp_path / "ends.mp4", "start_end",
                          end_image_path=str(source))
    posts = [(url, payload) for method, url, payload in seen if method == "POST"]
    assert [payload["model"] for _, payload in posts] == ["wan2.6-i2v", "wan2.7-i2v"]
    assert all(url.endswith("/ai/v1/videos") for url, _ in posts)
    assert [frame["frame_type"] for frame in posts[1][1]["frame_images"]] == ["first_frame", "last_frame"]


def test_original_first_frame_legacy_call_remains_compatible(monkeypatch, tmp_path):
    source = image_file(tmp_path)
    seen = video_http(monkeypatch)
    client = VideoClient()
    client.poll_interval = 0
    result = client.image_to_video(str(source), "legacy", tmp_path / "legacy.mp4")
    assert result.read_bytes() == b"offline-video"
    assert seen[0][1] == "https://gateway.example.test/v1/videos"
    payload = seen[0][2]
    assert payload["model"] == "wan2.7-i2v"
    assert set(payload) == {"model", "prompt", "seconds", "size", "input_reference"}
    assert payload["seconds"] == "5" and payload["size"] == "1280x720"
    assert seen[1][1] == "https://gateway.example.test/v1/videos/offline-video"
