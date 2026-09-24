# -*- coding: utf-8 -*-
"""设置信息（只读）：当前模型配置、网关、内容安全开关等。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.core import config

router = APIRouter()


@router.get("/settings")
async def get_settings(user_id: str = Depends(get_current_user)) -> dict:
    s = config.settings
    return {
        "gateway": s.aihubmix_base,
        "models": {
            "llm": s.llm_model,
            "vlm": s.vlm_model,
            "image_t2i": s.image_t2i_model,
            "video_first_frame": s.video_first_frame_model,
            "video_reference": s.video_reference_model,
        },
        "content_review_enabled": s.content_review_enabled,
        "token_ttl_days": s.auth_token_ttl_days,
    }
