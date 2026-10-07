"""Durable media continuation, with isolated SQLite and offline HTTP."""
from contextlib import contextmanager
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest
from PIL import Image

from app.core import config
from app.core.errors import AppError
from app.models.image_client import ImageClient
from app.models.video_client import VideoClient
from app.services import db, execution_store, provider_jobs


@pytest.fixture
def media(data_dirs, monkeypatch):
    rates = {
        "qwen-image-2.0": {"kind": "image", "image_cny": 0.1, "source": "test fixture"},
        "qwen-image-2.0-pro": {"kind": "image", "image_cny": 0.2, "source": "test fixture"},
        "wan2.7-i2v": {"kind": "video", "video_second_cny": 0.1, "source": "test fixture"},
        "wan2.7-r2v": {"kind": "video", "video_second_cny": 0.1, "source": "test fixture"},
    }
    monkeypatch.setattr(config.settings, "model_cost_rates_json", json.dumps(rates))
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "offline-test")
    monkeypatch.setattr(config.settings, "aihubmix_base", "https://gateway.example.test")
    monkeypatch.setattr(config.settings, "image_t2i_model", "qwen-image-2.0")
    monkeypatch.setattr(config.settings, "video_first_frame_model", "wan2.7-i2v")
    monkeypatch.setattr(config.settings, "video_reference_model", "wan2.7-r2v")
    monkeypatch.setattr(config.settings, "max_retries", 0)
    monkeypatch.setattr("time.sleep", lambda _: None)
    clients = []
    calls = []

    def install(handler):
        def record(request):
            calls.append((request.method, str(request.url)))
            return handler(request)
        client = httpx.Client(transport=httpx.MockTransport(record))
        clients.append(client)
        for method in ("request", "post", "get"):
            monkeypatch.setattr(httpx, method, getattr(client, method))
        return calls
    yield install
    for client in clients:
        client.close()


def execution(owner="alice", entity="story1", stage="video_generation", status="running"):
    eid, now = uuid4().hex, time.time()
    with db.connect() as conn:
        conn.execute("INSERT INTO executions(execution_id,entity_type,entity_id,owner_id,stage,operation,input_hash,status,worker_pid,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (eid, "session", entity, owner, stage, "generate", "fixture", status, os.getpid(), now, now))
    return eid


def stop(eid, status="failed"):
    with db.connect() as conn:
        conn.execute("UPDATE executions SET status=? WHERE execution_id=?", (status, eid))


@contextmanager
def running(eid):
    token = execution_store._current_execution.set(eid)
    try:
        yield
    finally:
        execution_store._current_execution.reset(token)


def source_image(tmp_path):
    path = tmp_path / "reference.png"
    Image.new("RGB", (160, 90), "navy").save(path)
    return path


def test_transient_poll_failure_recovers_original_video_without_second_post(media, tmp_path):
    source = source_image(tmp_path)
    polls = []

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "existing-job", "status": "pending"})
        if str(request.url).endswith("existing-job"):
            polls.append(True)
            if len(polls) == 1:
                raise httpx.ReadTimeout("temporary query outage", request=request)
            return httpx.Response(200, json={"status": "completed", "output": [{"content_url": "/video.mp4"}]})
        return httpx.Response(200, content=b"video")

    calls = media(handler)
    eid = execution()
    with running(eid):
        result = video(VideoClient(), source, tmp_path / "video.mp4")
    assert result.read_bytes() == b"video"
    assert len(polls) == 2
    assert sum(method == "POST" for method, _ in calls) == 1


def test_paid_recovery_paths_are_scoped_to_owner_and_entity(media, tmp_path):
    eid = execution(owner="alice", entity="project1")
    with running(eid):
        job = provider_jobs.begin("image", "https://gateway.example.test/images", {"model": "qwen-image-2.0", "prompt": "one"})
        path = tmp_path / "paid.png"
        path.write_bytes(b"paid")
        provider_jobs.completed(job, path)
    assert provider_jobs.retained_local_paths("alice", "session", "project1") == {str(path.resolve())}
    assert not provider_jobs.retained_local_paths("bob", "session", "project1")
    assert not provider_jobs.retained_local_paths("alice", "session", "other")


