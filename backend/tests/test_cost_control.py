"""Real isolated SQLite budget/concurrency contracts; no paid provider traffic."""
import json
import asyncio
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from PIL import Image

from app.core import config
from app.core.errors import AppError
from app.models.llm_client import LLMClient
from app.models.vlm_client import VLMClient
from app.schemas.session import SessionMeta
from app.services import cost_control as costs, db, execution_store as executions, session_store


@pytest.fixture(autouse=True)
def configured_budget(data_dirs, monkeypatch, isolated_client_budget):
    # These synthetic rates exist only in this isolated fixture, not production.
    rates = {
        "text": {"kind": "text", "input_per_million_cny": 10, "output_per_million_cny": 20, "source": "test://rates"},
        "image": {"kind": "image", "image_cny": 30, "source": "test://rates"},
        "video": {"kind": "video", "video_second_cny": {"720P": 1, "1080P": 2}, "source": "test://rates"},
    }
    rates["qwen3.5-plus"] = rates["text"]
    rates["qwen3-vl-flash"] = rates["text"]
    monkeypatch.setattr(config.settings, "model_cost_rates_json", json.dumps(rates))
    monkeypatch.setattr(config.settings, "monthly_budget_cny", "500")
    monkeypatch.setattr(config.settings, "monthly_user_budget_cny", "100")
    monkeypatch.setattr(config.settings, "max_active_executions", 2)
    monkeypatch.setattr(config.settings, "max_active_executions_per_user", 1)
    monkeypatch.setattr(config.settings, "aihubmix_api_key", "test-offline")
    monkeypatch.setattr(config.settings, "llm_model", "qwen3.5-plus")
    monkeypatch.setattr(config.settings, "vlm_model", "qwen3-vl-flash")


def _session(sid, owner="alice"):
    return session_store.create_session(SessionMeta(session_id=sid, owner_id=owner, idea="隔离验收"))


def _claim(sid):
    return executions.claim_session(sid, "script_generation", "generate", {})


def _attempt(fn):
    try:
        return fn()
    except AppError as exc:
        return exc.code


def test_budget_reserve_is_transactional_under_race():
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda i: _attempt(lambda: costs.reserve(str(i), "image", "image", {"images": 1}, owner_id="alice")), range(12)))
    assert sum(isinstance(x, dict) for x in results) == 3
    assert results.count("USER_BUDGET_EXCEEDED") == 9
    assert costs.usage("alice")["reserved_cny"] == 90


def test_reservation_idempotency_and_unknown_are_not_free():
    first = costs.reserve("one", "image", "image", {"images": 1}, owner_id="alice")
    second = costs.reserve("one", "image", "image", {"images": 1}, owner_id="alice")
    assert second["reused"] is True
    costs.settle("one", "uncertain")
    summary = costs.usage("alice")
    assert summary["reserved_cny"] == first["reserved_cny"] == 30
    assert summary["uncertain_calls"] == 1
    assert summary["provider_reported_cny"] is None
    assert summary["remaining_cny"] == 70


@pytest.mark.parametrize("owner,units", [("bob", {"images": 1}), ("alice", {"images": 2})])
def test_call_id_cannot_change_owner_or_input(owner, units):
    costs.reserve("one", "image", "image", {"images": 1}, owner_id="alice")
    with pytest.raises(AppError) as exc:
        costs.reserve("one", "image", "image", units, owner_id=owner)
    assert exc.value.code == "BUDGET_CALL_CONFLICT"


def test_budget_respects_global_limit_across_owners(monkeypatch):
    monkeypatch.setattr(config.settings, "monthly_budget_cny", "50")
    costs.reserve("one", "image", "image", {"images": 1}, owner_id="alice")
    with pytest.raises(AppError) as exc:
        costs.reserve("two", "image", "image", {"images": 1}, owner_id="bob")
    assert exc.value.code == "GLOBAL_BUDGET_EXCEEDED"
    assert costs.usage("bob")["reserved_cny"] == 0


def test_rejection_releases_but_completion_is_not_provider_payment():
    costs.reserve("reject", "image", "image", {"images": 1}, owner_id="alice")
    costs.settle("reject", "rejected")
    costs.reserve("done", "image", "image", {"images": 1}, owner_id="alice")
    costs.settle("done", "completed")
    summary = costs.usage("alice")
    assert summary["reserved_cny"] == 0
    assert summary["calculated_cny"] == 30
    assert summary["provider_reported_cny"] is None
    assert summary["estimated_completed_calls"] == 1
    costs.settle("done", "rejected")
    assert costs.usage("alice")["calculated_cny"] == 30


def test_settlement_uses_reserved_price_snapshot(monkeypatch):
    costs.reserve("text1", "text", "text", {"input_tokens": 1000, "output_tokens": 1000}, owner_id="alice")
    monkeypatch.setattr(config.settings, "model_cost_rates_json", "{}")
    result = costs.settle("text1", actual_units={"input_tokens": 100, "output_tokens": 50})
    assert result["calculated_cny"] == 0.002
    assert result["accounting_basis"] == "price_estimate"
    assert costs.usage("alice")["reserved_cny"] == 0


