# -*- coding: utf-8 -*-
"""内容安全结果侧审查测试（mock VLM，不调真实模型）。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.errors import AppError  # noqa: E402
from app.services.content_review import ContentReviewer, STRUCTURAL_FLAGS  # noqa: E402


class _VLM:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    def review(self, image_path: str, instruction: str) -> str:
        self.calls.append((image_path, instruction))
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
    assert r.vlm.calls == []


def _strict_payload(**changes):
    return {
        "safe": True,
        "person_count": 1,
        **{field: False for field in STRUCTURAL_FLAGS},
        "reason": "恰好1人，未见结构违规",
        **changes,
    }


def _review_payload(payload):
    raw = json.dumps(payload, ensure_ascii=False)
    result = ContentReviewer(vlm=_VLM(raw)).review_image("/tmp/x.png", expected_person_count=1)
    assert result["raw_response"] == raw
    return result


def test_strict_single_person_passes_and_preserves_evidence():
    result = _review_payload(_strict_payload())
    assert result["review_valid"] is True
    assert result["accepted"] is result["safe"] is True
    assert result["person_count"] == result["人物数量"] == 1
    assert result["content_safe"] is True
    assert result["rule_reasons"] == []


@pytest.mark.parametrize("count", [0, 2, 3])
def test_strict_program_uses_count_not_model_accepted(count):
    result = _review_payload(_strict_payload(person_count=count, accepted=True))
    assert result["review_valid"] is True
    assert result["accepted"] is result["safe"] is False
    assert result["人物数量"] == count
    assert f"人物数量为{count}" in result["reason"]


@pytest.mark.parametrize("count", [True, False, "1", 1.0, None, -1, [], {}, "未知"])
def test_strict_rejects_non_integer_or_unknown_count(count):
    result = _review_payload(_strict_payload(person_count=count))
    assert result["review_valid"] is result["accepted"] is False
    assert result["person_count"] is None


@pytest.mark.parametrize("field", ["safe", "person_count", *STRUCTURAL_FLAGS, "reason"])
def test_strict_missing_fields_do_not_count_as_decisive_rejection(field):
    payload = _strict_payload()
    del payload[field]
    result = _review_payload(payload)
    assert result["review_valid"] is result["accepted"] is False
    assert result["rule_reasons"]


@pytest.mark.parametrize("field", STRUCTURAL_FLAGS)
def test_strict_every_structure_flag_blocks_even_with_one_person(field):
    result = _review_payload(_strict_payload(**{field: True}))
    assert result["review_valid"] is True
    assert result["accepted"] is result["safe"] is False
    assert result[field] is True


@pytest.mark.parametrize("field", ["safe", *STRUCTURAL_FLAGS])
@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_strict_boolean_fields_are_not_coerced(field, value):
    result = _review_payload(_strict_payload(**{field: value}))
    assert result["review_valid"] is result["accepted"] is False


def test_strict_preserves_safety_gate():
    result = _review_payload(_strict_payload(safe=False))
    assert result["review_valid"] is True
    assert result["accepted"] is result["content_safe"] is False


@pytest.mark.parametrize("raw", ["not JSON", "[]", "null", "true", '{"safe":true,"safe":false}'])
def test_strict_invalid_json_returns_raw_indeterminate_evidence(raw):
    result = ContentReviewer(vlm=_VLM(raw)).review_image("/tmp/x.png", expected_person_count=1)
    assert result["review_valid"] is result["accepted"] is False
    assert result["raw_response"] == raw


def test_strict_chinese_person_count_alias():
    payload = _strict_payload()
    payload["人物数量"] = payload.pop("person_count")
    result = _review_payload(payload)
    assert result["accepted"] is True
    assert result["person_count"] == result["人物数量"] == 1


@pytest.mark.parametrize("alias", [2, True, "1"])
def test_strict_conflicting_or_invalid_count_alias_blocks(alias):
    result = _review_payload(_strict_payload(**{"人物数量": alias}))
    assert result["review_valid"] is result["accepted"] is False


def test_strict_review_cannot_be_disabled(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.content_review_enabled", False)
    vlm = _VLM(json.dumps(_strict_payload(person_count=3, multi_view_layout=True)))
    result = ContentReviewer(vlm=vlm).review_image("/tmp/x.png", expected_person_count=1)
    assert result["review_valid"] is True
    assert result["accepted"] is False
    assert len(vlm.calls) == 1
    instruction = vlm.calls[0][1]
    for phrase in ["恰好 1", "两个侧身人物加一个独立头像必须报告3", "侵权 IP", *STRUCTURAL_FLAGS]:
        assert phrase in instruction


@pytest.mark.parametrize("expected", [True, False, "1", 1.0, -1])
def test_invalid_expected_count_rejected_before_model_call(expected):
    vlm = _VLM("unused")
    with pytest.raises(ValueError):
        ContentReviewer(vlm=vlm).review_image("/tmp/x.png", expected_person_count=expected)
    assert vlm.calls == []


def test_zero_expected_people_supported():
    vlm = _VLM(json.dumps(_strict_payload(person_count=0)))
    assert ContentReviewer(vlm=vlm).review_image("/tmp/x.png", expected_person_count=0)["accepted"] is True


def test_legacy_protocol_and_return_shape_unchanged(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.content_review_enabled", True)
    vlm = _VLM('{"safe": true, "reason": "安全"}')
    assert ContentReviewer(vlm=vlm).review_image("/tmp/x.png") == {"safe": True, "reason": "安全"}
    assert "person_count" not in vlm.calls[0][1]


def test_comic07_regression_manifest_is_hash_bound_and_has_no_fabricated_judgments():
    manifest = json.loads((BACKEND / "tests/fixtures/comic07_review_leak_regression.json").read_text())
    assert manifest["evidence_kind"] == "regression_inputs_not_model_results"
    assert manifest["case_id"] == "COMIC07"
    samples = manifest["samples"]
    assert [s["id"] for s in samples] == ["character", "panel_1", "panel_2", "panel_3", "panel_4"]
    assert len({s["sha256"] for s in samples}) == 5
    for sample in samples:
        assert sample["expected_person_count"] == 1
        assert sample["expected_block"] is True
        assert len(sample["sha256"]) == 64
        assert not Path(sample["image_path"]).is_absolute()
        assert "actual_result" not in sample
        assert "person_count" not in sample


def test_comic07_local_regression_assets_match_original_hashes():
    manifest = json.loads((BACKEND / "tests/fixtures/comic07_review_leak_regression.json").read_text())
    root = BACKEND.parent
    samples = manifest["samples"]
    if not all((root / sample["image_path"]).is_file() for sample in samples):
        pytest.skip("历史原图未随代码分发；需导入manifest所绑定的本地回归素材")
    for sample in samples:
        content = (root / sample["image_path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == sample["sha256"]
        assert len(content) == sample["bytes"]
