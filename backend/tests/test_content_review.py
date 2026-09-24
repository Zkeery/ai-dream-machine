# -*- coding: utf-8 -*-
"""内容安全结果侧审查测试（mock VLM，不调真实模型）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.errors import AppError  # noqa: E402
from app.services.content_review import ContentReviewer  # noqa: E402


class _VLM:
    def __init__(self, reply: str):
        self.reply = reply

    def review(self, image_path: str, instruction: str) -> str:
        return self.reply


def test_safe_image_passes(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.content_review_enabled", True)
    r = ContentReviewer(vlm=_VLM('{"safe": true, "reason": ""}'))
    assert r.review_image("/tmp/x.png")["safe"] is True


def test_unsafe_image_flagged(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.content_review_enabled", True)
    r = ContentReviewer(vlm=_VLM('{"safe": false, "reason": "出现真人肖像"}'))
    result = r.review_image("/tmp/x.png")
    assert result["safe"] is False
    assert "真人肖像" in result["reason"]


def test_invalid_json_raises(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.content_review_enabled", True)
    r = ContentReviewer(vlm=_VLM("这不是 JSON"))
    with pytest.raises(AppError) as e:
        r.review_image("/tmp/x.png")
    assert e.value.code == "CONTENT_REVIEW_FAILED"


def test_disabled_review_skips(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.content_review_enabled", False)
    r = ContentReviewer(vlm=_VLM("不会调用"))
    assert r.review_image("/tmp/x.png")["safe"] is True
