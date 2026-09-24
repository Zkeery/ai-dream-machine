# -*- coding: utf-8 -*-
"""模型输出解析器（纯函数单测）：宽容解析 JSON。"""
from __future__ import annotations

import pytest

from app.models.llm_client import _extract_json


def test_plain_json():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_fenced_json():
    text = '```json\n{"a": 1}\n```'
    assert _extract_json(text) == {"a": 1}


def test_noise_around_json():
    text = '好的，以下是结果：\n{"title": "片名"}\n以上。'
    assert _extract_json(text) == {"title": "片名"}


def test_nested_object():
    text = '{"chars": [{"name": "猫"}], "n": {"x": 1}}'
    assert _extract_json(text)["chars"][0]["name"] == "猫"


@pytest.mark.parametrize("bad", ["", "没有 JSON", "只有 { 没有闭合", "hello"])
def test_invalid(bad):
    with pytest.raises(ValueError):
        _extract_json(bad)
