"""ASGI/SSE recovery acceptance against real persistent media clients, offline."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app.api import sessions, tasks
from app.api.deps import get_current_user, get_orchestrator
from app.core import config
from app.core.errors import AppError, register_exception_handlers
from app.models.image_client import ImageClient
from app.schemas.session import SessionCreate
from app.services import db, execution_store, session_store, task_store, ffmpeg_util
from app.services.short_pipelines import ShortPipelines


async def noop(*args):
    pass


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


@pytest.fixture
def api(orch, monkeypatch):
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "offline-test")
    monkeypatch.setattr(config.settings, "aihubmix_base", "https://gateway.example.test")
    monkeypatch.setattr(config.settings, "image_t2i_model", "qwen-image-2.0")
    monkeypatch.setattr(config.settings, "max_retries", 0)
    monkeypatch.setattr(config.settings, "model_cost_rates_json", json.dumps({
        "qwen-image-2.0": {"kind": "image", "image_cny": 0.1, "source": "test fixture"},
        "qwen-image-2.0-pro": {"kind": "image", "image_cny": 0.2, "source": "test fixture"},
    }))
    state = {"fail_download": True, "unknown": False, "posts": 0, "gets": 0, "fail_on": 1}
    def transport(request):
        if request.method == "POST":
            state["posts"] += 1
            if state.get("reject_post") == state["posts"]:
                return httpx.Response(state.get("reject_status", 402), json={"error": "balance"})
            if state["unknown"]:
                raise httpx.ReadTimeout("unknown submit", request=request)
            return httpx.Response(200, json={"data": [{"url": f'/asset{state["posts"]}.png'}]})
        state["gets"] += 1
        if state["fail_download"] and state["gets"] == state["fail_on"]:
            raise httpx.ReadTimeout("download interrupted", request=request)
        return httpx.Response(200, content=b"offline-image")
    http = httpx.Client(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(httpx, "post", http.post)
    monkeypatch.setattr(httpx, "get", http.get)
    orch.image = ImageClient()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(sessions.router, prefix="/api")
    app.include_router(tasks.router, prefix="/api")
    app.dependency_overrides[get_orchestrator] = lambda: orch
    app.dependency_overrides[get_current_user] = lambda: "owner"
    yield app, orch, state
    http.close()


async def session_ready(orch):
    meta = orch.create(SessionCreate(idea="一只猫站在雨夜街道"), owner_id="owner")
    await orch.execute_stage(meta.session_id, "script_generation", noop)
    orch.continue_session(meta.session_id)
    return meta.session_id


@pytest.mark.asyncio
async def test_agent_sse_resume_keeps_plan_brief_and_paid_job_sequence(api):
    from tests.test_agent_runtime import Decisions
    from app.services.agent_runtime import PlannerIntent
    app, orch, state = api
    sid = await session_ready(orch)
    meta = orch.get(sid)
    meta.orchestration_mode = "multi_agent"
    session_store.touch(meta)
    orch.llm = Decisions(role="designer")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(f"/api/sessions/{sid}/execute/character_design")
        assert events(first)[-1]["type"] == "error"
        status = (await client.get(f"/api/sessions/{sid}/recovery")).json()
        assert status["can_resume"], status
        state["fail_download"] = False
        resumed = await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": status["execution_id"]})
        result = events(resumed)[-1]
        assert result["type"] == "done", result
        assert state["posts"] == 2
        assert sum(schema is PlannerIntent for schema, _ in orch.llm.calls) == 1
        runs = result["session"]["agent_runs"]["character_design"]
        assert len(runs) == 1 and runs[0]["status"] == "completed"
        assert "output" not in runs[0]
        assert "observations" not in runs[0]["tasks"]["t1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_agent_validation_failure_and_legacy_checkpoint_resume_without_replanning(api, monkeypatch, legacy):
    from tests.test_agent_runtime import Decisions
    from app.services.agent_runtime import PlannerIntent
    app, orch, state = api
    sid = await session_ready(orch)
    meta = orch.get(sid)
    meta.orchestration_mode = "multi_agent"
    session_store.touch(meta)

    def invalid(*args, **kwargs):
        raise AppError("MODEL_OUTPUT_INVALID", "模型输出格式不合法，多次重试仍失败", 502)

    monkeypatch.setattr(orch.llm, "generate_json", invalid)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        failed = await client.post(f"/api/sessions/{sid}/execute/character_design")
        assert events(failed)[-1]["type"] == "error"
        if legacy:
            meta = orch.get(sid)
            run = meta.agent_runs["character_design"][-1]
            run.pop("decision_error")
            run.pop("legacy_invalid_acknowledged", None)
            run["pending_decision"] = "planner"
            session_store.touch(meta)
        status = (await client.get(f"/api/sessions/{sid}/recovery")).json()
        assert status["can_resume"], status
        assert not status["requires_reconciliation"] and status["jobs"] == []
        assert state["posts"] == 0
        state["fail_download"] = False
        orch.llm = Decisions(role="designer")
        resumed = await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": status["execution_id"]})
        assert events(resumed)[-1]["type"] == "done", events(resumed)[-1]
        assert state["posts"] == 2
        assert not any(schema is PlannerIntent for schema, _ in orch.llm.calls)
        assert len(orch.get(sid).agent_runs["character_design"]) == 1


@pytest.mark.asyncio
async def test_unknown_control_after_known_failure_is_not_retried_again(api, monkeypatch):
    app, orch, state = api
    sid = await session_ready(orch)
    meta = orch.get(sid)
    meta.orchestration_mode = "multi_agent"
    session_store.touch(meta)

    def invalid(*args, **kwargs):
        raise AppError("MODEL_OUTPUT_INVALID", "模型输出格式不合法，多次重试仍失败", 502)

    monkeypatch.setattr(orch.llm, "generate_json", invalid)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(f"/api/sessions/{sid}/execute/character_design")
        status = (await client.get(f"/api/sessions/{sid}/recovery")).json()
        assert status["can_resume"]

        def unknown(*args, **kwargs):
            raise AppError("MODEL_REQUEST_INTERRUPTED", "结果未知", 502)

        monkeypatch.setattr(orch.llm, "generate_json", unknown)
        failed = await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": status["execution_id"]})
        assert events(failed)[-1]["type"] == "error"
        status = (await client.get(f"/api/sessions/{sid}/recovery")).json()
        assert not status["can_resume"]
        assert status["reason_code"] == "AGENT_DECISION_UNCERTAIN"
        assert state["posts"] == 0


@pytest.mark.asyncio
async def test_sse_resume_completed_first_image_and_http_402_second_uses_new_attempt(api):
    app, orch, state = api
    state.update(fail_download=False, reject_post=2)
    sid = await session_ready(orch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(f"/api/sessions/{sid}/execute/character_design")
        assert events(first)[-1]["type"] == "error"
        status = (await client.get(f"/api/sessions/{sid}/recovery")).json()
        assert status["can_resume"], status
        assert [j["status"] for j in status["jobs"]] == ["completed", "rejected"]
        original = status["execution_id"]
        rejected_id = status["jobs"][1]["id"]
        resumed = await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": original}, headers={"Idempotency-Key": "retry-402"})
        assert resumed.status_code == 200 and events(resumed)[-1]["type"] == "done", events(resumed)
        # Completed paid media survives stage rollback; only the rejected
        # second image is submitted and downloaded on continuation.
        assert state["posts"] == 3 and state["gets"] == 2
        repeated = await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": original}, headers={"Idempotency-Key": "retry-402"})
        assert repeated.status_code == 200 and events(repeated)[-1]["type"] == "done"
        assert state["posts"] == 3
        with db.connect() as conn:
            rows = conn.execute("SELECT * FROM model_cost_ledger ORDER BY created_at").fetchall()
            assert len(rows) == 3 and [r["status"] for r in rows] == ["completed", "rejected", "completed"]
            assert rows[1]["call_id"] == rejected_id
            attempt = conn.execute("SELECT * FROM provider_job_attempts WHERE retry_of=?", (rejected_id,)).fetchone()
            assert attempt["job_id"] == rows[2]["call_id"] and attempt["attempt"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_on", [1, 2])
async def test_session_sse_resume_reuses_old_images_appends_missing_and_is_idempotent(api, fail_on):
    app, orch, state = api
    state["fail_on"] = fail_on
    sid = await session_ready(orch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(f"/api/sessions/{sid}/execute/character_design")
        assert events(first)[-1]["type"] == "error"
        status = (await client.get(f"/api/sessions/{sid}/recovery")).json()
        assert status["can_resume"], status
        original = status["execution_id"]
        from pathlib import Path
        with db.connect() as conn:
            completed = conn.execute("SELECT local_path FROM provider_jobs WHERE scope_execution_id=? AND status='completed'", (original,)).fetchall()
        assert all(Path(row[0]).is_file() for row in completed)
        state["fail_download"] = False
        resumed = await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": original}, headers={"Idempotency-Key": "resume-one"})
        assert resumed.status_code == 200
        assert events(resumed)[-1]["type"] == "done", events(resumed)
        assert state["posts"] == 2  # One character and one setting in total.
        repeated = await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": original}, headers={"Idempotency-Key": "resume-one"})
        assert repeated.status_code == 200
        assert events(repeated)[-1]["type"] == "done"
        assert state["posts"] == 2
        regenerated = await client.post(f"/api/sessions/{sid}/intervene", json={"stage": "character_design", "modifications": {"operation": "regenerate"}})
        assert events(regenerated)[-1]["type"] == "done"
        assert state["posts"] == 4  # Explicit regeneration is a new paid scope.


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["upstream", "model", "ratio"])
async def test_session_recovery_rejects_changed_business_input(api, change):
    app, orch, state = api
    sid = await session_ready(orch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        await client.post(f"/api/sessions/{sid}/execute/character_design")
        original = execution_store.snapshot("session", sid)["execution_id"]
        meta = orch.get(sid)
        if change == "upstream":
            meta.artifacts["script_generation"]["title"] = "edited title"
            session_store.touch(meta)
        elif change == "ratio":
            meta.video_ratio = "9:16"
            session_store.touch(meta)
        else:
            assert (await client.patch(f"/api/sessions/{sid}/models", json={"model_selection": {"image": "qwen-image-2.0-pro"}})).status_code == 200
        status = (await client.get(f"/api/sessions/{sid}/recovery")).json()
        assert not status["can_resume"] and status["reason_code"] == "RESUME_INPUT_CHANGED"
        result = await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": original})
        assert result.status_code == 409
        assert state["posts"] == 1


@pytest.mark.asyncio
async def test_recovery_owner_deleted_unknown_and_legacy_guards(api):
    app, orch, state = api
    sid = await session_ready(orch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        empty = (await client.get(f"/api/sessions/{sid}/recovery")).json()
        assert not empty["can_resume"] and empty["execution_id"] is None
        await client.post(f"/api/sessions/{sid}/execute/character_design")
        original = execution_store.snapshot("session", sid)["execution_id"]
        app.dependency_overrides[get_current_user] = lambda: "other"
        assert (await client.get(f"/api/sessions/{sid}/recovery")).status_code == 404
        assert (await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": original})).status_code == 404
        app.dependency_overrides[get_current_user] = lambda: "owner"
        with db.connect() as conn:
            conn.execute("DELETE FROM execution_recovery_inputs WHERE execution_id=?", (original,))
        legacy = (await client.get(f"/api/sessions/{sid}/recovery")).json()
        assert not legacy["can_resume"] and legacy["reason_code"] == "RESUME_INPUT_MISSING"
        assert (await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": original})).status_code == 409
        assert (await client.delete(f"/api/sessions/{sid}")).status_code == 204
        assert (await client.get(f"/api/sessions/{sid}/recovery")).status_code == 404
        assert (await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": original})).status_code == 404
        sid2 = await session_ready(orch)
        state["unknown"] = True
        await client.post(f"/api/sessions/{sid2}/execute/character_design")
        uncertain = (await client.get(f"/api/sessions/{sid2}/recovery")).json()
        assert uncertain["requires_reconciliation"] and not uncertain["can_resume"]
        assert (await client.post(f"/api/sessions/{sid2}/resume", json={"execution_id": uncertain["execution_id"]})).status_code == 409
        assert state["posts"] == 2


@pytest.mark.asyncio
async def test_regenerate_recovery_replays_original_target_modifications(api):
    app, orch, state = api
    sid = await session_ready(orch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        request = {"operation": "regenerate", "target_ids": ["c1"], "prompts": {"c1": "original custom prompt"}}
        response = await client.post(f"/api/sessions/{sid}/intervene", json={"stage": "character_design", "modifications": request})
        assert events(response)[-1]["type"] == "error"
        status = (await client.get(f"/api/sessions/{sid}/recovery")).json()
        assert status["can_resume"], status
        state["fail_download"] = False
        response = await client.post(f"/api/sessions/{sid}/resume", json={"execution_id": status["execution_id"]})
        assert events(response)[-1]["type"] == "done", events(response)
        assert state["posts"] == 1
        assert orch.get(sid).artifacts["character_design"]["characters"][0]["prompt"] == "original custom prompt"


@pytest.mark.asyncio
@pytest.mark.parametrize("short_text", [False, True])
async def test_task_resume_uses_original_inputs_and_existing_stream(api, monkeypatch, short_text):
    app, orch, state = api
    async def compose(*args):
        target = args[-1]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video")
        return target
    monkeypatch.setattr(ffmpeg_util, "image_audio_to_video", compose)
    monkeypatch.setattr(ffmpeg_util, "concat_videos", compose)
    llm_calls = []
    class TextModel:
        def generate(self, system, user, **kwargs):
            llm_calls.append(kwargs["model"])
            return "雨夜的小猫在街道旁边等候久未回家的主人，它的眼睛望着远方，雨水滑过屋檐。"
    pipelines = ShortPipelines(llm=TextModel(), image=orch.image, tts=orch.tts)
    monkeypatch.setattr(tasks, "get_pipelines", lambda: pipelines)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/tasks", json={"type": "literary_video", "input": {"text": "雨夜小猫" if short_text else "雨夜的小猫在街道旁边等候久未回家的主人，它的眼睛望着远方，雨水滑过屋檐。"}})
        tid = response.json()["task_id"]
        first = await client.get(f"/api/tasks/{tid}/stream")
        assert events(first)[-1]["type"] == "error"
        status = (await client.get(f"/api/tasks/{tid}/recovery")).json()
        assert status["can_resume"], status
        state["fail_download"] = False
        response = await client.post(f"/api/tasks/{tid}/resume", json={"execution_id": status["execution_id"]})
        assert response.json() == {"task_id": tid}
        final = await client.get(f"/api/tasks/{tid}/stream")
        assert events(final)[-1]["type"] == "done", events(final)
        assert state["posts"] == 1
        assert len(llm_calls) == int(short_text)
        app.dependency_overrides[get_current_user] = lambda: "other"
        assert (await client.get(f"/api/tasks/{tid}/recovery")).status_code == 404
        assert (await client.post(f"/api/tasks/{tid}/resume", json={"execution_id": status["execution_id"]})).status_code == 404


@pytest.mark.asyncio
async def test_task_changed_original_input_blocks_resume(api, monkeypatch):
    app, orch, state = api
    monkeypatch.setattr(tasks, "get_pipelines", lambda: ShortPipelines(llm=orch.llm, image=orch.image, tts=orch.tts))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/tasks", json={"type": "literary_video", "input": {"text": "雨夜的小猫在街道旁边等候久未回家的主人，它的眼睛望着远方，雨水滑过屋檐。"}})
        tid = created.json()["task_id"]
        await client.get(f"/api/tasks/{tid}/stream")
        original = execution_store.snapshot("task", tid)["execution_id"]
        meta = task_store.load_task(tid)
        meta.input["style"] = "changed"
        task_store.save_task(meta)
        status = (await client.get(f"/api/tasks/{tid}/recovery")).json()
        assert not status["can_resume"] and status["reason_code"] == "RESUME_INPUT_CHANGED"
        assert (await client.post(f"/api/tasks/{tid}/resume", json={"execution_id": original})).status_code == 409
        assert state["posts"] == 1


@pytest.mark.asyncio
async def test_tts_retry_reuses_paid_expansion_and_image_but_synthesizes_again(api, monkeypatch):
    app, orch, state = api
    state["fail_download"] = False
    calls = {"text": 0, "tts": 0}
    class TextModel:
        def generate(self, *args, **kwargs):
            calls["text"] += 1
            return "猫咪走过雨夜街道。"
    class Voice:
        async def synthesize(self, text, target, **kwargs):
            calls["tts"] += 1
            if calls["tts"] == 1:
                raise AppError("TTS_FAILED", "配音中断", 502)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"audio")
            return target
    async def compose(*args):
        target = args[-1]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"video")
        return target
    monkeypatch.setattr(ffmpeg_util, "image_audio_to_video", compose)
    monkeypatch.setattr(ffmpeg_util, "concat_videos", compose)
    pipelines = ShortPipelines(llm=TextModel(), image=orch.image, tts=Voice())
    monkeypatch.setattr(tasks, "get_pipelines", lambda: pipelines)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/tasks", json={"type": "literary_video", "input": {"text": "雨夜小猫"}})
        tid = created.json()["task_id"]
        first = events(await client.get(f"/api/tasks/{tid}/stream"))[-1]
        assert first["error"]["code"] == "TTS_FAILED"
        status = (await client.get(f"/api/tasks/{tid}/recovery")).json()
        assert status["can_resume"]
        assert (await client.post(f"/api/tasks/{tid}/resume", json={"execution_id": status["execution_id"]})).status_code == 200
        assert events(await client.get(f"/api/tasks/{tid}/stream"))[-1]["type"] == "done"
        assert calls == {"text": 1, "tts": 2}
        assert state["posts"] == 1