@pytest.mark.parametrize("kind", ["image", "video"])
def test_manual_resume_retries_only_definite_rejected_slot_with_new_cost_call(media, tmp_path, kind):
    state = {"posts": 0, "reject": True}
    source = source_image(tmp_path)

    def handler(request):
        if request.method == "POST":
            state["posts"] += 1
            if state["posts"] == 2 and state["reject"]:
                return httpx.Response(402, json={"error": "insufficient balance"})
            if kind == "image":
                return httpx.Response(200, json={"data": [{"url": f'/asset{state["posts"]}.png'}]})
            return httpx.Response(200, json={"id": f'job{state["posts"]}', "status": "completed", "output": [{"content_url": f'/asset{state["posts"]}.mp4'}]})
        return httpx.Response(200, content=b"completed-media")

    calls = media(handler)
    def generate(prompt, name):
        if kind == "image":
            return ImageClient().text_to_image(prompt, tmp_path / name)
        return video(VideoClient(), source, tmp_path / name, prompt=prompt)

    original = execution()
    with running(original):
        generate("first", "first.bin")
        with pytest.raises(AppError) as error:
            generate("second", "second.bin")
        assert error.value.code == f"{kind.upper()}_SUBMIT_REJECTED"
    stop(original)
    status = provider_jobs.public_status(original, "alice")
    assert status["can_resume"] and not status["requires_reconciliation"]
    assert [j["status"] for j in status["jobs"]] == ["completed", "rejected"]
    rejected_id = status["jobs"][1]["id"]
    assert status["jobs"][1]["http_status"] == 402
    assert status["jobs"][1]["retry_requires_submission"]
    readonly = execution()
    with running(readonly), provider_jobs.resume_scope(original, "alice"):
        generate("first", "first-readonly.bin")
        with pytest.raises(AppError) as error:
            generate("second", "second-readonly.bin")
        assert error.value.code == "PROVIDER_RETRY_REQUIRES_SUBMISSION"
    stop(readonly)
    assert state["posts"] == 2
    resumed = execution()
    with running(resumed), provider_jobs.resume_scope(original, "alice", allow_new=True):
        generate("first", "first-resumed.bin")
        generate("second", "second-resumed.bin")
        generate("third", "third-appended.bin")
    assert state["posts"] == 4  # first was reused; second retry + third new.
    assert [m for m, _ in calls].count("GET") == 3
    with db.connect() as conn:
        attempts = conn.execute("SELECT * FROM provider_job_attempts WHERE retry_of=?", (rejected_id,)).fetchall()
        assert len(attempts) == 1 and attempts[0]["attempt"] == 2
        new_id = attempts[0]["job_id"]
        ledger = {r["call_id"]: r for r in conn.execute("SELECT * FROM model_cost_ledger")}
        assert len(ledger) == 4 and new_id != rejected_id
        assert ledger[rejected_id]["status"] == "rejected" and ledger[rejected_id]["charged_micros"] == 0
        assert ledger[new_id]["status"] == "completed" and ledger[new_id]["execution_id"] == resumed
    active = provider_jobs.public_status(original, "alice")["jobs"]
    assert [j["status"] for j in active] == ["completed"] * 3
    assert active[1]["id"] == new_id and active[1]["attempt"] == 2


@pytest.mark.parametrize("kind", ["image", "video"])
@pytest.mark.parametrize("http_status", [408, 409, 425, 500])
def test_ambiguous_http_never_releases_budget_or_becomes_new_submission(media, tmp_path, kind, http_status):
    calls = media(lambda req: httpx.Response(http_status, json={"error": "uncertain"}))
    source = source_image(tmp_path)
    original = execution()
    with running(original), pytest.raises(AppError):
        if kind == "image":
            ImageClient().text_to_image("same", tmp_path / "out.png")
        else:
            video(VideoClient(), source, tmp_path / "out.mp4")
    stop(original)
    status = provider_jobs.public_status(original, "alice")
    assert status["requires_reconciliation"] and not status["can_resume"]
    assert status["jobs"][0]["status"] == "unknown"
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM model_cost_ledger").fetchone()[0] == "uncertain"
    with running(execution()), pytest.raises(AppError, match="上次提交结果未确认"):
        with provider_jobs.resume_scope(original, "alice", allow_new=True):
            pytest.fail("unknown submission must never enter recovery")
    assert [m for m, _ in calls] == ["POST"]


