"""第三批 SQLite 埋点、同意、问卷与原图留存；不记录凭证明码或媒体链接。"""
from __future__ import annotations

import csv
import hashlib
import json
import time
import uuid
from pathlib import Path

from app.core import config
from app.core.errors import AppError
from app.services import db

POLICY_VERSION = "trial-2026-10-11"
FIELDS = {
    "invite_open": (), "consent_ok": ("policy_version",),
    "create_start": ("kind",), "prompt_submit": ("character_count", "has_reference_image"),
    "estimate_shown": ("estimated_cny", "mode", "project_type"),
    "gen_done": ("task_id", "elapsed_seconds", "actual_cny", "mode"),
    "gen_fail": ("task_id", "error_type", "pending_reconciliation"),
    "quota_block": ("reason",), "paywall_click": ("plan_id", "task_id"), "survey_done": (),
}
SURVEY_FIELDS = ("participant_code", "intent_raw", "independent_completion", "blocker_raw",
    "need_fit", "character_consistency", "visual_quality", "camera_motion", "audio_subtitles",
    "satisfaction", "recommendation", "paywall_clicked", "selected_plan", "willing_monthly_raw",
    "no_pay_reason_raw", "other_raw")
PLANS = [
    {"id": "basic", "label": "基础版", "price_cny": 29, "included_videos": 2,
     "confirmation_required": False, "benefits": "每月含 2 条"},
    {"id": "standard", "label": "标准版", "price_cny": 69, "included_videos": 5,
     "confirmation_required": False, "benefits": "每月含 5 条"},
    {"id": "professional", "label": "专业版", "price_cny": 129, "included_videos": 9,
     "confirmation_required": False, "benefits": "每月含 9 条"},
]


