"""Zero-cost mock acceptance: real socket guard plus actual local media."""
import asyncio
import json
import socket
import subprocess
import sys
import threading

import httpx
import pytest
from PIL import Image

from app.core import config, offline
from app.core.errors import AppError
from app.models.image_client import ImageClient
from app.models.llm_client import LLMClient
from app.models.tts_client import TTSClient
from app.models.video_client import VideoClient
from app.models.vlm_client import VLMClient
from app.schemas.session import ComicStoryboardArtifact, ScriptArtifact, StoryboardArtifact


@pytest.fixture(autouse=True)
def strict_offline(monkeypatch):
    assert offline.network_guard_status()["installed"]
    monkeypatch.setattr(config.settings, "mode_mock", True)
    offline.reset_mock_state()
    # Mock providers must never reserve paid units or reach HTTP/Edge TTS.
    from app.services import cost_control
    def forbidden(*args, **kwargs):
        pytest.fail("Mock provider entered paid/network HTTP path")
    monkeypatch.setattr(cost_control, "reserve", forbidden)
    monkeypatch.setattr(cost_control, "settle", forbidden)
    monkeypatch.setattr(httpx, "post", forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    monkeypatch.setattr(httpx, "request", forbidden)


@pytest.mark.parametrize("host", ["example.com", "203.0.113.1", "2001:db8::1"])
def test_dns_is_blocked_before_resolution(host):
    before = offline.network_guard_status()["blocked_attempts"]
    with pytest.raises(PermissionError, match="OFFLINE_NETWORK_BLOCKED"):
        socket.getaddrinfo(host, 443)
    assert offline.network_guard_status()["blocked_attempts"] == before + 1


@pytest.mark.parametrize("transport", ["tcp", "udp"])
def test_raw_socket_external_egress_is_blocked(transport):
    before = offline.network_guard_status()["blocked_attempts"]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if transport == "tcp" else socket.SOCK_DGRAM) as sock:
        with pytest.raises(PermissionError, match="OFFLINE_NETWORK_BLOCKED"):
            sock.connect(("203.0.113.1", 443)) if transport == "tcp" else sock.sendto(b"mock", ("203.0.113.1", 443))
    assert offline.network_guard_status()["blocked_attempts"] == before + 1


def test_localhost_remains_usable():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received = []
        def accept():
            conn, _ = server.accept()
            with conn:
                received.append(conn.recv(4))
                conn.sendall(b"mock")
        worker = threading.Thread(target=accept, daemon=True)
        worker.start()
        with socket.create_connection(("localhost", port), timeout=2) as client:
            client.sendall(b"test")
            assert client.recv(4) == b"mock"
        worker.join(timeout=2)
        assert received == [b"test"]


def test_mock_ignores_keys_and_does_not_initialize_edge_sdk():
    assert config.settings.aihubmix_api_key == ""
    assert "edge_tts" not in sys.modules
    assert LLMClient().api_key == ImageClient().api_key == VideoClient().api_key == VLMClient().api_key == ""


@pytest.mark.parametrize("schema", [ScriptArtifact, StoryboardArtifact, ComicStoryboardArtifact])
def test_structured_text_uses_current_contract(schema):
    result = LLMClient().generate_json("", "离线测试", schema)
    assert isinstance(result, schema)
    if schema is not ScriptArtifact:
        assert len(result.shots) == 3
    assert offline.mock_call_counts() == {"text": 1}


def test_mock_generate_text_and_visual_review(tmp_path):
    assert "MOCK" in LLMClient().generate("", "hello")
    image = ImageClient().text_to_image("测试", tmp_path / "test.png")
    with Image.open(image) as source:
        assert source.size == (1280, 720)
    copied = ImageClient().image_to_image(image, "测试", tmp_path / "second.png")
    assert copied.is_file()
    review = json.loads(VLMClient().review(str(image), '只返回JSON {"safe": true}'))
    assert review["mock"] is True
    assert offline.mock_call_counts() == {"text": 1, "image": 2, "vlm": 1}


@pytest.mark.parametrize("expected_person_count", [0, 1, 2])
def test_mock_visual_fixture_satisfies_strict_contract_without_claiming_vision(tmp_path, expected_person_count):
    from app.services.content_review import ContentReviewer, STRUCTURAL_FLAGS
    image = ImageClient().text_to_image("测试", tmp_path / "test.png")
    review = ContentReviewer().review_image(str(image), expected_person_count=expected_person_count)
    assert review["review_valid"] is True
    assert review["accepted"] is True
    assert review["person_count"] == expected_person_count
    assert all(review[flag] is False for flag in STRUCTURAL_FLAGS)
    raw = json.loads(review["raw_response"])
    assert raw["mock"] is True
    assert "不验证真实" in raw["reason"]
    assert offline.mock_call_counts() == {"image": 1, "vlm": 1}


def _probe(path):
    import shutil
    result = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)


def test_audio_and_video_are_valid_local_synthetic_media(tmp_path):
    image = ImageClient().text_to_image("测试", tmp_path / "test.png")
    audio = asyncio.run(TTSClient().synthesize("这是测试音轨。", tmp_path / "tone.mp3"))
    video = VideoClient().image_to_video(str(image), "测试", tmp_path / "pattern.mp4")
    assert any(stream["codec_type"] == "audio" for stream in _probe(audio)["streams"])
    data = _probe(video)
    assert {stream["codec_type"] for stream in data["streams"]} == {"video", "audio"}
    assert data["format"]["tags"]["comment"] == "MOCK offline test pattern"
    assert float(data["format"]["duration"]) >= 5
    assert offline.mock_call_counts() == {"image": 1, "tts": 1, "video": 1}
    assert "edge_tts" not in sys.modules


@pytest.mark.parametrize("failure", ["timeout", "disconnect"])
@pytest.mark.parametrize("kind", ["text", "image", "video", "tts", "vlm"])
def test_unknown_result_never_causes_second_mock_request(kind, failure, tmp_path):
    fixture = tmp_path / "fixture.png"
    Image.new("RGB", (64, 64)).save(fixture)
    offline.fail_next_mock_call(kind, failure)
    with pytest.raises(AppError) as error:
        if kind == "text":
            LLMClient().generate_json("", "", ScriptArtifact)
        elif kind == "image":
            ImageClient().text_to_image("", tmp_path / "out.png")
        elif kind == "video":
            VideoClient().image_to_video(str(fixture), "", tmp_path / "out.mp4")
        elif kind == "tts":
            asyncio.run(TTSClient().synthesize("测试", tmp_path / "out.mp3"))
        else:
            VLMClient().review(str(fixture), "测试")
    assert error.value.code == "MODEL_REQUEST_INTERRUPTED"
    assert error.value.details["result_state"] == "awaiting_reconciliation"
    assert offline.mock_call_counts() == {kind: 1}
    assert not list(tmp_path.glob("out.*"))


def test_mock_embedding_never_downloads_models():
    vectors = list(offline.MockEmbedding().embed(["测试资料", "测试资料"]))
    assert len(vectors) == 2 and vectors[0].shape == (512,)
    assert (vectors[0] == vectors[1]).all()


def test_unknown_mock_schema_fails_closed():
    from pydantic import BaseModel
    class Unsupported(BaseModel):
        value: str
    with pytest.raises(AppError, match="离线测试尚未配置结构"):
        LLMClient().generate_json("", "", Unsupported)
    assert offline.mock_call_counts() == {"text": 1}
