# -*- coding: utf-8 -*-
"""测试夹具：隔离数据目录 + mock 模型客户端。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 保证能 import app（backend 目录加入 path）
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core import config  # noqa: E402


class MockLLM:
    """返回固定结构化输出，不调用真实模型。"""

    def __init__(self, script: dict | None = None, storyboard: dict | None = None):
        self.script = script or {
            "title": "测试片",
            "logline": "一句梗概",
            "genre": ["测试"],
            "mood": "平静",
            "characters": [{"name": "猫", "description": "流浪猫", "role": "主角"}],
            "settings": [{"name": "雨夜街道", "description": "下雨的街道"}],
            "episodes": [{"episode_number": 1, "act_title": "相遇", "content": "画面：雨夜。动作：猫躲雨。对白：喵。"}],
        }
        self.storyboard = storyboard or {
            "shots": [
                {"shot_id": "s1", "episode_number": 1, "description": "雨夜街道", "prompt": "rainy street, cat"},
                {"shot_id": "s2", "episode_number": 1, "description": "猫躲雨", "prompt": "cat hiding"},
            ]
        }

    def generate(self, system: str, user: str) -> str:
        return "扩写后的一句话梗概"

    def generate_json(self, system: str, user: str, model_cls):
        from app.schemas.session import ScriptArtifact, StoryboardArtifact

        if model_cls is StoryboardArtifact:
            data = self.storyboard
        else:
            data = self.script
        return model_cls.model_validate(data)


class MockImage:
    def text_to_image(self, prompt: str, out_path: Path, model: str | None = None) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-image")
        return out_path

    def image_to_image(self, image_path, prompt, out_path, model=None):
        return self.text_to_image(prompt, out_path)


class MockVideo:
    def image_to_video(self, image_url: str, prompt: str, out_path: Path, mode: str = "first_frame") -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-video")
        return out_path


class MockTTS:
    async def synthesize(self, text: str, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-audio")
        return out_path


class MockReviewer:
    """默认关闭审查，避免测试真实调用 VLM。"""

    enabled = False

    def review_image(self, path: str) -> dict:
        return {"safe": True, "reason": ""}


@pytest.fixture
def data_dirs(tmp_path, monkeypatch):
    """把运行数据目录指到临时目录，隔离测试。"""
    for attr in ("DATA_DIR", "SESSIONS_DIR", "IMAGE_DIR", "VIDEO_DIR", "SCRIPT_DIR", "UPLOAD_DIR", "RESULT_DIR"):
        d = tmp_path / attr.lower()
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, attr, d)
    from app.services import db
    db.init_db()
    return tmp_path


@pytest.fixture
def orch(data_dirs):
    from app.services.orchestrator import Orchestrator

    return Orchestrator(llm=MockLLM(), image=MockImage(), video=MockVideo(), tts=MockTTS(), reviewer=MockReviewer())
