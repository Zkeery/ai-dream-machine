"""Transactional generation reservations, estimated charges and explicit unknowns.

Amounts are integer micro-CNY. A calculated charge is not a provider invoice.
Each provider POST must reserve a stable call_id before network I/O. Recovery
polling and downloads reuse that call_id and never reserve another generation.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
import hashlib
import json
import time
from zoneinfo import ZoneInfo

from app.core import config
from app.core.errors import AppError
from app.services import db

_SCALE = Decimal(1_000_000)
_HELD = ("reserved", "uncertain")
_ACTIVE_GENERATION = "status IN ('pending','running') AND operation NOT IN ('save','select')"


def _month(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m")


def _money(value) -> int:
    try:
        decimal = Decimal(str(value))
        if not decimal.is_finite() or decimal < 0:
            raise ValueError
        return int((decimal * _SCALE).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise AppError("BUDGET_CONFIG_INVALID", "费用配置无效，请联系管理员", 503) from None


def _cny(micros: int) -> float:
    return float(Decimal(micros) / _SCALE)


def _provider_usd(value) -> int:
    """Reported USD is not a CNY invoice; keep its own six-decimal field."""
    try:
        if isinstance(value, bool):
            raise ValueError
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        return int((amount * _SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise AppError("BUDGET_PROVIDER_CHARGE_INVALID", "平台实扣金额无效，请核对账单后再继续", 409) from None


def _reported_usd_budget(row, value, source: str | None) -> tuple[int, int, dict]:
    if not isinstance(source, str) or not source:
        raise AppError("BUDGET_PROVIDER_CHARGE_INVALID", "平台实扣缺少可追溯来源，请核对账单后再继续", 409)
    usd_micros = _provider_usd(value)
    rate = json.loads(row["rates"])
    factor = rate.get("budget_conversion_cny_per_usd", 8)
    try:
        conversion = Decimal(str(factor))
        if not conversion.is_finite() or conversion <= 0:
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        raise AppError("BUDGET_CONFIG_INVALID", "费用折算配置无效，请联系管理员", 503)
    charged = _money(Decimal(usd_micros) / _SCALE * conversion)
    return charged, usd_micros, {"currency": "USD", "basis": "provider_reported_charge",
        "source": source, "budget_conversion_cny_per_usd": factor,
        "notice": "Provider reported USD; CNY is a fixed budget conversion, not an actual RMB payment."}


def _ensure(conn) -> None:
    # execute, not executescript: do not implicitly commit a caller's write lock.
    conn.execute("""CREATE TABLE IF NOT EXISTS model_cost_ledger (
        call_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, execution_id TEXT,
        model TEXT NOT NULL, kind TEXT NOT NULL, month TEXT NOT NULL,
        input_hash TEXT NOT NULL, units TEXT NOT NULL, rates TEXT NOT NULL,
        reserved_micros INTEGER NOT NULL, charged_micros INTEGER NOT NULL DEFAULT 0,
        provider_paid_micros INTEGER, status TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_model_cost_month_owner ON model_cost_ledger(month,owner_id)")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(model_cost_ledger)")}
    for name, column_type in (("actual_units", "TEXT"), ("settlement_rates", "TEXT"),
                              ("calculated_usd_micros", "INTEGER"), ("provider_paid_usd_micros", "INTEGER")):
        if name not in columns:
            conn.execute(f"ALTER TABLE model_cost_ledger ADD COLUMN {name} {column_type}")


def check_execution_limits(conn, owner_id: str | None) -> None:
    """Call inside the execution claim's BEGIN IMMEDIATE, after idempotency lookup."""
    global_count = conn.execute(f"SELECT COUNT(*) FROM executions WHERE {_ACTIVE_GENERATION}").fetchone()[0]
    if global_count >= config.settings.max_active_executions:
        raise AppError("GLOBAL_CONCURRENCY_LIMIT", "当前生成任务已达平台并发上限，请稍后再试", 429)
    user_count = conn.execute(f"SELECT COUNT(*) FROM executions WHERE owner_id IS ? AND {_ACTIVE_GENERATION}",
                              (owner_id,)).fetchone()[0]
    if user_count >= config.settings.max_active_executions_per_user:
        raise AppError("USER_CONCURRENCY_LIMIT", "你已有任务正在生成，请等待完成后再开始新任务", 429)


