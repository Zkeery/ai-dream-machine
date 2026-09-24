# -*- coding: utf-8 -*-
"""状态机流转测试（模型全 mock，离线）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AppError
from app.schemas.session import SessionCreate
from app.services import orchestrator as orch_mod


async def _noop(stage, message, percent):
    pass


@pytest.fixture(autouse=True)
def _mock_ffmpeg(monkeypatch):
    async def fake_concat(parts, out_path, audio_path=None):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-final")
        return out_path

    monkeypatch.setattr(orch_mod, "concat_videos", fake_concat)


def test_create(orch):
    m = orch.create(SessionCreate(idea="一只猫的故事"))
    assert m.session_id
    assert m.status == "idle"


@pytest.mark.asyncio
async def test_full_flow(orch):
    m = orch.create(SessionCreate(idea="一只猫的故事", episodes=1))
    for stage in orch_mod.STAGE_ORDER:
        m = await orch.execute_stage(m.session_id, stage, _noop)
        assert m.status == "stage_completed"
        assert stage in m.stages_completed
        m = orch.continue_session(m.session_id)
    assert m.status == "session_completed"
    # 产物已落盘
    assert m.artifacts["script_generation"]["title"] == "测试片"
    assert m.artifacts["post_production"]["final_video"]


@pytest.mark.asyncio
async def test_unknown_stage(orch):
    m = orch.create(SessionCreate(idea="x"))
    with pytest.raises(AppError) as e:
        await orch.execute_stage(m.session_id, "nope", _noop)
    assert e.value.code == "UNKNOWN_STAGE"


@pytest.mark.asyncio
async def test_continue_not_ready(orch):
    m = orch.create(SessionCreate(idea="x"))
    with pytest.raises(AppError) as e:
        orch.continue_session(m.session_id)
    assert e.value.code == "NOT_READY"


@pytest.mark.asyncio
async def test_script_artifact_persisted(orch):
    from app.core import config

    m = orch.create(SessionCreate(idea="x"))
    m = await orch.execute_stage(m.session_id, "script_generation", _noop)
    script_file = config.SCRIPT_DIR / m.session_id / "script.json"
    assert script_file.exists()
    assert "测试片" in script_file.read_text(encoding="utf-8")
