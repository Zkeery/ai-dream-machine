# -*- coding: utf-8 -*-
"""认证路由：邀请码登录、登出、当前用户。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.services import auth

router = APIRouter()


class LoginRequest(BaseModel):
    invite_code: str = Field(..., min_length=1, max_length=64, description="邀请码")


@router.post("/auth/login")
async def login(req: LoginRequest) -> dict:
    return auth.login(req.invite_code)


@router.post("/auth/logout")
async def logout(request: Request, user_id: str = Depends(get_current_user)) -> dict:
    authorization = request.headers.get("authorization", "")
    token = authorization[len("Bearer "):].strip() if authorization.startswith("Bearer ") else ""
    auth.revoke_token(token)
    return {"ok": True}


@router.get("/auth/me")
async def me(user_id: str = Depends(get_current_user)) -> dict:
    return auth.get_user(user_id)
