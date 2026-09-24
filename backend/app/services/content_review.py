# -*- coding: utf-8 -*-
"""内容安全结果侧审查：用 VLM 判定图片是否命中红线。"""
from __future__ import annotations

import json

from app.core import config
from app.core.errors import AppError
from app.models.vlm_client import VLMClient

INSTRUCTION = (
    "请判断这张图片是否包含以下违规内容：真实公众人物或真人肖像、侵权 IP（影视/动漫/游戏角色）、"
    "违法或低俗内容。只返回 JSON：{\"safe\": true 或 false, \"reason\": \"简短原因\"}，不要输出其他文字。"
)


class ContentReviewer:
    def __init__(self, vlm: VLMClient | None = None) -> None:
        self.vlm = vlm or VLMClient()
        self.enabled = config.settings.content_review_enabled

    def review_image(self, path: str) -> dict:
        """返回 {"safe": bool, "reason": str}；审查关闭时直接放行。"""
        if not self.enabled:
            return {"safe": True, "reason": "审查已关闭"}
        raw = self.vlm.review(path, INSTRUCTION)
        try:
            data = json.loads(raw)
            safe = data["safe"]
            if not isinstance(safe, bool):
                raise ValueError("safe 非布尔")
            return {"safe": safe, "reason": str(data.get("reason", ""))}
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            # 审查解析失败：不静默放行，报错待人工复核
            raise AppError("CONTENT_REVIEW_FAILED", "内容审查未能得出确定结论，请人工复核", 502) from None
