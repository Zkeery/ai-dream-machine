# -*- coding: utf-8 -*-
"""依赖注入：编排器单例、当前用户鉴权。"""
from __future__ import annotations

from functools import lru_cache

from fastapi import Header

from app.core.errors import AppError
from app.services import auth
from app.services.orchestrator import Orchestrator


@lru_cache(maxsize=1)
def get_orchestrator() -> Orchestrator:
    return Orchestrator()


def get_current_user(authorization: str = Header(default="")) -> str:
    """解析 `Authorization: Bearer <token>`，返回 user_id；缺失/无效/过期 → 401。"""
    if not authorization.startswith("Bearer "):
        raise AppError("AUTH_REQUIRED", "未登录", 401)
    token = authorization[len("Bearer "):].strip()
    return auth.validate_token(token)