def init_events() -> None:
    with db.connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS trial_events (
          event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, participant_code TEXT NOT NULL,
          event TEXT NOT NULL, created_at REAL NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS trial_event_kind ON trial_events(event,created_at);
        CREATE TABLE IF NOT EXISTS trial_consents (
          user_id TEXT PRIMARY KEY, policy_version TEXT NOT NULL, accepted_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS trial_surveys (
          survey_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL, fields TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trial_deletion_requests (
          request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS trial_upload_retention (
          filename TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at REAL NOT NULL, deleted_at REAL);
        """)


def participant_label(user_id: str) -> str:
    with db.connect() as conn:
        row = conn.execute("SELECT invite_code FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row and conn.execute("SELECT 1 FROM sqlite_master WHERE name='trial_invites'").fetchone():
            digest = hashlib.sha256(row[0].encode()).hexdigest()
            label = conn.execute("SELECT label FROM trial_invites WHERE code_sha256=?", (digest,)).fetchone()
            if label:
                return label[0]
    return "匿名内测用户"


def consented(user_id: str) -> bool:
    init_events()
    with db.connect() as conn:
        row = conn.execute("SELECT policy_version FROM trial_consents WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row[0] == POLICY_VERSION)


def record_event(user_id: str, event: str, payload: dict | None = None) -> str:
    if event not in FIELDS:
        raise AppError("TRIAL_EVENT_INVALID", "不支持的内测事件", 422)
    payload = payload or {}
    if set(payload) - set(FIELDS[event]):
        raise AppError("TRIAL_EVENT_FIELDS", "事件包含未允许的字段", 422)
    # Only small categorical/count values; never arbitrary provider text or URLs.
    for value in payload.values():
        if isinstance(value, (dict, list)) or isinstance(value, str) and (
            len(value) > 128 or "://" in value or "bearer " in value.lower()):
            raise AppError("TRIAL_EVENT_FIELDS", "事件字段不符合隐私范围", 422)
    init_events()
    identifier = uuid.uuid4().hex
    with db.connect() as conn:
        conn.execute("INSERT INTO trial_events VALUES(?,?,?,?,?,?)",
                     (identifier, user_id, participant_label(user_id), event, time.time(),
                      json.dumps(payload, ensure_ascii=False)))
    return identifier


def open_invite(user_id: str) -> None:
    init_events()
    with db.connect() as conn:
        existing = conn.execute("SELECT 1 FROM trial_events WHERE user_id=? AND event='invite_open'", (user_id,)).fetchone()
    if not existing:
        record_event(user_id, "invite_open")


def accept_consent(user_id: str) -> None:
    init_events()
    old = consented(user_id)
    with db.connect() as conn:
        conn.execute("INSERT INTO trial_consents VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                     "policy_version=excluded.policy_version,accepted_at=excluded.accepted_at",
                     (user_id, POLICY_VERSION, time.time()))
    if not old:
        record_event(user_id, "consent_ok", {"policy_version": POLICY_VERSION})


def save_survey(user_id: str, fields: dict) -> str:
    if not consented(user_id):
        raise AppError("CONSENT_REQUIRED", "请先同意知情说明", 403)
    if set(fields) - set(SURVEY_FIELDS) or set(SURVEY_FIELDS) - set(fields):
        raise AppError("TRIAL_SURVEY_FIELDS", "请按原表填写全部16个问卷字段", 422)
    for name in ("need_fit", "character_consistency", "visual_quality", "camera_motion"):
        if type(fields[name]) is not int or not 1 <= fields[name] <= 3:
            raise AppError("TRIAL_SURVEY_SCORE", "五维评分请填写1至3", 422)
    audio = fields["audio_subtitles"]
    if audio != "n/a" and (type(audio) is not int or not 1 <= audio <= 3):
        raise AppError("TRIAL_SURVEY_SCORE", "配音字幕填写1至3或不适用", 422)
    for name, maximum in (("satisfaction", 5), ("recommendation", 10)):
        minimum = 0 if name == "recommendation" else 1
        if type(fields[name]) is not int or not minimum <= fields[name] <= maximum:
            raise AppError("TRIAL_SURVEY_SCORE", "整体评分超出范围", 422)
    if fields["independent_completion"] not in ("yes", "no") or type(fields["paywall_clicked"]) is not bool:
        raise AppError("TRIAL_SURVEY_FIELDS", "请填写是否独立出片和是否点击套餐", 422)
    if fields["selected_plan"] not in ("", None, *(p["id"] for p in PLANS)):
        raise AppError("TRIAL_SURVEY_FIELDS", "套餐档位不存在", 422)
    for name in ("intent_raw", "blocker_raw", "willing_monthly_raw", "no_pay_reason_raw", "other_raw"):
        if not isinstance(fields[name], str) or len(fields[name]) > 4000:
            raise AppError("TRIAL_SURVEY_FIELDS", "原话字段请填写4000字以内文本", 422)
    with db.connect() as conn:
        last_click = conn.execute("SELECT payload FROM trial_events WHERE user_id=? AND event='paywall_click' "
                                  "ORDER BY created_at DESC LIMIT 1", (user_id,)).fetchone()
    click = json.loads(last_click[0]) if last_click else {}
    saved = {**fields, "participant_code": participant_label(user_id),
             "paywall_clicked": bool(last_click), "selected_plan": click.get("plan_id", "")}
    identifier = uuid.uuid4().hex
    with db.connect() as conn:
        conn.execute("INSERT INTO trial_surveys VALUES(?,?,?,?)",
                     (identifier, user_id, time.time(), json.dumps(saved, ensure_ascii=False)))
    record_event(user_id, "survey_done")
    return identifier


def request_deletion(user_id: str) -> str:
    init_events()
    identifier = uuid.uuid4().hex
    with db.connect() as conn:
        conn.execute("INSERT INTO trial_deletion_requests VALUES(?,?,?,'pending')", (identifier, user_id, time.time()))
    return identifier


def register_original(filename: str, user_id: str, created_at: float | None = None) -> None:
    if Path(filename).name != filename:
        raise AppError("UPLOAD_INVALID", "原图路径不合法", 422)
    init_events()
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO trial_upload_retention VALUES(?,?,?,NULL)",
                     (filename, user_id, time.time() if created_at is None else created_at))


def cleanup_originals(now: float | None = None) -> dict:
    init_events()
    now = time.time() if now is None else now
    cutoff = now - 7 * 86400
    removed = 0
    with db.connect() as conn:
        rows = conn.execute("SELECT filename FROM trial_upload_retention WHERE deleted_at IS NULL AND created_at<=?", (cutoff,)).fetchall()
        for row in rows:
            name = row[0]
            if Path(name).name != name:
                continue
            target = config.UPLOAD_DIR / name
            # Never follow a symlink out of the uploads directory.
            if target.is_symlink():
                target.unlink()
            elif target.resolve().parent == config.UPLOAD_DIR.resolve():
                target.unlink(missing_ok=True)
            else:
                continue
            conn.execute("UPDATE trial_upload_retention SET deleted_at=? WHERE filename=?", (now, name))
            conn.execute("DELETE FROM uploads WHERE filename=?", (name,))
            removed += 1
    return {"deleted_originals": removed, "retention_days": 7}


def export_events(output: Path, event: str | None = None) -> int:
    if event is not None and event not in FIELDS:
        raise AppError("TRIAL_EVENT_INVALID", "不支持的内测事件", 422)
    init_events()
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM trial_events " + ("WHERE event=? " if event else "") + "ORDER BY created_at,event_id",
                            (event,) if event else ()).fetchall()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("event_id", "user_id", "participant_code", "event", "created_at", "payload"))
        writer.writeheader()
        writer.writerows(dict(row) for row in rows)
    return len(rows)
