# -*- coding: utf-8 -*-
"""短管线任务结构定义。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict, field_validator

from app.schemas.models import ModelSelection
from app.core.errors import AppError

TASK_TYPES = ("literary_video", "motion_transfer", "talking_head")


def talking_input(inputs: dict) -> tuple[str, str]:
    """Legacy tasks stay static; only explicit lip-sync requests use a paid model."""
    mode = inputs.get("talking_mode", "static")
    if mode not in ("static", "lip_sync"):
        raise AppError("TALKING_MODE_INVALID", "请选择静态图片配音或人物嘴型同步", 422)
    script = inputs.get("script")
    if not isinstance(script, str) or not script.strip():
        raise AppError("VALIDATION_ERROR", "请填写口播文案", 422)
    script = script.strip()
    if mode == "lip_sync" and len("".join(script.split())) > 40:
        raise AppError("TALKING_SCRIPT_TOO_LONG", "嘴型同步目前生成 10 秒视频，文案最多 40 个非空白字符；请缩短文案或选择静态图片配音", 422)
    return mode, script


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["literary_video", "motion_transfer", "talking_head"]
    input: dict[str, Any] = Field(default_factory=dict, description="管线输入")

    @field_validator("input")
    @classmethod
    def validate_model_shape(cls, value):
        if "model_selection" in value:
            ModelSelection.model_validate(value["model_selection"])
        return value


class TaskMeta(BaseModel):
    """任务元数据（持久化到 data/tasks/{id}.json）。"""

    task_id: str
    type: str
    owner_id: str
    status: str = "pending"  # pending/running/completed/failed
    input: dict[str, Any] = {}
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
