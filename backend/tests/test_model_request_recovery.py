"""Recovery contracts at the HTTP boundary, with no external requests or fees."""
import ast
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.core import config
from app.core.errors import AppError
from app.models.image_client import ImageClient
from app.models.llm_client import LLMClient
from app.models.video_client import VideoClient
from app.schemas.session import ScriptArtifact


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "offline-test-key")
    monkeypatch.setattr(config.settings, "aihubmix_base", "https://gateway.example.test")
    monkeypatch.setattr(config.settings, "max_retries", 2)
    monkeypatch.setattr("time.sleep", lambda _: None)
    clients = []
    seen = []

    def install(handler):
        def record(request):
            seen.append(request)
            return handler(request)

        client = httpx.Client(transport=httpx.MockTransport(record))
        clients.append(client)
        for method in ("post", "get", "request"):
            monkeypatch.setattr(httpx, method, getattr(client, method))
        return seen

    yield install
    for client in clients:
        client.close()


def source_image(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (160, 90), "navy").save(source)
    return source


@pytest.mark.parametrize("reference", [False, True])
@pytest.mark.parametrize("failure", ["timeout", "server_error", "invalid_body"])
def test_image_unknown_submission_is_never_repeated(transport, tmp_path, reference, failure):
    def handler(request):
        assert request.method == "POST"
        if failure == "timeout":
            raise httpx.ReadTimeout("uncertain response", request=request)
        return httpx.Response(503 if failure == "server_error" else 200, text="invalid")

    seen = transport(handler)
    client, out = ImageClient(), tmp_path / "result.png"
    with pytest.raises(AppError) as error:
        if reference:
            client.image_to_image(source_image(tmp_path), "prompt", out)
        else:
            client.text_to_image("prompt", out)
    assert len(seen) == 1
    assert error.value.code in {"IMAGE_REQUEST_INTERRUPTED", "IMAGE_RESPONSE_INVALID"}
    assert "未确认" in error.value.message
    assert not out.exists()


@pytest.mark.parametrize("error_type,code,label", [
    (httpx.ReadTimeout, "IMAGE_REQUEST_TIMEOUT", "图片请求超时"),
    (httpx.RemoteProtocolError, "IMAGE_RESPONSE_DISCONNECTED", "图片服务在返回结果前断开连接"),
    (httpx.ConnectError, "IMAGE_CONNECTION_FAILED", "图片服务连接失败"),
])
def test_image_transport_diagnosis_is_saved_without_replay_or_secrets(transport, tmp_path, monkeypatch, caplog, error_type, code, label):
    from app.services import provider_jobs
    diagnoses = []
    original = provider_jobs.failed

    def failed(job, error_code, **kwargs):
        diagnoses.append((error_code, kwargs))
        return original(job, error_code, **kwargs)

    monkeypatch.setattr(provider_jobs, "failed", failed)

    def handler(request):
        raise error_type("private diagnostic secret must not leak", request=request)

    seen = transport(handler)
    with pytest.raises(AppError) as exc:
        ImageClient().text_to_image("private prompt must not leak", tmp_path / "out.png")
    assert label in exc.value.message and "未确认" in exc.value.message
    assert diagnoses == [(code, {"uncertain": True})]
    assert len(seen) == 1
    assert error_type.__name__ in caplog.text
    assert "private diagnostic secret" not in caplog.text
    assert "private prompt" not in caplog.text


@pytest.mark.parametrize("reference", [False, True])
@pytest.mark.parametrize("permanent", [False, True])
def test_image_download_retries_same_url_without_new_generation(transport, tmp_path, reference, permanent):
    downloads = []

    def handler(request):
        if request.method == "POST":
            body = {"status": "completed", "output": [{"content_url": "/asset.png"}]} if reference else {"data": [{"url": "/asset.png"}]}
            return httpx.Response(200, json=body)
        downloads.append(str(request.url))
        if len(downloads) == 1:
            raise httpx.ReadTimeout("download only", request=request)
        if permanent or len(downloads) == 2:
            return httpx.Response(503)
        return httpx.Response(200, content=b"completed-image")

    seen = transport(handler)
    client, out = ImageClient(), tmp_path / "result.png"
    def generate():
        return client.image_to_image(source_image(tmp_path), "p", out) if reference else client.text_to_image("p", out)
    if permanent:
        with pytest.raises(AppError) as error:
            generate()
        assert error.value.code == "IMAGE_DOWNLOAD_FAILED"
        assert "已生成" in error.value.message and "未重新提交" in error.value.message
        assert not out.exists()
    else:
        assert generate().read_bytes() == b"completed-image"
    assert sum(request.method == "POST" for request in seen) == 1
    assert len(downloads) == 3 and len(set(downloads)) == 1


