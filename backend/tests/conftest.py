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


@pytest.fixture(autouse=True)
def isolated_client_budget(monkeypatch):
    """Direct client contract tests have no account; only those calls use a stub.

    Requests with an execution or explicit owner always exercise the real ledger.
    Dedicated quota tests override these deliberately generous fixture limits.
    """
    from app.services import cost_control, execution_store
    original_reserve, original_settle = cost_control.reserve, cost_control.settle
    stub_calls = set()

    def reserve(call_id, model, kind, units, **kwargs):
        if not kwargs.get("owner_id") and not kwargs.get("execution_id") and not execution_store.current_execution_id():
            stub_calls.add(call_id)
            return {"call_id": call_id, "status": "isolated_test_stub"}
        return original_reserve(call_id, model, kind, units, **kwargs)

    def settle(call_id, *args, **kwargs):
        if call_id in stub_calls:
            return {"call_id": call_id, "status": "isolated_test_stub"}
        return original_settle(call_id, *args, **kwargs)

    monkeypatch.setattr(cost_control, "reserve", reserve)
    monkeypatch.setattr(cost_control, "settle", settle)
    monkeypatch.setattr(config.settings, "max_active_executions", 10000)
    monkeypatch.setattr(config.settings, "max_active_executions_per_user", 10000)
    monkeypatch.setenv("MAX_ACTIVE_EXECUTIONS", "10000")
    monkeypatch.setenv("MAX_ACTIVE_EXECUTIONS_PER_USER", "10000")
    return {"reserve": original_reserve, "settle": original_settle}


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
                {"shot_id": "s1", "episode_number": 1, "description": "雨夜街道", "prompt": "rainy street, cat",
                 "character_ids": ["c1"], "setting_ids": ["l1"]},
                {"shot_id": "s2", "episode_number": 1, "description": "猫躲雨", "prompt": "cat hiding",
                 "character_ids": ["c1"], "setting_ids": ["l1"]},
            ]
        }

    def generate(self, system: str, user: str, **kwargs) -> str:
        return "扩写后的一句话梗概"

    def generate_json(self, system: str, user: str, model_cls, **kwargs):
        from app.schemas.session import ScriptArtifact, StoryboardArtifact

        if model_cls is StoryboardArtifact:
            data = self.storyboard
        else:
            data = self.script
        return model_cls.model_validate(data)


class MockImage:
    def text_to_image(self, prompt: str, out_path: Path, model: str | None = None, **kwargs) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-image")
        return out_path

    def image_to_image(self, image_path, prompt, out_path, model=None, **kwargs):
        return self.text_to_image(prompt, out_path)


class MockVideo:
    def image_to_video(self, image_url: str, prompt: str, out_path: Path, mode: str = "first_frame", **kwargs) -> Path:
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
