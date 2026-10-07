# -*- coding: utf-8 -*-
"""邀请码登录与账号隔离：邀请码/用户/令牌/上传索引的 SQLite 存储与校验。"""
from __future__ import annotations

import secrets
import time
import uuid
from pathlib import Path

from app.core import config
from app.core.errors import AppError
from app.services import db

# 8 位大写字母数字，排除易混淆 0/O/1/I
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ---------- 邀请码 ----------

def _normalize_code(code: str) -> str:
    return (code or "").strip().upper()


def generate_invite_codes(n: int) -> list[str]:
    """生成 n 个未使用邀请码，返回明码列表。"""
    if not isinstance(n, int) or n < 1 or n > 1000:
        raise AppError("INVALID_REQUEST", "生成数量必须在 1~1000 之间")
    made: list[str] = []
    now = time.time()
    with db.connect() as conn:
        for _ in range(n):
            while True:
                code = "".join(secrets.choice(_ALPHABET) for _ in range(8))
                if conn.execute("SELECT 1 FROM invite_codes WHERE code=?", (code,)).fetchone() is None:
                    break
            conn.execute("INSERT INTO invite_codes (code, status, used_by, created_at, used_at) VALUES (?,?,?,?,?)",
                         (code, "unused", None, now, None))
            made.append(code)
    return made


def list_invite_codes() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM invite_codes ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def revoke_invite_code(code: str) -> None:
    code = _normalize_code(code)
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM invite_codes WHERE code=?", (code,)).fetchone()
        if row is None:
            raise AppError("INVITE_NOT_FOUND", "邀请码不存在", 404)
        if row["status"] != "unused":
            raise AppError("INVITE_NOT_UNUSED", "邀请码已使用或已作废", 400)
        conn.execute("UPDATE invite_codes SET status='revoked' WHERE code=?", (code,))


# ---------- 用户与令牌 ----------

def is_admin(user_id: str) -> bool:
    """Roles come only from the server allowlist, never from request data."""
    return bool(user_id) and user_id in config.settings.admin_user_ids


def _bind_user(code: str) -> dict:
    """未使用码 → 创建账号并绑定；已使用码 → 返回原账号。作废码拒绝。"""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM invite_codes WHERE code=?", (code,)).fetchone()
        if row is None or row["status"] == "revoked":
            raise AppError("INVITE_INVALID", "邀请码无效", 403)
        if row["status"] == "used":
            user = conn.execute("SELECT * FROM users WHERE user_id=?", (row["used_by"],)).fetchone()
            if user is None:
                raise AppError("USER_NOT_FOUND", "账号数据异常，请联系管理员", 500)
            return dict(user)
        user_id = uuid.uuid4().hex
        now = time.time()
        conn.execute("INSERT INTO users (user_id, invite_code, created_at) VALUES (?,?,?)", (user_id, code, now))
        conn.execute("UPDATE invite_codes SET status='used', used_by=?, used_at=? WHERE code=?", (user_id, now, code))
        return {"user_id": user_id, "invite_code": code, "created_at": now}


def login(invite_code: str) -> dict:
    code = _normalize_code(invite_code)
    if len(code) != 8 or any(c not in _ALPHABET for c in code):
        raise AppError("INVITE_INVALID", "邀请码无效", 403)
    user = _bind_user(code)
    return issue_token(user["user_id"])


def issue_token(user_id: str) -> dict:
    token = secrets.token_hex(32)
    now = time.time()
    expires_at = now + config.settings.auth_token_ttl_days * 86400
    with db.connect() as conn:
        conn.execute("INSERT INTO auth_tokens (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                     (token, user_id, now, expires_at))
    return {"token": token, "user_id": user_id, "expires_at": expires_at, "is_admin": is_admin(user_id)}


def validate_token(token: str) -> str:
    """校验令牌，返回 user_id；无效/过期抛 AUTH_REQUIRED。"""
    token = (token or "").strip()
    if not token:
        raise AppError("AUTH_REQUIRED", "未登录", 401)
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM auth_tokens WHERE token=?", (token,)).fetchone()
    if row is None:
        raise AppError("AUTH_REQUIRED", "未登录或令牌无效", 401)
    if row["expires_at"] < time.time():
        raise AppError("AUTH_REQUIRED", "登录已过期，请重新登录", 401)
    return row["user_id"]


def revoke_token(token: str) -> None:
    token = (token or "").strip()
    if not token:
        return
    with db.connect() as conn:
        conn.execute("DELETE FROM auth_tokens WHERE token=?", (token,))


def get_user(user_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        raise AppError("USER_NOT_FOUND", "用户不存在", 404)
    return {**dict(row), "is_admin": is_admin(user_id)}


# ---------- 上传归属 ----------

def record_upload(filename: str, owner_id: str, original_name: str) -> None:
    with db.connect() as conn:
        conn.execute("INSERT INTO uploads (filename, owner_id, original_name, created_at) VALUES (?,?,?,?)",
                     (filename, owner_id, original_name, time.time()))


def get_upload(filename: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM uploads WHERE filename=?", (filename,)).fetchone()
    return dict(row) if row else None


def require_upload_owner(filename: str, user_id: str) -> Path:
    """校验上传文件属于该用户，返回其本地路径；否则 404（不泄露）。"""
    entry = get_upload(filename)
    if entry is None or entry["owner_id"] != user_id:
        raise AppError("UPLOAD_NOT_FOUND", "上传文件不存在", 404)
    path = config.UPLOAD_DIR / filename
    if not path.exists():
        raise AppError("UPLOAD_NOT_FOUND", "上传文件不存在", 404)
    return path
