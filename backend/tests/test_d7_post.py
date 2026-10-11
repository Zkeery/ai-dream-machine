# -*- coding: utf-8 -*-
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.services import d7_post

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")


def _clip(path: Path, size: str = "160x90", sheet_first: bool = False) -> Path:
    src = f"testsrc2=size={size}:rate=24:duration=1"
    if sheet_first:  # 第 0 帧换成一张完全不同的纯色「定妆图」
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=white:size={size}:rate=24,trim=end_frame=1",
               "-f", "lavfi", "-i", src, "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]"]
    else:
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src]
    subprocess.run([*cmd, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_filter_trims_only_flagged_clips():
    graph = d7_post._filter(2, [True, False])
    assert "[0:v]trim=start_frame=1,setpts=PTS-STARTPTS,setsar=1[v0]" in graph
    assert "[1:v]setsar=1[v1]" in graph
    assert "concat=n=2:v=1:a=0,format=yuv420p" in graph


def test_sheet_detection(tmp_path):
    assert d7_post.first_frame_is_sheet(_clip(tmp_path / "a.mp4"))["trim"] is False
    assert d7_post.first_frame_is_sheet(_clip(tmp_path / "b.mp4", sheet_first=True))["trim"] is True


def test_finalize_outputs_master_and_preview(tmp_path):
    clips = [_clip(tmp_path / "s1.mp4"), _clip(tmp_path / "s2.mp4", sheet_first=True)]
    out = d7_post.finalize(clips, tmp_path / "out")
    assert Path(out["master"]).name == "final-master.mp4" and Path(out["preview"]).name == "final.mp4"
    for key in ("master_info", "preview_info"):
        info = out[key]
        assert (info["width"], info["height"], info["fps"], info["has_audio"]) == (160, 90, 24.0, False)
        assert info["frames"] == 24 + 24  # 第二镜的定妆帧被去掉
    assert out["preview_info"]["size"] < d7_post.PREVIEW_LIMIT_BYTES


def test_finalize_rejects_mixed_resolution(tmp_path):
    clips = [_clip(tmp_path / "s1.mp4"), _clip(tmp_path / "s2.mp4", size="128x72")]
    with pytest.raises(AppError) as err:
        d7_post.finalize(clips, tmp_path / "out")
    assert err.value.code == "D7_RESOLUTION_MIXED"


def test_reusable_sheet(tmp_path):
    good = tmp_path / "a.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20_000)
    tiny = tmp_path / "b.png"
    tiny.write_bytes(b"\x89PNG\r\n\x1a\n")
    junk = tmp_path / "c.png"
    junk.write_bytes(b"x" * 20_000)
    assert d7_post.reusable_sheet(good)
    assert not d7_post.reusable_sheet(tiny)
    assert not d7_post.reusable_sheet(junk)
    assert not d7_post.reusable_sheet(tmp_path / "missing.png")


def _sized_clip(path: Path, size: str, seconds: float, rate: int = 24) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=gray:size={size}:rate={rate}:duration={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def test_reusable_clip_and_plan(tmp_path):
    out = tmp_path / "S9"
    (out / "shots").mkdir(parents=True)
    (out / "sheets").mkdir()
    _sized_clip(out / "shots" / "s1.mp4", "1920x1080", 5.04)          # 合格
    _sized_clip(out / "shots" / "s2.mp4", "1280x720", 5.04)           # 分辨率不对
    _sized_clip(out / "shots" / "s3.mp4", "1920x1080", 2.0)           # 帧数不够
    _sized_clip(out / "shots" / "s4.mp4", "1920x1080", 5.04, rate=30)  # 帧率不对
    (out / "sheets" / "hero.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20_000)
    sample = {"assets": [{"id": "hero"}, {"id": "place"}],
              "shots": [{"shot_id": f"s{i}"} for i in range(1, 6)]}
    plan = d7_post.reuse_plan(sample, out)
    assert plan["assets"] == {"hero": True, "place": False}
    assert plan["shots"] == {"s1": True, "s2": False, "s3": False, "s4": False, "s5": False}


def test_quota_error_stops_run():
    import importlib.util, sys as _sys
    spec = importlib.util.spec_from_file_location(
        "run_d7_round", Path(__file__).resolve().parents[1] / "scripts" / "run_d7_round.py")
    mod = importlib.util.module_from_spec(spec)
    old = list(_sys.argv)
    _sys.argv = ["run_d7_round.py"]
    try:
        spec.loader.exec_module(mod)
    finally:
        _sys.argv = old
    assert mod._is_quota_error(AppError("VIDEO_SUBMIT_REJECTED", "视频接口返回 402，请求未被受理", 502))
    assert mod._is_quota_error(AppError("IMAGE_SUBMIT_REJECTED", "图片接口返回 402，请求未被受理", 502))
    assert not mod._is_quota_error(AppError("VIDEO_SUBMIT_REJECTED", "视频接口返回 400，请求未被受理", 502))
    assert not mod._is_quota_error(AppError("VIDEO_SUBMIT_UNCONFIRMED", "视频接口返回 503", 502))
