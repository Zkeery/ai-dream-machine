"""Talking mode, account-bound media and paid-task continuation contracts, offline."""
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from app.api import tasks
from app.api.deps import get_current_user
from app.core import config
from app.core.errors import AppError, register_exception_handlers
from app.models.video_client import VideoClient
from app.schemas.task import talking_input
from app.services import auth, db, ffmpeg_util, model_catalog, prompts, task_store
from app.services.short_pipelines import ShortPipelines


async def noop(*args):
    pass


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


class NoTTS:
    async def synthesize(self, *args, **kwargs):
        raise AssertionError("嘴型模式不能调用独立配音")


@pytest.fixture
def api(data_dirs, monkeypatch):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(tasks.router, prefix="/api")
    identity = {"owner": "alice"}
    app.dependency_overrides[get_current_user] = lambda: identity["owner"]
    source = config.UPLOAD_DIR / "person.png"
    Image.new("RGB", (1280, 720), "navy").save(source)
    auth.record_upload(source.name, "alice", "人物.png")
    monkeypatch.setattr(config.settings, "video_speech_model", "wan2.6-i2v")
    monkeypatch.setattr(config.settings, "public_video_speech_models", frozenset({"wan2.6-i2v"}))
    return app, identity, source


@pytest.fixture
def native_media(monkeypatch):
    async def verify(path, *, expected_duration):
        assert Path(path).read_bytes() == b"native-video-with-speech"
        assert expected_duration == 10
        return {"duration": 10.02}

    async def extract(path, target):
        target.write_bytes(b"native-speech")
        return target

    async def trim(path, target, *, duration):
        return {"path": path, "duration": duration, "trimmed_tail": 0.0}

    async def no_static(*args, **kwargs):
        raise AssertionError("嘴型失败也不能回退为静态合成")

    monkeypatch.setattr(ffmpeg_util, "verify_talking_video", verify)
    monkeypatch.setattr(ffmpeg_util, "extract_audio", extract)
    monkeypatch.setattr(ffmpeg_util, "trim_talking_silence", trim)
    monkeypatch.setattr(ffmpeg_util, "image_audio_to_video", no_static)


@pytest.mark.parametrize("scope", [{"task_type": "talking_head"}, {"task_type": "motion_transfer"},
                                   {"task_type": "literary_video"}, {"project_type": "story"},
                                   {"project_type": "comic"}])
def test_speech_model_is_rejected_outside_explicit_lip_sync(scope):
    with pytest.raises(AppError) as error:
        model_catalog.validate_selection({"video_speech": "wan2.6-i2v"}, **scope)
    assert error.value.code == "MODEL_NOT_APPLICABLE"


def test_lip_sync_catalog_and_legacy_usage():
    selected = {"video_speech": "wan2.6-i2v"}
    assert model_catalog.validate_selection(selected, task_type="talking_head", talking_mode="lip_sync") == selected
    assert model_catalog.task_usage("talking_head", {})["models"] == {}
    assert model_catalog.task_usage("talking_head", {"talking_mode": "lip_sync"})["models"] == selected
    assert model_catalog.public_catalog()["groups"]["video_speech"]["default"] == "wan2.6-i2v"


def test_script_limit_counts_non_whitespace_without_silent_truncation():
    text = "你 好。\n" * 13
    assert talking_input({"talking_mode": "lip_sync", "script": text}) == ("lip_sync", text.strip())
    with pytest.raises(AppError) as error:
        talking_input({"talking_mode": "lip_sync", "script": "你" * 41})
    assert error.value.code == "TALKING_SCRIPT_TOO_LONG"
    assert talking_input({"script": "你" * 100})[0] == "static"


@pytest.mark.asyncio
async def test_lip_sync_uses_native_speech_and_preserves_video(data_dirs, native_media):
    calls = []

    class NativeVideo:
        def talking_head(self, image, script, target, **kwargs):
            calls.append((image, script, kwargs))
            target.write_bytes(b"native-video-with-speech")
            return target

    result = await ShortPipelines(video=NativeVideo(), tts=NoTTS()).run(
        "native-task", "talking_head", {"person_image": "person.png", "script": "你好，欢迎来到我的世界。",
                                        "talking_mode": "lip_sync"}, noop)
    assert calls == [("person.png", "你好，欢迎来到我的世界。", {"model": "wan2.6-i2v", "duration": 10})]
    assert result["talking_mode"] == "lip_sync" and result["audio_source"] == "video_model"
    assert result["model_usage"]["models"] == {"video_speech": "wan2.6-i2v"}
    assert Path(result["final_video"]).read_bytes() == b"native-video-with-speech"
    assert Path(result["audio"]).read_bytes() == b"native-speech"