@pytest.mark.parametrize("mode", ["first_frame", "reference"])
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_video_unknown_submission_never_repeats(transport, tmp_path, mode, failure):
    def handler(request):
        assert request.method == "POST"
        if failure == "timeout":
            raise httpx.ReadTimeout("unknown result", request=request)
        return httpx.Response(503)
    seen = transport(handler)
    with pytest.raises(AppError) as error:
        VideoClient().image_to_video(str(source_image(tmp_path)), "p", tmp_path / "video.mp4", mode=mode)
    assert len(seen) == 1
    assert error.value.code == "VIDEO_SUBMIT_UNCONFIRMED"
    assert "停止自动重试" in error.value.message


@pytest.mark.parametrize("mode", ["first_frame", "reference"])
def test_video_poll_and_download_retry_without_new_submission(transport, tmp_path, mode):
    polls, downloads = [], []
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "job1", "status": "pending"})
        if request.url.path.endswith("job1"):
            polls.append(request)
            if len(polls) == 1:
                raise httpx.ReadTimeout("query only", request=request)
            return httpx.Response(200, json={"status": "completed", "url": "/asset.mp4", "output": [{"content_url": "/asset.mp4"}]})
        downloads.append(request)
        return httpx.Response(503) if len(downloads) == 1 else httpx.Response(200, content=b"completed-video")
    seen = transport(handler)
    client = VideoClient()
    client.poll_interval = 0
    result = client.image_to_video(str(source_image(tmp_path)), "p", tmp_path / "video.mp4", mode=mode)
    assert result.read_bytes() == b"completed-video"
    assert sum(request.method == "POST" for request in seen) == 1
    assert len(polls) == len(downloads) == 2


@pytest.mark.parametrize("failure", ["recoverable_503", "permanent_503", "timeout", "400"])
def test_text_retries_only_explicit_server_failures(transport, failure):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        if failure == "timeout":
            raise httpx.ReadTimeout("unknown text result", request=request)
        if failure == "400":
            return httpx.Response(400)
        if failure == "permanent_503" or len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    transport(handler)
    client = LLMClient()
    if failure == "recoverable_503":
        assert client.generate("system", "user", model="qwen3.5-flash") == "ok"
        expected_calls = 2
    else:
        with pytest.raises(AppError) as error:
            client.generate("system", "user", model="qwen3.5-flash")
        expected_calls = 3 if failure == "permanent_503" else 1
        assert error.value.code == {"permanent_503": "MODEL_RETRY_EXHAUSTED", "timeout": "MODEL_REQUEST_INTERRUPTED", "400": "MODEL_ERROR"}[failure]
    assert len(calls) == expected_calls
    assert all(payload["model"] == "qwen3.5-flash" for payload in calls)


def test_acceptance_llm_stub_matches_model_keyword_contract():
    # Extract only the class: importing the server script would change DATA_DIR.
    source = Path(__file__).resolve().parents[1] / "scripts" / "serve_comic_acceptance.py"
    tree = ast.parse(source.read_text())
    stub = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LocalLLM")
    records = []
    namespace = {"record": lambda *args, **kwargs: records.append(kwargs), "ScriptArtifact": ScriptArtifact}
    exec(compile(ast.Module(body=[stub], type_ignores=[]), str(source), "exec"), namespace)
    client = namespace["LocalLLM"]()
    assert client.generate("s", "u", model="qwen3.5-flash")
    assert isinstance(client.generate_json("s", "u", ScriptArtifact, model="qwen3.5-flash"), ScriptArtifact)
    assert all(row["model"] == "qwen3.5-flash" for row in records)
