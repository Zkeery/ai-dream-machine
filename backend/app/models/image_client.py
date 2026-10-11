# -*- coding: utf-8 -*-
"""图像客户端：AIHubMix OpenAI 兼容图片生成（qwen-image-2.0）。"""
from __future__ import annotations

import logging
import time
import base64
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.core import config
from app.core.errors import AppError
from app.services import provider_jobs

logger = logging.getLogger(__name__)

SEEDREAM_MODELS = frozenset({"doubao-seedream-5-0-pro-260628", "doubao-seedream-5.0-pro"})


class ImageClient:
    def __init__(self) -> None:
        self.api_key = config.settings.aihubmix_api_key
        self.base = config.settings.aihubmix_base
        self.timeout = config.settings.image_timeout
        self.max_retries = config.settings.max_retries

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _download(self, url: str, out_path: Path) -> Path:
        url = urljoin(f"{self.base}/", url)
        headers = self._headers() if urlparse(url).netloc == urlparse(self.base).netloc else {}
        for attempt in range(self.max_retries + 1):
            try:
                resp = httpx.get(url, headers=headers, timeout=self.timeout, follow_redirects=True)
                if resp.status_code < 400:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_bytes(resp.content)
                    return out_path
                if resp.status_code < 500:
                    break
            except httpx.HTTPError:
                pass
            if attempt < self.max_retries:
                time.sleep(min(2**attempt, 4))
        raise AppError("IMAGE_DOWNLOAD_FAILED", "图片已生成，但下载失败；未重新提交生成，请检查任务记录", 502)

    def _submit(self, endpoint: str, payload: dict, job: provider_jobs.Job) -> dict:
        """一次提交；没有供应商幂等契约时，未知结果不能自动重提。"""
        from app.services import cost_control
        try:
            cost_control.reserve(job.id, payload["model"], "image", {"images": 1})
        except AppError as error:
            provider_jobs.not_submitted(job, error.code)
            raise
        try:
            provider_jobs.check_active(job)
        except AppError as error:
            provider_jobs.not_submitted(job, error.code)
            cost_control.settle(job.id, status="rejected")
            raise
        started = time.monotonic()
        try:
            resp = httpx.post(f"{self.base}{endpoint}", headers=self._headers(), json=payload, timeout=self.timeout)
        except httpx.HTTPError as error:
            # Preserve a safe transport diagnosis in the durable job, without
            # treating missing HTTP evidence as proof of an unbilled request.
            if isinstance(error, httpx.TimeoutException):
                code, message = "IMAGE_REQUEST_TIMEOUT", f"图片请求超时（等待上限 {self.timeout:g} 秒）"
            elif isinstance(error, httpx.RemoteProtocolError):
                code, message = "IMAGE_RESPONSE_DISCONNECTED", "图片服务在返回结果前断开连接"
            elif isinstance(error, (httpx.ConnectError, httpx.ProxyError)):
                code, message = "IMAGE_CONNECTION_FAILED", "图片服务连接失败"
            else:
                code, message = "IMAGE_TRANSPORT_ERROR", "图片请求传输中断"
            provider_jobs.failed(job, code, uncertain=True)
            cost_control.settle(job.id, status="uncertain")
            logger.warning("Image request interrupted: job=%s model=%s error=%s elapsed=%.2fs",
                           job.id, payload["model"], type(error).__name__, time.monotonic() - started)
            raise AppError("IMAGE_REQUEST_INTERRUPTED", f"{message}，生成结果未确认，已停止自动重试；请先核对供应商任务记录", 502) from None
        if resp.status_code >= 500:
            provider_jobs.failed(job, "IMAGE_REQUEST_INTERRUPTED", uncertain=True)
            cost_control.settle(job.id, status="uncertain")
            raise AppError("IMAGE_REQUEST_INTERRUPTED", "图片服务异常，生成结果未确认，已停止自动重试；请检查任务记录", 502)
        if resp.status_code >= 400:
            if resp.status_code in provider_jobs.REJECTED_HTTP_STATUSES:
                provider_jobs.rejected(job, resp.status_code)
                cost_control.settle(job.id, status="rejected")
                raise AppError("IMAGE_SUBMIT_REJECTED", f"图片接口返回 {resp.status_code}，请求未被受理；处理原因后可人工继续任务", 502)
            provider_jobs.failed(job, "IMAGE_REQUEST_INTERRUPTED", uncertain=True)
            cost_control.settle(job.id, status="uncertain")
            raise AppError("IMAGE_REQUEST_INTERRUPTED", f"图片接口返回 {resp.status_code}，提交结果未确认；请先核对供应商任务", 502)
        try:
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ValueError:
            provider_jobs.failed(job, "IMAGE_RESPONSE_INVALID", uncertain=True)
            cost_control.settle(job.id, status="uncertain")
            raise AppError("IMAGE_RESPONSE_INVALID", "图片接口响应不完整，结果未确认；请检查任务记录", 502) from None

    def _save_result(self, job: provider_jobs.Job, url: str, out_path: Path) -> Path:
        from app.services import cost_control
        provider_jobs.ready(job, url)
        cost_control.settle(job.id, status="completed")
        try:
            result = self._download(url, out_path)
        except AppError as error:
            provider_jobs.failed(job, error.code, uncertain=True)
            raise
        provider_jobs.completed(job, result)
        return result

    def _resume(self, job: provider_jobs.Job, out_path: Path) -> Path:
        if provider_jobs.reuse_local(job, out_path):
            return out_path
        url = job.row.get("result_url")
        if not url:
            raise AppError("IMAGE_RESULT_UNCONFIRMED", "尚未取得图片产物地址，无法续接下载；请先核对供应商任务", 409)
        return self._save_result(job, url, out_path)

    @staticmethod
    def _invalid_result(job: provider_jobs.Job):
        from app.services import cost_control
        provider_jobs.failed(job, "IMAGE_RESPONSE_INVALID", uncertain=True)
        cost_control.settle(job.id, status="uncertain")

    def text_to_image(self, prompt: str, out_path: Path, model: str | None = None, *, size: str = "1280x720") -> Path:
        """文生图：返回本地图片路径。"""
        from app.services.agent_runtime import with_brief
        prompt = with_brief(prompt)
        if not self.api_key:
            raise AppError("MISSING_API_KEY", "未配置 AIHUBMIX_API_KEY", 503)
        payload = {
            "model": model or config.settings.image_t2i_model,
            "prompt": prompt,
            "size": size,
            "n": 1,
            "response_format": "url",
        }
        endpoint = "/v1/images/generations"
        if payload["model"] in SEEDREAM_MODELS:
            # Seedream adds an "AI generated" watermark by default; only the native
            # /ai/v1 schema exposes extra.watermark (call/schema, 2026-10-10).
            payload.update({"async": False, "extra": {"watermark": False}})
            endpoint = "/ai/v1/images/generations"
        job = provider_jobs.begin("image", f"{self.base}{endpoint}", payload)
        if not job.new:
            return self._resume(job, out_path)
        data = self._submit(endpoint, payload, job)
        try:
            if endpoint.startswith("/ai/"):
                if data.get("status") not in (None, "completed"):
                    raise ValueError
                url = data["output"][0]["content_url"]
            else:
                url = data["data"][0]["url"]
            if not isinstance(url, str) or not url:
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            self._invalid_result(job)
            raise AppError("IMAGE_RESPONSE_INVALID", "图片接口未返回完成产物，结果未确认；请检查任务记录", 502) from None
        return self._save_result(job, url, out_path)

    def image_to_image(self, image_path: Path | list[Path], prompt: str, out_path: Path,
                       model: str | None = None, *, size: str = "1280x720") -> Path:
        """Qwen 原生多图参考：真实传入图片，能力不匹配时明确失败。

        Contract: docs.aihubmix.com/cn/api/aihubmix-image-generation
        Reviewed schemas (2026-10-04): /call/schema/models/
        qwen-image-2.0/endpoints and qwen-image-2.0-pro/endpoints.
        """
        from app.services.agent_runtime import with_brief
        prompt = with_brief(prompt)
        model = model or config.settings.image_t2i_model
        if model not in {"qwen-image-2.0", "qwen-image-2.0-pro"}:
            raise AppError("IMAGE_REFERENCE_UNSUPPORTED", "当前图片模型未验证多图参考能力", 400)
        paths = image_path if isinstance(image_path, list) else [image_path]
        if not 1 <= len(paths) <= 3:
            raise AppError("IMAGE_REFERENCE_LIMIT", "每个分镜最多引用 3 张角色/场景图，请调整分镜引用", 400)
        if not self.api_key:
            raise AppError("MISSING_API_KEY", "未配置 AIHUBMIX_API_KEY", 503)
        images = []
        for path in paths:
            path = Path(path)
            if not path.is_file():
                raise AppError("IMAGE_NOT_FOUND", "参考图不存在", 400)
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(path.suffix.lower())
            if not mime:
                raise AppError("UNSUPPORTED_TYPE", "参考图类型不支持", 400)
            images.append(f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}")
        payload = {"model": model, "prompt": prompt, "images": images, "size": size,
                   "n": 1, "response_format": "url", "async": False}
        endpoint = "/ai/v1/images/generations"
        job = provider_jobs.begin("image", f"{self.base}{endpoint}", payload)
        if not job.new:
            return self._resume(job, out_path)
        data = self._submit(endpoint, payload, job)
        if data.get("status") == "failed":
            from app.services import cost_control
            provider_jobs.failed(job, "IMAGE_MODEL_ERROR")
            cost_control.settle(job.id, status="uncertain")
            raise AppError("IMAGE_MODEL_ERROR", "图片参考生成失败", 502)
        try:
            url = data["output"][0]["content_url"]
            if data.get("status") != "completed" or not isinstance(url, str) or not url:
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            self._invalid_result(job)
            raise AppError("IMAGE_RESPONSE_INVALID", "图片接口未返回完成产物，请检查任务记录后重试", 502) from None
        return self._save_result(job, url, out_path)
