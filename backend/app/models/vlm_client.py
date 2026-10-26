# -*- coding: utf-8 -*-
"""视觉审查客户端：AIHubMix OpenAI 兼容 VLM（qwen3-vl），用于内容安全结果侧拦截。"""
from __future__ import annotations

import base64
import io
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx
from PIL import Image, ImageOps

from app.core import config
from app.core.errors import AppError
from app.core import offline
from app.services import cost_control
from app.services import review_store


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
        if config.settings.mode_mock:
            import re
            offline.mock_invocation("vlm")
            # Deterministic contract fixture, never a genuine content verdict.
            if '"checks"' in instruction:
                from app.services.content_review import PROP_STATE_CHECKS
                return json.dumps({"checks": {key: {"yes": True, "evidence": "MOCK 测试占位观察"} for key in PROP_STATE_CHECKS}}, ensure_ascii=False)
            fixture = {"safe": True, "reason": "MOCK 离线合同测试，不验证真实内容安全或画面结构", "mock": True,
                       "person_count": 1, "text_or_signage": False, "character_visible": True}
            if '"multi_view_layout"' in instruction:
                expected = re.search(r"恰好\s*(\d+)\s*个人物表现", instruction)
                if not expected:
                    raise AppError("MOCK_SCHEMA_UNSUPPORTED", "离线严格视觉审查缺少可识别的数量合同", 503)
                # This is an explicit fixture of the required output schema.
                # It does not infer any feature from the synthetic mock image.
                fixture.update(person_count=int(expected.group(1)), multi_view_layout=False,
                               floating_head=False, detached_limbs=False)
            return json.dumps(fixture, ensure_ascii=False)
        if not self.api_key:
            raise AppError("MISSING_API_KEY", "未配置 AIHUBMIX_API_KEY", 503)
        image_data_uri = _image_to_data_uri(image_path)
        review_copy = base64.b64decode(image_data_uri.split(",", 1)[1])
        review_store.update(review_store.current_id(), request_model=self.model, instruction=instruction,
                            review_copy={"mime": "image/jpeg", "bytes": len(review_copy),
                                         "sha256": hashlib.sha256(review_copy).hexdigest()})
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_data_uri}},
                        {"type": "text", "text": instruction},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": config.settings.vlm_max_output_tokens,
        }
        call_id = uuid4().hex
        review_store.update(review_store.current_id(), cost_call_id=call_id)
        cost_control.reserve(call_id, self.model, "text", {
            "input_tokens": config.settings.vlm_max_input_tokens + len(instruction.encode("utf-8")) + 256,
            "output_tokens": payload["max_tokens"],
        })
        try:
            resp = httpx.post(
                f"{self.base}/v1/chat/completions", headers=self._headers(), json=payload, timeout=self.timeout,
            )
            review_store.update(review_store.current_id(), http_status=resp.status_code)
            if resp.status_code >= 400:
                cost_control.settle(call_id, "uncertain" if resp.status_code >= 500 else "rejected")
                raise AppError("VLM_ERROR", f"视觉审查接口返回 {resp.status_code}", 502)
            # Keep the HTTP body even if the provider envelope itself is not
            # JSON. Missing usage still stops as an accounting/system error.
            review_store.update(review_store.current_id(), provider_body=getattr(resp, "text", None))
            data = resp.json()
            choices = data.get("choices") if isinstance(data, dict) else None
            message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text", ""), str))
            raw = content if isinstance(content, str) else "" if content is None else json.dumps(content, ensure_ascii=False)
            review_store.response(raw, provider_response=data,
                                  response_id=data.get("id") if isinstance(data, dict) else None,
                                  model=data.get("model") if isinstance(data, dict) else None)
            usage = (data.get("usage") or {}) if isinstance(data, dict) else {}
            actual_units = cost_control.actual_text_units(usage)
            settlement = cost_control.settle(call_id, "completed", actual_units=actual_units)
            cost_control.require_confirmed_settlement(settlement)
        except httpx.HTTPError:
            cost_control.settle(call_id, "uncertain")
            raise AppError("VLM_REQUEST_INTERRUPTED", "视觉审核请求中断，结果未确认；已保留费用预留", 502) from None
        except (ValueError, TypeError):
            cost_control.settle(call_id, "uncertain")
            raise AppError("VLM_RESPONSE_INVALID", "视觉审核响应无效，已保留费用预留", 502) from None
        return raw
