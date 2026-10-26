"""Review route contracts and evidence-before-cost errors. All clients are fake."""
import json
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image
import pytest

from app.core import config
from app.core.errors import AppError
from app.models.vlm_client import VLMClient
from app.schemas.session import SessionMeta
from app.services import cost_control, db, execution_store, recovery, review_store, session_store
from app.services.content_review import ContentReviewer, INSTRUCTION, ordinary_result


@pytest.mark.parametrize("safe,reason,route", [
    (True, "原创画面，未发现侵权或真人肖像", "accepted"),
    (False, "构图质量较差", "manual"),
    (False, "人物数量与分镜不符", "manual"),
    (False, "未发现色情、暴力、违法内容，但构图较差", "manual"),
    (True, "没有违法内容和侵权IP或真人肖像", "accepted"),
    (True, "No real person, copyright infringement, or illegal content", "accepted"),
    (False, "No copyright infringement but poor composition", "manual"),
    (False, "存在真人肖像或侵权 IP", "compliance_rejected"),
    (False, "copyright infringement", "compliance_rejected"),
    (False, "real-public-figure", "compliance_rejected"),
    (False, "explicit sexual content", "compliance_rejected"),
    (False, "violence or illegal content", "compliance_rejected"),
    (False, "没有侵权，但存在暴力内容", "compliance_rejected"),
    ("false", "存在侵权IP", "compliance_rejected"),
    (True, "存在侵权IP", "compliance_rejected"),
    (False, "未通过检查", "unclassified_rejected"),
    (False, "", "manual"), (True, "", "manual"),
])
def test_review_routing_preserves_provider_fields(safe, reason, route):
    raw = json.dumps({"safe": safe, "reason": reason}, ensure_ascii=False)
    result = ordinary_result(raw)
    assert result["route"] == route
    assert result["raw_response"] == raw
    assert result["reason"] == reason
    assert result["provider_safe"] == safe


@pytest.mark.parametrize("raw", ["not JSON", '{"safe":', '{"safe":"true","reason":""}',
                                   '```json\n{"safe":true,"reason":"原创"}\n```',
                                   '{"safe":true,"safe":false,"reason":"构图差"}'])
def test_http_success_unparseable_content_needs_human(raw):
    result = ordinary_result(raw)
    assert result["route"] == "manual" and result["review_valid"] is False
    assert result["raw_response"] == raw


@pytest.fixture
def review_scope(data_dirs, monkeypatch):
    monkeypatch.setenv("TRIAL_MODE_ENABLED", "0")
    monkeypatch.setenv("F1_REAL_ENABLED", "0")
    monkeypatch.setattr(config.settings, "content_review_enabled", True)
    sid = uuid4().hex
    meta = session_store.create_session(SessionMeta(session_id=sid, owner_id="fixture-owner", idea="local fixture"))
    eid, _ = execution_store.claim_session(sid, "reference_generation", "generate", {})
    recovery.capture(eid, recovery.session_input(meta, "reference_generation", {}))
    path = config.IMAGE_DIR / sid / "fixture.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 24), "green").save(path)
    token = execution_store._current_execution.set(eid)
    yield sid, eid, path
    execution_store._current_execution.reset(token)


def test_exact_prompt_raw_and_provider_decision_are_durable(review_scope):
    sid, eid, path = review_scope
    raw = '{"safe":false,"reason":"构图质量较差"}'
    class FakeVLM:
        model = "fake-observed-model"
        def review(self, received_path, instruction):
            assert received_path == str(path) and instruction == INSTRUCTION
            return raw
    result = ContentReviewer(FakeVLM()).review_image(str(path))
    record = review_store.records(sid, "fixture-owner")[0]
    assert record["instruction"] == INSTRUCTION and record["model"] == "fake-observed-model"
    assert record["execution_id"] == eid and record["image_sha256"] == review_store.file_sha(path)
    assert record["evidence"]["raw_response"] == raw
    assert record["evidence"]["parsed_safe"] is False
    assert record["evidence"]["parsed_reason"] == "构图质量较差"
    assert result["safe"] is False and record["status"] == "pending"
    assert review_store.records(sid, "other-owner") == []


def test_original_response_survives_unknown_cost_before_business_parse(review_scope, monkeypatch):
    sid, _, path = review_scope
    raw = '{"safe":false,"reason":"构图质量较差"}'
    monkeypatch.setattr(config.settings, "mode_mock", False)
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "local-http-double")
    monkeypatch.setattr(cost_control, "reserve", lambda *args, **kwargs: {})
    posts = []
    class Response:
        status_code = 200
        def json(self):
            return {"id": "fake-response-id", "model": "qwen3-vl-plus", "usage": {},
                    "choices": [{"message": {"content": raw}}]}
    def fake_post(*args, **kwargs):
        posts.append(kwargs["json"])
        return Response()
    def unknown_cost(*args, **kwargs):
        record = review_store.records(sid, "fixture-owner")[0]
        assert record["evidence"]["raw_response"] == raw
        assert record["evidence"]["parsed_safe"] is False
        assert record["evidence"]["parsed_reason"] == "构图质量较差"
        return {"status": "uncertain"}
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(cost_control, "settle", unknown_cost)
    with pytest.raises(AppError, match="实际用量") as caught:
        ContentReviewer(VLMClient()).review_image(str(path))
    assert caught.value.code == "BUDGET_USAGE_UNCONFIRMED" and len(posts) == 1
    record = review_store.records(sid, "fixture-owner")[0]
    assert record["status"] == "failed" and record["route"] == "system_error"
    assert record["evidence"]["raw_response"] == raw
    assert record["evidence"]["manual_decision"] is None


@pytest.mark.parametrize("failure", ["http500", "timeout"])
def test_system_failures_cannot_enter_human_route(review_scope, monkeypatch, failure):
    sid, _, path = review_scope
    monkeypatch.setattr(config.settings, "mode_mock", False)
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "local-http-double")
    monkeypatch.setattr(cost_control, "reserve", lambda *args, **kwargs: {})
    monkeypatch.setattr(cost_control, "settle", lambda *args, **kwargs: {"status": "uncertain"})
    calls = []
    def fake_post(*args, **kwargs):
        calls.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        return type("Response", (), {"status_code": 500})()
    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(AppError):
        ContentReviewer(VLMClient()).review_image(str(path))
    record = review_store.records(sid, "fixture-owner")[0]
    assert len(calls) == 1 and record["status"] == "failed" and record["route"] == "system_error"
    assert record["evidence"]["raw_response"] is None and review_store.pending(sid, "fixture-owner") is None


def test_strict_protocol_keeps_original_structure_gate(review_scope):
    sid, _, path = review_scope
    raw = json.dumps({"safe": True, "reason": "数量正确但多视图", "person_count": 1,
                      "multi_view_layout": True, "floating_head": False, "detached_limbs": False,
                      "text_or_signage": False}, ensure_ascii=False)
    class FakeVLM:
        def review(self, *args): return raw
    result = ContentReviewer(FakeVLM()).review_image(str(path), expected_person_count=1)
    assert result["safe"] is False and result["accepted"] is False and result["content_safe"] is True
    assert result["route"] == "structural_rejected" and result["raw_response"] == raw
    record = review_store.records(sid, "fixture-owner")[0]
    assert record["evidence"]["parsed_safe"] is True and record["status"] == "blocked"
