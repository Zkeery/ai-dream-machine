# -*- coding: utf-8 -*-
"""LLM/VLM 客户端：AIHubMix OpenAI 兼容 chat completions（qwen 系列）。

模型输出统一走「模型输出 → 确定性解析 → Pydantic 校验」，解析宽容、有限重试。
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx
from pydantic import BaseModel, ValidationError

from app.core import config
from app.core.errors import AppError

logger = logging.getLogger(__name__)


def _extract_json(text: str) -> dict:
    """宽容解析：兼容 ```json 围栏、纯 JSON、前后噪声。失败抛 ValueError。"""
    if not text:
        raise ValueError("空输出")
    # 1) 去掉 markdown 围栏
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fenced:
        text = fenced.group(1)
    # 2) 找第一个 { 到最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("未找到 JSON 对象")
    return json.loads(text[start : end + 1])


class LLMClient:
    """qwen 系列文本/视觉模型，OpenAI 兼容接口。"""

    def __init__(self) -> None:
        self.api_key = config.settings.aihubmix_api_key
        self.base = config.settings.aihubmix_base
        self.model = config.settings.llm_model
        self.timeout = config.settings.llm_timeout
        self.max_retries = config.settings.max_retries

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def generate(self, system: str, user: str) -> str:
        """返回模型文本输出；含有限重试。"""
        if not self.api_key:
            raise AppError("MISSING_API_KEY", "未配置 AIHUBMIX_API_KEY", 503)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
        }
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = httpx.post(
                    f"{self.base}/v1/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code >= 400:
                    raise AppError("MODEL_ERROR", f"模型接口返回 {resp.status_code}", 502)
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if isinstance(content, list):  # 部分视觉返回结构
                    content = "".join(
                        (p.get("text", "") for p in content if isinstance(p, dict))
                    )
                return str(content)
            except AppError:
                raise  # 4xx（如余额不足）不重试
            except (httpx.HTTPError, KeyError, IndexError, TypeError) as e:
                last_err = e
            time.sleep(min(2**attempt, 4))
        raise AppError("MODEL_RETRY_EXHAUSTED", f"模型调用失败：{last_err}", 502)

    def generate_json(self, system: str, user: str, model_cls: type[BaseModel]) -> BaseModel:
        """生成并强校验结构化输出；解析失败重试，仍失败统一报错。"""
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            raw = self.generate(system, user)
            try:
                obj = _extract_json(raw)
                return model_cls.model_validate(obj)
            except (ValueError, ValidationError, TypeError) as e:
                last_err = e
                logger.warning("JSON 解析/校验失败（第 %s 次）：%s", attempt + 1, e)
        raise AppError("MODEL_OUTPUT_INVALID", "模型输出格式不合法，多次重试仍失败", 502)
