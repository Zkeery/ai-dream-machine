# -*- coding: utf-8 -*-
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core.errors import AppError
from app.services import d7_voiceover as vo

LINES = Path(__file__).resolve().parents[2] / "docs" / "evidence" / "D7-角色一致性" / "配音台词-S1到S5.json"


def test_lines_file_complete():
    data = json.loads(LINES.read_text(encoding="utf-8"))
    assert [s["id"] for s in data["samples"]] == ["S1", "S2", "S3", "S4", "S5"]
    from app.models.tts_client import SUPPORTED_VOICES
    allowed = {v["voice"] for v in SUPPORTED_VOICES}
    for sample in data["samples"]:
        assert [line["shot_id"] for line in sample["lines"]] == [f"s{i}" for i in range(1, 6)]
        for line in sample["lines"]:
            assert line["voice"] in allowed and line["text"].strip() and line["role"]
            assert 0 <= line["start_s"] < line["end_s"] <= 25.21


def test_rate_for():
    assert vo.rate_for(4.5) == 0
    assert vo.rate_for(4.8) == 0
    assert vo.rate_for(5.5) == 20  # 5.5/4.6 = 1.196 -> +20%
    assert vo.rate_for(30.0) == vo.MAX_RATE_PCT
    assert vo.rate_for(5.0, 10) >= 10


def test_build_ass_times_and_style():
    lines = [{"text": "第一句"}, {"text": "第二{句}"}]
    ass = vo.build_ass(lines, [(0.0, 5.0417), (5.0417, 10.0833)])
    assert "Style: Default,Noto Sans CJK SC,60,&H00FFFFFF" in ass and ",1,4,1,2," in ass
    assert "Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,第一句" in ass
    assert "Dialogue: 0,0:00:05.04,0:00:10.04,Default,,0,0,0,,第二\\{句\\}" in ass
    with pytest.raises(AppError):
        vo.build_ass(lines, [(0.0, 5.0)])


def test_mix_filter_delays_and_pads():
    graph = vo.mix_filter([0.0, 5.0417], [3.2, 5.0], 25.208)
    assert "adelay=0|0" in graph and "adelay=5042|5042" in graph
    assert "afade=t=out:st=4.850:d=0.15" in graph
    assert "amix=inputs=2:normalize=0" in graph and "apad=whole_dur=25.208,atrim=0:25.208" in graph


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")
def test_synthesize_speeds_up_long_lines(tmp_path):
    calls = []

    class FakeTTS:
        def __init__(self, rate):
            self.rate = rate

        async def synthesize(self, text, out_path, *, voice=None):
            calls.append(self.rate)
            seconds = 6.0 if self.rate == "+0%" else 4.5
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            f"sine=frequency=440:duration={seconds}", str(out_path)], check=True)
            return out_path

    lines = [{"text": "很长的一句", "voice": "zh-CN-YunyangNeural"}, {"text": "短句", "voice": "zh-CN-YunyangNeural",
                                                                  "tts_text": "短句读法"}]
    res = asyncio.run(vo.synthesize_lines(lines, tmp_path, FakeTTS))
    assert calls[0] == "+0%" and calls[1].startswith("+") and calls[1] != "+0%"
    assert res[0]["duration"] <= vo.MAX_LINE_S and not res[0]["over"]


def test_line_offset_and_subtitle_start():
    lines = [{"shot_id": "s1", "text": "甲"}, {"shot_id": "s2", "text": "乙", "offset_s": 1.32}]
    bounds = [(0.0, 5.0417), (5.0417, 10.0833)]
    assert vo.line_starts(lines, bounds) == [0.0, 5.0417 + 1.32]
    assert "Dialogue: 0,0:00:06.36,0:00:10.04,Default,,0,0,0,,乙" in vo.build_ass(lines, bounds)
    with pytest.raises(AppError):
        vo.line_starts([{"shot_id": "s1", "text": "x", "offset_s": 6}], [(0.0, 5.0)])


def test_rate_strings():
    assert vo._pct("-10%") == -10 and vo._pct(None) == 0 and vo._pct("+19%") == 19
    assert vo._rate_str(-10) == "-10%" and vo._rate_str(19) == "+19%"
