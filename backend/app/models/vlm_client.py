# -*- coding: utf-8 -*-
"""视觉审查客户端：AIHubMix OpenAI 兼容 VLM（qwen3-vl），用于内容安全结果侧拦截。"""
from __future__ import annotations

import base64
import io
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image, ImageOps

from app.core import config
from app.core.errors import AppError
from app.services import cost_control


def _image_to_data_uri(path: str) -> str:
    p = Path(path)
    if not p.exists():
        raise AppError("IMAGE_NOT_FOUND", "图片不存在", 400)
    # Bound visual tokens and the upload size used by the review request.
    # The original asset is unchanged; only the safety-review copy is resized.
    with Image.open(p) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((1024, 1024))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
    b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


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
            "max_tokens": config.settings.vlm_max_output_tokens,
        }
        call_id = uuid4().hex
        cost_control.reserve(call_id, self.model, "text", {
            "input_tokens": config.settings.vlm_max_input_tokens + len(instruction.encode("utf-8")) + 256,
            "output_tokens": payload["max_tokens"],
        })
        try:
            resp = httpx.post(
                f"{self.base}/v1/chat/completions", headers=self._headers(), json=payload, timeout=self.timeout,
            )
            if resp.status_code >= 400:
                cost_control.settle(call_id, "uncertain" if resp.status_code >= 500 else "rejected")
                raise AppError("VLM_ERROR", f"视觉审查接口返回 {resp.status_code}", 502)
            data = resp.json()
            usage = data.get("usage") or {}
            actual_units = None
            if isinstance(usage.get("prompt_tokens"), int) and isinstance(usage.get("completion_tokens"), int):
                actual_units = {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]}
            cost_control.settle(call_id, "completed", actual_units=actual_units)
        except httpx.HTTPError:
            cost_control.settle(call_id, "uncertain")
            raise AppError("VLM_REQUEST_INTERRUPTED", "视觉审核请求中断，结果未确认；已保留费用预留", 502) from None
        except (ValueError, TypeError):
            cost_control.settle(call_id, "uncertain")
            raise AppError("VLM_RESPONSE_INVALID", "视觉审核响应无效，已保留费用预留", 502) from None
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join((p.get("text", "") for p in content if isinstance(p, dict)))
        return str(content)
