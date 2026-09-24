# -*- coding: utf-8 -*-
"""视觉审查客户端：AIHubMix OpenAI 兼容 VLM（qwen3-vl），用于内容安全结果侧拦截。"""
from __future__ import annotations

import base64
from pathlib import Path

import httpx

from app.core import config
from app.core.errors import AppError


def _image_to_data_uri(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise AppError("IMAGE_NOT_FOUND", "图片不存在", 400)
    ext = p.suffix.lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


class VLMClient:
    def __init__(self) -> None:
        self.api_key = config.settings.aihubmix_api_key
        self.base = config.settings.aihubmix_base
        self.model = config.settings.vlm_model
        self.timeout = config.settings.llm_timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def review(self, image_path: str, instruction: str) -> str:
        """返回 VLM 文本输出。"""
        if not self.api_key:
            raise AppError("MISSING_API_KEY", "未配置 AIHUBMIX_API_KEY", 503)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": _image_to_data_uri(image_path)}},
                        {"type": "text", "text": instruction},
                    ],
                }
            ],
            "temperature": 0,
        }
        resp = httpx.post(
            f"{self.base}/v1/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        if resp.status_code >= 400:
            raise AppError("VLM_ERROR", f"视觉审查接口返回 {resp.status_code}", 502)
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join((p.get("text", "") for p in content if isinstance(p, dict)))
        return str(content)
