"""真实隔离 SQLite 与 ASGI 合同；模型/FFmpeg 替身，零外部生成调用。"""
import asyncio
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.api import models, sessions
from app.api.deps import get_current_user, get_orchestrator
from app.core import config
from app.core.errors import AppError, register_exception_handlers
from app.schemas.models import ModelSelection
from app.schemas.session import SessionCreate
from app.schemas.task import TaskCreate
from app.services import db, execution_store, model_catalog, session_store, ffmpeg_util
from app.services.short_pipelines import ShortPipelines


async def noop(*args):
    pass


def test_catalog_intersects_admin_allowlist_and_hides_credentials(monkeypatch):
    monkeypatch.setattr(config.settings, "public_text_models", frozenset({"qwen3.5-flash", "unknown"}))
    data = model_catalog.public_catalog()
    assert data["groups"]["text"]["default"] is None
    assert [item["id"] for item in data["groups"]["text"]["options"]] == ["qwen3.5-flash"]
    assert set(data) == {"provider", "groups"}
    assert not any(key in str(data) for key in ("api_key", "baseURL", "content_review"))
    with pytest.raises(AppError) as error:
        model_catalog.resolve({}, ["text"])
    assert error.value.code == "MODEL_NOT_AVAILABLE"


@pytest.mark.parametrize("value", [{"provider": "evil"}, {"image": "unsupported"}, {"video_reference": "wan2.6-i2v"}])
def test_unknown_models_provider_and_incompatible_modes_fail(value):
    with pytest.raises(AppError):
        model_catalog.validate_selection(value)


def test_schema_and_workflow_scopes_reject_provider_override():
    with pytest.raises(ValidationError):
        SessionCreate(idea="猫", provider="evil")
    with pytest.raises(ValidationError):
        TaskCreate(type="literary_video", input={"model_selection": {"baseURL": "evil"}})
    with pytest.raises(AppError):
        model_catalog.validate_selection({"video_first_frame": "wan2.7-i2v"}, project_type="comic")
    with pytest.raises(AppError):
        model_catalog.validate_selection({"image": "qwen-image-2.0"}, task_type="talking_head")