def _rate(model: str, kind: str) -> dict:
    try:
        rates = json.loads(config.settings.model_cost_rates_json)
        rate = rates.get(model) if isinstance(rates, dict) else None
        if not isinstance(rate, dict) or rate.get("kind") != kind:
            raise ValueError
        if rate.get("currency", "CNY") != "CNY" or not rate.get("source"):
            raise ValueError
        return rate
    except (ValueError, TypeError):
        raise AppError("BUDGET_PRICE_UNCONFIGURED", f"模型 {model} 尚未配置可核对的费用估算，暂不能生成，请联系管理员", 503) from None


def _estimate(kind: str, units: dict, rate: dict, *, allow_zero: bool = False) -> int:
    def number(key: str) -> Decimal:
        value = Decimal(str(units[key]))
        if not value.is_finite() or value < 0:
            raise ValueError
        return value

    def price(value) -> Decimal:
        value = Decimal(str(value))
        if not value.is_finite() or value < 0:
            raise ValueError
        return value

    try:
        if kind == "text":
            amount = (number("input_tokens") * price(rate["input_per_million_cny"])
                      + number("output_tokens") * price(rate["output_per_million_cny"])) / _SCALE
        elif kind == "image":
            amount = number("images") * price(rate["image_cny"])
        elif kind == "video":
            selected_price = rate["video_second_cny"]
            if isinstance(selected_price, dict):
                selected_price = selected_price[str(units["resolution"]).upper()]
            amount = number("seconds") * price(selected_price)
        else:
            raise ValueError
        # Missing / negative / free-looking unknown estimates never authorize calls.
        if not amount.is_finite() or amount < 0 or (amount == 0 and not allow_zero):
            raise ValueError
        return _money(amount)
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise AppError("BUDGET_PRICE_UNCONFIGURED", "当前模型或生成规格缺少有效价格，暂不能生成，请联系管理员", 503) from None


def estimate_cny(model: str, kind: str, units: dict) -> float:
    """Read-only price estimate; does not reserve budget or submit a model call."""
    return _cny(_estimate(kind, units, _rate(model, kind)))


def actual_text_units(usage) -> dict | None:
    """Accept observed usage only; a token limit is never evidence of usage."""
    if not isinstance(usage, dict):
        return None
    units = {"input_tokens": usage.get("prompt_tokens"),
             "output_tokens": usage.get("completion_tokens")}
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        return None
    for name, usage_name in (("cached_input_tokens", "cached_tokens"),
                             ("cache_write_input_tokens", "cache_write_tokens")):
        value = details.get(usage_name, usage.get(usage_name))
        if value is not None:
            units[name] = value
    if any(type(value) is not int or value < 0 for value in units.values()):
        return None
    if units.get("cached_input_tokens", 0) + units.get("cache_write_input_tokens", 0) > units["input_tokens"]:
        return None
    return units


def _settlement_amount(kind: str, units: dict, rate: dict) -> tuple[int, int | None, dict]:
    """Price actual usage, independent of the conservative reservation rate.

    USD is rounded to the platform's six decimal display precision, then mapped
    to the fixed CNY budget factor. Neither value is an actual RMB payment.
    A flat CNY table needs an explicit verified-unit-price marker. Conservative
    or unverified price tables may reserve budget, but cannot settle actuals.
    """
    selected = rate.get("settlement")
    if selected is None:
        if rate.get("settlement_basis") == "verified_flat_unit_price":
            return _estimate(kind, units, rate, allow_zero=True), None, rate
        raise AppError("BUDGET_PRICE_UNCONFIGURED", "实际结算价格尚未核定，请核对账单后再继续", 503)
    try:
        if not isinstance(selected, dict) or selected.get("currency") != "USD" or not selected.get("source"):
            raise ValueError

        def count(name: str, default=None) -> int:
            value = units.get(name, default)
            if type(value) is not int or value < 0:
                raise ValueError
            return value

        def price(value) -> Decimal:
            value = Decimal(str(value))
            if not value.is_finite() or value < 0:
                raise ValueError
            return value

        applied = dict(selected)
        if kind == "text":
            input_tokens, output_tokens = count("input_tokens"), count("output_tokens")
            tiers = selected["tiers"]
            matches = [tier for tier in tiers if tier["min_input_tokens"] <= input_tokens <= tier["max_input_tokens"]]
            if len(matches) != 1:
                raise ValueError
            tier = matches[0]
            cached, written = count("cached_input_tokens", 0), count("cache_write_input_tokens", 0)
            if cached + written > input_tokens:
                raise ValueError
            amount = ((input_tokens - cached - written) * price(tier["input_per_million_usd"])
                      + output_tokens * price(tier["output_per_million_usd"])) / _SCALE
            if cached:
                amount += cached * price(tier["cached_input_per_million_usd"]) / _SCALE
            if written:
                amount += written * price(tier["cache_write_input_per_million_usd"]) / _SCALE
            applied["applied_tier"] = tier
        elif kind == "image":
            amount = count("images") * price(selected["image_usd"])
        else:
            raise ValueError
        factor = price(selected["budget_conversion_cny_per_usd"])
        if not factor or not amount.is_finite():
            raise ValueError
        usd_micros = int((amount * _SCALE).to_integral_value(rounding=ROUND_HALF_UP))
        return _money(Decimal(usd_micros) / _SCALE * factor), usd_micros, applied
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise AppError("BUDGET_PRICE_UNCONFIGURED", "实际用量或结算价格不完整，请核对账单后再继续", 503) from None


