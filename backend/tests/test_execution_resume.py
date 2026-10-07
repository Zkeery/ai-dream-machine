"""Recovery claims preserve exact input and never evade ownership or quotas."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core import config
from app.core.errors import AppError
from app.schemas.session import SessionMeta
from app.schemas.task import TaskMeta
from app.services import db, execution_store as executions, session_store


@pytest.fixture(autouse=True)
def isolated(data_dirs):
    pass


def _source(sid="story", owner="alice", payload=None):
    session_store.create_session(SessionMeta(session_id=sid, owner_id=owner, idea="original", current_stage="script_generation"))
    eid, _ = executions.claim_session(sid, "script_generation", "regenerate", payload or {"prompt": "original"})
    executions.fail(eid, "INTERRUPTED", "test interrupted", interrupted=True)
    return eid


def test_resume_preserves_original_operation_payload_hash():
    source = _source(payload={"prompt": "original", "target_ids": ["one"]})
    eid, created = executions.claim_resume(source, "alice", "retry-once")
    assert created is True
    assert executions.request_payload(eid) == {"prompt": "original", "target_ids": ["one"]}
    with db.connect() as conn:
        a = conn.execute("SELECT * FROM executions WHERE execution_id=?", (source,)).fetchone()
        b = conn.execute("SELECT * FROM executions WHERE execution_id=?", (eid,)).fetchone()
    assert b["input_hash"] == a["input_hash"]
    assert b["operation"] == "regenerate"
    assert b["stage"] == a["stage"]
    assert executions.claim_resume(source, "alice", "retry-once") == (eid, False)


def test_resume_claim_race_returns_one_worker():
    source = _source()
    with ThreadPoolExecutor(max_workers=5) as pool:
        values = list(pool.map(lambda i: executions.claim_resume(source, "alice", f"retry{i}"), range(5)))
    assert len({x[0] for x in values}) == 1
    assert sum(x[1] for x in values) == 1


def test_resume_checks_owner_and_tombstone():
    source = _source()
    with pytest.raises(AppError) as exc:
        executions.claim_resume(source, "bob")
    assert exc.value.status_code == 404
    session_store.delete_session("story", "alice")
    with pytest.raises(AppError) as exc:
        executions.claim_resume(source, "alice")
    assert exc.value.code == "SESSION_NOT_FOUND"


def test_completed_execution_cannot_resume():
    source = _source()
    with db.connect() as conn:
        conn.execute("UPDATE executions SET status='completed' WHERE execution_id=?", (source,))
    with pytest.raises(AppError) as exc:
        executions.claim_resume(source, "alice")
    assert exc.value.code == "RESUME_NOT_AVAILABLE"


def test_resume_obeys_account_concurrency(monkeypatch):
    source = _source()
    monkeypatch.setattr(config.settings, "max_active_executions_per_user", 1)
    session_store.create_session(SessionMeta(session_id="other", owner_id="alice", idea="other"))
    executions.claim_session("other", "script_generation", "generate", {})
    with pytest.raises(AppError) as exc:
        executions.claim_resume(source, "alice")
    assert exc.value.code == "USER_CONCURRENCY_LIMIT"


def test_resume_reuses_completed_recovery_instead_of_new_paid_worker():
    source = _source()
    eid, _ = executions.claim_resume(source, "alice")
    executions.finish(eid, {"type": "done"}, "completed")
    assert executions.claim_resume(source, "alice") == (eid, False)


def test_task_resume_preserves_task_input():
    meta = TaskMeta(task_id="tool", owner_id="alice", type="literary_video", input={"text": "original", "model_selection": {"text": "qwen3.5-plus"}})
    _, source, _ = executions.create_task(meta, "create")
    executions.fail(source, "INTERRUPTED", "test", interrupted=True)
    eid, created = executions.claim_resume(source, "alice")
    assert created is True
    assert executions.request_payload(eid) == meta.input


def test_old_execution_without_payload_is_explicitly_not_resumable():
    source = _source()
    with db.connect() as conn:
        conn.execute("DELETE FROM execution_requests WHERE execution_id=?", (source,))
    with pytest.raises(AppError) as exc:
        executions.claim_resume(source, "alice")
    assert exc.value.code == "RESUME_INPUT_MISSING"


def test_resume_alias_key_still_deduplicates_after_recovery_failure():
    source = _source()
    eid, _ = executions.claim_resume(source, "alice", "tab-one")
    assert executions.claim_resume(source, "alice", "tab-two") == (eid, False)
    executions.fail(eid, "INTERRUPTED", "test", interrupted=True)
    assert executions.claim_resume(source, "alice", "tab-two") == (eid, False)
    new_id, created = executions.claim_resume(source, "alice", "explicit-new-attempt")
    assert created is True
    assert new_id != eid


def test_changed_stage_cannot_resume_old_execution():
    source = _source()
    meta = session_store.load_session("story")
    meta.current_stage = "character_design"
    session_store.touch(meta)
    with pytest.raises(AppError) as exc:
        executions.claim_resume(source, "alice")
    assert exc.value.code == "RESUME_INPUT_CHANGED"
