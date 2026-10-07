# -*- coding: utf-8 -*-
"""视频客户端网络加固测试：有限重试、4xx 快速失败、整体截止。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402

from app.core import config  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.models.video_client import VideoClient  # noqa: E402


def _resp(status, body=None):
    return httpx.Response(status, json=body if body is not None else {})


def _fake_download(self, url, out):
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"v")
    return out


def test_post_does_not_resubmit_after_5xx(monkeypatch, tmp_path):
    posts = {"n": 0}

    def fake_request(method, url, **kwargs):
        if method == "POST":
            posts["n"] += 1
            if posts["n"] == 1:
                return _resp(500)
            return _resp(200, {"id": "vid1"})
        return _resp(200, {"status": "completed", "url": "http://x/v"})

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr(VideoClient, "_download", _fake_download)
    monkeypatch.setattr(config.settings, "max_retries", 2)
    c = VideoClient()
    c.poll_interval = 0
    with pytest.raises(AppError) as caught:
        c.image_to_video(__file__, "挥手", tmp_path / "o.mp4", mode="reference")
    assert caught.value.code == "VIDEO_SUBMIT_UNCONFIRMED"
    assert posts["n"] == 1
    assert not (tmp_path / "o.mp4").exists()


def test_post_fails_fast_on_4xx(monkeypatch, tmp_path):
    posts = {"n": 0}

    def fake_request(method, url, **kwargs):
        if method == "POST":
            posts["n"] += 1
            return _resp(400, {"error": "bad"})
        return _resp(200, {"status": "completed"})

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr(config.settings, "max_retries", 3)
    c = VideoClient()
    with pytest.raises(AppError):
        c.image_to_video(__file__, "挥手", tmp_path / "o.mp4")
    assert posts["n"] == 1  # 4xx 不重试


def test_post_does_not_resubmit_after_unknown_transport_result(monkeypatch, tmp_path):
    posts = {"n": 0}

    def fake_request(method, url, **kwargs):
        if method == "POST":
            posts["n"] += 1
            if posts["n"] <= 2:
                raise httpx.ReadTimeout("read timeout")
            return _resp(200, {"id": "vid1"})
        return _resp(200, {"status": "completed", "url": "http://x/v"})

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr(VideoClient, "_download", _fake_download)
    monkeypatch.setattr(config.settings, "max_retries", 2)
    c = VideoClient()
    c.poll_interval = 0
    with pytest.raises(AppError) as caught:
        c.image_to_video(__file__, "挥手", tmp_path / "o.mp4")
    assert caught.value.code == "VIDEO_SUBMIT_UNCONFIRMED"
    assert posts["n"] == 1
    assert not (tmp_path / "o.mp4").exists()
