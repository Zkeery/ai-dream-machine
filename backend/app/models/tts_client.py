# -*- coding: utf-8 -*-
"""TTS 客户端：本地 Edge TTS，不产生云端费用。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import edge_tts

from app.core.errors import AppError

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "zh-CN-YunjianNeural"


class TTSClient:
    def __init__(self, voice: str = DEFAULT_VOICE, rate: str = "+20%") -> None:
        self.voice = voice
        self.rate = rate

    async def synthesize(self, text: str, out_path: Path) -> Path:
        """文本转语音，保存为 mp3。"""
        if not text or not text.strip():
            raise AppError("TTS_EMPTY", "配音文本为空")
        try:
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            await communicate.save(str(out_path))
            return out_path
        except Exception as e:  # edge-tts 网络/合成失败
            raise AppError("TTS_FAILED", f"配音失败：{e}") from e