@pytest.mark.parametrize("change", ["prompt", "model", "reference"])
def test_rejected_retry_keeps_original_payload_binding(media, tmp_path, change):
    calls = media(lambda req: httpx.Response(402, json={"error": "balance"}))
    source = source_image(tmp_path)
    original = execution()
    with running(original), pytest.raises(AppError):
        ImageClient().image_to_image(source, "original", tmp_path / "out.png")
    stop(original)
    if change == "reference":
        Image.new("RGB", (160, 90), "red").save(source)
    with running(execution()), provider_jobs.resume_scope(original, "alice", allow_new=True):
        with pytest.raises(AppError) as error:
            ImageClient().image_to_image(source, "edited" if change == "prompt" else "original", tmp_path / "retry.png",
                                          model="qwen-image-2.0-pro" if change == "model" else "qwen-image-2.0")
        assert error.value.code == "PROVIDER_INPUT_CHANGED"
    assert [m for m, _ in calls] == ["POST"]


def test_legacy_rejected_ledger_requires_explicit_matching_402_evidence(media, tmp_path):
    media(lambda req: httpx.Response(402, json={"error": "balance"}))
    original = execution()
    with running(original), pytest.raises(AppError):
        ImageClient().text_to_image("original", tmp_path / "out.png")
    stop(original)
    with db.connect() as conn:
        job_id = conn.execute("SELECT id FROM provider_jobs").fetchone()[0]
        conn.execute("UPDATE provider_jobs SET status='failed',error_code='IMAGE_MODEL_ERROR'")
        conn.execute("DELETE FROM provider_job_attempts")
        conn.execute("UPDATE executions SET error='图片接口返回 409' WHERE execution_id=?", (original,))
    assert not provider_jobs.public_status(original, "alice")["can_resume"]
    with pytest.raises(AppError) as error:
        provider_jobs.reconcile_legacy_http_402(job_id, "bob")
    assert error.value.status_code == 404
    with pytest.raises(AppError) as error:
        provider_jobs.reconcile_legacy_http_402(job_id, "alice")
    assert error.value.code == "PROVIDER_REJECTION_UNCONFIRMED"
    with db.connect() as conn:
        conn.execute("UPDATE executions SET error='图片接口返回 402' WHERE execution_id=?", (original,))
    # Nothing automatically treats a generic historic failure as safe to retry.
    assert not provider_jobs.public_status(original, "alice")["can_resume"]
    repaired = provider_jobs.reconcile_legacy_http_402(job_id, "alice")
    assert repaired["status"] == "rejected" and repaired["http_status"] == 402
    assert provider_jobs.public_status(original, "alice")["can_resume"]


def test_retry_requires_new_budget_reservation_and_local_gate_remains_recoverable(media, tmp_path, monkeypatch):
    state = {"posts": 0}
    def handler(request):
        if request.method == "POST":
            state["posts"] += 1
            if state["posts"] == 2:
                return httpx.Response(402, json={"error": "balance"})
            return httpx.Response(200, json={"data": [{"url": "/asset.png"}]})
        return httpx.Response(200, content=b"media")
    media(handler)
    original = execution()
    def generate(prompt):
        return ImageClient().text_to_image(prompt, tmp_path / f"{prompt}.png")
    with running(original):
        generate("first")
        with pytest.raises(AppError):
            generate("second")
    stop(original)
    monkeypatch.setattr(config.settings, "monthly_user_budget_cny", "0.1")
    blocked = execution()
    with running(blocked), provider_jobs.resume_scope(original, "alice", allow_new=True):
        generate("first")
        with pytest.raises(AppError) as error:
            generate("second")
        assert error.value.code == "USER_BUDGET_EXCEEDED"
    stop(blocked)
    assert state["posts"] == 2
    assert provider_jobs.public_status(blocked, "alice")["can_resume"]
    monkeypatch.setattr(config.settings, "monthly_user_budget_cny", "0.2")
    with running(execution()), provider_jobs.resume_scope(blocked, "alice", allow_new=True):
        generate("first")
        generate("second")
    assert state["posts"] == 3
    with db.connect() as conn:
        ledger = conn.execute("SELECT status FROM model_cost_ledger ORDER BY created_at").fetchall()
        assert [r[0] for r in ledger] == ["completed", "rejected", "completed"]
    assert provider_jobs.public_status(original, "alice")["jobs"][1]["attempt"] == 3


