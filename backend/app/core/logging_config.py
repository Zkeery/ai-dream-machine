# -*- coding: utf-8 -*-
"""日志配置：分级、不记密钥/密码/完整用户文档。"""
from __future__ import annotations

import logging


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # 关闭可能回显敏感信息的第三方库 debug
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
