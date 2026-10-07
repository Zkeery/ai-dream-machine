# -*- coding: utf-8 -*-
"""Edge TTS 网络配音客户端；沿用现有服务，不增加付费模型调用。"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import edge_tts

from app.core.errors import AppError

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "zh-CN-YunjianNeural"
SUPPORTED_VOICES = (
    {"voice": "zh-CN-XiaoxiaoNeural", "name": "晓晓", "gender": "female"},
    {"voice": "zh-CN-XiaoyiNeural", "name": "晓伊", "gender": "female"},
    {"voice": "zh-CN-YunxiNeural", "name": "云希", "gender": "male"},
    {"voice": "zh-CN-YunjianNeural", "name": "云健", "gender": "male"},
    {"voice": "zh-CN-YunyangNeural", "name": "云扬", "gender": "male"},
    {"voice": "zh-CN-YunxiaNeural", "name": "云夏", "gender": "male"},
)


class TTSClient:
    def __init__(self, voice: str = DEFAULT_VOICE, rate: str = "+20%") -> None:
        self.voice = voice
        self.rate = rate

    @staticmethod
    def list_voices() -> list[dict]:
        """本期明确支持的声线；不表示此刻已联网测试各声音可用性。"""
        return [dict(voice) for voice in SUPPORTED_VOICES]

    async def synthesize(self, text: str, out_path: Path, *, voice: str | None = None) -> Path:
        """文本转语音，保存为 mp3。"""
        if not text or not text.strip():
            raise AppError("TTS_EMPTY", "配音文本为空")
        selected_voice = voice or self.voice
        if voice is not None and selected_voice not in {item["voice"] for item in SUPPORTED_VOICES}:
            raise AppError("TTS_VOICE_INVALID", "请选择本期支持的角色声线", 422)
        try:
            communicate = edge_tts.Communicate(text, selected_voice, rate=self.rate)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.wait_for(communicate.save(str(out_path)), timeout=90)
            return out_path
        except asyncio.CancelledError:
            out_path.unlink(missing_ok=True)
            raise
        except Exception:  # 不把服务端错误/请求全文回显给用户。
            out_path.unlink(missing_ok=True)
            raise AppError("TTS_FAILED", "配音服务暂不可用或请求超时，请稍后重试", 502) from None
