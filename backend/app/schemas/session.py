# -*- coding: utf-8 -*-
"""会话与产物结构定义。"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.models import ModelSelection

VIDEO_RATIOS = ("16:9", "9:16", "1:1")


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orchestration_mode: Literal["workflow", "multi_agent"] = "workflow"
    model_selection: ModelSelection = Field(default_factory=ModelSelection)
    project_type: Literal["story", "comic"] = "story"
    idea: str = Field(..., min_length=1, max_length=2000, description="创作创意/梗概")
    style: str = Field("realistic", description="视觉风格")
    episodes: int = Field(1, ge=1, le=20, description="剧集数（默认 1，首稿更快）")
    video_ratio: Literal["16:9", "9:16", "1:1"] = "16:9"
    resolution: str = Field("720P", description="视频分辨率")
    expand_idea: bool = Field(False, description="是否先扩写创意")
    video_generation_mode: Literal["first_frame", "start_end", "reference"] = "first_frame"
    knowledge_library_ids: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def comic_limits(self):
        if self.project_type == "comic" and "video_ratio" not in self.model_fields_set:
            self.video_ratio = "9:16"
        if self.project_type == "comic" and self.episodes != 1:
            raise ValueError("漫剧首期仅支持单集，episodes 必须为 1")
        if self.project_type == "comic" and self.resolution.upper() not in {"720P", "1080P"}:
            raise ValueError("漫剧支持 720P 或 1080P")
        return self


class SessionKnowledgeUpdate(BaseModel):
    knowledge_library_ids: list[str] = Field(default_factory=list, max_length=3)


class SessionMeta(BaseModel):
    """会话元数据（持久化到 data/sessions/{id}.json）。"""

    session_id: str
    orchestration_mode: Literal["workflow", "multi_agent"] = "workflow"
    agent_runs: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    model_selection: ModelSelection = Field(default_factory=ModelSelection)
    project_type: Literal["story", "comic"] = "story"
    owner_id: Optional[str] = None  # 账号隔离归属（第三刀新增；旧会话为 None）
    idea: str
    style: str = "realistic"
    episodes: int = 1
    video_ratio: str = "16:9"
    resolution: str = "720P"
    expand_idea: bool = False
    video_generation_mode: str = "first_frame"
    status: str = "idle"  # idle/running/waiting/stage_completed/session_completed/failed
    current_stage: Optional[str] = None
    stages_completed: list[str] = []
    artifacts: dict[str, Any] = {}
    stale_stages: list[str] = Field(default_factory=list)
    artifact_versions: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    selected_versions: dict[str, str] = Field(default_factory=dict)
    execution_inputs: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_library_ids: list[str] = Field(default_factory=list)
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
    model_usage: dict[str, Any] = Field(default_factory=dict)
    id: str
    name: str
    description: str = ""
    selected: str = ""  # 选中图片路径
    versions: list[str] = []
    prompt: str = ""
    prompt_override: str = ""
    input_versions: dict[str, str] = Field(default_factory=dict)
    input_dependencies: dict[str, Any] = Field(default_factory=dict)


class CharacterDesignArtifact(BaseModel):
    characters: list[CharacterDesignItem] = []
    settings: list[CharacterDesignItem] = []
    stale_items: list[str] = Field(default_factory=list)


class StoryboardShot(BaseModel):
    shot_id: str
    episode_number: int = 0
    description: str = ""
    prompt: str = ""
    character_ids: list[str] = Field(default_factory=list)
    setting_ids: list[str] = Field(default_factory=list)


class StoryboardArtifact(BaseModel):
    shots: list[StoryboardShot] = []


class ReferenceArtifact(BaseModel):
    shots: list[dict[str, Any]] = []  # shot_id -> 首帧图路径
    stale_items: list[str] = Field(default_factory=list)


class VideoSegment(BaseModel):
    model_usage: dict[str, Any] = Field(default_factory=dict)
    requested_duration_seconds: int | None = None
    segment_id: str
    shot_id: str = ""
    path: str = ""
    prompt: str = ""
    selected: str = ""
    versions: list[str] = Field(default_factory=list)
    input_paths: list[str] = Field(default_factory=list)
    prompt_override: str = ""
    input_versions: dict[str, str] = Field(default_factory=dict)
    input_dependencies: dict[str, Any] = Field(default_factory=dict)


class VideoArtifact(BaseModel):
    segments: list[VideoSegment] = []
    mode: str = "first_frame"
    tail_frames: dict[str, str] = Field(default_factory=dict)
    stale_items: list[str] = Field(default_factory=list)


class PostProductionArtifact(BaseModel):
    final_video: str = ""
    parts: list[str] = []


# 漫剧拥有独立的结构化台词与镜头合同，不替换历史故事结构。
COMIC_VOICES = (
    "zh-CN-XiaoxiaoNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunxiNeural",
    "zh-CN-YunjianNeural", "zh-CN-YunyangNeural", "zh-CN-YunxiaNeural",
)


class ComicDialogueLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    line_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    speaker_id: str = Field(..., min_length=1, max_length=64)
    text: str = Field(..., min_length=1, max_length=400)
    emotion: str = Field("", max_length=100, description="仅创作备注，不作为 TTS 情绪参数")

    @model_validator(mode="after")
    def nonempty_text(self):
        if not self.text.strip():
            raise ValueError("台词不能只有空白")
        return self


class ComicStoryboardShot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    episode_number: Literal[1] = 1
    description: str = Field(..., min_length=1, max_length=2000)
    prompt: str = Field("", max_length=4000)
    character_ids: list[str] = Field(default_factory=list, max_length=3)
    setting_ids: list[str] = Field(default_factory=list, max_length=3)
    motion: Literal["static", "push_in", "pan_left", "pan_right"] = "static"
    silent_duration: float = Field(3.0, ge=1, le=10)
    dialogues: list[ComicDialogueLine] = Field(default_factory=list, max_length=8)


class ComicStoryboardArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shots: list[ComicStoryboardShot] = Field(..., min_length=1, max_length=12)
    voice_map: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_ids_and_voices(self):
        if len({shot.shot_id for shot in self.shots}) != len(self.shots):
            raise ValueError("镜头 ID 不得重复")
        lines = [line.line_id for shot in self.shots for line in shot.dialogues]
        if len(set(lines)) != len(lines):
            raise ValueError("台词 ID 不得重复")
        if any(voice not in COMIC_VOICES for voice in self.voice_map.values()):
            raise ValueError("请选择受支持的中文声线")
        return self
