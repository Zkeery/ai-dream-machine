# -*- coding: utf-8 -*-
"""视频客户端：AIHubMix OpenAI 兼容视频生成（通义万相 wan2.7 图生视频）。

三种模式映射：
- first_frame → wan2.7-i2v（首帧生视频）
- start_end  → wan2.7-i2v（首尾帧，MVP 先按首帧图，多帧参数待真实冒烟核验）
- reference  → wan2.7-r2v（参考图生视频）
本地图片以 base64 data URI 作为 input_reference，免上传。

网络加固：提交与轮询对瞬时失败（超时/连接/5xx）做有限重试，4xx 立即失败；
整体仍受 video_timeout 截止时间约束。
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path

import httpx

from app.core import config
from app.core.errors import AppError

logger = logging.getLogger(__name__)


def _image_to_data_uri(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise AppError("IMAGE_NOT_FOUND", "参考图不存在", 400)
    ext = p.suffix.lower().lstrip(".")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext, "image/png")
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


class VideoClient:
    def __init__(self) -> None:
        self.api_key = config.settings.aihubmix_api_key
        self.base = config.settings.aihubmix_base
        self.timeout = config.settings.video_timeout  # 整体截止（秒）
        self.max_retries = config.settings.max_retries
        self.poll_interval = 15.0
        self.req_timeout = 60.0  # 单次 HTTP 请求超时

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _model_for(self, mode: str) -> str:
        if mode == "reference":
            return config.settings.video_reference_model
        return config.settings.video_first_frame_model

    def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """对瞬时失败（超时/连接/5xx）有限重试；4xx 立即失败。"""
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = httpx.request(method, url, headers=self._headers(), timeout=self.req_timeout, **kwargs)
                if resp.status_code >= 500:
                    last_err = AppError("VIDEO_SERVER_ERROR", f"视频接口返回 {resp.status_code}", 502)
                elif resp.status_code >= 400:
                    raise AppError("VIDEO_MODEL_ERROR", f"视频接口返回 {resp.status_code}: {resp.text[:200]}", 502)
                else:
                    return resp
            except AppError:
                raise
            except httpx.HTTPError as e:
                last_err = e
            time.sleep(min(2 ** attempt, 4))
        raise AppError("VIDEO_SUBMIT_FAILED", f"视频请求失败：{last_err}", 502)

    def image_to_video(self, image_path: str, prompt: str, out_path: Path, mode: str = "first_frame") -> Path:
        """图生视频：image_path 为本地图片路径，转 base64 后作为参考图。"""
        if not self.api_key:
            raise AppError("MISSING_API_KEY", "未配置 AIHUBMIX_API_KEY", 503)
        payload = {
            "model": self._model_for(mode),
            "prompt": prompt,
            "seconds": "5",
            "size": "1280x720",
            "input_reference": _image_to_data_uri(image_path),
        }
        resp = self._request_with_retry("POST", f"{self.base}/v1/videos", json=payload)
        video_id = resp.json()["id"]

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            st = self._request_with_retry("GET", f"{self.base}/v1/videos/{video_id}")
            data = st.json()
            status = data.get("status")
            if status == "completed":
                url = data.get("url") or f"{self.base}/v1/videos/{video_id}/content"
                return self._download(url, out_path)
            if status == "failed":
                raise AppError("VIDEO_TASK_FAILED", "视频生成任务失败", 502)
        raise AppError("VIDEO_TIMEOUT", "视频生成超时", 504)

    def _download(self, url: str, out_path: Path) -> Path:
        resp = httpx.get(url, headers=self._headers(), timeout=self.timeout, follow_redirects=True)
        if resp.status_code >= 400:
            raise AppError("VIDEO_DOWNLOAD_FAILED", "视频下载失败", 502)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        return out_path