@pytest.mark.parametrize("kind", ["image", "video"])
def test_accepted_async_failure_never_becomes_rejected_retry(media, tmp_path, kind):
    calls = media(lambda request: httpx.Response(200, json={"id": "accepted-id", "status": "failed"}))
    original = execution()
    source = source_image(tmp_path)
    with running(original), pytest.raises(AppError):
        if kind == "image":
            ImageClient().image_to_image(source, "original", tmp_path / "out.png")
        else:
            video(VideoClient(), source, tmp_path / "out.mp4")
    stop(original)
    status = provider_jobs.public_status(original, "alice")
    assert not status["can_resume"] and status["jobs"][0]["status"] == "failed"
    assert not status["jobs"][0]["retry_requires_submission"]
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM model_cost_ledger").fetchone()[0] == "uncertain"
    assert [m for m, _ in calls] == ["POST"]


def video(client, source, target, prompt="same prompt", model="wan2.7-i2v"):
    client.poll_interval = 0
    return client.image_to_video(str(source), prompt, target, video_ratio="16:9", model=model)


@pytest.mark.parametrize("append", [False, True])
def test_video_job_survives_new_process_and_only_appends_when_enabled(media, tmp_path, append):
    original = execution()
    source = source_image(tmp_path)
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "remote-job-1", "status": "pending"})
        with db.connect() as conn:
            assert conn.execute("SELECT vendor_job_id FROM provider_jobs").fetchone()[0] == "remote-job-1"
        raise httpx.ReadTimeout("poll interrupted", request=request)
    calls = media(handler)
    with running(original), pytest.raises(AppError, match="查询视频任务失败"):
        video(VideoClient(), source, tmp_path / "first.mp4")
    stop(original, "interrupted")
    assert provider_jobs.public_status(original, "alice")["can_resume"]
    resumed = execution()
    code = """
import json,sys
from pathlib import Path
import httpx
from app.core import config
from app.services import execution_store,provider_jobs
from app.models.video_client import VideoClient
args=json.loads(sys.argv[1]);config.DATA_DIR=Path(args['data']);config.settings.aihubmix_api_key='offline-test';config.settings.aihubmix_base='https://gateway.example.test';config.settings.max_retries=0;config.settings.model_cost_rates_json=args['rates']
seen=[]
def handler(req):
 seen.append(req.method)
 if req.method=='POST':
  assert args['append'] and json.loads(req.content)['prompt']=='second shot'
  return httpx.Response(200,json={'id':'new-job-2','status':'completed','output':[{'content_url':'/second.mp4'}]})
 if req.url.path.endswith('remote-job-1'):return httpx.Response(200,json={'status':'completed','output':[{'content_url':'/asset.mp4'}]})
 return httpx.Response(200,content=b'resumed-video')
with httpx.Client(transport=httpx.MockTransport(handler)) as http:
 httpx.request=http.request;httpx.get=http.get
 token=execution_store._current_execution.set(args['current'])
 try:
  with provider_jobs.resume_scope(args['source_execution'],'alice',allow_new=args['append']):
   client=VideoClient();client.poll_interval=0
   out=client.image_to_video(args['source'],'same prompt',Path(args['out']),video_ratio='16:9',model='wan2.7-i2v')
   assert out.read_bytes()==b'resumed-video'
   if args['append']:
    second=client.image_to_video(args['source'],'second shot',Path(args['out']+'.second'),video_ratio='16:9',model='wan2.7-i2v')
    assert second.read_bytes()==b'resumed-video'
 finally:execution_store._current_execution.reset(token)
print(json.dumps({'methods':seen}))
"""
    args = {"data": str(config.DATA_DIR), "current": resumed, "source_execution": original,
            "source": str(source), "out": str(tmp_path / "resumed.mp4"), "append": append,
            "rates": config.settings.model_cost_rates_json}
    result = subprocess.run([sys.executable, "-c", code, json.dumps(args)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["methods"] == (["GET", "GET", "POST", "GET"] if append else ["GET", "GET"])
    assert sum(method == "POST" for method, _ in calls) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM model_cost_ledger").fetchone()[0] == 1 + int(append)
        assert all(row[0] == "completed" for row in conn.execute("SELECT status FROM provider_jobs").fetchall())


@pytest.mark.parametrize("kind", ["image", "video"])
def test_completed_url_survives_download_failure_without_new_post(media, tmp_path, kind):
    original = execution(stage="reference_generation" if kind == "image" else "video_generation")
    source = source_image(tmp_path)
    def broken(request):
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "/result.png"}]} if kind == "image" else {"id": "v1", "status": "completed", "output": [{"content_url": "/result.mp4"}]})
        raise httpx.ReadTimeout("download interrupted", request=request)
    calls = media(broken)
    with running(original), pytest.raises(AppError):
        if kind == "image":
            ImageClient().text_to_image("prompt", tmp_path / "first.png")
        else:
            video(VideoClient(), source, tmp_path / "first.mp4")
    stop(original)
    before = provider_jobs.public_status(original, "alice")
    assert before["can_resume"] and before["jobs"][0]["status"] == "ready"
    resumed = execution(stage="reference_generation" if kind == "image" else "video_generation")
    def download(request):
        assert request.method == "GET"
        return httpx.Response(200, content=b"completed")
    media(download)
    with running(resumed), provider_jobs.resume_scope(original, "alice"):
        if kind == "image":
            out = ImageClient().text_to_image("prompt", tmp_path / "resumed.png")
        else:
            out = video(VideoClient(), source, tmp_path / "resumed.mp4")
    assert out.read_bytes() == b"completed"
    assert sum(method == "POST" for method, _ in calls) == 1


