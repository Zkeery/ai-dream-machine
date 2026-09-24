# -*- coding: utf-8 -*-
"""图像客户端：AIHubMix OpenAI 兼容图片生成（qwen-image-2.0）。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from app.core import config
from app.core.errors import AppError

logger = logging.getLogger(__name__)


class ImageClient:
    def __init__(self) -> None:
        self.api_key = config.settings.aihubmix_api_key
        self.base = config.settings.aihubmix_base
        self.timeout = config.settings.image_timeout
        self.max_retries = config.settings.max_retries

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _download(self, url: str, out_path: Path) -> Path:
        resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
        if resp.status_code >= 400:
            raise AppError("IMAGE_DOWNLOAD_FAILED", "图片下载失败", 502)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        return out_path

    def text_to_image(self, prompt: str, out_path: Path, model: str | None = None) -> Path:
        """文生图：返回本地图片路径。"""
        if not self.api_key:
            raise AppError("MISSING_API_KEY", "未配置 AIHUBMIX_API_KEY", 503)
        payload = {
            "model": model or config.settings.image_t2i_model,
            "prompt": prompt,
            "size": "1280x720",
            "n": 1,
            "response_format": "url",
        }
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = httpx.post(
                    f"{self.base}/v1/images/generations",
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code >= 400:
                    raise AppError("IMAGE_MODEL_ERROR", f"图像接口返回 {resp.status_code}", 502)
                data = resp.json()
                url = data["data"][0]["url"]
                return self._download(url, out_path)
            except AppError:
                raise  # 4xx（如余额不足）不重试
            except (KeyError, IndexError, httpx.HTTPError) as e:
                last_err = e
            time.sleep(min(2**attempt, 4))
        raise AppError("IMAGE_RETRY_EXHAUSTED", f"图像生成失败：{last_err}", 502)

    def image_to_image(self, image_path: Path, prompt: str, out_path: Path, model: str | None = None) -> Path:
        """图生图（参考图风格迁移/首帧精修）。首版 MVP 复用文生图通道。"""
        return self.text_to_image(prompt, out_path, model)