@pytest.mark.asyncio
async def test_public_catalog_requires_login_and_patch_preserves_artifacts(orch):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(models.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.dependency_overrides[get_orchestrator] = lambda: orch
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/models")).status_code == 401
        app.dependency_overrides[get_current_user] = lambda: "owner"
        assert (await client.get("/api/models")).status_code == 200
        meta = orch.create(SessionCreate(idea="测试模型"), owner_id="owner")
        meta = await orch.execute_stage(meta.session_id, "script_generation", noop)
        old = (deepcopy(meta.artifacts), deepcopy(meta.artifact_versions), list(meta.stale_stages), list(meta.stages_completed))
        response = await client.patch(f"/api/sessions/{meta.session_id}/models", json={"model_selection": {"text": "qwen3.5-flash"}})
        assert response.status_code == 200
        assert response.json()["model_selection"] == {"text": "qwen3.5-flash"}
        saved = orch.get(meta.session_id)
        assert (saved.artifacts, saved.artifact_versions, saved.stale_stages, saved.stages_completed) == old
        assert saved.artifacts["script_generation"]["model_usage"]["models"]["text"] == "qwen3.5-plus"
        assert (await client.patch(f"/api/sessions/{meta.session_id}/models", json={"model_selection": {}, "provider": "evil"})).status_code == 422
        app.dependency_overrides[get_current_user] = lambda: "other"
        assert (await client.patch(f"/api/sessions/{meta.session_id}/models", json={"model_selection": {}})).status_code == 404


def test_patch_locks_pending_execution_and_idempotency_captures_actual_model(orch):
    meta = orch.create(SessionCreate(idea="测试模型"), owner_id="owner")
    first, _ = execution_store.claim_session(meta.session_id, "script_generation", "generate", {}, "request")
    assert execution_store.snapshot("session", meta.session_id)["model_usage"]["models"] == {"text": "qwen3.5-plus"}
    with pytest.raises(AppError) as error:
        session_store.update_models(meta.session_id, "owner", {"text": "qwen3.5-flash"})
    assert error.value.code == "SESSION_RUNNING"
    execution_store.finish(first, {"type": "done"}, "completed")
    meta = orch.get(meta.session_id)
    meta.status = "idle"
    session_store.touch(meta)
    session_store.update_models(meta.session_id, "owner", {"text": "qwen3.5-flash"})
    with pytest.raises(AppError) as error:
        execution_store.claim_session(meta.session_id, "script_generation", "generate", {}, "request")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_video_snapshot_uses_requested_mode_not_old_meta(orch):
    meta = orch.create(SessionCreate(idea="测试模型", model_selection={"video_first_frame": "wan2.6-i2v", "video_reference": "wan2.7-r2v"}), owner_id="owner")
    execution_store.claim_session(meta.session_id, "video_generation", "regenerate", {"video_generation_mode": "reference"})
    snapshot = execution_store.snapshot("session", meta.session_id)
    assert snapshot["model_usage"]["models"] == {"video_reference": "wan2.7-r2v"}


@pytest.mark.asyncio
async def test_two_projects_share_client_without_leaking_selected_text_model(orch, monkeypatch):
    first = orch.create(SessionCreate(idea="甲", model_selection={"text": "qwen3.5-flash"}), owner_id="a")
    await asyncio.sleep(0.002)
    second = orch.create(SessionCreate(idea="乙", model_selection={"text": "qwen3.5-plus"}), owner_id="b")
    seen = {}
    original = orch.llm.generate_json
    def generate(system, user, schema, *, model=None):
        seen[model] = user
        return original(system, user, schema)
    monkeypatch.setattr(orch.llm, "generate_json", generate)
    results = await asyncio.gather(orch.execute_stage(first.session_id, "script_generation", noop), orch.execute_stage(second.session_id, "script_generation", noop))
    assert set(seen) == {"qwen3.5-plus", "qwen3.5-flash"}
    for result in results:
        usage = result.execution_inputs[-1]["model_usage"]
        assert usage == result.artifacts["script_generation"]["model_usage"] == result.artifact_versions["script_generation"][-1]["model_usage"]
        assert usage["models"]["text"] == result.model_selection.text


@pytest.mark.asyncio
async def test_targeted_image_override_keeps_unselected_item_model(orch, monkeypatch):
    meta = orch.create(SessionCreate(idea="猫"), owner_id="owner")
    for stage in ("script_generation", "character_design", "storyboard", "reference_generation"):
        meta = await orch.execute_stage(meta.session_id, stage, noop)
    old = deepcopy(meta.artifacts["reference_generation"]["shots"][1])
    session_store.update_models(meta.session_id, "owner", {"image": "qwen-image-2.0-pro"})
    calls = []
    original = orch.image.image_to_image
    def generate(paths, prompt, output, **kwargs):
        calls.append(kwargs["model"])
        return original(paths, prompt, output, **kwargs)
    monkeypatch.setattr(orch.image, "image_to_image", generate)
    meta = await orch.intervene(meta.session_id, "reference_generation", {"target_ids": ["s1"]}, noop)
    assert calls == ["qwen-image-2.0-pro"]
    assert meta.artifacts["reference_generation"]["shots"][1] == old
    assert old["model_usage"]["models"]["image"] == "qwen-image-2.0"
    assert meta.artifacts["reference_generation"]["shots"][0]["model_usage"]["models"]["image"] == "qwen-image-2.0-pro"
    assert "reference_generation" not in meta.stale_stages
    original_path = meta.artifact_versions["reference_generation"][0]["artifact"]["shots"][0]["path"]
    meta = await orch.intervene(meta.session_id, "reference_generation", {"operation": "select", "selections": [{"collection": "shots", "id": "s1", "path": original_path}]}, noop)
    restored = meta.artifacts["reference_generation"]["shots"][0]
    assert restored["path"] == original_path
    assert restored["model_usage"]["models"]["image"] == "qwen-image-2.0"


@pytest.mark.asyncio
async def test_video_override_and_mode_switch_record_actual_models(orch, monkeypatch):
    meta = orch.create(SessionCreate(idea="猫", model_selection={"video_first_frame": "wan2.6-i2v", "video_reference": "wan2.7-r2v"}), owner_id="owner")
    for stage in ("script_generation", "character_design", "storyboard", "reference_generation"):
        meta = await orch.execute_stage(meta.session_id, stage, noop)
    seen = []
    original = orch.video.image_to_video
    def generate(path, prompt, output, mode, **kwargs):
        seen.append((mode, kwargs["model"]))
        return original(path, prompt, output, mode, **kwargs)
    monkeypatch.setattr(orch.video, "image_to_video", generate)
    meta = await orch.execute_stage(meta.session_id, "video_generation", noop)
    assert seen == [("first_frame", "wan2.6-i2v")] * 2
    assert meta.artifacts["video_generation"]["segments"][0]["model_usage"]["models"] == {"video_first_frame": "wan2.6-i2v"}
    meta = await orch.intervene(meta.session_id, "video_generation", {"video_generation_mode": "reference", "target_ids": ["s1"]}, noop)
    assert seen[-1] == ("reference", "wan2.7-r2v")
    assert meta.execution_inputs[-1]["model_usage"]["models"] == {"video_reference": "wan2.7-r2v"}


@pytest.mark.asyncio
async def test_task_api_persists_models_snapshot_and_rejects_invalid_input(orch, monkeypatch):
    from app.api import tasks
    from app.services import task_store
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(tasks.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "owner"
    class Pipeline:
        async def run(self, task_id, task_type, inputs, progress):
            return {"model_usage": model_catalog.task_usage(task_type, inputs)}
    monkeypatch.setattr(tasks, "get_pipelines", lambda: Pipeline())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = {"type": "literary_video", "input": {"text": "猫", "model_selection": {"text": "qwen3.5-flash", "image": "qwen-image-2.0-pro"}}}
        response = await client.post("/api/tasks", json=body, headers={"Idempotency-Key": "task"})
        assert response.status_code == 200
        task_id = response.json()["task_id"]
        await client.get(f"/api/tasks/{task_id}/stream")
        meta = task_store.load_task(task_id)
        assert meta.input["model_selection"] == body["input"]["model_selection"]
        assert meta.input["model_usage"] == meta.result["model_usage"] == execution_store.snapshot("task", task_id)["model_usage"]
        body["input"]["model_selection"]["text"] = "qwen3.5-plus"
        assert (await client.post("/api/tasks", json=body, headers={"Idempotency-Key": "task"})).status_code == 409
        assert (await client.post("/api/tasks", json={"type": "talking_head", "input": {"model_selection": {"text": "qwen3.5-plus"}}})).status_code == 422
        assert (await client.post("/api/tasks", json={"type": "literary_video", "input": {"text": "猫", "provider": "evil"}})).status_code == 422


def test_model_update_and_execution_claim_share_transaction_lock(orch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    meta = orch.create(SessionCreate(idea="并发选择"), owner_id="owner")
    barrier = Barrier(2)
    def claim():
        barrier.wait()
        return execution_store.claim_session(meta.session_id, "script_generation", "generate", {})
    def update():
        barrier.wait()
        try:
            session_store.update_models(meta.session_id, "owner", {"text": "qwen3.5-flash"})
            return "updated"
        except AppError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_future = pool.submit(claim)
        update_future = pool.submit(update)
        claim_future.result()
        outcome = update_future.result()
    usage = execution_store.snapshot("session", meta.session_id)["model_usage"]["models"]["text"]
    assert (outcome, usage) in {("updated", "qwen3.5-flash"), ("SESSION_RUNNING", "qwen3.5-plus")}


def test_legacy_sqlite_model_column_is_idempotent_and_keeps_artifacts(data_dirs):
    with db.connect() as connection:
        connection.execute("ALTER TABLE sessions DROP COLUMN model_selection")
        connection.execute("INSERT INTO sessions(session_id,idea,artifacts,created_at,updated_at) VALUES(?,?,?,?,?)", ("legacy", "旧故事", '{"script_generation":{"title":"旧标题"}}', 1, 1))
    db.init_db()
    db.init_db()
    meta = session_store.load_session("legacy")
    assert meta.model_selection.model_dump(exclude_none=True) == {}
    assert meta.artifacts["script_generation"]["title"] == "旧标题"


@pytest.mark.asyncio
async def test_quick_tools_use_only_applicable_models_and_actual_usage(orch, monkeypatch):
    async def media(*args):
        output = Path(args[-1]); output.parent.mkdir(parents=True, exist_ok=True); output.write_bytes(b"fake"); return output
    monkeypatch.setattr(ffmpeg_util, "image_audio_to_video", media)
    monkeypatch.setattr(ffmpeg_util, "concat_videos", media)
    monkeypatch.setattr(ffmpeg_util, "extract_first_frame", media)
    pipeline = ShortPipelines(llm=orch.llm, image=orch.image, video=orch.video, tts=orch.tts)
    calls = []
    original = orch.image.text_to_image
    def image(prompt, output, **kwargs):
        calls.append(kwargs["model"]); return original(prompt, output, **kwargs)
    monkeypatch.setattr(orch.image, "text_to_image", image)
    result = await pipeline.run("literary", "literary_video", {"text": "猫", "model_selection": {"text": "qwen3.5-flash", "image": "qwen-image-2.0-pro"}}, noop)
    assert calls == ["qwen-image-2.0-pro"]
    assert result["model_usage"]["models"] == {"text": "qwen3.5-flash", "image": "qwen-image-2.0-pro"}
    result = await pipeline.run("long", "literary_video", {"text": "一段无需再扩写的完整文字。" * 4}, noop)
    assert result["model_usage"]["models"] == {"image": "qwen-image-2.0"}
    with pytest.raises(AppError):
        await pipeline.run("bad", "talking_head", {"model_selection": {"image": "qwen-image-2.0"}}, noop)
