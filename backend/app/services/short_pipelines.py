# -*- coding: utf-8 -*-
"""三条一次性短管线：文艺短视频、动作迁移、数字人口播。

复用第一刀模型客户端；产物落盘到 image/video 目录；进度通过回调上报。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from app.core import config
from app.core.errors import AppError
from app.models.image_client import ImageClient
from app.models.llm_client import LLMClient
from app.models.tts_client import TTSClient
from app.models.video_client import VideoClient
from app.schemas.task import talking_input
from app.services import ffmpeg_util, model_catalog, prompts, task_store

ProgressCb = Callable[[str, str, int], Awaitable[None]]


def _split_sentences(text: str, limit: int = 8) -> list[str]:
    parts: list[str] = []
    for ch in ("。", "！", "？", "\n", "；"):
        text = text.replace(ch, "|")
    raw = [s.strip() for s in text.split("|") if s.strip()]
    return raw[:limit]


class ShortPipelines:
    def __init__(self, llm=None, image=None, video=None, tts=None):
        self.llm = llm or LLMClient()
        self.image = image or ImageClient()
        self.video = video or VideoClient()
        self.tts = tts or TTSClient()

    async def run(self, task_id: str, ptype: str, inputs: dict, progress: ProgressCb) -> dict:
        if ptype not in model_catalog.TASK_KEYS:
            raise AppError("INVALID_TASK_TYPE", f"未知管线类型：{ptype}", 400)
        mode = talking_input(inputs)[0] if ptype == "talking_head" else "static"
        model_catalog.validate_selection(inputs.get("model_selection"), task_type=ptype, talking_mode=mode)
        usage = model_catalog.task_usage(ptype, inputs)
        if ptype == "literary_video":
            result = await self._literary_video(task_id, inputs, progress)
        elif ptype == "motion_transfer":
            result = await self._motion_transfer(task_id, inputs, progress)
        elif ptype == "talking_head":
            result = await self._talking_head(task_id, inputs, progress)
        else:
            raise AppError("INVALID_TASK_TYPE", f"未知管线类型：{ptype}", 400)
        return {**result, "model_usage": usage}

    # ---------- 文艺短视频 ----------

    async def _literary_video(self, task_id: str, inputs: dict, progress: ProgressCb) -> dict:
        text = (inputs.get("text") or "").strip()
        if not text:
            raise AppError("VALIDATION_ERROR", "文案不能为空")
        style = inputs.get("style") or "realistic"

        await progress("prepare", "整理文案", 5)
        if len(text) <= 30:
            from app.services import recovery
            system = "你是短视频文案，把用户的创意扩写成一段 2~4 句的画面感文案，只输出文案本身。"
            expansion_input = recovery.literary_expansion_input(inputs)
            saved = recovery.load_intermediate("literary_expansion", expansion_input)
            if saved is None:
                text = (await asyncio.to_thread(self.llm.generate, system, text, model=expansion_input["model"])).strip()
                recovery.save_intermediate("literary_expansion", expansion_input, text)
            else:
                text = saved
        sentences = _split_sentences(text)
        if not sentences:
            raise AppError("VALIDATION_ERROR", "文案没有可用的句子")

        img_dir = config.IMAGE_DIR / task_id
        aud_dir = config.VIDEO_DIR / task_id
        seg_dir = config.VIDEO_DIR / task_id / "seg"
        img_dir.mkdir(parents=True, exist_ok=True)
        aud_dir.mkdir(parents=True, exist_ok=True)
        seg_dir.mkdir(parents=True, exist_ok=True)

        segments: list[Path] = []
        total = len(sentences)
        for i, s in enumerate(sentences, 1):
            pct = int(10 + (i - 1) / total * 70)
            await progress("generate", f"生成第 {i}/{total} 句画面与配音", pct)
            img = await asyncio.to_thread(self.image.text_to_image, f"{s}，{style}", img_dir / f"{i}.png",
                                         model=model_catalog.resolve(inputs.get("model_selection"), ["image"])["image"])
            aud = await self.tts.synthesize(s, aud_dir / f"{i}.mp3")
            seg = await ffmpeg_util.image_audio_to_video(img, aud, seg_dir / f"{i}.mp4")
            segments.append(seg)

        await progress("concat", "合成成片", 85)
        final = await ffmpeg_util.concat_videos(segments, config.VIDEO_DIR / task_id / "final.mp4")
        await progress("done", "完成", 100)
        return {"final_video": str(final), "text": text, "sentences": sentences,
                "images": [str(img_dir / f"{i}.png") for i in range(1, total + 1)]}

    # ---------- 动作迁移 ----------

    async def _motion_transfer(self, task_id: str, inputs: dict, progress: ProgressCb) -> dict:
        character = inputs.get("character_image")
        prompt = (inputs.get("prompt") or "").strip()
        if not character or not prompt:
            raise AppError("VALIDATION_ERROR", "角色图与动作描述缺一不可")

        await progress("video", "生成动作视频", 30)
        prompt = prompts.compose_shot_prompt(prompt)
        final = await asyncio.to_thread(self.video.image_to_video, character, prompt, config.VIDEO_DIR / task_id / "final.mp4", "reference",
                                       model=model_catalog.resolve(inputs.get("model_selection"), ["video_reference"])["video_reference"])
        await progress("done", "完成", 100)
        return {"final_video": str(final)}

    # ---------- 数字人口播 ----------

    async def _talking_head(self, task_id: str, inputs: dict, progress: ProgressCb) -> dict:
        person = inputs.get("person_image")
        mode, script = talking_input(inputs)
        if not person or not script:
            raise AppError("VALIDATION_ERROR", "人物图与口播文案缺一不可")

        aud_dir = config.VIDEO_DIR / task_id
        aud_dir.mkdir(parents=True, exist_ok=True)
        if mode == "lip_sync":
            await progress("lip_sync", "生成人物嘴型与声音", 20)
            model = model_catalog.resolve(inputs.get("model_selection"), ["video_speech"])["video_speech"]
            final = await asyncio.to_thread(self.video.talking_head, person, script, aud_dir / "final.mp4",
                                           model=model, duration=10)
            await progress("validate", "检查视频与声音", 85)
            media = await ffmpeg_util.verify_talking_video(final, expected_duration=10)
            raw_video = final
            prepared = await ffmpeg_util.trim_talking_silence(final, aud_dir / "playback.mp4",
                                                            duration=media["duration"])
            final = prepared["path"]
            audio = await ffmpeg_util.extract_audio(final, aud_dir / "voice.m4a")
            await progress("done", "完成", 100)
            return {"final_video": str(final), "audio": str(audio), "script": script,
                    "talking_mode": mode, "audio_source": "video_model", "duration": prepared["duration"],
                    "raw_video": str(raw_video), "source_duration": media["duration"],
                    "trimmed_tail": prepared["trimmed_tail"]}

        await progress("tts", "生成配音", 20)
        audio = await self.tts.synthesize(script, aud_dir / "voice.mp3")
        task_store.save_partial_result(task_id, {"audio": str(audio), "script": script, "talking_mode": mode,
                                                "model_usage": model_catalog.task_usage("talking_head", inputs)})

        await progress("video", "合成口播视频", 60)
        final = await ffmpeg_util.image_audio_to_video(person, audio, aud_dir / "final.mp4")
        await progress("done", "完成", 100)
        return {"final_video": str(final), "audio": str(audio), "script": script, "talking_mode": mode,
                "audio_source": "edge_tts"}