@pytest.mark.asyncio
async def test_api_rejects_invalid_mode_and_long_script_before_claim(api):
    app, _, source = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for fields, code in [({"talking_mode": "fake"}, "TALKING_MODE_INVALID"),
                             ({"talking_mode": "lip_sync", "script": "你" * 41}, "TALKING_SCRIPT_TOO_LONG"),
                             ({"model_selection": {"video_speech": "wan2.6-i2v"}}, "MODEL_NOT_APPLICABLE")]:
            response = await client.post("/api/tasks", json={"type": "talking_head", "input": {
                "person_image": source.name, "script": "你好", **fields}})
            assert response.status_code == 422 and response.json()["error"]["code"] == code
        with db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM executions").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_static_failure_preserves_audio_with_account_protection(api, monkeypatch):
    app, identity, source = api

    class Voice:
        async def synthesize(self, text, target):
            target.write_bytes(b"finished-voice")
            return target

    async def fail_compose(*args):
        raise AppError("FFMPEG_FAILED", "合成失败", 422)

    monkeypatch.setattr(ffmpeg_util, "image_audio_to_video", fail_compose)
    monkeypatch.setattr(tasks, "get_pipelines", lambda: ShortPipelines(tts=Voice()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/tasks", json={"type": "talking_head", "input": {
            "person_image": source.name, "script": "你好"}})
        tid = created.json()["task_id"]
        assert events(await client.get(f"/api/tasks/{tid}/stream"))[-1]["type"] == "error"
        meta = task_store.load_task(tid)
        assert meta.status == "failed" and meta.result["talking_mode"] == "static"
        assert "final_video" not in meta.result and meta.input["script"] == "你好"
        response = await client.get(f"/api/tasks/{tid}/audio")
        assert response.content == b"finished-voice" and response.headers["content-type"] == "audio/mpeg"
        identity["owner"] = "bob"
        assert (await client.get(f"/api/tasks/{tid}/audio")).status_code == 404
        identity["owner"] = "alice"
        meta.result["audio"] = str(source)
        task_store.save_task(meta)
        assert (await client.get(f"/api/tasks/{tid}/audio")).status_code == 404


@pytest.mark.asyncio
async def test_motion_api_accepts_image_and_description_and_checks_legacy_video(api, monkeypatch):
    app, identity, source = api
    calls = []

    class Video:
        def image_to_video(self, image, prompt, target, mode, **kwargs):
            calls.append((image, prompt, mode))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"motion-video")
            return target

    monkeypatch.setattr(tasks, "get_pipelines", lambda: ShortPipelines(video=Video()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/tasks", json={"type": "motion_transfer", "input": {
            "character_image": source.name, "prompt": "向镜头挥手"}})
        assert created.status_code == 200
        tid = created.json()["task_id"]
        assert events(await client.get(f"/api/tasks/{tid}/stream"))[-1]["type"] == "done"
        assert calls == [(str(source), prompts.compose_shot_prompt("向镜头挥手"), "reference")]
        assert calls[0][1].startswith("向镜头挥手\n避免出现：")
        legacy = config.UPLOAD_DIR / "legacy.mp4"
        legacy.write_bytes(b"unused-video")
        auth.record_upload(legacy.name, "bob", "历史视频.mp4")
        request = {"type": "motion_transfer", "input": {
            "character_image": source.name, "prompt": "挥手", "motion_video": legacy.name}}
        assert (await client.post("/api/tasks", json=request)).status_code == 404
        assert legacy.read_bytes() == b"unused-video"
        own_legacy = config.UPLOAD_DIR / "own-legacy.mp4"
        own_legacy.write_bytes(b"retained-legacy-video")
        auth.record_upload(own_legacy.name, "alice", "旧素材.mp4")
        request["input"]["motion_video"] = own_legacy.name
        resumed = await client.post("/api/tasks", json=request)
        assert resumed.status_code == 200
        legacy_tid = resumed.json()["task_id"]
        assert events(await client.get(f"/api/tasks/{legacy_tid}/stream"))[-1]["type"] == "done"
        assert task_store.load_task(legacy_tid).input["motion_video"] == str(own_legacy)
        assert own_legacy.read_bytes() == b"retained-legacy-video"
        identity["owner"] = "bob"
        assert (await client.post("/api/tasks", json={"type": "motion_transfer", "input": {
            "character_image": source.name, "prompt": "挥手"}})).status_code == 404