def test_cancellation_keeps_remote_job_and_never_reposts(media, tmp_path):
    eid = execution()
    def handler(request):
        assert request.method == "POST"
        stop(eid, "interrupted")  # Cancellation arrived while POST was in flight.
        return httpx.Response(200, json={"id": "accepted-before-cancel", "status": "pending"})
    calls = media(handler)
    with running(eid), pytest.raises(AppError) as error:
        video(VideoClient(), source_image(tmp_path), tmp_path / "cancelled.mp4")
    assert error.value.code == "PROVIDER_EXECUTION_INACTIVE"
    assert len(calls) == 1
    status = provider_jobs.public_status(eid, "alice")
    assert status["can_resume"] and status["jobs"][0]["status"] == "job_pending"


def test_malformed_poll_output_keeps_confirmed_job_resumable(media, tmp_path):
    eid = execution()
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "known-job", "status": "pending"})
        return httpx.Response(200, json={"status": "completed", "output": {"unexpected": "shape"}})
    calls = media(handler)
    with running(eid), pytest.raises(AppError) as error:
        video(VideoClient(), source_image(tmp_path), tmp_path / "bad.mp4")
    assert error.value.code == "VIDEO_RESPONSE_INVALID"
    stop(eid)
    assert provider_jobs.public_status(eid, "alice")["can_resume"]
    assert sum(method == "POST" for method, _ in calls) == 1


def test_unknown_submit_never_reposts_and_requires_reconciliation(media, tmp_path):
    eid = execution()
    def handler(request):
        raise httpx.ReadTimeout("unknown submit", request=request)
    calls = media(handler)
    source = source_image(tmp_path)
    with running(eid):
        for _ in range(2):
            with pytest.raises(AppError):
                video(VideoClient(), source, tmp_path / "unknown.mp4")
    stop(eid)
    state = provider_jobs.public_status(eid, "alice")
    assert state["requires_reconciliation"] and not state["can_resume"]
    with pytest.raises(AppError) as error:
        provider_jobs.validate_resume(eid, "alice")
    assert error.value.code == "PROVIDER_SUBMIT_UNCONFIRMED"
    resumed = execution()
    with running(resumed), pytest.raises(AppError) as rejected:
        with provider_jobs.resume_scope(eid, "alice", allow_new=True):
            video(VideoClient(), source, tmp_path / "not-resubmitted.mp4")
    assert rejected.value.code == "PROVIDER_SUBMIT_UNCONFIRMED"
    assert sum(method == "POST" for method, _ in calls) == 1


