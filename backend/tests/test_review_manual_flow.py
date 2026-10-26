"""Real session/reference SSE flow with persistent fake model outputs only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.api import sessions
from app.api.deps import get_current_user, get_orchestrator
from app.core import config
from app.core.errors import AppError, register_exception_handlers
from app.schemas.session import SessionCreate
from app.services import auth, db, execution_store, provider_jobs, recovery, review_store, session_store, trial_events
from app.services.content_review import ContentReviewer
from app.services.orchestrator import Orchestrator
from tests.conftest import MockLLM, MockTTS, MockVideo


class CountingImage:
    def __init__(self): self.posts = {}

    def _generate(self, prompt, out_path, model, size, references=()):
        payload = {"model": model or "qwen-image-2.0", "prompt": prompt, "size": size,
                   "references": [review_store.file_sha(p) for p in references]}
        job = provider_jobs.begin("image", "fake://local-images", payload)
        if provider_jobs.reuse_local(job, out_path):
            return out_path
        if not job.new:
            raise AssertionError("Fake model cannot repeat an unresolved generation")
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        self.posts[digest] = self.posts.get(digest, 0) + 1
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1280, 720), tuple(bytes.fromhex(digest[:6]))).save(out_path)
        provider_jobs.ready(job, "fake://completed/" + job.id)
        provider_jobs.completed(job, out_path)
        return out_path

    def text_to_image(self, prompt, out_path, model=None, *, size="1280x720"):
        return self._generate(prompt, out_path, model, size)

    def image_to_image(self, paths, prompt, out_path, model=None, *, size="1280x720"):
        return self._generate(prompt, out_path, model, size, paths if isinstance(paths, list) else [paths])


class StageVLM:
    model = "local-fake-vlm"
    def __init__(self):
        self.reference_raw = ['{"safe":false,"reason":"构图质量较差"}']
        self.posts = {}
        self.reference_posts = 0

    def review(self, path, instruction):
        digest = review_store.file_sha(path)
        self.posts[digest] = self.posts.get(digest, 0) + 1
        with db.connect() as conn:
            row = conn.execute("SELECT stage FROM executions WHERE execution_id=?", (execution_store.current_execution_id(),)).fetchone()
        if row[0] == "reference_generation":
            index = self.reference_posts
            self.reference_posts += 1
            if index < len(self.reference_raw):
                return self.reference_raw[index]
        return '{"safe":true,"reason":"原创测试画面，无侵权或真人肖像"}'


def events(response):
    assert response.status_code == 200, response.text
    return [json.loads(line[5:].strip()) for line in response.text.splitlines() if line.startswith("data:")]


@pytest.fixture
def flow(data_dirs, monkeypatch):
    monkeypatch.setenv("TRIAL_MODE_ENABLED", "1")
    monkeypatch.setenv("F1_REAL_ENABLED", "0")
    monkeypatch.setattr(config.settings, "mode_mock", True)
    monkeypatch.setattr(config.settings, "content_review_enabled", True)
    monkeypatch.setattr(config.settings, "disable_outbound_network", True)
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "")
    trial_events.init_events()
    owner = auth.login(auth.generate_invite_codes(1)[0])["user_id"]
    trial_events.accept_consent(owner)
    llm = MockLLM()
    llm.storyboard["shots"].append({**llm.storyboard["shots"][1], "shot_id": "s3", "description": "第三镜", "prompt": "cat after rain"})
    image, vlm = CountingImage(), StageVLM()
    orch = Orchestrator(llm=llm, image=image, video=MockVideo(), tts=MockTTS(), reviewer=ContentReviewer(vlm))
    meta = orch.create(SessionCreate(idea="local mock manual review flow", project_type="story"), owner_id=owner)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(sessions.router, prefix="/api")
    async def current_user(request: Request):
        return request.headers.get("X-Test-User", owner)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_orchestrator] = lambda: orch
    with TestClient(app) as client:
        for stage in ["script_generation", "character_design", "storyboard"]:
            result = events(client.post(f"/api/sessions/{meta.session_id}/execute/{stage}"))
            assert result[-1]["type"] == "done", result
        yield client, app, orch, meta.session_id, owner, image, vlm


def pause(flow):
    client, _, _, sid, owner, image, vlm = flow
    result = events(client.post(f"/api/sessions/{sid}/execute/reference_generation"))
    assert result[-1]["type"] == "review_required", result
    pending = result[-1]["pending_review"]
    assert result[-1]["session"]["status"] == "awaiting_review"
    assert result[-1]["session"]["execution"]["status"] == "awaiting_review"
    assert pending["status"] == "pending" and pending["reason"] and pending["image_url"]
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM trial_generations WHERE task_id=?", (sid,)).fetchone()[0] == "reserved"
        assert conn.execute("SELECT COUNT(*) FROM trial_events WHERE user_id=? AND event='gen_fail'", (owner,)).fetchone()[0] == 0
    return pending


def test_reference_pause_approve_reuses_image_and_review_once(flow):
    client, _, _, sid, owner, image, vlm = flow
    pending = pause(flow)
    first_trace = next(r for r in review_store.records(sid, owner) if r["review_id"] == pending["review_id"])
    original_path = first_trace["image_path"]
    assert vlm.reference_posts == 1 and sum(image.posts.values()) == 3
    # A duplicate ordinary execute is only an event replay, never a model retry.
    duplicate = events(client.post(f"/api/sessions/{sid}/execute/reference_generation"))
    assert duplicate[-1]["type"] == "review_required" and vlm.reference_posts == 1
    response = events(client.post(f"/api/sessions/{sid}/reviews/{pending['review_id']}/confirm", json={"accepted": True}))
    assert response[-1]["type"] == "done", response
    assert response[-1]["session"]["pending_review"] is None
    assert response[-1]["session"]["execution"]["execution_id"] == pending["execution_id"]
    assert vlm.reference_posts == 3 and sum(image.posts.values()) == 5
    assert all(count == 1 for count in image.posts.values()) and all(count == 1 for count in vlm.posts.values())
    assert Path(original_path).is_file() and client.get(pending["image_url"]).status_code == 200
    trace = next(r for r in review_store.records(sid, owner) if r["review_id"] == pending["review_id"])
    assert trace["status"] == "approved" and trace["evidence"]["parsed_safe"] is False
    assert trace["evidence"]["manual_decision"]["accepted"] is True
    before = (dict(image.posts), dict(vlm.posts))
    repeated = events(client.post(f"/api/sessions/{sid}/reviews/{pending['review_id']}/confirm", json={"accepted": True}))
    assert repeated[-1]["type"] == "done" and before == (image.posts, vlm.posts)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM executions WHERE entity_id=? AND stage='reference_generation'", (sid,)).fetchone()[0] == 1


def test_human_reject_stops_without_more_generation_and_is_idempotent(flow):
    client, _, _, sid, owner, image, vlm = flow
    pending = pause(flow)
    before = (dict(image.posts), dict(vlm.posts))
    url = f"/api/sessions/{sid}/reviews/{pending['review_id']}/confirm"
    response = client.post(url, json={"accepted": False})
    assert response.status_code == 200 and response.json()["status"] == "failed" and response.json()["pending_review"] is None
    assert client.post(url, json={"accepted": False}).status_code == 200
    assert client.post(url, json={"accepted": True}).status_code == 409
    assert before == (image.posts, vlm.posts)
    with db.connect() as conn:
        assert conn.execute("SELECT status FROM trial_generations WHERE task_id=?", (sid,)).fetchone()[0] == "pending_reconciliation"


def test_owner_only_trace_and_confirmation(flow):
    client, _, _, sid, owner, image, vlm = flow
    pending = pause(flow)
    outsider = {"X-Test-User": "unrelated-person"}
    assert client.get(f"/api/sessions/{sid}/reviews", headers=outsider).status_code == 404
    assert client.post(f"/api/sessions/{sid}/reviews/{pending['review_id']}/confirm", headers=outsider, json={"accepted": True}).status_code == 404
    mine = client.get(f"/api/sessions/{sid}/reviews").json()["reviews"]
    record = next(r for r in mine if r["review_id"] == pending["review_id"])
    assert record["owner_id"] == owner and record["instruction"] and record["evidence"]["raw_response"]
    assert vlm.reference_posts == 1


@pytest.mark.parametrize("change", ["image", "input"])
def test_changed_image_or_original_input_cannot_approve(flow, change):
    client, _, orch, sid, owner, image, vlm = flow
    pending = pause(flow)
    if change == "image":
        record = next(r for r in review_store.records(sid, owner) if r["review_id"] == pending["review_id"])
        Path(record["image_path"]).write_bytes(b"tampered image fixture")
    else:
        meta = orch.get(sid)
        meta.idea = "changed original input"
        session_store.touch(meta)
    response = client.post(f"/api/sessions/{sid}/reviews/{pending['review_id']}/confirm", json={"accepted": True})
    assert response.status_code == 409 and vlm.reference_posts == 1 and sum(image.posts.values()) == 3


def test_parallel_confirm_claims_one_worker_and_one_execution(flow):
    client, _, _, sid, _, image, vlm = flow
    pending = pause(flow)
    url = f"/api/sessions/{sid}/reviews/{pending['review_id']}/confirm"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json={"accepted": True}), range(2)))
    for response in responses:
        assert events(response)[-1]["type"] == "done"
    assert vlm.reference_posts == 3 and all(count == 1 for count in image.posts.values())
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM executions WHERE entity_id=? AND stage='reference_generation'", (sid,)).fetchone()[0] == 1


def test_restart_preserves_waiting_and_original_human_decision(flow, monkeypatch):
    client, _, _, sid, owner, image, vlm = flow
    pending = pause(flow)
    meta = session_store.load_session(sid)
    request = execution_store.request_payload(pending["execution_id"])
    input_hash = execution_store.fingerprint(recovery.session_input(meta, "reference_generation", request))
    eid, claimed, _ = review_store.confirm(sid, owner, pending["review_id"], True, input_hash)
    assert claimed and eid == pending["execution_id"]
    before = review_store.records(sid, owner)[-1]["evidence"]["manual_decision"]["confirmed_at"]
    with db.connect() as conn:
        conn.execute("UPDATE executions SET worker_pid=0 WHERE execution_id=?", (eid,))
    monkeypatch.setattr(execution_store, "_process_alive", lambda pid: False)
    execution_store.recover_unfinished()
    assert session_store.load_session(sid).status == "awaiting_review"
    assert execution_store.snapshot("session", sid)["status"] == "awaiting_review"
    response = events(client.post(f"/api/sessions/{sid}/reviews/{pending['review_id']}/confirm", json={"accepted": True}))
    assert response[-1]["type"] == "done", response
    record = next(r for r in review_store.records(sid, owner) if r["review_id"] == pending["review_id"])
    assert record["evidence"]["manual_decision"]["confirmed_at"] == before
    assert vlm.reference_posts == 3 and all(count == 1 for count in image.posts.values())


@pytest.mark.parametrize("raw,kind", [('{"safe":false,"reason":"存在侵权IP"}', "blocked"),
                                     ('{"safe":"false","reason":"copyright infringement"}', "blocked"),
                                     ('{"safe":true,"reason":"explicit sexual content"}', "blocked"),
                                     ('{"safe":false,"reason":"不通过"}', "blocked")])
def test_compliance_and_unknown_reject_have_no_manual_override(flow, raw, kind):
    client, _, _, sid, owner, image, vlm = flow
    vlm.reference_raw = [raw]
    result = events(client.post(f"/api/sessions/{sid}/execute/reference_generation"))
    assert result[-1]["type"] == "error" and all(r["type"] != "review_required" for r in result)
    record = review_store.records(sid, owner)[-1]
    assert record["status"] == kind and record["evidence"]["raw_response"] == raw
    response = client.post(f"/api/sessions/{sid}/reviews/{record['review_id']}/confirm", json={"accepted": True})
    assert response.status_code == 409 and vlm.reference_posts == 1 and sum(image.posts.values()) == 3


@pytest.mark.parametrize("raw", ['{"safe":true,"reason":""}', 'unparseable content'])
def test_empty_reason_and_unparseable_success_pause(flow, raw):
    client, _, _, sid, owner, _, vlm = flow
    vlm.reference_raw = [raw]
    pending = pause(flow)
    assert pending["reason"] == review_store.MANUAL_REASON
    record = next(r for r in review_store.records(sid, owner) if r["review_id"] == pending["review_id"])
    assert record["evidence"]["raw_response"] == raw and vlm.reference_posts == 1
