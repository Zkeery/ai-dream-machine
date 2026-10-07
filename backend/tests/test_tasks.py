# -*- coding: utf-8 -*-
"""第二刀：三条短管线任务测试（mock 客户端，不调真实模型/FFmpeg）。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.services import auth, ffmpeg_util  # noqa: E402
from app.services.short_pipelines import ShortPipelines  # noqa: E402


# ---------- mock FFmpeg ----------

async def _fake_video(parts=None, out_path=None, audio_path=None, image_path=None, video_path=None):
    out_path = out_path or Path("/tmp/x.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b"fake-mp4")
    return out_path


async def _fake_image_audio(image_path, audio_path, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b"fake-mp4")
    return out_path


async def _fake_first_frame(video_path, out_path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b"fake-jpg")
    return out_path


@pytest.fixture
def mock_ffmpeg(monkeypatch):
    monkeypatch.setattr(ffmpeg_util, "concat_videos", _fake_video)
    monkeypatch.setattr(ffmpeg_util, "image_audio_to_video", _fake_image_audio)
    monkeypatch.setattr(ffmpeg_util, "extract_first_frame", _fake_first_frame)


# ---------- mock 客户端 ----------

class _LLM:
    def generate(self, system, user, **kwargs):
        return "雨夜的街。一只猫躲在檐下。有人撑伞蹲下。"


class _Image:
    def text_to_image(self, prompt, out_path, model=None):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-img")
        return out_path


class _Video:
    def image_to_video(self, image_path, prompt, out_path, mode="first_frame", **kwargs):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-video")
        return out_path


class _TTS:
    async def synthesize(self, text, out_path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-audio")
        return out_path


def _pipelines():
    return ShortPipelines(llm=_LLM(), image=_Image(), video=_Video(), tts=_TTS())


async def _noop(stage, msg, pct):
    pass


# ---------- 单元：三条管线 ----------

def test_literary_video(data_dirs, mock_ffmpeg):
    result = asyncio.run(_pipelines().run("t1", "literary_video", {"text": "一只猫", "style": "realistic"}, _noop))
    assert result["final_video"].endswith("final.mp4")
    assert len(result["sentences"]) >= 1
    assert Path(result["final_video"]).exists()


def test_literary_video_full_text(data_dirs, mock_ffmpeg):
    text = "清晨的巷子。早餐摊冒着热气。老板笑着招呼客人。"
    result = asyncio.run(_pipelines().run("t2", "literary_video", {"text": text}, _noop))
    assert len(result["sentences"]) == 3


@pytest.mark.parametrize("legacy_video", [None, "/tmp/m.mp4"])
def test_motion_transfer(data_dirs, mock_ffmpeg, monkeypatch, legacy_video):
    async def unused_frame(*args):
        raise AssertionError("动作描述生成不应提取未使用的视频首帧")
    monkeypatch.setattr(ffmpeg_util, "extract_first_frame", unused_frame)
    inputs = {"character_image": "/tmp/c.jpg", "prompt": "挥手"}
    if legacy_video:
        inputs["motion_video"] = legacy_video
    result = asyncio.run(_pipelines().run("t3", "motion_transfer",
                                          inputs, _noop))
    assert result["final_video"].endswith("final.mp4")
    assert "motion_frame" not in result


def test_talking_head(data_dirs, mock_ffmpeg):
    result = asyncio.run(_pipelines().run("t4", "talking_head",
                                          {"person_image": "/tmp/p.jpg", "script": "大家好"}, _noop))
    assert result["final_video"].endswith("final.mp4")
    assert result["audio"].endswith("voice.mp3")
    assert result["talking_mode"] == "static"
    assert result["model_usage"]["models"] == {}


def test_pipeline_validation():
    with pytest.raises(Exception):
        asyncio.run(_pipelines().run("t5", "literary_video", {"text": ""}, _noop))
    with pytest.raises(Exception):
        asyncio.run(_pipelines().run("t6", "talking_head", {"person_image": "/tmp/p.jpg"}, _noop))


# ---------- API：任务创建/列表/隔离 ----------

@pytest.fixture
def client(data_dirs, mock_ffmpeg, monkeypatch):
    import app.api.tasks as tasks_api

    monkeypatch.setattr(tasks_api, "get_pipelines", _pipelines)
    from app.main import app

    return TestClient(app)


def _login(client, code):
    r = client.post("/api/auth/login", json={"invite_code": code})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_task_requires_auth(client, data_dirs):
    assert client.post("/api/tasks", json={"type": "literary_video", "input": {"text": "x"}}).status_code == 401
    assert client.get("/api/tasks").status_code == 401


def test_create_task_and_isolation(client, data_dirs):
    code_a = auth.generate_invite_codes(1)[0]
    code_b = auth.generate_invite_codes(1)[0]
    ta = _login(client, code_a)
    tb = _login(client, code_b)
    ua = client.get("/api/auth/me", headers=_h(ta)).json()["user_id"]

    r = client.post("/api/tasks", json={"type": "literary_video", "input": {"text": "一只猫"}}, headers=_h(ta))
    assert r.status_code == 200
    tid = r.json()["task_id"]

    # A 能看到自己的任务
    tasks_a = client.get("/api/tasks", headers=_h(ta)).json()
    assert any(t["task_id"] == tid for t in tasks_a)

    # B 看不到 A 的任务，访问 404
    tasks_b = client.get("/api/tasks", headers=_h(tb)).json()
    assert all(t["task_id"] != tid for t in tasks_b)
    assert client.get(f"/api/tasks/{tid}", headers=_h(tb)).status_code == 404

    # A 能看到详情
    assert client.get(f"/api/tasks/{tid}", headers=_h(ta)).status_code == 200


def test_task_input_upload_ownership(client, data_dirs):
    code = auth.generate_invite_codes(1)[0]
    token = _login(client, code)
    # 未上传文件就引用 → 404/校验失败
    r = client.post("/api/tasks", json={"type": "talking_head", "input": {"person_image": "nope.jpg", "script": "hi"}},
                    headers=_h(token))
    assert r.status_code == 404