def test_owner_scope_input_fingerprint_and_new_regenerate(media, tmp_path):
    original = execution(stage="reference_generation")
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "/result.png"}]})
        return httpx.Response(200, content=b"image")
    calls = media(handler)
    with running(original):
        ImageClient().text_to_image("prompt", tmp_path / "first.png")
    stop(original)
    with pytest.raises(AppError) as error:
        provider_jobs.public_status(original, "bob")
    assert error.value.status_code == 404
    foreign = execution(owner="bob", entity="bob-story", stage="reference_generation")
    with running(foreign), pytest.raises(AppError):
        with provider_jobs.resume_scope(original, "bob"):
            pass
    wrong_entity = execution(entity="other", stage="reference_generation")
    with running(wrong_entity), pytest.raises(AppError) as mismatch:
        with provider_jobs.resume_scope(original, "alice"):
            pass
    assert mismatch.value.code == "PROVIDER_RESUME_MISMATCH"
    resumed = execution(stage="reference_generation")
    with running(resumed), provider_jobs.resume_scope(original, "alice"):
        ImageClient().text_to_image("prompt", tmp_path / "copied.png")
    assert sum(method == "POST" for method, _ in calls) == 1
    assert (tmp_path / "copied.png").read_bytes() == b"image"
    stop(resumed, "completed")
    new_generation = execution(stage="reference_generation")
    with running(new_generation):
        ImageClient().text_to_image("prompt", tmp_path / "new.png")
    assert sum(method == "POST" for method, _ in calls) == 2
    public = json.dumps(provider_jobs.public_status(original, "alice"))
    assert "result.png" not in public and "prompt" not in public and "gateway" not in public


@pytest.mark.parametrize("changed", ["prompt", "reference", "model"])
def test_resume_rejects_changed_input_without_paid_submit(media, tmp_path, changed):
    eid, source = execution(), source_image(tmp_path)
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "pending1", "status": "pending"})
        raise httpx.ReadTimeout("poll", request=request)
    calls = media(handler)
    with running(eid), pytest.raises(AppError):
        video(VideoClient(), source, tmp_path / "first.mp4")
    stop(eid)
    if changed == "reference":
        Image.new("RGB", (160, 90), "red").save(source)
    resumed = execution()
    with running(resumed), provider_jobs.resume_scope(eid, "alice", allow_new=True), pytest.raises(AppError) as error:
        video(VideoClient(), source, tmp_path / "resume.mp4", prompt="changed" if changed == "prompt" else "same prompt",
              model="wan2.6-i2v" if changed == "model" else "wan2.7-i2v")
    assert error.value.code == "PROVIDER_INPUT_CHANGED"
    assert sum(method == "POST" for method, _ in calls) == 1


def test_append_permission_cannot_replace_completed_first_slot(media, tmp_path):
    eid = execution(stage="character_design")
    def handler(request):
        return httpx.Response(200, json={"data": [{"url": "/a.png"}]}) if request.method == "POST" else httpx.Response(200, content=b"image")
    calls = media(handler)
    with running(eid):
        ImageClient().text_to_image("first original", tmp_path / "a.png")
    stop(eid)
    resumed = execution(stage="character_design")
    with running(resumed), provider_jobs.resume_scope(eid, "alice", allow_new=True), pytest.raises(AppError) as error:
        ImageClient().text_to_image("first changed", tmp_path / "changed.png")
    assert error.value.code == "PROVIDER_INPUT_CHANGED"
    assert sum(method == "POST" for method, _ in calls) == 1


@pytest.mark.asyncio
async def test_order_cursor_is_shared_across_to_thread_and_appends_last(media, tmp_path):
    eid = execution(stage="character_design")
    interrupted = True
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "/a.png"}]})
        if interrupted:
            raise httpx.ReadTimeout("download interrupted", request=request)
        return httpx.Response(200, content=b"image")
    calls = media(handler)
    with running(eid), pytest.raises(AppError):
        await asyncio.to_thread(ImageClient().text_to_image, "first", tmp_path / "a.png")
    stop(eid)
    interrupted = False
    resumed = execution(stage="character_design")
    with running(resumed), provider_jobs.resume_scope(eid, "alice", allow_new=True):
        first = await asyncio.to_thread(ImageClient().text_to_image, "first", tmp_path / "recovered.png")
        second = await asyncio.to_thread(ImageClient().text_to_image, "second", tmp_path / "new.png")
    assert first.read_bytes() == second.read_bytes() == b"image"
    assert sum(method == "POST" for method, _ in calls) == 2
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM provider_job_steps").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM model_cost_ledger").fetchone()[0] == 2