def require_confirmed_settlement(result: dict | None) -> None:
    if isinstance(result, dict) and result.get("status") == "uncertain":
        raise AppError("BUDGET_USAGE_UNCONFIRMED", "模型已响应，但实际用量或结算价格未确认，已保留费用预留并停止；请核对账单后再继续", 409)


def _totals(conn, month: str, owner_id: str | None = None) -> dict:
    where, args = "month=?", [month]
    if owner_id is not None:
        where += " AND owner_id=?"
        args.append(owner_id)
    row = conn.execute(f"""SELECT
        COALESCE(SUM(CASE WHEN status IN ('reserved','uncertain') THEN reserved_micros ELSE 0 END),0) AS held,
        COALESCE(SUM(CASE WHEN status='completed' THEN charged_micros ELSE 0 END),0) AS charged,
        COALESCE(SUM(provider_paid_micros),0) AS provider_paid,
        SUM(CASE WHEN status='uncertain' THEN 1 ELSE 0 END) AS uncertain,
        SUM(CASE WHEN status='completed' AND provider_paid_micros IS NULL AND provider_paid_usd_micros IS NULL THEN 1 ELSE 0 END) AS estimated
        , SUM(CASE WHEN provider_paid_micros IS NOT NULL THEN 1 ELSE 0 END) AS reported_count
        , COALESCE(SUM(provider_paid_usd_micros),0) AS provider_paid_usd
        , SUM(CASE WHEN provider_paid_usd_micros IS NOT NULL THEN 1 ELSE 0 END) AS usd_reported_count
        FROM model_cost_ledger WHERE {where}""", args).fetchone()
    return {key: int(row[key] or 0) for key in row.keys()}


def _public(row, *, reused: bool = False) -> dict:
    return {"call_id": row["call_id"], "model": row["model"], "kind": row["kind"], "month": row["month"],
            "status": row["status"], "reserved_cny": _cny(row["reserved_micros"]),
            "calculated_cny": _cny(row["charged_micros"]),
            "provider_paid_cny": None if row["provider_paid_micros"] is None else _cny(row["provider_paid_micros"]),
            "accounting_basis": ("provider_reported_usd_budget_conversion" if row["provider_paid_usd_micros"] is not None
                                 else "provider_reported" if row["provider_paid_micros"] is not None else "price_estimate"),
            "provider_paid_usd": None if row["provider_paid_usd_micros"] is None else _cny(row["provider_paid_usd_micros"]),
            "actual_units": None if not row["actual_units"] else json.loads(row["actual_units"]),
            "settlement_rates": None if not row["settlement_rates"] else json.loads(row["settlement_rates"]),
            "calculated_usd": None if row["calculated_usd_micros"] is None else _cny(row["calculated_usd_micros"]),
            "reused": reused}