@pytest.mark.asyncio
async def test_native_video_resume_reuses_provider_job_budget_and_audio(api, native_media, monkeypatch):
    app, identity, source = api
    state = {"posts": 0, "polls": 0, "fail": True}
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "offline-test")
    monkeypatch.setattr(config.settings, "aihubmix_base", "https://gateway.example.test")
    monkeypatch.setattr(config.settings, "max_retries", 0)
    monkeypatch.setattr(config.settings, "model_cost_rates_json", json.dumps({
        "wan2.6-i2v": {"kind": "video", "video_second_cny": {"720P": 0.7232}, "source": "fixture"}}))

    def provider(request):
        if request.method == "POST":
            state["posts"] += 1
            return httpx.Response(200, json={"id": "remote-1", "status": "pending"})
        if request.url.path.endswith("/remote-1"):
            state["polls"] += 1
            if state["fail"]:
                raise httpx.ReadTimeout("temporary query failure", request=request)
            return httpx.Response(200, json={"id": "remote-1", "status": "completed",
                                            "output": [{"content_url": "/native.mp4"}]})
        return httpx.Response(200, content=b"native-video-with-speech")

    with httpx.Client(transport=httpx.MockTransport(provider)) as remote:
        monkeypatch.setattr(httpx, "request", remote.request)
        monkeypatch.setattr(httpx, "get", remote.get)
        video = VideoClient()
        video.poll_interval = 0
        monkeypatch.setattr(tasks, "get_pipelines", lambda: ShortPipelines(video=video, tts=NoTTS()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/tasks", json={"type": "talking_head", "input": {
                "person_image": source.name, "script": "你好，我正在说话。", "talking_mode": "lip_sync"}})
            tid = created.json()["task_id"]
            assert events(await client.get(f"/api/tasks/{tid}/stream"))[-1]["type"] == "error"
            assert task_store.load_task(tid).result is None
            status = (await client.get(f"/api/tasks/{tid}/recovery")).json()
            assert status["can_resume"], status
            changed = task_store.load_task(tid)
            changed.input["talking_mode"] = "static"
            task_store.save_task(changed)
            blocked = (await client.get(f"/api/tasks/{tid}/recovery")).json()
            assert not blocked["can_resume"] and blocked["reason_code"] == "RESUME_INPUT_CHANGED"
            assert (await client.post(f"/api/tasks/{tid}/resume", json={"execution_id": status["execution_id"]})).status_code == 409
            assert state["posts"] == 1
            changed.input["talking_mode"] = "lip_sync"
            task_store.save_task(changed)
            state["fail"] = False
            resumed = await client.post(f"/api/tasks/{tid}/resume", json={"execution_id": status["execution_id"]},
                                        headers={"Idempotency-Key": "one-resume"})
            assert resumed.status_code == 200
            assert events(await client.get(f"/api/tasks/{tid}/stream"))[-1]["type"] == "done"
            assert state["posts"] == 1 and state["polls"] == 4  # Three bounded failed GETs, then the original job resumes.
            meta = task_store.load_task(tid)
            assert meta.result["talking_mode"] == "lip_sync"
            assert meta.result["model_usage"]["models"] == {"video_speech": "wan2.6-i2v"}
            assert (await client.get(f"/api/tasks/{tid}/audio")).content == b"native-speech"
            with db.connect() as conn:
                rows = conn.execute("SELECT owner_id,units,status FROM model_cost_ledger").fetchall()
                assert len(rows) == 1 and rows[0]["owner_id"] == "alice" and rows[0]["status"] == "completed"
                assert json.loads(rows[0]["units"]) == {"seconds": 10.0, "resolution": "720P"}
            identity["owner"] = "bob"
            assert (await client.get(f"/api/tasks/{tid}/audio")).status_code == 404
            assert (await client.post(f"/api/tasks/{tid}/resume", json={"execution_id": status["execution_id"]})).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,code", [
    ({"streams": [{"codec_type": "video"}], "format": {"duration": "10"}}, "TALKING_AUDIO_MISSING"),
    ({"streams": [{"codec_type": "audio"}], "format": {"duration": "10"}}, "TALKING_AUDIO_MISSING"),
    ({"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], "format": {"duration": "5"}}, "TALKING_DURATION_INVALID"),
    ({"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], "format": {"duration": "nan"}}, "TALKING_DURATION_INVALID"),
])
async def test_native_result_requires_audio_and_expected_duration(monkeypatch, payload, code):
    async def probe(_):
        return payload
    monkeypatch.setattr(ffmpeg_util, "probe_media", probe)
    with pytest.raises(AppError) as error:
        await ffmpeg_util.verify_talking_video(Path("unused.mp4"))
    assert error.value.code == code
