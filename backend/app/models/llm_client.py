# -*- coding: utf-8 -*-
"""LLM/VLM 客户端：AIHubMix OpenAI 兼容 chat completions（qwen 系列）。

模型输出统一走「模型输出 → 确定性解析 → Pydantic 校验」，解析宽容、有限重试。
"""
from __future__ import annotations

import json
import logging
import re
import time
from uuid import uuid4

import httpx
from pydantic import BaseModel, ValidationError

from app.core import config
from app.core.errors import AppError
from app.services import cost_control

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

    def generate(self, system: str, user: str, *, model: str | None = None, owner_id: str | None = None) -> str:
        """返回文本；仅对明确的 5xx 响应重试，不重提结果未知的请求。"""
        from app.services.agent_runtime import CONTROL_DECISION, with_brief
        user = with_brief(user)
        if not self.api_key:
            raise AppError("MISSING_API_KEY", "未配置 AIHUBMIX_API_KEY", 503)

        payload = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.7,
            "max_tokens": config.settings.llm_max_output_tokens,
        }
        if CONTROL_DECISION.get():
            payload["temperature"] = 0.2
            payload["max_tokens"] = min(payload["max_tokens"], 2048)
            # AIHubMix's model schema lists this Qwen chat-completions extension.
            # Only bounded agent decisions use it; creative generation is unchanged.
            if payload["model"] in {"qwen3.5-plus", "qwen3.5-flash"}:
                payload["enable_thinking"] = False
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            call_id = uuid4().hex
            cost_control.reserve(call_id, payload["model"], "text", {
                "input_tokens": len(system.encode("utf-8")) + len(user.encode("utf-8")) + 256,
                "output_tokens": payload["max_tokens"],
            }, owner_id=owner_id)
            started = time.monotonic()
            try:
                resp = httpx.post(
                    f"{self.base}/v1/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code >= 500:
                    cost_control.settle(call_id, "uncertain")
                    last_err = AppError("MODEL_SERVER_ERROR", f"模型接口返回 {resp.status_code}", 502)
                    if attempt < self.max_retries:
                        time.sleep(min(2**attempt, 4))
                    continue
                if resp.status_code >= 400:
                    cost_control.settle(call_id, "rejected")
                    raise AppError("MODEL_ERROR", f"模型接口返回 {resp.status_code}", 502)
                data = resp.json()
                usage = data.get("usage") or {}
                actual_units = None
                if isinstance(usage.get("prompt_tokens"), int) and isinstance(usage.get("completion_tokens"), int):
                    actual_units = {"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]}
                cost_control.settle(call_id, "completed", actual_units=actual_units)
                content = data["choices"][0]["message"]["content"]
                if isinstance(content, list):  # 部分视觉返回结构
                    content = "".join(
                        (p.get("text", "") for p in content if isinstance(p, dict))
                    )
                return str(content)
            except httpx.HTTPError as error:
                cost_control.settle(call_id, "uncertain")
                logger.warning("Text request interrupted: model=%s error=%s elapsed=%.1fs control=%s",
                               payload["model"], type(error).__name__, time.monotonic() - started, CONTROL_DECISION.get())
                if isinstance(error, httpx.TimeoutException):
                    label = "Agent 决策" if CONTROL_DECISION.get() else "文本生成"
                    raise AppError("MODEL_REQUEST_INTERRUPTED", f"{label}请求超时（等待上限 {self.timeout:g} 秒），供应商结果未确认，已停止自动重试；请核对用量后再重新生成", 502) from None
                raise AppError("MODEL_REQUEST_INTERRUPTED", "文本请求中断，结果未确认，已停止自动重试；请检查任务记录后再决定是否重新生成", 502) from None
            except (ValueError, KeyError, IndexError, TypeError):
                cost_control.settle(call_id, "uncertain")
                raise AppError("MODEL_RESPONSE_INVALID", "文本接口响应不完整，结果未确认；请检查任务记录后再决定是否重新生成", 502) from None
        raise AppError("MODEL_RETRY_EXHAUSTED", f"模型服务暂不可用，有限重试后仍失败：{last_err}", 502)

    def generate_json(self, system: str, user: str, model_cls: type[BaseModel], *,
                      model: str | None = None, owner_id: str | None = None) -> BaseModel:
        """生成并强校验结构化输出；解析失败重试，仍失败统一报错。"""
        last_err: Exception | None = None
        issues: list[dict] = []
        retry_system = system
        for attempt in range(self.max_retries + 1):
            raw = self.generate(retry_system, user, model=model, owner_id=owner_id)
            try:
                obj = _extract_json(raw)
                return model_cls.model_validate(obj)
            except (ValueError, ValidationError, TypeError) as e:
                last_err = e
                issues = [{"field": ".".join(map(str, item["loc"])) or "$", "type": item["type"],
                           "message": item["msg"][:200]}
                          for item in e.errors(include_input=False, include_url=False)[:8]] if isinstance(e, ValidationError) else [
                              {"field": "$", "type": "json_parse", "message": "必须返回一个完整的 JSON 对象"}]
                logger.warning("JSON validation failed: schema=%s attempt=%s issues=%s", model_cls.__name__, attempt + 1, issues)
                retry_system = system + "\n上一份响应未通过校验。请修正以下字段并重新输出完整JSON，不要重复原格式错误：\n" + json.dumps(issues, ensure_ascii=False) + "\n必须符合JSON Schema：\n" + json.dumps(model_cls.model_json_schema(), ensure_ascii=False)
        raise AppError("MODEL_OUTPUT_INVALID", "模型输出格式不合法，多次重试仍失败", 502,
                       details={"schema": model_cls.__name__, "validation_errors": issues}) from last_err
