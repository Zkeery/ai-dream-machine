"""第三批内测门禁：事务配额、预算预留、幂等任务与待对账。

Mock 估价只用于验证拒绝门槛，不能作为真实模型价格或实扣。
已有 model_cost_ledger 只读，所有金额用整数 micro-CNY 保存。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import nullcontext
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from app.core.errors import AppError
from app.services import db

FULL_MESSAGE = "名额已满，可查看样片并填写问卷"
PENDING_MESSAGE = "待对账：结果尚不确定，请勿重试或重新提交，请联系作者核对"
_SCALE = Decimal(1_000_000)
_HELD = ("reserved", "pending_reconciliation")


def enabled() -> bool:
    return os.getenv("TRIAL_MODE_ENABLED", "true").strip().lower() in {"true", "1", "yes"}


def _micros(value) -> int:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError
        return int((number * _SCALE).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise AppError("TRIAL_CONFIG_INVALID", "内测费用配置无效，请联系作者", 503) from None


def _cny(value: int) -> float:
    return float(Decimal(value) / _SCALE)


def limits(user_id: str | None = None) -> dict:
    from app.services import f1_controls
    try:
        count = int(os.getenv("TRIAL_USER_GENERATION_LIMIT", "1"))
        if count < 0:
            raise ValueError
    except ValueError:
        raise AppError("TRIAL_CONFIG_INVALID", "内测配额配置无效，请联系作者", 503) from None
    return {"user_limit": 1 if f1_controls.allowed(user_id) else count,
            "max_estimate_micros": f1_controls.cap_micros() if f1_controls.allowed(user_id) else _micros(os.getenv("TRIAL_MAX_ESTIMATE_CNY", "12")),
            "site_fuse_micros": _micros(os.getenv("TRIAL_SITE_FUSE_CNY", "38"))}


def _ensure(conn) -> None:
    # Do not use executescript: it would commit a caller's BEGIN IMMEDIATE.
    conn.execute("""CREATE TABLE IF NOT EXISTS trial_generations (
        task_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, kind TEXT NOT NULL,
        request_key TEXT, input_hash TEXT NOT NULL,
        estimated_micros INTEGER NOT NULL, actual_micros INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL, status TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS trial_generation_request ON trial_generations(owner_id,request_key) WHERE request_key IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS trial_generation_owner ON trial_generations(owner_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS trial_invites (
        label TEXT PRIMARY KEY, code_sha256 TEXT UNIQUE NOT NULL, created_at REAL NOT NULL)""")


def init_trial() -> None:
    with db.connect() as conn:
        _ensure(conn)


def require_consent(user_id: str) -> None:
    if not enabled():
        return
    from app.services.trial_events import consented
    if not consented(user_id):
        raise AppError("CONSENT_REQUIRED", "请先阅读知情说明并同意后再开始创作", 403)


def _mock_mode() -> bool:
    # Imported lazily so pure SQLite unit tests can inject config safely.
    from app.core import config
    return bool(getattr(config.settings, "mode_mock", False))


def estimate_for(kind: str, user_id: str | None = None) -> float:
    """Explicit mock budget fixture; real estimates must be confirmed in F1 later."""
    if not _mock_mode():
        from app.services import f1_controls
        if f1_controls.allowed(user_id) and kind == "three_shot":
            return 12.468
        raise AppError("TRIAL_REAL_DISABLED", "当前内测只开放免费演示，真实生成尚未开启", 403)
    suffix = "COMIC" if kind == "comic" else "THREE_SHOT"
    return _cny(_micros(os.getenv(f"MOCK_TRIAL_{suffix}_ESTIMATE_CNY", "1")))


def _hash(kind: str, value: dict | None) -> str:
    payload = json.dumps({"kind": kind, "input": value or {}}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _existing_actual(conn) -> int:
    """Read verified paid amounts without changing the historical ledger."""
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_cost_ledger'").fetchone()
    if not table:
        return 0
    columns = {row[1] for row in conn.execute("PRAGMA table_info(model_cost_ledger)")}
    if "provider_paid_micros" not in columns:
        return 0
    if "execution_id" in columns:
        return int(conn.execute("SELECT COALESCE(SUM(MAX(COALESCE(l.provider_paid_micros,0),0)),0) FROM model_cost_ledger l "
            "WHERE NOT EXISTS (SELECT 1 FROM executions e JOIN trial_generations g ON g.task_id=e.entity_id WHERE e.execution_id=l.execution_id AND g.mode='real')").fetchone()[0])
    return int(conn.execute("SELECT COALESCE(SUM(MAX(COALESCE(provider_paid_micros,0),0)),0) FROM model_cost_ledger").fetchone()[0])


def _totals(conn) -> tuple[int, int]:
    actual = int(conn.execute("SELECT COALESCE(SUM(actual_micros),0) FROM trial_generations").fetchone()[0]) + _existing_actual(conn)
    held = int(conn.execute("SELECT COALESCE(SUM(estimated_micros),0) FROM trial_generations WHERE status IN (?,?)", _HELD).fetchone()[0])
    return actual, held


def _public(row, *, created: bool = False) -> dict:
    return {"task_id": row["task_id"], "created": created, "kind": row["kind"],
            "status": row["status"], "estimated_cny": _cny(row["estimated_micros"]),
            "actual_cny": _cny(row["actual_micros"]), "mock": row["mode"] == "mock"}


def reserve_generation(user_id: str, task_id: str, kind: str, estimate_cny,
                       *, request_key: str | None = None,
                       input_payload: dict | None = None, _conn=None,
                       _consent_checked: bool = False) -> dict:
    """Reserve once before model dispatch. A duplicate request never dispatches again."""
    if not _consent_checked:
        require_consent(user_id)
    mode = "mock" if _mock_mode() else "real"
    if mode == "real":
        from app.services import f1_controls
        if not f1_controls.enabled():
            raise AppError("TRIAL_REAL_DISABLED", "当前内测只开放免费演示，真实生成尚未开启", 403)
        f1_controls.require_user(user_id)
        if kind != "three_shot":
            raise AppError("F1_SCOPE_DISABLED", "F1仅允许单条3镜短视频", 403)
    if not user_id or not task_id:
        raise AppError("VALIDATION_ERROR", "生成必须关联已登录账号与唯一任务号", 422)
    if request_key is not None and (not request_key.strip() or len(request_key) > 128):
        raise AppError("VALIDATION_ERROR", "唯一任务号需为 1 到 128 个字符", 422)
    estimate = _micros(estimate_cny)
    settings = limits(user_id)
    fingerprint = _hash(kind, input_payload)
    now = time.time()
    with (db.connect() if _conn is None else nullcontext(_conn)) as conn:
        if _conn is None:
            conn.execute("BEGIN IMMEDIATE")
        _ensure(conn)
        row = conn.execute("SELECT * FROM trial_generations WHERE task_id=?", (task_id,)).fetchone()
        if row is None and request_key is not None:
            row = conn.execute("SELECT * FROM trial_generations WHERE owner_id=? AND request_key=?", (user_id, request_key)).fetchone()
        if row is not None:
            if row["owner_id"] != user_id or row["input_hash"] != fingerprint:
                raise AppError("IDEMPOTENCY_CONFLICT", "唯一任务号对应的创作内容不同，请联系作者", 409)
            return _public(row)
        count = (f1_controls.approved_count(conn, user_id) if mode == "real" else
                 conn.execute("SELECT COUNT(*) FROM trial_generations WHERE owner_id=? AND mode=?", (user_id, mode)).fetchone()[0])
        if count >= settings["user_limit"]:
            raise AppError("TRIAL_QUOTA_EXHAUSTED", FULL_MESSAGE, 403)
        actual, held = _totals(conn)
        if (estimate > settings["max_estimate_micros"] or
                actual >= settings["site_fuse_micros"] or
                actual + held + estimate > settings["site_fuse_micros"]):
            raise AppError("TRIAL_BUDGET_EXHAUSTED", FULL_MESSAGE, 403)
        if mode == "real":
            f1_controls.claim_task(conn, user_id, task_id)
        conn.execute("INSERT INTO trial_generations VALUES(?,?,?,?,?,?,0,?,'reserved',?,?)",
                     (task_id, user_id, kind, request_key, fingerprint, estimate, mode, now, now))
        return _public(conn.execute("SELECT * FROM trial_generations WHERE task_id=?", (task_id,)).fetchone(), created=True)


def elapsed_seconds(task_id: str) -> float:
    with db.connect() as conn:
        _ensure(conn)
        row = conn.execute("SELECT created_at FROM trial_generations WHERE task_id=?", (task_id,)).fetchone()
    return max(0, round(time.time() - row[0], 3)) if row else 0.0


def generation(task_id: str, user_id: str | None = None) -> dict | None:
    with db.connect() as conn:
        _ensure(conn)
        row = conn.execute("SELECT * FROM trial_generations WHERE task_id=?", (task_id,)).fetchone()
    if row is None or (user_id is not None and row["owner_id"] != user_id):
        return None
    return _public(row)


def mark_completed(task_id: str, actual_cny=0, *, mock: bool = True) -> None:
    amount = _micros(actual_cny)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure(conn)
        row = conn.execute("SELECT status,mode,owner_id FROM trial_generations WHERE task_id=?", (task_id,)).fetchone()
        if row and row["status"] == "pending_reconciliation":
            raise AppError("TRIAL_PENDING_RECONCILIATION", PENDING_MESSAGE, 409)
        if row and row["mode"] == "real":
            from app.services import f1_controls
            f1_controls.require_user(row["owner_id"])
            amount = f1_controls.accounting_micros(conn, task_id)
            if amount > f1_controls.cap_micros():
                raise AppError("F1_HARD_CAP_EXCEEDED", "任务记账金额超过硬上限，需核对", 409)
        elif not mock or amount != 0:
            raise AppError("TRIAL_REAL_DISABLED", "本次 mock 验收禁止真实调用或实扣", 403)
        conn.execute("UPDATE trial_generations SET actual_micros=?,status='completed',updated_at=? WHERE task_id=?", (amount, time.time(), task_id))


def mark_pending_reconciliation(task_id: str) -> None:
    with db.connect() as conn:
        _ensure(conn)
        conn.execute("UPDATE trial_generations SET status='pending_reconciliation',updated_at=? WHERE task_id=? AND status!='completed'", (time.time(), task_id))


def recover_interrupted_reservations() -> int:
    """Run after execution_store.recover_unfinished on startup; never resend work."""
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure(conn)
        result = conn.execute("""UPDATE trial_generations SET status='pending_reconciliation',updated_at=?
            WHERE status='reserved' AND (
                NOT EXISTS (SELECT 1 FROM executions e WHERE e.entity_id=trial_generations.task_id)
                OR EXISTS (SELECT 1 FROM executions e WHERE e.entity_id=trial_generations.task_id
                    AND e.operation NOT IN ('save','select') AND e.status IN ('failed','interrupted')))
            AND NOT EXISTS (SELECT 1 FROM executions e WHERE e.entity_id=trial_generations.task_id
                AND e.status IN ('pending','running'))""", (time.time(),))
        return result.rowcount


def require_no_retry(task_id: str) -> None:
    if enabled():
        # Even failed historical attempts cannot resend in this restricted trial.
        raise AppError("TRIAL_RETRY_DISABLED", PENDING_MESSAGE, 409)


def overview(user_id: str) -> dict:
    settings = limits(user_id)
    mode = "mock" if _mock_mode() else "real"
    with db.connect() as conn:
        _ensure(conn)
        count = conn.execute("SELECT COUNT(*) FROM trial_generations WHERE owner_id=? AND mode=?", (user_id, mode)).fetchone()[0]
        if mode == "real":
            from app.services import f1_controls
            count = f1_controls.approved_count(conn, user_id) if f1_controls.allowed(user_id) else settings["user_limit"]
        actual, held = _totals(conn)
        rows = conn.execute("SELECT * FROM trial_generations WHERE owner_id=? ORDER BY created_at", (user_id,)).fetchall()
        label = None
        user = conn.execute("SELECT invite_code FROM users WHERE user_id=?", (user_id,)).fetchone()
        if user:
            code_hash = hashlib.sha256(user["invite_code"].encode()).hexdigest()
            invitation = conn.execute("SELECT label FROM trial_invites WHERE code_sha256=?", (code_hash,)).fetchone()
            label = invitation["label"] if invitation else None
    exhausted = count >= settings["user_limit"] or actual >= settings["site_fuse_micros"] or actual + held >= settings["site_fuse_micros"]
    if mode == "real":
        from app.services import f1_controls
        exhausted = exhausted or not f1_controls.allowed(user_id)
    return {"invite_label": label, "generation_limit": settings["user_limit"],
            "used": count, "remaining": max(0, settings["user_limit"] - count),
            "max_estimate_cny": _cny(settings["max_estimate_micros"]),
            "site_actual_cny": _cny(actual), "site_reserved_cny": _cny(held),
            "site_fuse_cny": _cny(settings["site_fuse_micros"]),
            "blocked": exhausted, "blocked_message": FULL_MESSAGE if exhausted else None,
            "tasks": [_public(row) for row in rows]}
