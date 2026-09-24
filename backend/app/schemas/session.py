# -*- coding: utf-8 -*-
"""会话与产物结构定义。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

VIDEO_RATIOS = ("16:9", "9:16", "1:1")


class SessionCreate(BaseModel):
    idea: str = Field(..., min_length=1, max_length=2000, description="创作创意/梗概")
    style: str = Field("realistic", description="视觉风格")
    episodes: int = Field(4, ge=1, le=20, description="剧集数")
    video_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    resolution: str = Field("720P", description="视频分辨率")
    expand_idea: bool = Field(False, description="是否先扩写创意")
    video_generation_mode: Literal["first_frame", "start_end", "reference"] = "first_frame"


class SessionMeta(BaseModel):
    """会话元数据（持久化到 data/sessions/{id}.json）。"""

    session_id: str
    owner_id: Optional[str] = None  # 账号隔离归属（第三刀新增；旧会话为 None）
    idea: str
    style: str = "realistic"
    episodes: int = 4
    video_ratio: str = "16:9"
    resolution: str = "720P"
    expand_idea: bool = False
    video_generation_mode: str = "first_frame"
    status: str = "idle"  # idle/running/waiting/stage_completed/session_completed/failed
    current_stage: Optional[str] = None
    stages_completed: list[str] = []
    artifacts: dict[str, Any] = {}
    error: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0


# ---- 剧本产物（LLM 结构化输出，强校验）----

class ScriptCharacter(BaseModel):
    name: str
    character_id: str = ""
    description: str = ""
    role: str = ""


class ScriptSetting(BaseModel):
    name: str
    setting_id: str = ""
    description: str = ""


class ScriptEpisode(BaseModel):
    episode_number: int
    act_title: str = ""
    content: str = ""


class ScriptArtifact(BaseModel):
    title: str = ""
    logline: str = ""
    genre: list[str] = []
    mood: str = ""
    characters: list[ScriptCharacter] = []
    settings: list[ScriptSetting] = []
    episodes: list[ScriptEpisode] = []


# ---- 其余阶段产物（多为路径与元数据，弱校验）----

class CharacterDesignItem(BaseModel):
    id: str
    name: str
    description: str = ""
    selected: str = ""  # 选中图片路径
    versions: list[str] = []


class CharacterDesignArtifact(BaseModel):
    characters: list[CharacterDesignItem] = []
    settings: list[CharacterDesignItem] = []


class StoryboardShot(BaseModel):
    shot_id: str
    episode_number: int = 0
    description: str = ""
    prompt: str = ""


class StoryboardArtifact(BaseModel):
    shots: list[StoryboardShot] = []


class ReferenceArtifact(BaseModel):
    shots: list[dict[str, Any]] = []  # shot_id -> 首帧图路径


class VideoSegment(BaseModel):
    segment_id: str
    shot_id: str = ""
    path: str = ""
    prompt: str = ""


class VideoArtifact(BaseModel):
    segments: list[VideoSegment] = []
    mode: str = "first_frame"


class PostProductionArtifact(BaseModel):
    final_video: str = ""
    parts: list[str] = []
