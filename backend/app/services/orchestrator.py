# -*- coding: utf-8 -*-
"""核心编排器：6 阶段状态机 + 各阶段专职 Agent。

固定流程由代码状态机编排（蓝图工作流运行时）；模型调用集中在本层，不散落路由。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import Awaitable, Callable, Optional
from uuid import uuid4

from app.core import config
from app.core.errors import AppError
from app.core.path_security import artifact_subdir, ensure_inside
from app.models.image_client import ImageClient
from app.models.llm_client import LLMClient
from app.models.tts_client import TTSClient
from app.models.video_client import VideoClient
from app.models import video_contracts
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
from app.services import prompts, session_store, execution_store, model_catalog, provider_jobs
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

# 只有完整生成成功（含旧会话迁移）才算阶段版本；失败留存 / 点选 / 保存不算版本。
STAGE_VERSION_REASONS = frozenset({"generated", "legacy"})

STAGE_DEPENDENCIES = {
    "script_generation": [],
    "character_design": ["script_generation"],
    "storyboard": ["script_generation"],
    "reference_generation": ["script_generation", "character_design", "storyboard"],
    "video_generation": ["character_design", "storyboard", "reference_generation"],
    "post_production": ["video_generation"],
}

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
        from app.services.knowledge_retrieval import validate_libraries
        library_ids = list(dict.fromkeys(req.knowledge_library_ids))
        selection = model_catalog.validate_selection(req.model_selection, project_type=req.project_type)
        if library_ids:
            validate_libraries(owner_id, library_ids)
        session_id = str(int(time.time() * 1000))
        meta = SessionMeta(
            session_id=session_id,
            orchestration_mode=req.orchestration_mode,
            project_type=req.project_type,
            model_selection=selection,
            owner_id=owner_id,
            idea=req.idea,
            style="manga" if req.project_type == "comic" and req.style == "realistic" else req.style,
            episodes=req.episodes,
            video_ratio=req.video_ratio,
            resolution=req.resolution,
            expand_idea=req.expand_idea,
            video_generation_mode=req.video_generation_mode,
            knowledge_library_ids=library_ids,
            status="idle",
            current_stage=STAGE_ORDER[0],  # 从第一个阶段（剧本）开始
        )
        return session_store.create_session(meta)

    def get(self, session_id: str) -> SessionMeta:
        meta = session_store.load_session(session_id)
        if meta.project_type == "story" and meta.status != "running" and self._repair_empty_media_completion(meta):
            session_store.touch(meta)
        return meta

    @staticmethod
    def _has_media_output(stage: str, artifact: dict | None) -> bool:
        collections = {"character_design": ("characters", "settings"),
                       "reference_generation": ("shots",), "video_generation": ("segments",)}
        return any(item.get("selected") or item.get("path")
                   for collection in collections.get(stage, ())
                   for item in (artifact or {}).get(collection, []))

    def _repair_empty_media_completion(self, meta: SessionMeta) -> bool:
        """Repair old metadata-only media stages without touching real assets."""
        invalid = [stage for stage in ("character_design", "reference_generation", "video_generation")
                   if stage in meta.stages_completed and not self._has_media_output(stage, meta.artifacts.get(stage))]
        if not invalid:
            return False
        for stage in invalid:
            if stage in meta.stages_completed:
                meta.stages_completed.remove(stage)
            if stage not in meta.stale_stages:
                meta.stale_stages.append(stage)
            versions = meta.artifact_versions.get(stage, [])
            kept = [version for version in versions if self._has_media_output(stage, version.get("artifact"))]
            if kept:
                meta.artifact_versions[stage] = kept
            else:
                meta.artifact_versions.pop(stage, None)
            if meta.selected_versions.get(stage) not in {v["version_id"] for v in kept}:
                meta.selected_versions.pop(stage, None)
            artifact = meta.artifacts.get(stage, {})
            if set(artifact) <= {"stale_items", "model_usage"}:
                meta.artifacts.pop(stage, None)
            self._invalidate_downstream(meta, stage)
        first = min(invalid, key=STAGE_ORDER.index)
        if meta.current_stage in STAGE_ORDER and STAGE_ORDER.index(meta.current_stage) >= STAGE_ORDER.index(first):
            meta.current_stage, meta.status, meta.error = first, "idle", None
        return True

    async def _run_stage_agents(self, meta, stage, progress, generate):
        if meta.orchestration_mode == "multi_agent":
            from app.services.agent_runtime import AgentRuntime
            await AgentRuntime(self, meta, stage, progress, generate).run()
        else:
            await generate()

    def list_sessions(self, owner_id: str) -> list[SessionMeta]:
        return session_store.list_sessions(owner_id)

    @staticmethod
    def stage_order(meta: SessionMeta) -> list[str]:
        if meta.project_type == "comic":
            from app.services.comic_workflow import COMIC_STAGES
            return list(COMIC_STAGES)
        return list(STAGE_ORDER)

    @staticmethod
    def completion_stage(meta: SessionMeta) -> str:
        return "comic_composition" if meta.project_type == "comic" else "post_production"

    def continue_session(self, session_id: str) -> SessionMeta:
        meta = self.get(session_id)
        if meta.project_type == "comic":
            from app.services.comic_workflow import ComicWorkflow
            return ComicWorkflow(self).continue_session(meta)
        if meta.status not in ("stage_completed",):
            raise AppError("NOT_READY", "当前阶段尚未完成，不能继续", 409)
        if meta.current_stage in meta.stale_stages:
            raise AppError("STALE_STAGE", "当前产物已失效，请先重新生成", 409)
        idx = STAGE_ORDER.index(meta.current_stage)
        pending = [s for s in STAGE_ORDER[idx + 1:] if s not in meta.stages_completed or s in meta.stale_stages]
        if not pending:
            meta.status = "session_completed"
        else:
            meta.current_stage = pending[0]
            meta.status = "idle"
        session_store.touch(meta)
        return meta

    # ---------- 阶段执行 ----------

    def _prune_non_versions(self, meta: SessionMeta) -> bool:
        """失败留存 / 点选 / 保存不得占用版本号；顺带清理历史脏数据。"""
        changed = False
        cleaned: dict[str, list[dict]] = {}
        for stage, versions in (meta.artifact_versions or {}).items():
            kept = [v for v in versions if v.get("reason") in STAGE_VERSION_REASONS]
            if len(kept) != len(versions):
                changed = True
            if kept:
                cleaned[stage] = kept
            elif versions:
                changed = True
        if changed:
            meta.artifact_versions = cleaned
        for stage, version_id in list(meta.selected_versions.items()):
            ids = {v["version_id"] for v in meta.artifact_versions.get(stage, [])}
            if version_id in ids:
                continue
            changed = True
            versions = meta.artifact_versions.get(stage, [])
            if not versions:
                meta.selected_versions.pop(stage, None)
                continue
            match = next((v["version_id"] for v in versions
                          if v.get("artifact") == meta.artifacts.get(stage)), None)
            meta.selected_versions[stage] = match or versions[-1]["version_id"]
        return changed

    def _seed_versions(self, meta: SessionMeta) -> None:
        """旧会话第一次编辑时登记现有产物，保持旧文件可选。"""
        if self._prune_non_versions(meta):
            session_store.touch(meta)
        for stage, artifact in meta.artifacts.items():
            if stage in self.stage_order(meta) and artifact and not meta.artifact_versions.get(stage):
                if stage in {"character_design", "reference_generation", "video_generation"} and not self._has_media_output(stage, artifact):
                    continue
                self._record_version(meta, stage, "legacy")

    def _record_version(self, meta: SessionMeta, stage: str, reason: str, inputs: dict | None = None) -> str:
        if reason not in STAGE_VERSION_REASONS:
            raise AppError("INVALID_VERSION_REASON", "只有完整生成成功才能登记为版本", 500)
        version_id = uuid4().hex
        meta.artifact_versions.setdefault(stage, []).append({
            "version_id": version_id,
            "artifact": deepcopy(meta.artifacts[stage]),
            "input_versions": deepcopy(inputs or {}),
            "created_at": time.time(),
            "reason": reason,
            "parent_execution_id": self._parent_execution_id(),
            **({"model_usage": deepcopy(meta.artifacts[stage]["model_usage"])} if "model_usage" in meta.artifacts[stage] else {}),
        })
        meta.selected_versions[stage] = version_id
        return version_id

    @staticmethod
    def public_versions(meta: SessionMeta, stage: str | None = None) -> dict[str, list[dict]] | list[dict]:
        """对外可见的阶段版本：仅完整生成成功与旧会话迁移。"""
        raw = meta.artifact_versions or {}
        if stage is not None:
            return [v for v in raw.get(stage, []) if v.get("reason") in STAGE_VERSION_REASONS]
        return {name: [v for v in versions if v.get("reason") in STAGE_VERSION_REASONS]
                for name, versions in raw.items()}

    @staticmethod
    def _paths_in_artifact(stage: str, artifact: dict | None) -> set[str]:
        if not artifact:
            return set()
        paths: set[str] = set()
        collections = {
            "character_design": ("characters", "settings"),
            "reference_generation": ("shots",),
            "video_generation": ("segments",),
            "post_production": (),
            "comic_panels": ("shots",),
            "comic_audio": ("shots",),
            "comic_composition": ("segments",),
        }.get(stage, ())
        for collection in collections:
            for item in artifact.get(collection, []) or []:
                for key in ("selected", "path"):
                    if item.get(key):
                        paths.add(str(item[key]))
                for path in item.get("versions", []) or []:
                    if path:
                        paths.add(str(path))
                for line in item.get("lines", []) or []:
                    if line.get("path"):
                        paths.add(str(line["path"]))
                if item.get("srt_path"):
                    paths.add(str(item["srt_path"]))
        if stage == "video_generation":
            for path in (artifact.get("tail_frames") or {}).values():
                if path:
                    paths.add(str(path))
        if stage in {"post_production", "comic_composition"}:
            if artifact.get("final_video"):
                paths.add(str(artifact["final_video"]))
            for path in artifact.get("parts", []) or []:
                if path:
                    paths.add(str(path))
            if artifact.get("subtitle_path"):
                paths.add(str(artifact["subtitle_path"]))
        return paths

    def _referenced_media_paths(self, meta: SessionMeta) -> set[str]:
        paths = provider_jobs.retained_local_paths(meta.owner_id, "session", meta.session_id)
        for stage, artifact in (meta.artifacts or {}).items():
            paths |= self._paths_in_artifact(stage, artifact)
        for stage, versions in (meta.artifact_versions or {}).items():
            for version in versions:
                if version.get("reason") in STAGE_VERSION_REASONS:
                    paths |= self._paths_in_artifact(stage, version.get("artifact"))
        for stage, runs in meta.agent_runs.items():
            for run in runs:
                paths |= self._paths_in_artifact(stage, run.get("output"))
        return paths

    def _canonical_success_paths(self, meta: SessionMeta, stage: str) -> set[str]:
        """完整成功版本里真正采用的素材路径（不含失败尝试残留）。"""
        paths: set[str] = set()
        for version in self.public_versions(meta, stage):
            artifact = version.get("artifact") or {}
            collections = {
                "character_design": ("characters", "settings"),
                "reference_generation": ("shots",),
                "video_generation": ("segments",),
            }.get(stage, ())
            for collection in collections:
                for item in artifact.get(collection, []) or []:
                    for key in ("selected", "path"):
                        if item.get(key):
                            paths.add(str(item[key]))
            if stage == "video_generation":
                for path in (artifact.get("tail_frames") or {}).values():
                    if path:
                        paths.add(str(path))
        return paths

    def _trim_item_version_lists(self, meta: SessionMeta, stage: str, keep: set[str]) -> None:
        artifacts = [meta.artifacts.get(stage)]
        artifacts += [v.get("artifact") for v in meta.artifact_versions.get(stage, [])
                      if v.get("reason") in STAGE_VERSION_REASONS]
        collections = {
            "character_design": ("characters", "settings"),
            "reference_generation": ("shots",),
            "video_generation": ("segments",),
        }.get(stage, ())
        for artifact in artifacts:
            if not artifact:
                continue
            for collection in collections:
                for item in artifact.get(collection, []) or []:
                    selected = item.get("selected") or item.get("path")
                    trimmed = [p for p in item.get("versions", []) or [] if p in keep]
                    if selected and selected not in trimmed:
                        trimmed.append(selected)
                    item["versions"] = list(dict.fromkeys(trimmed))

    def _discard_failed_attempt_media(self, meta: SessionMeta, stage: str) -> None:
        """成功生成后删除失败尝试残留：收紧 versions 列表，并清理磁盘未引用文件。"""
        if stage in {"character_design", "reference_generation", "video_generation"}:
            keep_stage = self._canonical_success_paths(meta, stage)
            self._trim_item_version_lists(meta, stage, keep_stage)
            # 最新成功版本快照与当前产物同步为收紧后的列表。
            if meta.artifact_versions.get(stage) and meta.artifacts.get(stage):
                latest = next((v for v in reversed(meta.artifact_versions[stage])
                               if v.get("reason") == "generated"), None)
                if latest and latest.get("version_id") == meta.selected_versions.get(stage):
                    latest["artifact"] = deepcopy(meta.artifacts[stage])
        keep = {Path(p).resolve() for p in self._referenced_media_paths(meta) if p}
        for kind in ("image", "video"):
            base = artifact_subdir(meta.session_id, kind).resolve()
            if not base.is_dir():
                continue
            for path in list(base.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    resolved = path.resolve()
                    ensure_inside(base, resolved)
                except AppError:
                    continue
                if resolved not in keep:
                    try:
                        resolved.unlink()
                    except OSError:
                        logger.warning("清理失败残留文件失败：%s", resolved)

    @staticmethod
    def _parent_execution_id() -> str | None:
        return getattr(execution_store, "current_execution_id", lambda: None)()

    def _invalidate_downstream(self, meta: SessionMeta, stage: str) -> None:
        affected = {stage}
        for candidate in STAGE_ORDER:
            if candidate != stage and any(dep in affected for dep in STAGE_DEPENDENCIES[candidate]):
                affected.add(candidate)
        for candidate in STAGE_ORDER:
            if candidate != stage and candidate in affected:
                if candidate not in meta.stale_stages:
                    meta.stale_stages.append(candidate)
                if candidate in meta.stages_completed:
                    meta.stages_completed.remove(candidate)

    def _require_inputs(self, meta: SessionMeta, stage: str) -> None:
        for dep in STAGE_DEPENDENCIES[stage]:
            if not meta.artifacts.get(dep) or (dep in {"character_design", "reference_generation", "video_generation"}
                                              and not self._has_media_output(dep, meta.artifacts[dep])):
                raise AppError("MISSING_STAGE_INPUT", f"缺少上游产物：{dep}", 409)
            if dep in meta.stale_stages:
                raise AppError("STALE_STAGE_INPUT", f"上游产物已失效，请先重新生成：{dep}", 409)

    def _input_versions(self, meta: SessionMeta, stage: str) -> dict[str, str]:
        return {dep: meta.selected_versions[dep] for dep in STAGE_DEPENDENCIES[stage] if dep in meta.selected_versions}

    def _safe_asset(self, meta: SessionMeta, path: str, kind: str = "image") -> Path:
        root = ((config.IMAGE_DIR if kind == "image" else config.VIDEO_DIR) / meta.session_id).resolve()
        result = ensure_inside(root, Path(path))
        allowed = {".png", ".jpg", ".jpeg", ".webp"} if kind == "image" else {".mp4"}
        if not result.is_file() or result.suffix.lower() not in allowed:
            raise AppError("ASSET_NOT_FOUND", "选中的素材不存在或类型不支持", 400)
        return result

    def _selected_design_paths(self, meta: SessionMeta, shot: dict) -> list[str]:
        paths: list[str] = []
        for _, item in self._referenced_design_items(meta, shot):
            selected = item.get("selected")
            if not selected or selected not in item.get("versions", [selected]):
                raise AppError("INVALID_ASSET_VERSION", "角色或场景选中版本未登记", 400)
            path = str(self._safe_asset(meta, selected))
            if path not in paths:
                paths.append(path)
        return paths

    def _referenced_design_items(self, meta: SessionMeta, shot: dict) -> list[tuple[str, dict]]:
        design = meta.artifacts.get("character_design") or {}
        result = []
        for collection, ids_key in (("characters", "character_ids"), ("settings", "setting_ids")):
            items = design.get(collection, [])
            if ids_key not in shot:
                # 兼容真正的旧分镜：字段缺失时才回退为全部已选素材。
                items = [item for item in items if item.get("selected")]
            else:
                ids = shot.get(ids_key) or []
                if not isinstance(ids, list):
                    raise AppError("INVALID_MODIFICATIONS", "分镜角色/场景引用必须为列表", 400)
                if ids:
                    missing = set(ids) - {item.get("id") for item in items}
                    if missing:
                        raise AppError("UNKNOWN_DESIGN_REFERENCE", "分镜引用了不存在的角色或场景", 400)
                    items = [item for item in items if item.get("id") in ids]
                else:
                    # 空列表表示该镜头不引用这类素材，不能再偷换成“全部角色/场景”。
                    items = []
            result.extend((collection, item) for item in items)
        return result

    @staticmethod
    def _image_size(meta: SessionMeta) -> str:
        if meta.resolution.upper() not in {"720P", "1080P"} or meta.video_ratio not in {"16:9", "9:16", "1:1"}:
            raise AppError("VIDEO_FORMAT_UNSUPPORTED", "支持 720P/1080P 与 16:9、9:16、1:1", 400)
        edge = 720 if meta.resolution.upper() == "720P" else 1080
        landscape = (1280, 720) if edge == 720 else (1920, 1080)
        w, h = landscape if meta.video_ratio == "16:9" else tuple(reversed(landscape)) if meta.video_ratio == "9:16" else (edge, edge)
        return f"{w}x{h}"

    @staticmethod
    def _normalize_script_ids(art: ScriptArtifact) -> None:
        for items, field, prefix in ((art.characters, "character_id", "c"), (art.settings, "setting_id", "l")):
            used = set()
            for i, item in enumerate(items):
                value = getattr(item, field) or f"{prefix}{i+1}"
                if value in used:
                    raise AppError("DUPLICATE_ENTITY_ID", "角色或场景 ID 不能重复", 400)
                setattr(item, field, value)
                used.add(value)

    @staticmethod
    def _write_text_artifact(directory: Path, name: str, text: str) -> None:
        (directory / f"{name}_{uuid4().hex}.json").write_text(text, encoding="utf-8")
        # 保留旧读取入口；版本内容另外以不可覆盖文件/元数据快照保存。
        (directory / f"{name}.json").write_text(text, encoding="utf-8")

    @staticmethod
    def _trace_paths(meta: SessionMeta, paths: list[str]) -> None:
        if meta.execution_inputs:
            row = meta.execution_inputs[-1]
            row["input_paths"] = list(dict.fromkeys([*row.get("input_paths", []), *paths]))

    def _require_registered_reference(self, meta: SessionMeta, path: str) -> None:
        artifacts = [meta.artifacts.get("reference_generation") or {}]
        artifacts += [v.get("artifact", {}) for v in meta.artifact_versions.get("reference_generation", [])]
        registered = {p for artifact in artifacts for item in artifact.get("shots", [])
                      for p in [item.get("path"), item.get("selected"), *item.get("versions", [])] if p}
        if path not in registered:
            raise AppError("INVALID_ASSET_VERSION", "尾帧必须选自本会话已登记的参考图", 400)
        self._safe_asset(meta, path)

    def _media_sources(self, meta: SessionMeta, stage: str) -> dict[str, list[dict]]:
        if stage == "character_design":
            script = meta.artifacts.get("script_generation") or {}
            result = {}
            for collection, kind in (("characters", "character"), ("settings", "setting")):
                result[collection] = [{**item, "id": item.get(f"{kind}_id") or f"{kind}_{i}"}
                                      for i, item in enumerate(script.get(collection, []))]
            return result
        if stage in {"reference_generation", "video_generation"}:
            return {"shots" if stage == "reference_generation" else "segments":
                    deepcopy((meta.artifacts.get("storyboard") or {}).get("shots", []))}
        return {}

    def _validate_generation_request(self, meta: SessionMeta, stage: str, request: dict) -> dict:
        if not isinstance(request, dict) or set(request) - {"target_ids", "prompts", "descriptions"}:
            raise AppError("INVALID_GENERATION_REQUEST", "重生成范围或提示词字段不合法", 400)
        if not request:
            return {}
        if stage not in {"character_design", "reference_generation", "video_generation"}:
            raise AppError("TARGETED_GENERATION_UNSUPPORTED", "当前阶段不支持指定素材重生成", 400)
        sources = self._media_sources(meta, stage)
        known = [item.get("id") if stage == "character_design" else item.get("shot_id")
                 for items in sources.values() for item in items]
        if any(not isinstance(x, str) or not x for x in known) or len(set(known)) != len(known):
            raise AppError("INVALID_ENTITY_IDS", "上游角色、场景或镜头 ID 不完整或重复", 400)
        targets = request.get("target_ids", known)
        if not isinstance(targets, list) or not targets or any(not isinstance(x, str) or x not in known for x in targets):
            raise AppError("INVALID_GENERATION_TARGETS", "请选择非空且有效的角色、场景或镜头范围", 400)
        if len(set(targets)) != len(targets):
            raise AppError("INVALID_GENERATION_TARGETS", "重生成范围不能包含重复 ID", 400)
        result = {"target_ids": targets}
        for field in ("prompts", "descriptions"):
            values = request.get(field, {})
            if not isinstance(values, dict) or any(k not in targets or not isinstance(v, str) or not v.strip() or len(v) > 4000 for k, v in values.items()):
                raise AppError("INVALID_GENERATION_PROMPTS", "提示词 / 描述必须为选定条目的非空文本，最多 4000 字符", 400)
            if field == "descriptions" and values and stage != "character_design":
                raise AppError("INVALID_GENERATION_PROMPTS", "只有角色 / 场景支持独立描述编辑", 400)
            if values:
                result[field] = values
        return result

    @staticmethod
    def _generation_request(meta: SessionMeta) -> dict:
        return meta.execution_inputs[-1].get("generation_request", {}) if meta.execution_inputs else {}

    def _bound_media_artifact(self, meta: SessionMeta, stage: str) -> dict:
        artifact = deepcopy(meta.artifacts.get(stage) or {})
        version = next((v for v in meta.artifact_versions.get(stage, [])
                        if v["version_id"] == meta.selected_versions.get(stage)), {})
        bindings = self._item_lineage(meta, stage, artifact, version)
        for collection, items in self._media_sources(meta, stage).items():
            for item in artifact.get(collection, []):
                item_id = item.get("id") if stage == "character_design" else item.get("shot_id")
                item["input_versions"] = deepcopy(item.get("input_versions") or bindings.get(f"{collection}:{item_id}", {}))
        return artifact

    def _refresh_media_validity(self, meta: SessionMeta, stage: str) -> None:
        artifact = meta.artifacts.setdefault(stage, {})
        missing = []
        sources = self._media_sources(meta, stage)
        for collection, source_items in sources.items():
            current = artifact.get(collection, [])
            for source in source_items:
                item_id = source.get("id") if stage == "character_design" else source.get("shot_id")
                item = next((x for x in current if (x.get("id") if stage == "character_design" else x.get("shot_id")) == item_id), {})
                if not self._media_item_matches(meta, stage, collection, item, source):
                    missing.append(item_id)
        artifact["stale_items"] = missing
        if missing or not any(sources.values()) or not self._has_media_output(stage, artifact):
            if stage not in meta.stale_stages:
                meta.stale_stages.append(stage)
            if stage in meta.stages_completed:
                meta.stages_completed.remove(stage)
        else:
            if stage in meta.stale_stages:
                meta.stale_stages.remove(stage)
            if stage not in meta.stages_completed:
                meta.stages_completed.append(stage)

    def _media_item_dependencies(self, meta: SessionMeta, stage: str, source: dict) -> dict:
        """实际逐项输入；阶段版本另保留追溯，不能误伤未引用变更的镜头。"""
        if stage == "character_design":
            return {"source": source, "style": meta.style, "size": self._image_size(meta)}
        if stage == "reference_generation":
            return {"shot": source, "design_paths": self._selected_design_paths(meta, source),
                    "style": meta.style, "size": self._image_size(meta)}
        shot_id = source["shot_id"]
        reference = next((x for x in (meta.artifacts.get("reference_generation") or {}).get("shots", [])
                          if x.get("shot_id") == shot_id), {})
        mode = meta.video_generation_mode
        tails = (meta.artifacts.get("video_generation") or {}).get("tail_frames", {})
        return {"shot": source, "reference_path": reference.get("selected") or reference.get("path"),
                "design_paths": self._selected_design_paths(meta, source) if mode == "reference" else [],
                "tail_path": tails.get(shot_id) if mode == "start_end" else None,
                "mode": mode, "video_ratio": meta.video_ratio, "resolution": meta.resolution.upper()}

    def _media_item_matches(self, meta: SessionMeta, stage: str, collection: str, item: dict, source: dict) -> bool:
        if not (item.get("selected") or item.get("path")):
            return False
        recorded = item.get("input_dependencies")
        if not recorded:
            # 老数据没有实际逐项输入证据，继续按整阶段版本保守处理。
            return self._lineage_matches(meta, stage, item.get("input_versions", {}))
        try:
            if recorded != self._media_item_dependencies(meta, stage, source):
                return False
            if stage == "character_design":
                return "script_generation" not in meta.stale_stages
            if "storyboard" in meta.stale_stages:
                return False
            if stage == "reference_generation" or meta.video_generation_mode == "reference":
                design_sources = self._media_sources(meta, "character_design")
                for kind, design in self._referenced_design_items(meta, source):
                    original = next((x for x in design_sources[kind] if x["id"] == design.get("id")), {})
                    if not original or not self._media_item_matches(meta, "character_design", kind, design, original):
                        return False
            if stage == "video_generation":
                ref = next((x for x in (meta.artifacts.get("reference_generation") or {}).get("shots", [])
                            if x.get("shot_id") == source["shot_id"]), {})
                return self._media_item_matches(meta, "reference_generation", "shots", ref, source)
            return True
        except AppError:
            return False

    def _attach_item_lineage(self, meta: SessionMeta, stage: str) -> None:
        if stage not in {"character_design", "reference_generation", "video_generation"}:
            return
        artifact = meta.artifacts.get(stage) or {}
        version = meta.artifact_versions[stage][-1]
        version["artifact"] = deepcopy(artifact)
        version["item_input_versions"] = {
            f"{collection}:{item.get('id') if stage == 'character_design' else item.get('shot_id')}": deepcopy(item.get("input_versions", {}))
            for collection in self._media_sources(meta, stage) for item in artifact.get(collection, [])}

    def _refresh_downstream_media(self, meta: SessionMeta, stage: str) -> None:
        for candidate in STAGE_ORDER[STAGE_ORDER.index(stage) + 1:]:
            if candidate in meta.stale_stages and candidate in {"character_design", "reference_generation", "video_generation"}:
                meta.artifacts[candidate] = self._bound_media_artifact(meta, candidate)
                self._refresh_media_validity(meta, candidate)

    def _abandon_failed_execution(self, meta: SessionMeta, stage: str, record: dict) -> None:
        """失败不留记录：回滚产物与状态，删除本次残留文件，并去掉执行台账。"""
        execution_id = record.get("execution_id")
        meta.execution_inputs = [row for row in meta.execution_inputs if row.get("execution_id") != execution_id]
        if record.get("input_artifact") is not None:
            meta.artifacts[stage] = deepcopy(record["input_artifact"])
        else:
            meta.artifacts.pop(stage, None)
        if record.get("input_artifact_version"):
            meta.selected_versions[stage] = record["input_artifact_version"]
        else:
            meta.selected_versions.pop(stage, None)
        meta.stale_stages = list(record.get("input_stale_stages", []))
        meta.stages_completed = list(record.get("input_stages_completed", []))
        self._discard_failed_attempt_media(meta, stage)

    async def execute_stage(self, session_id: str, stage: str, progress: ProgressCb,
                            *, generation_request: dict | None = None) -> SessionMeta:
        meta = self.get(session_id)
        if meta.project_type == "comic":
            from app.services.comic_workflow import ComicWorkflow
            return await ComicWorkflow(self).execute(meta, stage, progress, generation_request or {})
        if stage not in STAGE_ORDER:
            raise AppError("UNKNOWN_STAGE", "未知阶段", 404)
        self._seed_versions(meta)
        self._require_inputs(meta, stage)
        request = self._validate_generation_request(meta, stage, generation_request or {})
        if stage == "video_generation":
            model = model_catalog.resolve(meta.model_selection, ["video_" + meta.video_generation_mode])["video_" + meta.video_generation_mode]
            refs = {r["shot_id"]: r for r in meta.artifacts["reference_generation"].get("shots", [])}
            # Validate every requested shot before the Agent or first paid clip.
            for shot in (meta.artifacts.get("storyboard") or {}).get("shots", []):
                if "target_ids" in request and shot["shot_id"] not in request["target_ids"]:
                    continue
                ref = refs.get(shot["shot_id"], {})
                paths = [ref.get("selected") or ref.get("path")]
                if meta.video_generation_mode == "reference":
                    paths += [item.get("selected") for _, item in self._referenced_design_items(meta, shot)]
                video_contracts.validate(model, meta.video_generation_mode, meta.video_ratio, meta.resolution,
                                         reference_count=len(set(p for p in paths if p)))
        inputs = self._input_versions(meta, stage)
        usage = model_catalog.stage_usage(meta, stage)
        record = {"execution_id": uuid4().hex, "stage": stage, "started_at": time.time(), "model_usage": usage,
                  "parent_execution_id": self._parent_execution_id(),
                  "input_versions": inputs, "input_artifact_version": meta.selected_versions.get(stage),
                  "input_artifact": deepcopy(meta.artifacts.get(stage)),
                  "input_stale_stages": list(meta.stale_stages),
                  "input_stages_completed": list(meta.stages_completed),
                  "generation_request": request, "status": "running"}
        meta.execution_inputs.append(record)
        if meta.artifacts.get(stage):
            self._invalidate_downstream(meta, stage)
            if stage not in meta.stale_stages:
                meta.stale_stages.append(stage)

        meta.current_stage = stage
        meta.status = "running"
        meta.error = None
        session_store.touch(meta)

        handler = getattr(self, f"_stage_{stage}")
        try:
            await self._run_stage_agents(meta, stage, progress, lambda: handler(meta, progress))
            meta.artifacts[stage]["model_usage"] = deepcopy(usage)
        except asyncio.CancelledError:
            self._abandon_failed_execution(meta, stage, record)
            meta.status = "failed"
            meta.error = "阶段执行已中断，请重试"
            session_store.touch(meta)
            raise
        except AppError as e:
            self._abandon_failed_execution(meta, stage, record)
            meta.status = "failed"
            meta.error = e.message
            session_store.touch(meta)
            raise
        except Exception as e:  # 兜底：任何未知异常都不崩溃会话
            self._abandon_failed_execution(meta, stage, record)
            logger.exception("阶段 %s 异常", stage)
            meta.status = "failed"
            meta.error = "阶段执行异常"
            session_store.touch(meta)
            raise AppError("STAGE_FAILED", f"阶段执行异常：{stage}") from e

        if stage in {"character_design", "reference_generation", "video_generation"}:
            self._refresh_media_validity(meta, stage)
        else:
            if stage not in meta.stages_completed:
                meta.stages_completed.append(stage)
            if stage in meta.stale_stages:
                meta.stale_stages.remove(stage)
        output_version = self._record_version(meta, stage, "generated", inputs)
        self._attach_item_lineage(meta, stage)
        self._refresh_downstream_media(meta, stage)
        # 完整成功后：失败/中断留下的非版本记录与未引用文件一并删掉。
        self._prune_non_versions(meta)
        self._discard_failed_attempt_media(meta, stage)
        record.update(status="completed", output_version=output_version, finished_at=time.time())
        record["remaining_stale_items"] = (meta.artifacts.get(stage) or {}).get("stale_items", [])
        meta.status = "idle" if stage in meta.stale_stages else "stage_completed"
        session_store.touch(meta)
        return meta

    async def _knowledge_context(self, meta: SessionMeta, stage: str, query: str, progress: ProgressCb) -> dict | None:
        if not meta.knowledge_library_ids:
            return None
        from app.services.knowledge_retrieval import build_context
        await progress(stage, "正在检索创作知识库", 5)
        context = await asyncio.to_thread(build_context, meta.owner_id, meta.knowledge_library_ids, query)
        # 在调用模型前保存真实输入；即使模型失败，也能检查资料来源与版本。
        meta.execution_inputs[-1]["knowledge_context"] = deepcopy(context)
        session_store.touch(meta)
        return context

    @staticmethod
    def _creative_input(artifact: dict | None) -> str:
        return json.dumps({k: v for k, v in artifact.items() if k not in {"knowledge_context", "model_usage"}}, ensure_ascii=False) if artifact else ""

    async def _await_thread_with_heartbeat(
        self,
        progress: ProgressCb,
        stage: str,
        message: str,
        fn,
        *args,
        start_pct: int = 15,
        max_pct: int = 90,
        interval: float = 12.0,
        model: str | None = None,
        wait_hint: str = "通常 1～3 分钟",
    ):
        """跑阻塞模型调用，并周期性推送进度，避免界面长时间停在同一百分比。"""
        task = asyncio.create_task(asyncio.to_thread(fn, *args, **({"model": model} if model is not None else {})))
        started = time.monotonic()
        pct = start_pct
        await progress(stage, message, pct)
        while True:
            done, _ = await asyncio.wait({task}, timeout=interval)
            if done:
                return task.result()
            elapsed = max(1, int(time.monotonic() - started))
            pct = min(max_pct, pct + 5)
            await progress(stage, f"{message}（已等待约 {elapsed} 秒，{wait_hint}）", pct)

    async def _stage_script_generation(self, meta: SessionMeta, progress: ProgressCb) -> None:
        context = await self._knowledge_context(meta, "script_generation", meta.idea, progress)
        idea = meta.idea
        existing = meta.artifacts.get("script_generation")
        if meta.expand_idea and not existing:
            idea = await self._await_thread_with_heartbeat(
                progress,
                "script_generation",
                "模型正在扩写创意，约需 1～3 分钟，请稍候",
                self.llm.generate,
                prompts.SCRIPT_SYSTEM + (prompts.KNOWLEDGE_RULE if context else ""),
                prompts.with_knowledge(f"请扩写这个创意为一句话梗概：{idea}", context),
                start_pct=8,
                max_pct=18,
                model=model_catalog.resolve(meta.model_selection, ["text"])["text"],
            )
        art = await self._await_thread_with_heartbeat(
            progress,
            "script_generation",
            "模型正在生成剧本，约需 1～3 分钟，请稍候",
            self.llm.generate_json,
            prompts.SCRIPT_SYSTEM + (prompts.KNOWLEDGE_RULE if context else ""),
            prompts.with_knowledge(prompts.build_script_user(idea, meta.episodes, meta.style, self._creative_input(existing)), context),
            ScriptArtifact,
            start_pct=20,
            max_pct=90,
            model=model_catalog.resolve(meta.model_selection, ["text"])["text"],
        )
        script_dir = artifact_subdir(meta.session_id, "script")
        self._normalize_script_ids(art)
        result = art.model_dump()
        if context is not None:
            result["knowledge_context"] = context
        self._write_text_artifact(script_dir, "script", json.dumps(result, ensure_ascii=False, indent=2))
        meta.artifacts["script_generation"] = result
        await progress("script_generation", "剧本已生成", 100)

    async def _stage_character_design(self, meta: SessionMeta, progress: ProgressCb) -> None:
        sources = self._media_sources(meta, "character_design")
        request = self._generation_request(meta)
        targets = set(request.get("target_ids", [x["id"] for items in sources.values() for x in items]))
        image_dir = artifact_subdir(meta.session_id, "image")
        existing = self._bound_media_artifact(meta, "character_design")
        result = {collection: {item["id"]: item for item in existing.get(collection, [])}
                  for collection in sources}
        completed = 0
        for collection, items in sources.items():
            for i, item in enumerate(items):
                item_id = item["id"]
                if item_id not in targets:
                    continue
                await progress("character_design", f"正在设计 {item.get('name', '')}", int(10 + 80 * completed / max(1, len(targets))))
                old = result[collection].get(item_id, {})
                valid_old = self._media_item_matches(meta, "character_design", collection, old, item)
                description = request.get("descriptions", {}).get(item_id, old.get("description", item.get("description", "")) if valid_old else item.get("description", ""))
                override = request.get("prompts", {}).get(item_id, old.get("prompt_override", "") if valid_old else "")
                template = prompts.CHARACTER_PROMPT if collection == "characters" else prompts.SETTING_PROMPT
                prompt = override or template.format(name=item.get("name", ""), description=description, style=meta.style)
                if not override and collection == "characters":
                    prompt = prompts.compose_sheet_prompt(prompt, kind="character")
                kind = "character" if collection == "characters" else "setting"
                out = image_dir / f"{kind}_{i}_{uuid4().hex}.png"
                await asyncio.to_thread(self.image.text_to_image, prompt, out, size=self._image_size(meta),
                                        model=model_catalog.resolve(meta.model_selection, ["image"])["image"])
                await self._check_content(str(out))
                result[collection][item_id] = CharacterDesignItem(
                    id=item_id, name=item.get("name", ""), description=description, selected=str(out),
                    model_usage=model_catalog.stage_usage(meta, "character_design"),
                    versions=list(dict.fromkeys([*old.get("versions", []), *([old["selected"]] if old.get("selected") else []), str(out)])),
                    prompt=prompt, prompt_override=override, input_versions=self._input_versions(meta, "character_design"),
                    input_dependencies=self._media_item_dependencies(meta, "character_design", item)).model_dump()
                meta.artifacts["character_design"] = CharacterDesignArtifact(
                    **{key: [result[key][x["id"]] for x in values if x["id"] in result[key]] for key, values in sources.items()}).model_dump()
                self._refresh_media_validity(meta, "character_design")
                session_store.touch(meta)
                completed += 1
        if not targets:
            meta.artifacts["character_design"] = CharacterDesignArtifact().model_dump()
        await progress("character_design", "角色/场景设计完成", 100)

    async def _stage_storyboard(self, meta: SessionMeta, progress: ProgressCb) -> None:
        script = meta.artifacts.get("script_generation", {})
        query = " ".join([meta.idea, str(script.get("title", "")), str(script.get("logline", "")),
                          " ".join(str(x.get("name", "")) for x in script.get("characters", [])), "分镜构图 视觉风格 " + meta.style])[:2000]
        context = await self._knowledge_context(meta, "storyboard", query, progress)
        script_json = self._creative_input(script)
        existing = meta.artifacts.get("storyboard")
        art = await self._await_thread_with_heartbeat(
            progress,
            "storyboard",
            "模型正在规划分镜，约需 1～3 分钟，请稍候",
            self.llm.generate_json,
            prompts.STORYBOARD_SYSTEM + (prompts.KNOWLEDGE_RULE if context else ""),
            prompts.with_knowledge(prompts.build_storyboard_user(script_json, meta.style, self._creative_input(existing), idea=meta.idea), context),
            StoryboardArtifact,
            model=model_catalog.resolve(meta.model_selection, ["text"])["text"],
            start_pct=20,
            max_pct=90,
        )
        script_dir = artifact_subdir(meta.session_id, "script")
        result = art.model_dump()
        if context is not None:
            result["knowledge_context"] = context
        self._write_text_artifact(script_dir, "storyboard", json.dumps(result, ensure_ascii=False, indent=2))
        meta.artifacts["storyboard"] = result
        await progress("storyboard", "分镜已生成", 100)

    async def _stage_reference_generation(self, meta: SessionMeta, progress: ProgressCb) -> None:
        shots = (meta.artifacts.get("storyboard") or {}).get("shots", [])
        if not shots:
            raise AppError("NO_STORYBOARD", "缺少分镜，无法生成参考图")
        image_dir = artifact_subdir(meta.session_id, "image")
        request = self._generation_request(meta)
        targets = set(request.get("target_ids", [x["shot_id"] for x in shots]))
        existing = self._bound_media_artifact(meta, "reference_generation")
        refs = {x["shot_id"]: x for x in existing.get("shots", [])}
        completed = 0
        for i, shot in enumerate(shots):
            shot_id = shot["shot_id"]
            if shot_id not in targets:
                continue
            await progress("reference_generation", f"正在生成参考图 {shot_id}", int(20 + 70 * completed / max(1, len(targets))))
            old = refs.get(shot_id, {})
            valid_old = self._media_item_matches(meta, "reference_generation", "shots", old, shot)
            override = request.get("prompts", {}).get(shot_id, old.get("prompt_override", "") if valid_old else "")
            prompt = prompts.compose_shot_prompt(prompts.REFERENCE_PROMPT.format(
                description=shot.get("description", ""), prompt=override or shot.get("prompt", ""), style=meta.style))
            out = image_dir / f"ref_{i}_{uuid4().hex}.png"
            inputs = self._selected_design_paths(meta, shot)
            if len(inputs) > 3:
                raise AppError("IMAGE_REFERENCE_LIMIT", "每个分镜最多引用 3 张角色/场景图，请调整分镜引用", 400)
            if inputs:
                await asyncio.to_thread(self.image.image_to_image, [Path(p) for p in inputs], prompt, out, size=self._image_size(meta),
                                        model=model_catalog.resolve(meta.model_selection, ["image"])["image"])
            else:
                await asyncio.to_thread(self.image.text_to_image, prompt, out, size=self._image_size(meta),
                                        model=model_catalog.resolve(meta.model_selection, ["image"])["image"])
            await self._check_content(str(out))
            refs[shot_id] = {"shot_id": shot_id, "path": str(out), "selected": str(out),
                         "model_usage": model_catalog.stage_usage(meta, "reference_generation"),
                         "versions": list(dict.fromkeys([*old.get("versions", []), *([old["path"]] if old.get("path") else []), str(out)])),
                         "input_paths": inputs, "prompt": prompt, "prompt_override": override,
                         "input_versions": self._input_versions(meta, "reference_generation"),
                         "input_dependencies": self._media_item_dependencies(meta, "reference_generation", shot)}
            self._trace_paths(meta, inputs)
            meta.artifacts["reference_generation"] = ReferenceArtifact(shots=[refs[x["shot_id"]] for x in shots if x["shot_id"] in refs]).model_dump()
            self._refresh_media_validity(meta, "reference_generation")
            session_store.touch(meta)
            completed += 1
        await progress("reference_generation", "参考图生成完成", 100)

    async def _stage_video_generation(self, meta: SessionMeta, progress: ProgressCb) -> None:
        refs = (meta.artifacts.get("reference_generation") or {}).get("shots", [])
        shots = (meta.artifacts.get("storyboard") or {}).get("shots", [])
        if not refs:
            raise AppError("NO_REFERENCE", "缺少参考图，无法生成视频")
        video_dir = artifact_subdir(meta.session_id, "video")
        request = self._generation_request(meta)
        targets = set(request.get("target_ids", [x["shot_id"] for x in shots]))
        mode = meta.video_generation_mode
        existing = self._bound_media_artifact(meta, "video_generation")
        selected_model = model_catalog.resolve(meta.model_selection, ["video_" + mode])["video_" + mode]
        duration = video_contracts.capabilities(selected_model)["duration_seconds"]
        segments = {x["shot_id"]: x for x in existing.get("segments", [])}
        if mode == "start_end":
            for shot_id in targets:
                tail = (existing.get("tail_frames") or {}).get(shot_id)
                if not tail:
                    raise AppError("END_FRAME_REQUIRED", f"镜头 {shot_id} 需要选择实际尾帧", 400)
                self._require_registered_reference(meta, tail)
        completed = 0
        for i, ref in enumerate(refs):
            shot_id = ref["shot_id"]
            if shot_id not in targets:
                continue
            message = f"正在生成镜头 {i + 1}/{len(shots)}（本次已完成 {completed}/{len(targets)} 个片段）"
            percent = int(10 + 80 * completed / max(1, len(targets)))
            await progress("video_generation", message, percent)
            shot = next((s for s in shots if s.get("shot_id") == ref.get("shot_id")), {})
            old = segments.get(shot_id, {})
            valid_old = self._media_item_matches(meta, "video_generation", "segments", old, shot)
            override = request.get("prompts", {}).get(shot_id, old.get("prompt_override", "") if valid_old else "")
            prompt = prompts.compose_shot_prompt(prompts.VIDEO_PROMPT.format(
                description=shot.get("description", ""), prompt=override or shot.get("prompt", "")))
            prompt = f"片段时长：{duration}秒。\n" + prompt
            out = video_dir / f"seg_{i}_{uuid4().hex}.mp4"
            ref_path = str(self._safe_asset(meta, ref.get("selected") or ref.get("path", "")))
            inputs = self._selected_design_paths(meta, shot) if mode == "reference" else []
            tail = (existing.get("tail_frames") or {}).get(ref.get("shot_id"))
            if tail:
                self._require_registered_reference(meta, tail)
            await self._await_thread_with_heartbeat(
                progress, "video_generation", message,
                lambda: self.video.image_to_video(ref_path, prompt, out, mode,
                    reference_paths=inputs, end_image_path=tail,
                    video_ratio=meta.video_ratio, resolution=meta.resolution,
                    model=selected_model),
                start_pct=percent, max_pct=percent,
                wait_hint="正在等待视频模型返回",
            )
            if self.reviewer.enabled:
                frame = video_dir / f"seg_{i}_{uuid4().hex}_frame.jpg"
                await extract_first_frame(out, frame)
                await self._check_content(str(frame))
            source_paths = list(dict.fromkeys([ref_path, *inputs, *([tail] if tail else [])]))
            segments[shot_id] = VideoSegment(segment_id=old.get("segment_id") or f"seg_{i}", shot_id=shot_id, path=str(out), selected=str(out),
                                         model_usage=model_catalog.stage_usage(meta, "video_generation"),
                                         requested_duration_seconds=duration,
                                         versions=list(dict.fromkeys([*old.get("versions", []), *([old["path"]] if old.get("path") else []), str(out)])),
                                         prompt=prompt, input_paths=source_paths, prompt_override=override,
                                         input_versions=self._input_versions(meta, "video_generation"),
                                         input_dependencies=self._media_item_dependencies(meta, "video_generation", shot)).model_dump()
            self._trace_paths(meta, source_paths)
            meta.artifacts["video_generation"] = VideoArtifact(
                segments=[segments[x["shot_id"]] for x in shots if x["shot_id"] in segments], mode=mode,
                tail_frames=existing.get("tail_frames") or {}).model_dump()
            self._refresh_media_validity(meta, "video_generation")
            session_store.touch(meta)
            completed += 1
        await progress("video_generation", "视频片段生成完成", 100)

    async def _stage_post_production(self, meta: SessionMeta, progress: ProgressCb) -> None:
        await progress("post_production", "正在剪辑成片", 30)
        segs = (meta.artifacts.get("video_generation") or {}).get("segments", [])
        parts = [self._safe_asset(meta, s.get("selected") or s["path"], "video") for s in segs if s.get("path")]
        if not parts:
            raise AppError("NO_VIDEO_SEGMENTS", "缺少有效视频片段，无法剪辑", 409)
        video_dir = artifact_subdir(meta.session_id, "video")
        final = video_dir / f"final_{uuid4().hex}.mp4"
        await concat_videos(parts, final)
        art = PostProductionArtifact(final_video=str(final), parts=[str(p) for p in parts])
        meta.artifacts["post_production"] = art.model_dump()
        await progress("post_production", "成片已生成", 100)

    # ---------- 干预（修改后重生成） ----------

    async def intervene(self, session_id: str, stage: str, modifications: dict, progress: ProgressCb) -> SessionMeta:
        meta = self.get(session_id)
        if meta.project_type == "comic":
            from app.services.comic_workflow import ComicWorkflow
            return await ComicWorkflow(self).intervene(meta, stage, modifications, progress)
        if stage not in STAGE_ORDER:
            raise AppError("UNKNOWN_STAGE", "未知阶段", 404)
        if not isinstance(modifications, dict):
            raise AppError("INVALID_MODIFICATIONS", "修改内容必须为对象", 400)
        operation = modifications.get("operation", "regenerate")
        if operation not in {"save", "select", "regenerate"}:
            raise AppError("INVALID_OPERATION", "不支持的修改操作", 400)
        self._seed_versions(meta)
        self._require_inputs(meta, stage)
        existing = deepcopy(meta.artifacts.get(stage) or {})
        original_knowledge_context = deepcopy(existing.get("knowledge_context"))
        original_model_usage = deepcopy(existing.get("model_usage"))
        source_inputs = self._input_versions(meta, stage)
        item_inputs: dict[str, dict[str, str]] = {}
        selected_stage_version = None
        reserved = {"operation", "artifact", "selections", "stage_version_id", "tail_frames", "video_generation_mode",
                    "target_ids", "prompts", "descriptions"}
        generation_request = {k: modifications[k] for k in ("target_ids", "prompts", "descriptions") if k in modifications}
        if generation_request and operation != "regenerate":
            raise AppError("INVALID_GENERATION_REQUEST", "重生成范围与提示词仅用于重新生成操作", 400)
        generation_request = self._validate_generation_request(meta, stage, generation_request)
        edits = modifications.get("artifact", {k: v for k, v in modifications.items() if k not in reserved})
        if not isinstance(edits, dict):
            raise AppError("INVALID_MODIFICATIONS", "产物修改必须为对象", 400)
        if edits:
            if stage not in {"script_generation", "storyboard"}:
                raise AppError("ARTIFACT_EDIT_UNSUPPORTED", "仅支持编辑剧本和分镜；素材请使用已有版本选择", 400)
            existing = self._validated_text_artifact(stage, {**existing, **edits})
        if operation == "save":
            if stage not in {"script_generation", "storyboard"}:
                raise AppError("ARTIFACT_EDIT_UNSUPPORTED", "仅支持保存剧本和分镜编辑", 400)
            existing = self._validated_text_artifact(stage, existing)
        if (edits or operation == "save") and original_knowledge_context is not None:
            # 编辑正文不会伪造或丢弃服务端检索证据；来源只代表此前提供给模型的资料。
            existing["knowledge_context"] = original_knowledge_context
            existing["knowledge_context"]["edited_since_generation"] = True
        if (edits or operation == "save") and original_model_usage is not None:
            existing["model_usage"] = original_model_usage
        if operation == "select":
            if edits:
                raise AppError("INVALID_MODIFICATIONS", "选版不能同时覆盖产物内容", 400)
            version_id = modifications.get("stage_version_id")
            if version_id:
                versions = self.public_versions(meta, stage)
                version = next((v for v in versions if v["version_id"] == version_id), None)
                if not version:
                    raise AppError("INVALID_ASSET_VERSION", "阶段版本不存在", 400)
                existing = deepcopy(version["artifact"])
                self._validate_media_artifact(meta, stage, existing)
                source_inputs = deepcopy(version.get("input_versions", {}))
                item_inputs = deepcopy(version.get("item_input_versions", {}))
                selected_stage_version = version_id
            elif modifications.get("selections"):
                current_version = next((v for v in self.public_versions(meta, stage)
                                        if v["version_id"] == meta.selected_versions.get(stage)), {})
                source_inputs = deepcopy(current_version.get("input_versions", {}))
                item_inputs = self._item_lineage(meta, stage, existing, current_version)
                self._select_items(meta, stage, existing, modifications["selections"])
                for choice in modifications["selections"]:
                    selected_item = next(x for x in existing[choice["collection"]]
                                         if (x.get("segment_id") if stage == "video_generation" else x.get("id") if stage == "character_design" else x.get("shot_id")) == choice["id"])
                    lineage_id = selected_item.get("shot_id") if stage == "video_generation" else choice["id"]
                    key = f"{choice['collection']}:{lineage_id}"
                    item_inputs[key] = self._path_lineage(meta, stage, choice["collection"], choice["id"], choice["path"])
                mismatched = [x for x in item_inputs.values() if not self._lineage_matches(meta, stage, x)]
                if mismatched:
                    # 单个镜头旧版不能伪装成全阶段使用当前上游；详细来源另存逐项映射。
                    source_inputs = deepcopy(mismatched[0])
            else:
                raise AppError("INVALID_ASSET_VERSION", "请选择已有版本", 400)
        if "video_generation_mode" in modifications:
            mode = modifications["video_generation_mode"]
            if stage != "video_generation" or mode not in {"first_frame", "start_end", "reference"}:
                raise AppError("VIDEO_MODE_UNSUPPORTED", "不支持的视频生成模式", 400)
            meta.video_generation_mode = mode
        if "tail_frames" in modifications:
            tails = modifications["tail_frames"]
            if stage != "video_generation" or not isinstance(tails, dict):
                raise AppError("INVALID_MODIFICATIONS", "尾帧设置必须为镜头映射", 400)
            valid_shots = {x.get("shot_id") for x in (meta.artifacts.get("storyboard") or {}).get("shots", [])}
            for shot_id, path in tails.items():
                if shot_id not in valid_shots or not isinstance(path, str):
                    raise AppError("INVALID_MODIFICATIONS", "尾帧镜头不合法", 400)
                self._require_registered_reference(meta, path)
            existing["tail_frames"] = tails
        meta.artifacts[stage] = existing
        if operation == "regenerate":
            # 编辑内容落在当前产物与执行记录里；只有最终生成成功才登记版本。
            session_store.touch(meta)
            return await self.execute_stage(session_id, stage, progress, generation_request=generation_request)
        self._invalidate_downstream(meta, stage)
        stale_selection = operation == "select" and (
            not self._lineage_matches(meta, stage, source_inputs)
            or any(not self._lineage_matches(meta, stage, x) for x in item_inputs.values()))
        if selected_stage_version:
            # 选择现有完整生成版本，保留它的身份和真实输入绑定。
            meta.selected_versions[stage] = selected_stage_version
        elif operation == "select":
            # 点选单张素材只更新当前产物，不新增版本；血缘记入执行记录。
            pass
        # save：只改当前产物，不新增版本；上次完整生成版本仍可从下拉恢复。
        if operation == "select":
            meta.execution_inputs.append({"execution_id": uuid4().hex, "stage": stage, "operation": "select",
                                          "parent_execution_id": self._parent_execution_id(),
                                          "source_version_id": selected_stage_version,
                                          "input_versions": deepcopy(source_inputs), "item_input_versions": deepcopy(item_inputs),
                                          "observed_input_versions": self._input_versions(meta, stage),
                                          "status": "stale" if stale_selection else "completed", "finished_at": time.time()})
        if stale_selection:
            if stage not in meta.stale_stages:
                meta.stale_stages.append(stage)
            if stage in meta.stages_completed:
                meta.stages_completed.remove(stage)
        else:
            if stage in meta.stale_stages:
                meta.stale_stages.remove(stage)
            if stage not in meta.stages_completed:
                meta.stages_completed.append(stage)
        meta.current_stage = stage
        meta.status = "idle" if stale_selection else "stage_completed"
        meta.error = None
        if stage in {"character_design", "reference_generation", "video_generation"}:
            meta.artifacts[stage] = self._bound_media_artifact(meta, stage)
            self._refresh_media_validity(meta, stage)
            meta.status = "idle" if stage in meta.stale_stages else "stage_completed"
        if operation == "select":
            meta.execution_inputs[-1].update(status="completed", remaining_stale_items=meta.artifacts[stage].get("stale_items", []))
        self._refresh_downstream_media(meta, stage)
        if stage in {"script_generation", "storyboard"}:
            self._write_text_artifact(artifact_subdir(meta.session_id, "script"),
                                      "script" if stage == "script_generation" else "storyboard",
                                      json.dumps(existing, ensure_ascii=False, indent=2))
        session_store.touch(meta)
        await progress(stage, "已选历史版本；上游已变化，请重新生成" if stale_selection else "修改已保存" if operation == "save" else "版本已选择", 100)
        return meta

    def _lineage_matches(self, meta: SessionMeta, stage: str, inputs: dict) -> bool:
        return all(dep not in meta.stale_stages and inputs.get(dep) == meta.selected_versions.get(dep)
                   and bool(inputs.get(dep)) for dep in STAGE_DEPENDENCIES[stage])

    def _item_lineage(self, meta: SessionMeta, stage: str, artifact: dict, version: dict) -> dict:
        collections = {"character_design": ["characters", "settings"], "reference_generation": ["shots"],
                       "video_generation": ["segments"]}.get(stage, [])
        id_key = "id" if stage == "character_design" else "shot_id"
        specific = version.get("item_input_versions", {})
        return {f"{collection}:{item.get(id_key)}": deepcopy(item.get("input_versions") or specific.get(f"{collection}:{item.get(id_key)}", version.get("input_versions", {})))
                for collection in collections for item in artifact.get(collection, [])}

    def _path_lineage(self, meta: SessionMeta, stage: str, collection: str, item_id: str, path: str) -> dict:
        id_key = "id" if stage == "character_design" else "shot_id" if stage == "reference_generation" else "segment_id"
        versions = meta.artifact_versions.get(stage, [])
        # 优先生成时登记的来源；versions 路径列表只证明文件存在，不证明它使用了最新输入。
        ordered = [v for v in versions if v.get("reason", "").startswith("generated")] + [v for v in versions if not v.get("reason", "").startswith("generated")]
        for version in ordered:
            item = next((x for x in version.get("artifact", {}).get(collection, []) if x.get(id_key) == item_id), {})
            if (item.get("selected") or item.get("path")) == path:
                lineage_id = item.get("shot_id") if stage == "video_generation" else item_id
                return deepcopy(item.get("input_versions") or version.get("item_input_versions", {}).get(f"{collection}:{lineage_id}", version.get("input_versions", {})))
        return {}

    def _path_generation_item(self, meta: SessionMeta, stage: str, collection: str, item_id: str, path: str) -> dict:
        key = "id" if stage == "character_design" else "shot_id" if stage == "reference_generation" else "segment_id"
        for version in meta.artifact_versions.get(stage, []):
            if not version.get("reason", "").startswith("generated"):
                continue
            for item in version.get("artifact", {}).get(collection, []):
                if item.get(key) == item_id and (item.get("selected") or item.get("path")) == path:
                    return item
        return {}

    def _validated_text_artifact(self, stage: str, artifact: dict) -> dict:
        artifact = {key: value for key, value in artifact.items() if key not in {"knowledge_context", "model_usage"}}
        model = ScriptArtifact if stage == "script_generation" else StoryboardArtifact
        nested = {"characters": ("name", "character_id", "description", "role"),
                  "settings": ("name", "setting_id", "description"),
                  "episodes": ("episode_number", "act_title", "content")} if stage == "script_generation" else {
                      "shots": ("shot_id", "episode_number", "description", "prompt", "character_ids", "setting_ids")}
        allowed = set(model.model_fields)
        if set(artifact) - allowed:
            raise AppError("INVALID_MODIFICATIONS", "产物包含不支持的字段", 400)
        for collection, keys in nested.items():
            items = artifact.get(collection, [])
            if not isinstance(items, list):
                raise AppError("INVALID_MODIFICATIONS", "剧本/分镜列表结构不合法", 400)
            for item in items:
                if not isinstance(item, dict) or set(item) - set(keys):
                    raise AppError("INVALID_MODIFICATIONS", "剧本/分镜只能修改文本与角色、场景引用", 400)
        try:
            art = model.model_validate(artifact)
        except Exception as e:
            raise AppError("INVALID_MODIFICATIONS", "剧本或分镜结构不合法", 400) from e
        if isinstance(art, ScriptArtifact):
            self._normalize_script_ids(art)
        elif len({s.shot_id for s in art.shots}) != len(art.shots):
            raise AppError("DUPLICATE_SHOT_ID", "分镜 ID 不能重复", 400)
        return art.model_dump()

    def _validate_media_artifact(self, meta: SessionMeta, stage: str, artifact: dict) -> None:
        if stage in {"script_generation", "storyboard"}:
            return
        collections = {"character_design": ["characters", "settings"], "reference_generation": ["shots"],
                       "video_generation": ["segments"]}.get(stage, [])
        for collection in collections:
            for item in artifact.get(collection, []):
                for path in [item.get("selected"), item.get("path"), *item.get("versions", [])]:
                    if path:
                        self._safe_asset(meta, path, "video" if stage == "video_generation" else "image")
        if stage == "post_production" and artifact.get("final_video"):
            self._safe_asset(meta, artifact["final_video"], "video")

    def _select_items(self, meta: SessionMeta, stage: str, artifact: dict, selections: list[dict]) -> None:
        collections = {"character_design": {"characters", "settings"}, "reference_generation": {"shots"},
                       "video_generation": {"segments"}}.get(stage, set())
        if not isinstance(selections, list):
            raise AppError("INVALID_MODIFICATIONS", "素材选版必须为列表", 400)
        for choice in selections:
            if not isinstance(choice, dict) or choice.get("collection") not in collections:
                raise AppError("INVALID_MODIFICATIONS", "素材分类不合法", 400)
            collection, item_id, path = choice["collection"], choice.get("id"), choice.get("path")
            id_key = "id" if stage == "character_design" else "shot_id" if stage == "reference_generation" else "segment_id"
            item = next((x for x in artifact.get(collection, []) if x.get(id_key) == item_id), None)
            if item is None:
                raise AppError("INVALID_ASSET_VERSION", "素材不存在", 400)
            registered = set(item.get("versions", [])) | {item.get("selected"), item.get("path")}
            for version in meta.artifact_versions.get(stage, []):
                old = next((x for x in version.get("artifact", {}).get(collection, []) if x.get(id_key) == item_id), {})
                registered.update([*old.get("versions", []), old.get("selected"), old.get("path")])
            if not path or path not in registered:
                raise AppError("INVALID_ASSET_VERSION", "只能选择该素材已有版本", 400)
            self._safe_asset(meta, path, "video" if stage == "video_generation" else "image")
            item["selected"] = path
            item["input_versions"] = self._path_lineage(meta, stage, collection, item_id, path)
            original = self._path_generation_item(meta, stage, collection, item_id, path)
            for field in ("input_dependencies", "input_paths", "prompt", "prompt_override", "description", "model_usage"):
                if field in original:
                    item[field] = deepcopy(original[field])
                elif field in {"input_dependencies", "model_usage"}:
                    item.pop(field, None)
            if stage != "character_design":
                item["path"] = path
            item["versions"] = list(dict.fromkeys([*item.get("versions", []), path]))
