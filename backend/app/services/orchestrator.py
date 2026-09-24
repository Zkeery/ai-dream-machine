# -*- coding: utf-8 -*-
"""核心编排器：6 阶段状态机 + 各阶段专职 Agent。

固定流程由代码状态机编排（蓝图工作流运行时）；模型调用集中在本层，不散落路由。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.core import config
from app.core.errors import AppError
from app.core.path_security import artifact_subdir
from app.models.image_client import ImageClient
from app.models.llm_client import LLMClient
from app.models.tts_client import TTSClient
from app.models.video_client import VideoClient
from app.schemas.session import (
    CharacterDesignArtifact,
    CharacterDesignItem,
    PostProductionArtifact,
    ReferenceArtifact,
    ScriptArtifact,
    SessionCreate,
    SessionMeta,
    StoryboardArtifact,
    StoryboardShot,
    VideoArtifact,
    VideoSegment,
)
from app.services import prompts, session_store
from app.services.content_review import ContentReviewer
from app.services.ffmpeg_util import concat_videos, extract_first_frame

logger = logging.getLogger(__name__)

STAGE_ORDER = [
    "script_generation",
    "character_design",
    "storyboard",
    "reference_generation",
    "video_generation",
    "post_production",
]

ProgressCb = Callable[[str, str, int], Awaitable[None]]


class Orchestrator:
    def __init__(
        self,
        llm: LLMClient | None = None,
        image: ImageClient | None = None,
        video: VideoClient | None = None,
        tts: TTSClient | None = None,
        reviewer: ContentReviewer | None = None,
    ) -> None:
        self.llm = llm or LLMClient()
        self.image = image or ImageClient()
        self.video = video or VideoClient()
        self.tts = tts or TTSClient()
        self.reviewer = reviewer or ContentReviewer()

    async def _check_content(self, path: str) -> None:
        """内容安全结果侧审查；命中红线则抛可读错误。"""
        if not self.reviewer.enabled:
            return
        result = await asyncio.to_thread(self.reviewer.review_image, path)
        if not result["safe"]:
            raise AppError("CONTENT_REVIEW_FAILED", f"内容安全拦截：{result['reason']}", 400)

    # ---------- 会话生命周期 ----------

    def create(self, req: SessionCreate, owner_id: str | None = None) -> SessionMeta:
        session_id = str(int(time.time() * 1000))
        meta = SessionMeta(
            session_id=session_id,
            owner_id=owner_id,
            idea=req.idea,
            style=req.style,
            episodes=req.episodes,
            video_ratio=req.video_ratio,
            resolution=req.resolution,
            expand_idea=req.expand_idea,
            video_generation_mode=req.video_generation_mode,
            status="idle",
            current_stage=STAGE_ORDER[0],  # 从第一个阶段（剧本）开始
        )
        return session_store.create_session(meta)

    def get(self, session_id: str) -> SessionMeta:
        return session_store.load_session(session_id)

    def list_sessions(self, owner_id: str) -> list[SessionMeta]:
        return session_store.list_sessions(owner_id)

    def continue_session(self, session_id: str) -> SessionMeta:
        meta = self.get(session_id)
        if meta.status not in ("stage_completed",):
            raise AppError("NOT_READY", "当前阶段尚未完成，不能继续", 409)
        idx = STAGE_ORDER.index(meta.current_stage)
        if idx == len(STAGE_ORDER) - 1:
            meta.status = "session_completed"
        else:
            meta.current_stage = STAGE_ORDER[idx + 1]
            meta.status = "idle"
        session_store.touch(meta)
        return meta

    # ---------- 阶段执行 ----------

    async def execute_stage(self, session_id: str, stage: str, progress: ProgressCb) -> SessionMeta:
        meta = self.get(session_id)
        if stage not in STAGE_ORDER:
            raise AppError("UNKNOWN_STAGE", "未知阶段", 404)
        if meta.status == "session_completed":
            raise AppError("SESSION_DONE", "会话已完成", 409)

        meta.current_stage = stage
        meta.status = "running"
        meta.error = None
        session_store.touch(meta)

        handler = getattr(self, f"_stage_{stage}")
        try:
            await handler(meta, progress)
        except AppError:
            meta.status = "failed"
            meta.error = "阶段执行失败"
            session_store.touch(meta)
            raise
        except Exception as e:  # 兜底：任何未知异常都不崩溃会话
            logger.exception("阶段 %s 异常", stage)
            meta.status = "failed"
            meta.error = "阶段执行异常"
            session_store.touch(meta)
            raise AppError("STAGE_FAILED", f"阶段执行异常：{stage}") from e

        if stage not in meta.stages_completed:
            meta.stages_completed.append(stage)
        meta.status = "stage_completed"
        session_store.touch(meta)
        return meta

    async def _stage_script_generation(self, meta: SessionMeta, progress: ProgressCb) -> None:
        await progress("script_generation", "正在生成剧本", 10)
        idea = meta.idea
        if meta.expand_idea:
            idea = await self.llm.generate(prompts.SCRIPT_SYSTEM, f"请扩写这个创意为一句话梗概：{idea}")
        art = await asyncio.to_thread(
            self.llm.generate_json,
            prompts.SCRIPT_SYSTEM,
            prompts.build_script_user(idea, meta.episodes, meta.style),
            ScriptArtifact,
        )
        script_dir = artifact_subdir(meta.session_id, "script")
        (script_dir / "script.json").write_text(art.model_dump_json(indent=2), encoding="utf-8")
        meta.artifacts["script_generation"] = art.model_dump()
        await progress("script_generation", "剧本已生成", 100)

    async def _stage_character_design(self, meta: SessionMeta, progress: ProgressCb) -> None:
        script = meta.artifacts.get("script_generation") or {}
        characters = script.get("characters", [])
        settings = script.get("settings", [])
        image_dir = artifact_subdir(meta.session_id, "image")
        total = len(characters) + len(settings) or 1

        async def render(item: dict, kind: str, i: int) -> CharacterDesignItem:
            prompt = prompts.CHARACTER_PROMPT.format(
                name=item.get("name", ""), description=item.get("description", ""), style=meta.style
            )
            out = image_dir / f"{kind}_{i}.png"
            await asyncio.to_thread(self.image.text_to_image, prompt, out)
            await self._check_content(str(out))
            return CharacterDesignItem(
                id=item.get(f"{kind}_id", "") or item.get("character_id") or item.get("setting_id") or f"{kind}_{i}",
                name=item.get("name", ""),
                description=item.get("description", ""),
                selected=str(out),
                versions=[str(out)],
            )

        chars, sets = [], []
        for i, c in enumerate(characters):
            await progress("character_design", f"正在设计角色 {c.get('name','')}", int(10 + 40 * i / total))
            chars.append(await render(c, "character", i))
        for i, s in enumerate(settings):
            await progress("character_design", f"正在设计场景 {s.get('name','')}", int(50 + 40 * i / total))
            sets.append(await render(s, "setting", i))
        art = CharacterDesignArtifact(characters=chars, settings=sets)
        meta.artifacts["character_design"] = art.model_dump()
        await progress("character_design", "角色/场景设计完成", 100)

    async def _stage_storyboard(self, meta: SessionMeta, progress: ProgressCb) -> None:
        await progress("storyboard", "正在规划分镜", 20)
        script_json = json.dumps(meta.artifacts.get("script_generation", {}), ensure_ascii=False)
        art = await asyncio.to_thread(
            self.llm.generate_json,
            prompts.STORYBOARD_SYSTEM,
            prompts.build_storyboard_user(script_json, meta.style),
            StoryboardArtifact,
        )
        script_dir = artifact_subdir(meta.session_id, "script")
        (script_dir / "storyboard.json").write_text(art.model_dump_json(indent=2), encoding="utf-8")
        meta.artifacts["storyboard"] = art.model_dump()
        await progress("storyboard", "分镜已生成", 100)

    async def _stage_reference_generation(self, meta: SessionMeta, progress: ProgressCb) -> None:
        shots = (meta.artifacts.get("storyboard") or {}).get("shots", [])
        if not shots:
            raise AppError("NO_STORYBOARD", "缺少分镜，无法生成参考图")
        image_dir = artifact_subdir(meta.session_id, "image")
        refs: list[dict] = []
        total = len(shots)
        for i, shot in enumerate(shots):
            await progress("reference_generation", f"正在生成参考图 {i+1}/{total}", int(20 + 70 * i / total))
            prompt = prompts.REFERENCE_PROMPT.format(description=shot.get("description", ""), prompt=shot.get("prompt", ""), style=meta.style)
            out = image_dir / f"ref_{shot.get('shot_id', i)}.png"
            await asyncio.to_thread(self.image.text_to_image, prompt, out)
            await self._check_content(str(out))
            refs.append({"shot_id": shot.get("shot_id", f"s{i}"), "path": str(out)})
        art = ReferenceArtifact(shots=refs)
        meta.artifacts["reference_generation"] = art.model_dump()
        await progress("reference_generation", "参考图生成完成", 100)

    async def _stage_video_generation(self, meta: SessionMeta, progress: ProgressCb) -> None:
        refs = (meta.artifacts.get("reference_generation") or {}).get("shots", [])
        shots = (meta.artifacts.get("storyboard") or {}).get("shots", [])
        if not refs:
            raise AppError("NO_REFERENCE", "缺少参考图，无法生成视频")
        video_dir = artifact_subdir(meta.session_id, "video")
        segments: list[VideoSegment] = []
        total = len(refs)
        mode = meta.video_generation_mode
        for i, ref in enumerate(refs):
            await progress("video_generation", f"正在生成视频片段 {i+1}/{total}", int(10 + 80 * i / total))
            shot = next((s for s in shots if s.get("shot_id") == ref.get("shot_id")), {})
            prompt = prompts.VIDEO_PROMPT.format(description=shot.get("description", ""), prompt=shot.get("prompt", ""))
            out = video_dir / f"seg_{i}.mp4"
            # 真实模式需先把本地参考图转成可访问 URL；MVP 阶段标注待验
            await asyncio.to_thread(self.video.image_to_video, ref.get("path", ""), prompt, out, mode)
            if self.reviewer.enabled:
                frame = video_dir / f"seg_{i}_frame.jpg"
                await extract_first_frame(out, frame)
                await self._check_content(str(frame))
            segments.append(VideoSegment(segment_id=f"seg_{i}", shot_id=ref.get("shot_id", ""), path=str(out), prompt=prompt))
        art = VideoArtifact(segments=segments, mode=mode)
        meta.artifacts["video_generation"] = art.model_dump()
        await progress("video_generation", "视频片段生成完成", 100)

    async def _stage_post_production(self, meta: SessionMeta, progress: ProgressCb) -> None:
        await progress("post_production", "正在剪辑成片", 30)
        segs = (meta.artifacts.get("video_generation") or {}).get("segments", [])
        parts = [Path(s["path"]) for s in segs if s.get("path")]
        if not parts:
            # 无视频片段时退化为把参考图拼成成片（静态成片）
            refs = (meta.artifacts.get("reference_generation") or {}).get("shots", [])
            parts = [Path(r["path"]) for r in refs if r.get("path")]
        video_dir = artifact_subdir(meta.session_id, "video")
        final = video_dir / "final.mp4"
        await concat_videos(parts, final)
        art = PostProductionArtifact(final_video=str(final), parts=[str(p) for p in parts])
        meta.artifacts["post_production"] = art.model_dump()
        await progress("post_production", "成片已生成", 100)

    # ---------- 干预（修改后重生成） ----------

    async def intervene(self, session_id: str, stage: str, modifications: dict, progress: ProgressCb) -> SessionMeta:
        meta = self.get(session_id)
        if stage not in STAGE_ORDER:
            raise AppError("UNKNOWN_STAGE", "未知阶段", 404)
        # MVP：把修改合并进现有产物上下文，再重跑该阶段
        existing = meta.artifacts.get(stage) or {}
        merged = {**existing, **modifications}
        meta.artifacts[stage] = merged
        session_store.touch(meta)
        return await self.execute_stage(session_id, stage, progress)