def reserve(call_id: str, model: str, kind: str, units: dict, *,
            owner_id: str | None = None, execution_id: str | None = None) -> dict:
    from app.services.execution_store import current_execution_id
    execution_id = execution_id or current_execution_id()
    if not call_id or len(call_id) > 128:
        raise AppError("BUDGET_CALL_INVALID", "费用请求标识无效", 400)
    fingerprint = hashlib.sha256(json.dumps({"model": model, "kind": kind, "units": units},
                                ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure(conn)
        if execution_id:
            execution = conn.execute("SELECT owner_id,status FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
            if not execution or (owner_id and execution["owner_id"] != owner_id):
                raise AppError("BUDGET_OWNER_REQUIRED", "生成请求缺少有效账号归属", 403)
            if execution["status"] not in ("pending", "running"):
                raise AppError("BUDGET_EXECUTION_INACTIVE", "当前任务已停止，已阻止新的付费调用", 409)
            owner_id = execution["owner_id"]
        if not owner_id:
            raise AppError("BUDGET_OWNER_REQUIRED", "生成请求缺少账号归属，已阻止付费调用", 403)
        old = conn.execute("SELECT * FROM model_cost_ledger WHERE call_id=?", (call_id,)).fetchone()
        if old:
            if old["owner_id"] != owner_id or old["input_hash"] != fingerprint:
                raise AppError("BUDGET_CALL_CONFLICT", "费用请求标识与原输入不一致", 409)
            return _public(old, reused=True)
        rate = _rate(model, kind)
        estimate = _estimate(kind, units, rate)
        month = _month()
        from app.services import f1_controls
        user_cap = (_cny(f1_controls.cap_micros()) if f1_controls.allowed(owner_id)
                    else config.settings.monthly_user_budget_cny)
        f1_controls.enforce_call(conn, owner_id, execution_id, model, kind, estimate)
        for owner, cap, code, message in (
            (None, config.settings.monthly_budget_cny, "GLOBAL_BUDGET_EXCEEDED", "平台本月生成预算不足，已阻止新的付费调用"),
            (owner_id, user_cap, "USER_BUDGET_EXCEEDED", "你本月的生成额度不足，已阻止新的付费调用"),
        ):
            total = _totals(conn, month, owner)
            if total["held"] + total["charged"] + estimate > _money(cap):
                raise AppError(code, message, 429)
        now = time.time()
        from app.services import f1_controls
        conn.execute("""INSERT INTO model_cost_ledger
                     (call_id,owner_id,execution_id,model,kind,month,input_hash,units,rates,reserved_micros,
                      charged_micros,provider_paid_micros,status,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,0,NULL,'reserved',?,?)""",
                     (call_id, owner_id, execution_id, model, kind, month, fingerprint,
                      json.dumps(units), json.dumps(rate), estimate, now, now))
        return _public(conn.execute("SELECT * FROM model_cost_ledger WHERE call_id=?", (call_id,)).fetchone())


def settle(call_id: str, status: str = "completed", actual_units: dict | None = None,
           provider_charge_cny=None, *, provider_charge_usd=None,
           provider_charge_source: str | None = None) -> dict:
    if status not in ("completed", "rejected", "uncertain"):
        raise AppError("BUDGET_STATUS_INVALID", "费用结算状态无效", 400)
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _ensure(conn)
        row = conn.execute("SELECT * FROM model_cost_ledger WHERE call_id=?", (call_id,)).fetchone()
        if row is None:
            raise AppError("BUDGET_CALL_NOT_FOUND", "费用预留记录不存在", 404)
        if provider_charge_cny is not None and provider_charge_usd is not None:
            raise AppError("BUDGET_RECONCILIATION_CONFLICT", "美元实扣与人民币实付须分开核对，不能同时替代预算记账", 409)
        if row["status"] == "completed" and status == "completed" and provider_charge_usd is not None:
            charged, usd_micros, applied = _reported_usd_budget(row, provider_charge_usd, provider_charge_source)
            if (row["provider_paid_micros"] is not None or
                (row["provider_paid_usd_micros"] is not None and row["provider_paid_usd_micros"] != usd_micros)):
                raise AppError("BUDGET_RECONCILIATION_CONFLICT", "已有供应商结算金额与本次不一致，请人工核对", 409)
            if row["provider_paid_usd_micros"] is not None:
                return _public(row, reused=True)
            conn.execute("""UPDATE model_cost_ledger SET charged_micros=?,provider_paid_usd_micros=?,
                         calculated_usd_micros=?,settlement_rates=?,actual_units=COALESCE(?,actual_units),updated_at=? WHERE call_id=?""",
                         (charged, usd_micros, usd_micros, json.dumps(applied),
                          None if actual_units is None else json.dumps(actual_units), time.time(), call_id))
            return _public(conn.execute("SELECT * FROM model_cost_ledger WHERE call_id=?", (call_id,)).fetchone(), reused=True)
        if row["status"] == "completed" and status == "completed" and provider_charge_cny is not None:
            paid = _money(provider_charge_cny)
            if row["provider_paid_usd_micros"] is not None or (row["provider_paid_micros"] is not None and row["provider_paid_micros"] != paid):
                raise AppError("BUDGET_RECONCILIATION_CONFLICT", "已有供应商结算金额与本次不一致，请人工核对", 409)
            conn.execute("UPDATE model_cost_ledger SET charged_micros=?,provider_paid_micros=?,updated_at=? WHERE call_id=?",
                         (paid, paid, time.time(), call_id))
            return _public(conn.execute("SELECT * FROM model_cost_ledger WHERE call_id=?", (call_id,)).fetchone(), reused=True)
        if row["status"] in ("completed", "rejected"):
            return _public(row, reused=True)
        paid = None if provider_charge_cny is None else _money(provider_charge_cny)
        charged, usd_micros, applied, provider_usd_micros = 0, None, None, None
        if status == "completed":
            if provider_charge_usd is not None:
                try:
                    charged, usd_micros, applied = _reported_usd_budget(row, provider_charge_usd, provider_charge_source)
                    provider_usd_micros = usd_micros
                except AppError:
                    status = "uncertain"
            elif paid is not None:
                charged = paid
            elif not isinstance(actual_units, dict) or actual_units.get("usage_basis") == "provider_completed_requested_spec":
                status = "uncertain"
            else:
                rate = json.loads(row["rates"])
                # Older in-flight reservations may predate the split price table.
                if "settlement" not in rate:
                    try:
                        current = _rate(row["model"], row["kind"])
                        if "settlement" in current:
                            rate = current
                    except AppError:
                        pass
                try:
                    charged, usd_micros, applied = _settlement_amount(row["kind"], actual_units, rate)
                except AppError:
                    status = "uncertain"
        conn.execute("""UPDATE model_cost_ledger SET status=?,charged_micros=?,provider_paid_micros=?,
                     actual_units=?,settlement_rates=?,calculated_usd_micros=?,provider_paid_usd_micros=?,updated_at=? WHERE call_id=?""",
                     (status, charged, paid, None if actual_units is None else json.dumps(actual_units),
                      None if applied is None else json.dumps(applied), usd_micros, provider_usd_micros, time.time(), call_id))
        return _public(conn.execute("SELECT * FROM model_cost_ledger WHERE call_id=?", (call_id,)).fetchone())


def usage(owner_id: str | None, month: str | None = None) -> dict:
    month = month or _month()
    try:
        if datetime.strptime(month, "%Y-%m").strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise AppError("BUDGET_MONTH_INVALID", "月份格式须为 YYYY-MM", 400) from None
    with db.connect() as conn:
        _ensure(conn)
        total = _totals(conn, month, owner_id)
        if owner_id is None:
            active = conn.execute(f"SELECT COUNT(*) FROM executions WHERE {_ACTIVE_GENERATION}").fetchone()[0]
        else:
            active = conn.execute(f"SELECT COUNT(*) FROM executions WHERE owner_id=? AND {_ACTIVE_GENERATION}", (owner_id,)).fetchone()[0]
    limit = _money(config.settings.monthly_user_budget_cny if owner_id is not None else config.settings.monthly_budget_cny)
    groups = {"text": set(config.settings.public_text_models) | {config.settings.vlm_model},
              "image": set(config.settings.public_image_models),
              "video": set(config.settings.public_video_first_frame_models) | set(config.settings.public_video_start_end_models) | set(config.settings.public_video_reference_models) | set(config.settings.public_video_speech_models)}
    missing = []
    for kind, models in groups.items():
        for model in sorted(models):
            try:
                rate = _rate(model, kind)
                for resolution in ("720P", "1080P") if kind == "video" else (None,):
                    units = {"input_tokens": 1, "output_tokens": 1} if kind == "text" else {"images": 1} if kind == "image" else {"seconds": 1, "resolution": resolution}
                    _estimate(kind, units, rate)
            except AppError:
                missing.append(model)
    return {"scope": "account" if owner_id is not None else "platform", "month": month, "currency": "CNY",
            "limit_cny": _cny(limit), "reserved_cny": _cny(total["held"]), "calculated_cny": _cny(total["charged"]),
            "provider_reported_cny": _cny(total["provider_paid"]) if total["reported_count"] else None, "uncertain_calls": total["uncertain"],
            "provider_reported_usd": _cny(total["provider_paid_usd"]) if total["usd_reported_count"] else None,
            "estimated_completed_calls": total["estimated"],
            "remaining_cny": _cny(max(0, limit - total["held"] - total["charged"])),
            "concurrency": {"active": active, "account_limit": config.settings.max_active_executions_per_user,
                            "global_limit": config.settings.max_active_executions},
            "missing_price_models": missing,
            "notice": "预留及估算用于生成限额；按模型用量和价格表计算的费用不是供应商实付账单。未确认的提交保留预留。"}
