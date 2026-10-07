"""Voice selection contracts only; does not call the online Edge TTS service."""
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.models import tts_client


@pytest.mark.asyncio
async def test_old_interface_and_per_line_voice_selection(monkeypatch, tmp_path):
    calls = []
    class FakeCommunicate:
        def __init__(self, text, voice, rate):
            calls.append({"text": text, "voice": voice, "rate": rate})
        async def save(self, filename):
            Path(filename).write_bytes(b"voice-contract-fixture")
    monkeypatch.setattr(tts_client.edge_tts, "Communicate", FakeCommunicate)
    client = tts_client.TTSClient()
    await client.synthesize("旧接口", tmp_path / "old.mp3")
    await client.synthesize("角色台词", tmp_path / "new.mp3", voice="zh-CN-XiaoxiaoNeural")
    assert calls[0]["voice"] == tts_client.DEFAULT_VOICE
    assert calls[1]["voice"] == "zh-CN-XiaoxiaoNeural"
    assert len(client.list_voices()) == 6
    # Returned dictionaries cannot mutate the public voice catalogue.
    client.list_voices()[0]["voice"] = "edited"
    assert client.list_voices()[0]["voice"] == "zh-CN-XiaoxiaoNeural"


@pytest.mark.asyncio
async def test_unknown_voice_rejected_before_network(tmp_path):
    with pytest.raises(AppError) as error:
        await tts_client.TTSClient().synthesize("台词", tmp_path / "bad.mp3", voice="fake-voice")
    assert error.value.code == "TTS_VOICE_INVALID"


@pytest.mark.asyncio
async def test_network_error_does_not_leak_text_and_removes_partial_file(monkeypatch, tmp_path):
    class Failure:
        def __init__(self, *args, **kwargs):
            pass
        async def save(self, filename):
            Path(filename).write_bytes(b"partial")
            raise RuntimeError("private dialogue should not escape")
    monkeypatch.setattr(tts_client.edge_tts, "Communicate", Failure)
    output = tmp_path / "failed.mp3"
    with pytest.raises(AppError) as error:
        await tts_client.TTSClient().synthesize("私人台词", output)
    assert error.value.code == "TTS_FAILED"
    assert "private" not in error.value.message
    assert not output.exists()
