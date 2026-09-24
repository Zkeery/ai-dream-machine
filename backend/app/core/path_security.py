# -*- coding: utf-8 -*-
"""路径与文件安全：防路径遍历、安全文件名、真实类型/大小校验。"""
from __future__ import annotations

import re
from pathlib import Path

from app.core import config
from app.core.errors import AppError

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# 允许的媒体类型（按 PRD 7.2）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTS = {".mp4"}


def validate_session_id(session_id: str) -> None:
    if not _SAFE_SESSION.match(session_id or ""):
        raise AppError("INVALID_SESSION_ID", "会话 ID 不合法", 404)


def ensure_inside(root: Path, path: Path) -> Path:
    """确保 path 解析后在 root 之内，否则拒绝。"""
    try:
        resolved = path.resolve()
    except OSError:
        raise AppError("INVALID_PATH", "路径不合法")
    if root not in resolved.parents and resolved != root:
        raise AppError("INVALID_PATH", "路径越界")
    return resolved


def safe_filename(name: str) -> str:
    """返回安全文件名；不合法则拒绝。"""
    if not _SAFE_NAME.match(name or ""):
        raise AppError("INVALID_FILENAME", "文件名不合法")
    return name


def check_ext(filename: str, allowed: set[str]) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in allowed:
        raise AppError("UNSUPPORTED_TYPE", f"不支持的文件类型：{ext or '未知'}")
    return ext


def session_dir(session_id: str) -> Path:
    validate_session_id(session_id)
    d = config.SESSIONS_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def artifact_subdir(session_id: str, kind: str) -> Path:
    """kind ∈ {image, video, script}。"""
    base = {"image": config.IMAGE_DIR, "video": config.VIDEO_DIR, "script": config.SCRIPT_DIR}[kind]
    d = base / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d