def test_explicit_provider_charge_is_separate():
    costs.reserve("one", "image", "image", {"images": 1}, owner_id="alice")
    result = costs.settle("one", provider_charge_cny="12.34")
    assert result["provider_paid_cny"] == 12.34
    assert result["accounting_basis"] == "provider_reported"


def test_later_provider_invoice_can_reconcile_estimate_once():
    costs.reserve("one", "image", "image", {"images": 1}, owner_id="alice")
    costs.settle("one", "completed")
    costs.settle("one", provider_charge_cny="12.34")
    assert costs.usage("alice")["calculated_cny"] == 12.34
    assert costs.usage("alice")["provider_reported_cny"] == 12.34
    assert costs.usage("alice")["estimated_completed_calls"] == 0
    with pytest.raises(AppError) as exc:
        costs.settle("one", provider_charge_cny="13")
    assert exc.value.code == "BUDGET_RECONCILIATION_CONFLICT"


def test_month_boundary_keeps_old_unknown_in_original_month(monkeypatch):
    monkeypatch.setattr(costs, "_month", lambda: "2026-09")
    costs.reserve("old", "image", "image", {"images": 3}, owner_id="alice")
    costs.settle("old", "uncertain")
    monkeypatch.setattr(costs, "_month", lambda: "2026-10")
    costs.reserve("new", "image", "image", {"images": 3}, owner_id="alice")
    assert costs.usage("alice", "2026-09")["reserved_cny"] == 90
    assert costs.usage("alice", "2026-10")["reserved_cny"] == 90
    costs.settle("old", "completed")
    assert costs.usage("alice", "2026-10")["reserved_cny"] == 90


def test_billing_month_uses_shanghai_boundary():
    assert costs._month(datetime.fromisoformat("2026-09-30T15:59:59+00:00").timestamp()) == "2026-09"
    assert costs._month(datetime.fromisoformat("2026-09-30T16:00:00+00:00").timestamp()) == "2026-10"


@pytest.mark.parametrize("model,kind,units", [("unknown", "image", {"images": 1}), ("video", "video", {"seconds": 5, "resolution": "4K"})])
def test_unpriced_model_or_spec_is_blocked(model, kind, units):
    with pytest.raises(AppError) as exc:
        costs.reserve("noquote", model, kind, units, owner_id="alice")
    assert exc.value.code == "BUDGET_PRICE_UNCONFIGURED"
    assert costs.usage("alice")["reserved_cny"] == 0


def test_per_account_concurrency_race_and_same_request_reuse():
    _session("first")
    _session("second")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: _attempt(lambda: _claim(s)), ["first", "second"]))
    assert sum(isinstance(x, tuple) for x in results) == 1
    assert "USER_CONCURRENCY_LIMIT" in results
    winner = "first" if isinstance(results[0], tuple) else "second"
    again, created = _claim(winner)
    assert created is False
    assert again == next(x[0] for x in results if isinstance(x, tuple))


def test_global_concurrency_is_transactional():
    for i in range(4):
        _session(f"s{i}", f"owner{i}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: _attempt(lambda: _claim(f"s{i}")), range(4)))
    assert sum(isinstance(x, tuple) for x in results) == 2
    assert results.count("GLOBAL_CONCURRENCY_LIMIT") == 2


def test_quota_releases_after_execution_failure():
    _session("first")
    _session("second")
    eid, _ = _claim("first")
    executions.fail(eid, "TEST", "test")
    assert _claim("second")[1] is True


@pytest.mark.parametrize("operation", ["save", "select"])
def test_non_generation_edits_do_not_need_or_consume_quota(operation):
    _session("active-alice", "alice")
    _session("active-bob", "bob")
    _claim("active-alice")
    _claim("active-bob")
    _session("editing", "alice")
    eid, created = executions.claim_session("editing", "script_generation", operation, {"artifact": {"title": "edit"}})
    assert created is True
    assert costs.usage("alice")["concurrency"]["active"] == 1
    assert costs.usage(None)["concurrency"]["active"] == 2
    # The same project remains protected against concurrent edits/generation.
    with pytest.raises(AppError) as exc:
        executions.claim_session("active-alice", "script_generation", operation, {})
    assert exc.value.code == "SESSION_RUNNING"


def test_stage_confirmation_works_when_generation_quota_is_full(orch):
    _session("active-alice", "alice")
    _session("active-bob", "bob")
    _claim("active-alice")
    _claim("active-bob")
    meta = session_store.create_session(SessionMeta(session_id="confirm", owner_id="alice", idea="done",
                                                   current_stage="script_generation", status="stage_completed",
                                                   stages_completed=["script_generation"]))
    continued = orch.continue_session(meta.session_id)
    assert continued.current_stage == "character_design"
    assert continued.status == "idle"


@pytest.mark.asyncio
async def test_cancelled_worker_thread_cannot_retry_paid_llm(monkeypatch):
    _session("cancelled", "alice")
    eid, _ = _claim("cancelled")
    at_retry, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls, errors = [], []

    def post(*args, **kwargs):
        calls.append(kwargs)
        return httpx.Response(503, json={"error": "temporary"})

    def pause_retry(*args):
        at_retry.set()
        release.wait(5)

    def invoke():
        try:
            LLMClient().generate("system", "user")
        except AppError as exc:
            errors.append(exc.code)
        finally:
            finished.set()

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr("app.models.llm_client.time.sleep", pause_retry)
    token = executions._current_execution.set(eid)
    try:
        thread_task = asyncio.create_task(asyncio.to_thread(invoke))
        assert await asyncio.to_thread(at_retry.wait, 2)
        executions.fail(eid, "INTERRUPTED", "cancelled", interrupted=True)
        thread_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await thread_task
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    finally:
        release.set()
        executions._current_execution.reset(token)
    assert len(calls) == 1
    assert errors == ["BUDGET_EXECUTION_INACTIVE"]
    assert costs.usage("alice")["uncertain_calls"] == 1


@pytest.mark.parametrize("client_type", ["llm", "vlm"])
def test_insufficient_budget_never_calls_text_or_review_provider(monkeypatch, tmp_path, client_type):
    _session("work")
    eid, _ = _claim("work")
    monkeypatch.setattr(config.settings, "monthly_user_budget_cny", "0")
    calls = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append(kw))
    token = executions._current_execution.set(eid)
    try:
        with pytest.raises(AppError) as exc:
            if client_type == "llm":
                LLMClient().generate("system", "user")
            else:
                image = tmp_path / "review.png"
                Image.new("RGB", (20, 20)).save(image)
                VLMClient().review(str(image), "review")
        assert exc.value.code == "USER_BUDGET_EXCEEDED"
        assert calls == []
    finally:
        executions._current_execution.reset(token)


