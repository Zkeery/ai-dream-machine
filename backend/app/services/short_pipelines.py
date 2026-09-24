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
from app.services import ffmpeg_util

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
        if ptype == "literary_video":
            return await self._literary_video(task_id, inputs, progress)
        if ptype == "motion_transfer":
            return await self._motion_transfer(task_id, inputs, progress)
        if ptype == "talking_head":
            return await self._talking_head(task_id, inputs, progress)
        raise AppError("INVALID_TASK_TYPE", f"未知管线类型：{ptype}", 400)

    # ---------- 文艺短视频 ----------

    async def _literary_video(self, task_id: str, inputs: dict, progress: ProgressCb) -> dict:
        text = (inputs.get("text") or "").strip()
        if not text:
            raise AppError("VALIDATION_ERROR", "文案不能为空")
        style = inputs.get("style") or "realistic"

        await progress("prepare", "整理文案", 5)
        if len(text) <= 30:
            system = "你是短视频文案，把用户的创意扩写成一段 2~4 句的画面感文案，只输出文案本身。"
            text = (await asyncio.to_thread(self.llm.generate, system, text)).strip()
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
            img = await asyncio.to_thread(self.image.text_to_image, f"{s}，{style}", img_dir / f"{i}.png")
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
        motion = inputs.get("motion_video")
        prompt = (inputs.get("prompt") or "").strip()
        if not character or not motion or not prompt:
            raise AppError("VALIDATION_ERROR", "角色图、动作视频、提示词缺一不可")

        await progress("extract", "提取动作视频首帧", 10)
        img_dir = config.IMAGE_DIR / task_id
        img_dir.mkdir(parents=True, exist_ok=True)
        frame = await ffmpeg_util.extract_first_frame(motion, img_dir / "motion_frame.jpg")

        await progress("video", "生成动作视频", 30)
        final = await asyncio.to_thread(self.video.image_to_video, character, prompt, config.VIDEO_DIR / task_id / "final.mp4", "reference")
        await progress("done", "完成", 100)
        return {"final_video": str(final), "motion_frame": str(frame)}

    # ---------- 数字人口播 ----------

    async def _talking_head(self, task_id: str, inputs: dict, progress: ProgressCb) -> dict:
        person = inputs.get("person_image")
        script = (inputs.get("script") or "").strip()
        if not person or not script:
            raise AppError("VALIDATION_ERROR", "人物图与口播文案缺一不可")

        await progress("tts", "生成配音", 20)
        aud_dir = config.VIDEO_DIR / task_id
        aud_dir.mkdir(parents=True, exist_ok=True)
        audio = await self.tts.synthesize(script, aud_dir / "voice.mp3")

        await progress("video", "合成口播视频", 60)
        final = await ffmpeg_util.image_audio_to_video(person, audio, aud_dir / "final.mp4")
        await progress("done", "完成", 100)
        return {"final_video": str(final), "audio": str(audio), "script": script}
