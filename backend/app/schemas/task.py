# -*- coding: utf-8 -*-
"""短管线任务结构定义。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

TASK_TYPES = ("literary_video", "motion_transfer", "talking_head")


class TaskCreate(BaseModel):
    type: Literal["literary_video", "motion_transfer", "talking_head"]
    input: dict[str, Any] = Field(default_factory=dict, description="管线输入")


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