def test_llm_reserves_max_output_then_calculates_returned_usage(monkeypatch):
    _session("work")
    eid, _ = _claim("work")
    seen = []
    def post(*args, **kwargs):
        seen.append(kwargs["json"])
        assert costs.usage("alice")["reserved_cny"] > 0
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 20, "completion_tokens": 10}})
    monkeypatch.setattr(httpx, "post", post)
    token = executions._current_execution.set(eid)
    try:
        assert LLMClient().generate("system", "user") == "ok"
    finally:
        executions._current_execution.reset(token)
    assert seen[0]["max_tokens"] == config.settings.llm_max_output_tokens
    assert costs.usage("alice")["calculated_cny"] == 0.0004
    assert costs.usage("alice")["reserved_cny"] == 0


def test_llm_lost_response_keeps_reservation(monkeypatch):
    _session("work")
    eid, _ = _claim("work")
    def fail(*args, **kwargs):
        raise httpx.ReadTimeout("lost")
    monkeypatch.setattr(httpx, "post", fail)
    token = executions._current_execution.set(eid)
    try:
        with pytest.raises(AppError, match="结果未确认"):
            LLMClient().generate("system", "user")
    finally:
        executions._current_execution.reset(token)
    assert costs.usage("alice")["uncertain_calls"] == 1
    assert costs.usage("alice")["reserved_cny"] > 0


def test_usage_does_not_mix_accounts():
    costs.reserve("a", "image", "image", {"images": 1}, owner_id="alice")
    costs.reserve("b", "image", "image", {"images": 2}, owner_id="bob")
    assert costs.usage("alice")["reserved_cny"] == 30
    assert costs.usage("bob")["reserved_cny"] == 60
    assert costs.usage(None)["reserved_cny"] == 90
    assert costs.usage("alice")["concurrency"]["account_limit"] == 1
    assert "qwen-image-2.0" in costs.usage("alice")["missing_price_models"]


def test_production_reserve_requires_owner(isolated_client_budget):
    with pytest.raises(AppError) as exc:
        isolated_client_budget["reserve"]("unowned", "image", "image", {"images": 1})
    assert exc.value.code == "BUDGET_OWNER_REQUIRED"


def test_stopped_execution_cannot_start_another_paid_call():
    _session("work")
    eid, _ = _claim("work")
    executions.fail(eid, "INTERRUPTED", "test", interrupted=True)
    with pytest.raises(AppError) as exc:
        costs.reserve("late-post", "image", "image", {"images": 1}, execution_id=eid)
    assert exc.value.code == "BUDGET_EXECUTION_INACTIVE"


def test_usage_http_is_account_scoped_and_admin_is_protected():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import usage
    from app.api.deps import get_current_user
    from app.core.errors import register_exception_handlers
    costs.reserve("alice-call", "image", "image", {"images": 1}, owner_id="alice")
    costs.reserve("bob-call", "image", "image", {"images": 2}, owner_id="bob")
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(usage.router, prefix="/api")
    with TestClient(app) as client:
        assert client.get("/api/usage").status_code == 401
        app.dependency_overrides[get_current_user] = lambda: "alice"
        response = client.get("/api/usage?owner_id=bob")
        assert response.status_code == 200
        assert response.json()["reserved_cny"] == 30
        assert "bob" not in response.text
        assert client.get("/api/admin/usage").status_code == 403
