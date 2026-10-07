"""漫剧视频工作流：独立依赖图，复用项目归属、模型客户端与版本设施。"""
from __future__ import annotations

import asyncio
import json
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from app.core import config
from app.core.errors import AppError
from app.core.path_security import artifact_subdir, ensure_inside
from app.models.tts_client import TTSClient
from app.schemas.session import ComicStoryboardArtifact
from app.services import prompts, session_store, model_catalog

COMIC_STAGES = ["script_generation", "character_design", "comic_storyboard", "comic_panels", "comic_audio", "comic_composition"]
COMIC_DEPENDENCIES = {
    "script_generation": [], "character_design": ["script_generation"],
    "comic_storyboard": ["script_generation"],
    "comic_panels": ["character_design", "comic_storyboard"],
    "comic_audio": ["comic_storyboard"],
    "comic_composition": ["comic_panels", "comic_audio", "comic_storyboard"],
}


class ComicWorkflow:
    def __init__(self, host):
        self.host = host

    def _stage(self, stage):
        if stage not in COMIC_STAGES:
            raise AppError("UNKNOWN_STAGE", "漫剧项目不支持此阶段", 404)

    def _require(self, meta, stage):
        for dependency in COMIC_DEPENDENCIES[stage]:
            if not meta.artifacts.get(dependency):
                raise AppError("MISSING_STAGE_INPUT", f"缺少上游产物：{dependency}", 409)
            if dependency in meta.stale_stages:
                raise AppError("STALE_STAGE_INPUT", f"上游产物已失效：{dependency}", 409)

    def _versions(self, meta, stage):
        return {key: meta.selected_versions[key] for key in COMIC_DEPENDENCIES[stage] if key in meta.selected_versions}

    def _stage_inputs(self, meta, stage):
        if stage == "script_generation":
            return {"idea": meta.idea, "style": meta.style, "episodes": 1}
        if stage in {"character_design", "comic_storyboard"}:
            return {"script": self._script_input(meta), "style": meta.style}
        dependency = {"comic_panels": self._panel_dependencies, "comic_audio": self._audio_dependencies,
                      "comic_composition": self._composition_dependencies}[stage]
        return {"shots": [dependency(meta, shot) for shot in self._shots(meta)]}

    def _script_input(self, meta):
        return {key: deepcopy(value) for key, value in meta.artifacts.get("script_generation", {}).items() if key not in {"knowledge_context", "model_usage"}}

    def _shots(self, meta):
        return (meta.artifacts.get("comic_storyboard") or {}).get("shots", [])

    def _validate_board(self, meta, value):
        if not isinstance(value, dict) or set(value) - {"shots", "voice_map", "knowledge_context", "input_dependencies", "model_usage"}:
            raise AppError("INVALID_COMIC_STORYBOARD", "漫剧分镜结构不合法", 422)
        try:
            result = ComicStoryboardArtifact.model_validate({key: value[key] for key in ("shots", "voice_map") if key in value}).model_dump()
        except Exception as error:
            raise AppError("INVALID_COMIC_STORYBOARD", "分镜需包含1–12镜头、合法运镜与0–8句非空对白；台词ID及声线须有效", 422) from error
        script = self._script_input(meta)
        character_ids = {item["character_id"] for item in script.get("characters", [])}
        setting_ids = {item["setting_id"] for item in script.get("settings", [])}
        speakers = character_ids | {"narrator"}
        if set(result["voice_map"]) - speakers:
            raise AppError("INVALID_COMIC_SPEAKER", "声线只能绑定本项目角色或 narrator", 422)
        for shot in result["shots"]:
            if set(shot["character_ids"]) - character_ids or set(shot["setting_ids"]) - setting_ids:
                raise AppError("UNKNOWN_DESIGN_REFERENCE", "分镜引用了不存在的角色或场景", 422)
            if len(shot["character_ids"]) + len(shot["setting_ids"]) > 3:
                raise AppError("IMAGE_REFERENCE_LIMIT", "单镜头最多引用3张角色/场景图", 422)
            if any(line["speaker_id"] not in speakers for line in shot["dialogues"]):
                raise AppError("INVALID_COMIC_SPEAKER", "对白说话人必须是已有角色或 narrator", 422)
        for speaker in speakers:
            result["voice_map"].setdefault(speaker, "zh-CN-XiaoxiaoNeural" if speaker == "narrator" else "zh-CN-YunxiNeural")
        result["input_dependencies"] = {"script": script, "style": meta.style}
        return result

    def _safe(self, meta, path, kind="image"):
        if not isinstance(path, str) or not path:
            raise AppError("ASSET_NOT_FOUND", "素材路径缺失", 409)
        root = ((config.IMAGE_DIR if kind == "image" else config.VIDEO_DIR) / meta.session_id).resolve()
        result = ensure_inside(root, Path(path))
        allowed = {".png", ".jpg", ".jpeg", ".webp"} if kind == "image" else {".mp3", ".wav", ".mp4", ".srt"}
        if not result.is_file() or result.suffix.lower() not in allowed:
            raise AppError("ASSET_NOT_FOUND", "素材文件不存在或格式不支持", 409)
        return result

    def _design_inputs(self, meta, shot):
        paths = []
        design = meta.artifacts.get("character_design", {})
        for collection, ids_key in (("characters", "character_ids"), ("settings", "setting_ids")):
            ids = shot.get(ids_key, [])
            for item_id in ids:
                item = next((value for value in design.get(collection, []) if value.get("id") == item_id), {})
                if not item.get("selected") or item["selected"] not in item.get("versions", []):
                    raise AppError("INVALID_ASSET_VERSION", "角色参考图未登记或尚未生成", 409)
                paths.append(str(self._safe(meta, item["selected"])))
        return paths

    def _panel_dependencies(self, meta, shot):
        return {"visual": {key: deepcopy(shot[key]) for key in ("shot_id", "description", "prompt", "character_ids", "setting_ids")},
                "design_paths": self._design_inputs(meta, shot), "style": meta.style, "size": self.host._image_size(meta)}

    def _audio_dependencies(self, meta, shot):
        voices = meta.artifacts["comic_storyboard"]["voice_map"]
        return {"lines": [{"line_id": line["line_id"], "speaker_id": line["speaker_id"], "text": line["text"],
                            "voice": voices[line["speaker_id"]]} for line in shot["dialogues"]]}

    def _composition_dependencies(self, meta, shot):
        panel = next((item for item in meta.artifacts.get("comic_panels", {}).get("shots", []) if item["shot_id"] == shot["shot_id"]), {})
        audio = next((item for item in meta.artifacts.get("comic_audio", {}).get("shots", []) if item["shot_id"] == shot["shot_id"]), {})
        names = {character["character_id"]: character["name"] for character in self._script_input(meta).get("characters", [])}
        names["narrator"] = "旁白"
        lines = [{**deepcopy(line), "speaker": names[line["speaker_id"]]} for line in audio.get("lines", [])]
        return {"image_path": panel.get("selected") or panel.get("path"), "lines": lines,
                "motion": shot["motion"], "silent_duration": shot["silent_duration"] if not shot["dialogues"] else None,
                "ratio": meta.video_ratio, "resolution": meta.resolution.upper()}

    def refresh(self, meta):
        """按实际变化分别更新画图、配音和合成，声线不影响角色或面板。"""
        if meta.artifacts.get("character_design"):
            self.host._refresh_media_validity(meta, "character_design")
        board = meta.artifacts.get("comic_storyboard")
        if board:
            valid = board.get("input_dependencies") == {"script": self._script_input(meta), "style": meta.style}
            self._mark(meta, "comic_storyboard", [] if valid else ["storyboard"])
        for stage, dependency in (("comic_panels", self._panel_dependencies), ("comic_audio", self._audio_dependencies)):
            artifact = meta.artifacts.get(stage)
            if artifact is None:
                continue
            stale = []
            items = {item["shot_id"]: item for item in artifact.get("shots", [])}
            for shot in self._shots(meta):
                item = items.get(shot["shot_id"], {})
                try:
                    valid = item.get("input_dependencies") == dependency(meta, shot) and "comic_storyboard" not in meta.stale_stages
                    if stage == "comic_panels":
                        valid = valid and bool(item.get("path"))
                        self._safe(meta, item.get("selected") or item.get("path"))
                        referenced = set(shot["character_ids"] + shot["setting_ids"])
                        valid = valid and not referenced.intersection(meta.artifacts.get("character_design", {}).get("stale_items", []))
                    else:
                        valid = valid and len(item.get("lines", [])) == len(shot["dialogues"])
                        for line in item.get("lines", []):
                            self._safe(meta, line["path"], "video")
                except AppError:
                    valid = False
                if not valid:
                    stale.append(shot["shot_id"])
            self._mark(meta, stage, stale)
        composition = meta.artifacts.get("comic_composition")
        if composition:
            current = [self._composition_dependencies(meta, shot) for shot in self._shots(meta)]
            valid = composition.get("input_dependencies") == current and not any(stage in meta.stale_stages for stage in COMIC_DEPENDENCIES["comic_composition"])
            self._mark(meta, "comic_composition", [] if valid else [shot["shot_id"] for shot in self._shots(meta)])

    def _mark(self, meta, stage, stale):
        meta.artifacts[stage]["stale_items"] = stale
        if stale:
            if stage not in meta.stale_stages:
                meta.stale_stages.append(stage)
            if stage in meta.stages_completed:
                meta.stages_completed.remove(stage)
        else:
            if stage in meta.stale_stages:
                meta.stale_stages.remove(stage)
            if stage not in meta.stages_completed:
                meta.stages_completed.append(stage)

    def continue_session(self, meta):
        self.refresh(meta)
        if meta.status != "stage_completed" or meta.current_stage in meta.stale_stages:
            raise AppError("NOT_READY", "当前漫剧阶段尚未完成或已失效", 409)
        index = COMIC_STAGES.index(meta.current_stage)
        pending = [stage for stage in COMIC_STAGES[index + 1:] if stage not in meta.stages_completed or stage in meta.stale_stages]
        meta.current_stage = pending[0] if pending else meta.current_stage
        meta.status = "idle" if pending else "session_completed"
        session_store.touch(meta)
        return meta

    def _request(self, meta, stage, request):
        if stage == "character_design":
            return self.host._validate_generation_request(meta, stage, request)
        allowed = {"target_ids", "prompts"} if stage == "comic_panels" else {"target_ids"} if stage == "comic_audio" else set()
        if not isinstance(request, dict) or set(request) - allowed:
            raise AppError("INVALID_GENERATION_REQUEST", "当前漫剧阶段不支持此重生成参数", 422)
        if not request:
            return {}
        known = {shot["shot_id"] for shot in self._shots(meta)}
        targets = request.get("target_ids", list(known))
        if not isinstance(targets, list) or not targets or any(not isinstance(value, str) or value not in known for value in targets) or len(set(targets)) != len(targets):
            raise AppError("INVALID_GENERATION_TARGETS", "请选择非空、无重复的有效镜头", 422)
        overrides = request.get("prompts", {})
        if not isinstance(overrides, dict) or any(key not in targets or not isinstance(value, str) or not value.strip() or len(value) > 4000 for key, value in overrides.items()):
            raise AppError("INVALID_GENERATION_PROMPTS", "镜头提示词必须是选定镜头的非空文本", 422)
        return {"target_ids": targets, **({"prompts": overrides} if overrides else {})}

    async def execute(self, meta, stage, progress, request):
        self._stage(stage)
        self.host._seed_versions(meta)
        self.refresh(meta)
        self._require(meta, stage)
        request = self._request(meta, stage, request)
        inputs = self._versions(meta, stage)
        usage = model_catalog.stage_usage(meta, stage)
        record = {"execution_id": uuid4().hex, "parent_execution_id": self.host._parent_execution_id(), "model_usage": usage,
                  "project_type": "comic", "stage_inputs": self._stage_inputs(meta, stage),
                  "stage": stage, "started_at": time.time(), "status": "running", "input_versions": inputs,
                  "input_artifact": deepcopy(meta.artifacts.get(stage)), "input_artifact_version": meta.selected_versions.get(stage),
                  "input_stale_stages": list(meta.stale_stages), "input_stages_completed": list(meta.stages_completed), "generation_request": request}
        meta.execution_inputs.append(record)
        meta.current_stage, meta.status, meta.error = stage, "running", None
        session_store.touch(meta)
        async def generate():
            if stage in ("script_generation", "character_design"):
                await getattr(self.host, "_stage_" + stage)(meta, progress)
                if stage == "script_generation":
                    episodes = meta.artifacts[stage].get("episodes", [])
                    if len(episodes) != 1 or episodes[0]["episode_number"] != 1:
                        raise AppError("INVALID_COMIC_SCRIPT", "漫剧首期须生成且仅生成第1集", 422)
            else:
                await getattr(self, "_" + stage)(meta, progress, request)
        try:
            await self.host._run_stage_agents(meta, stage, progress, generate)
            meta.artifacts[stage]["model_usage"] = deepcopy(usage)
            self.refresh(meta)
            if stage == "script_generation" and stage not in meta.stages_completed:
                meta.stages_completed.append(stage)
            version_id = self.host._record_version(meta, stage, "generated", inputs)
            record.update(status="completed", output_version=version_id, finished_at=time.time(),
                          remaining_stale_items=meta.artifacts[stage].get("stale_items", []))
            meta.status = "idle" if stage in meta.stale_stages else "stage_completed"
            self.host._discard_failed_attempt_media(meta, stage)
            session_store.touch(meta)
            return meta
        except BaseException as error:
            self.host._abandon_failed_execution(meta, stage, record)
            meta.status = "failed"
            meta.error = error.message if isinstance(error, AppError) else "漫剧阶段执行失败，请重试"
            session_store.touch(meta)
            if isinstance(error, (AppError, asyncio.CancelledError)):
                raise
            raise AppError("COMIC_STAGE_FAILED", "漫剧阶段执行失败，请检查服务后重试", 500) from error

    async def _comic_storyboard(self, meta, progress, request):
        context = await self.host._knowledge_context(meta, "comic_storyboard", meta.idea + " 漫剧分镜 人物对白 漫画构图", progress)
        system = """你是漫剧视频编导。只输出 JSON，单集1–12镜头，每镜头0–8句对白。
格式：{\"shots\":[{\"shot_id\":\"s1\",\"episode_number\":1,\"description\":\"漫画画面描述\",\"prompt\":\"画面提示词，不画文字\",\"character_ids\":[\"c1\"],\"setting_ids\":[],\"motion\":\"push_in\",\"silent_duration\":3,\"dialogues\":[{\"line_id\":\"line1\",\"speaker_id\":\"c1\",\"text\":\"台词\",\"emotion\":\"仅备注\"}]}],\"voice_map\":{}}
角色与场景ID只能来自剧本；旁白使用narrator；镜头/台词ID只能ASCII字母数字下划线短横线且唯一。每镜头引用角色场景合计最多3张。motion仅static/push_in/pan_left/pan_right；台词不含动作或画面说明。不能生成文件路径。"""
        system += "\n" + prompts.SHOT_COUNT_RULE + "漫剧仅支持单集1–12镜头；未指定数量时按剧情需要安排，不固定为某个镜头数。"
        system += "\nvoice_map 可以是空对象；如填写，键只能是剧本角色ID或 narrator，值必须逐字使用以下可选声线的 voice 字段。不得自造声线ID或使用性别、年龄、情绪等描述替代ID。可选声线：\n" + json.dumps(TTSClient.list_voices(), ensure_ascii=False)
        user = "用户创意（保留明确的镜头数量等创作要求）：\n" + meta.idea + "\n剧本：\n" + json.dumps(self._script_input(meta), ensure_ascii=False) + "\n用户确认/修改的现有分镜（优先保留）：\n" + self.host._creative_input(meta.artifacts.get("comic_storyboard"))
        art = await self.host._await_thread_with_heartbeat(progress, "comic_storyboard", "正在规划漫剧镜头和对白", self.host.llm.generate_json,
                                                        system + prompts.SAFETY_RULE + prompts.KNOWLEDGE_RULE,
                                                        prompts.with_knowledge(user, context), ComicStoryboardArtifact, start_pct=10, max_pct=90,
                                                        model=model_catalog.resolve(meta.model_selection, ["text"])["text"])
        result = self._validate_board(meta, art.model_dump())
        if context is not None:
            result["knowledge_context"] = context
        meta.artifacts["comic_storyboard"] = result
        await progress("comic_storyboard", "漫剧分镜已生成", 100)

    async def _comic_panels(self, meta, progress, request):
        shots = self._shots(meta)
        targets = set(request.get("target_ids", [shot["shot_id"] for shot in shots]))
        existing = {item["shot_id"]: deepcopy(item) for item in meta.artifacts.get("comic_panels", {}).get("shots", [])}
        directory = artifact_subdir(meta.session_id, "image")
        for index, shot in enumerate(shots):
            if shot["shot_id"] not in targets:
                continue
            await progress("comic_panels", f"绘制漫画镜头 {shot['shot_id']}", int(10 + 80 * index / len(shots)))
            inputs = self._design_inputs(meta, shot)
            override = request.get("prompts", {}).get(shot["shot_id"], "")
            prompt = f"漫画/漫剧关键帧，{meta.style}，{shot['description']}。{override or shot['prompt']}。不要在画面中绘制字幕、气泡或文字。"
            output = directory / f"comic_panel_{uuid4().hex}.png"
            if inputs:
                await asyncio.to_thread(self.host.image.image_to_image, [Path(path) for path in inputs], prompt, output, size=self.host._image_size(meta),
                                        model=model_catalog.resolve(meta.model_selection, ["image"])["image"])
            else:
                await asyncio.to_thread(self.host.image.text_to_image, prompt, output, size=self.host._image_size(meta),
                                        model=model_catalog.resolve(meta.model_selection, ["image"])["image"])
            await self.host._check_content(str(output))
            old = existing.get(shot["shot_id"], {})
            existing[shot["shot_id"]] = {"shot_id": shot["shot_id"], "path": str(output), "selected": str(output),
                                         "model_usage": model_catalog.stage_usage(meta, "comic_panels"),
                                         "versions": list(dict.fromkeys([*old.get("versions", []), str(output)])), "prompt": prompt,
                                         "input_paths": inputs, "input_dependencies": self._panel_dependencies(meta, shot)}
            self.host._trace_paths(meta, inputs)
            meta.artifacts["comic_panels"] = {"shots": [existing[value["shot_id"]] for value in shots if value["shot_id"] in existing]}
            session_store.touch(meta)
        await progress("comic_panels", "漫画镜头已生成", 100)

    async def _comic_audio(self, meta, progress, request):
        shots = self._shots(meta)
        targets = set(request.get("target_ids", [shot["shot_id"] for shot in shots]))
        existing = {item["shot_id"]: deepcopy(item) for item in meta.artifacts.get("comic_audio", {}).get("shots", [])}
        directory = artifact_subdir(meta.session_id, "video")
        for index, shot in enumerate(shots):
            if shot["shot_id"] not in targets:
                continue
            await progress("comic_audio", f"生成对白配音 {shot['shot_id']}", int(10 + 80 * index / len(shots)))
            dependency = self._audio_dependencies(meta, shot)
            previous = {line["line_id"]: line for line in existing.get(shot["shot_id"], {}).get("lines", [])}
            lines = []
            for definition in dependency["lines"]:
                old = previous.get(definition["line_id"], {})
                if all(old.get(key) == value for key, value in definition.items()) and old.get("path"):
                    self._safe(meta, old["path"], "video")
                    lines.append(deepcopy(old))
                    continue
                output = directory / f"comic_voice_{uuid4().hex}.mp3"
                await self.host.tts.synthesize(definition["text"], output, voice=definition["voice"])
                self._safe(meta, str(output), "video")
                lines.append({**definition, "path": str(output)})
            existing[shot["shot_id"]] = {"shot_id": shot["shot_id"], "lines": lines, "input_dependencies": dependency}
            meta.artifacts["comic_audio"] = {"shots": [existing[value["shot_id"]] for value in shots if value["shot_id"] in existing]}
            session_store.touch(meta)
        await progress("comic_audio", "漫剧配音已生成", 100)

    async def _comic_composition(self, meta, progress, request):
        from app.services.comic_renderer import render_comic_shot, concat_comic_shots
        directory = artifact_subdir(meta.session_id, "video")
        previous = {item["shot_id"]: item for item in meta.artifacts.get("comic_composition", {}).get("segments", [])}
        rendered, dependencies = [], []
        for index, shot in enumerate(self._shots(meta)):
            await progress("comic_composition", f"合成运镜和字幕 {shot['shot_id']}", int(10 + 75 * index / len(self._shots(meta))))
            dependency = self._composition_dependencies(meta, shot)
            dependencies.append(dependency)
            old = previous.get(shot["shot_id"], {})
            if old.get("input_dependencies") == dependency and old.get("path"):
                self._safe(meta, old["path"], "video")
                rendered.append(deepcopy(old))
                continue
            image = self._safe(meta, dependency["image_path"])
            lines = [{**line, "audio_path": str(self._safe(meta, line["path"], "video"))} for line in dependency["lines"]]
            result = await render_comic_shot(image, lines, directory / f"comic_shot_{uuid4().hex}.mp4",
                                              ratio=meta.video_ratio, resolution=meta.resolution, motion=shot["motion"], silent_duration=shot["silent_duration"])
            rendered.append({**result, "shot_id": shot["shot_id"], "input_dependencies": dependency})
        final = await concat_comic_shots(rendered, directory / f"comic_final_{uuid4().hex}.mp4")
        meta.artifacts["comic_composition"] = {**final, "final_video": final["path"], "subtitle_path": final["srt_path"],
                                               "parts": [item["path"] for item in rendered], "segments": rendered, "input_dependencies": dependencies}
        await progress("comic_composition", "漫剧视频已生成", 100)

    async def intervene(self, meta, stage, modifications, progress):
        self._stage(stage)
        if not isinstance(modifications, dict) or set(modifications) - {"operation", "artifact", "target_ids", "prompts", "descriptions", "selections", "stage_version_id"}:
            raise AppError("INVALID_MODIFICATIONS", "漫剧修改参数不合法，项目类型创建后不可切换", 422)
        operation = modifications.get("operation", "regenerate")
        if operation not in {"save", "select", "regenerate"}:
            raise AppError("INVALID_OPERATION", "请选择 save、select 或 regenerate", 422)
        self.host._seed_versions(meta)
        self.refresh(meta)
        self._require(meta, stage)
        request = {key: modifications[key] for key in ("target_ids", "prompts", "descriptions") if key in modifications}
        if request and operation != "regenerate":
            raise AppError("INVALID_GENERATION_REQUEST", "只有生成操作支持指定范围", 422)
        request = self._request(meta, stage, request)
        if "artifact" in modifications:
            if stage not in {"script_generation", "comic_storyboard"} or operation == "select":
                raise AppError("ARTIFACT_EDIT_UNSUPPORTED", "仅可编辑剧本或结构化漫剧分镜", 422)
            original = meta.artifacts.get(stage) or {}
            if not isinstance(modifications["artifact"], dict):
                raise AppError("INVALID_MODIFICATIONS", "artifact 必须是对象", 422)
            merged = {**deepcopy(original), **modifications["artifact"]}
            if stage == "script_generation":
                value = self.host._validated_text_artifact(stage, merged)
                if len(value["episodes"]) != 1 or value["episodes"][0]["episode_number"] != 1:
                    raise AppError("INVALID_COMIC_SCRIPT", "漫剧首期只支持单集", 422)
            else:
                merged.pop("stale_items", None)
                value = self._validate_board(meta, merged)
            ignored = {"knowledge_context", "stale_items", "model_usage"}
            changed = ({key: item for key, item in original.items() if key not in ignored}
                       != {key: item for key, item in value.items() if key not in ignored})
            if original.get("knowledge_context"):
                value["knowledge_context"] = deepcopy(original["knowledge_context"])
                if changed:
                    value["knowledge_context"]["edited_since_generation"] = True
            if "model_usage" in original:
                value["model_usage"] = deepcopy(original["model_usage"])
            if changed:
                meta.selected_versions.pop(stage, None)
            meta.artifacts[stage] = value
        if operation == "save":
            if stage not in {"script_generation", "comic_storyboard"} or not meta.artifacts.get(stage):
                raise AppError("ARTIFACT_EDIT_UNSUPPORTED", "只有剧本或漫剧分镜可保存", 422)
        elif operation == "select":
            version_id = modifications.get("stage_version_id")
            if version_id:
                version = next((value for value in self.host.public_versions(meta, stage) if value["version_id"] == version_id), None)
                if version is None:
                    raise AppError("INVALID_ASSET_VERSION", "完整生成版本不存在", 422)
                for path in self.host._paths_in_artifact(stage, version["artifact"]):
                    self._safe(meta, path, "image" if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else "video")
                meta.artifacts[stage] = deepcopy(version["artifact"])
                meta.selected_versions[stage] = version_id
            elif stage == "character_design" and modifications.get("selections"):
                self.host._select_items(meta, stage, meta.artifacts[stage], modifications["selections"])
            elif stage == "comic_panels" and modifications.get("selections"):
                for selection in modifications["selections"]:
                    if not isinstance(selection, dict) or selection.get("collection") != "shots":
                        raise AppError("INVALID_ASSET_VERSION", "请选择已有漫画镜头版本", 422)
                    item_id, path = selection.get("id"), selection.get("path")
                    item = next((value for value in meta.artifacts[stage]["shots"] if value["shot_id"] == item_id), None)
                    original = None
                    for version in self.host.public_versions(meta, stage):
                        original = next((value for value in version["artifact"]["shots"] if value["shot_id"] == item_id and (value.get("selected") or value.get("path")) == path), original)
                    if item is None or original is None:
                        raise AppError("INVALID_ASSET_VERSION", "只可选择本会话已生成的镜头文件", 422)
                    self._safe(meta, path)
                    item.update(path=path, selected=path, input_dependencies=deepcopy(original["input_dependencies"]))
                    if "model_usage" in original:
                        item["model_usage"] = deepcopy(original["model_usage"])
                    else:
                        item.pop("model_usage", None)
            else:
                raise AppError("INVALID_ASSET_VERSION", "请选择完整生成版本", 422)
        self.refresh(meta)
        session_store.touch(meta)
        if operation == "regenerate":
            return await self.execute(meta, stage, progress, request)
        meta.current_stage = stage
        meta.status = "idle" if stage in meta.stale_stages else "stage_completed"
        meta.error = None
        meta.execution_inputs.append({"execution_id": uuid4().hex, "parent_execution_id": self.host._parent_execution_id(),
                                      "project_type": "comic", "stage": stage, "operation": operation, "status": "completed", "finished_at": time.time(),
                                      "remaining_stale_items": meta.artifacts.get(stage, {}).get("stale_items", [])})
        session_store.touch(meta)
        await progress(stage, "修改已保存" if operation == "save" else "版本已选择", 100)
        return meta
