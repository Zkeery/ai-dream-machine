"""Recording data must show real local media and real checker findings."""
import importlib.util
from pathlib import Path

import pytest
from app.core.errors import AppError
from app.schemas.session import SessionMeta, ScriptArtifact
from app.services.creation_checks import record_check

spec = importlib.util.spec_from_file_location("recording_fixtures", Path(__file__).parents[1] / "scripts/demo_fixtures.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def test_prepared_story_has_three_actual_findings(data_dirs):
    script = ScriptArtifact.model_validate(fixtures.SCRIPT).model_dump()
    assert all(word not in str(script).lower() for word in ("mock", "测试", "离线"))
    meta = SessionMeta(session_id="recording", idea="雨夜归途", artifacts={"script_generation": script})
    report = record_check(meta, "script_generation")
    assert report["status"] == "review"
    assert len(report["findings"]) == 3
    assert {f["location"] for f in report["findings"]} == {"剧本 · 人物1 · 描述", "剧本 · 场景1 · 描述", "剧本 · 第1集 · 剧情"}


def test_replacement_images_change_bytes_and_survive_client_restart(data_dirs, tmp_path):
    untouched = tmp_path / "setting_0_keep.png"
    untouched.write_bytes((fixtures.ASSETS / "walk-03.png").read_bytes())
    old_scene = untouched.read_bytes()
    contents = []
    for index in range(3):
        image = fixtures.make_orchestrator().image
        path = image.text_to_image("换个角度", tmp_path / f"character_0_v{index}.png")
        content = path.read_bytes()
        assert content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) > 100000
        contents.append(content)
    assert len(set(contents)) == 3
    assert untouched.read_bytes() == old_scene


def test_interruption_is_fixture_only_and_normal_demo_client_continues(data_dirs, tmp_path):
    out = tmp_path / "seg_1_version.mp4"
    with pytest.raises(AppError, match="第二个镜头"):
        fixtures.make_orchestrator(interrupt_video=True).video.image_to_video("", "", out)
    assert not out.exists()
    fixtures.make_orchestrator().video.image_to_video("", "", out)
    assert out.read_bytes() == (fixtures.ASSETS / "shot-02.mp4").read_bytes()
