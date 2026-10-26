# -*- coding: utf-8 -*-
"""日志配置：分级、不记密钥/密码/完整用户文档。"""
from __future__ import annotations

import logging
import re


class PrivacyFilter(logging.Filter):
    """Never log provider media URLs, credentials or exception payloads."""
    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        text = re.sub(r"https?://[^\s\"'<>]+", "[URL_REDACTED]", text)
        text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", text)
        text = re.sub(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
        text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[KEY_REDACTED]", text)
        record.msg, record.args = text, ()
        record.exc_info = None
        record.stack_info = None
        return True


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for handler in logging.getLogger().handlers:
        if not any(isinstance(item, PrivacyFilter) for item in handler.filters):
            handler.addFilter(PrivacyFilter())
    # 关闭可能回显敏感信息的第三方库 debug
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    # Access paths can contain private media identifiers; only application logs.
    logging.getLogger("uvicorn.access").disabled = True
