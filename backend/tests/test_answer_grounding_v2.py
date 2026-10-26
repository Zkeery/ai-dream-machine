"""Frozen v1 assertions plus new v2 local answerability-scope cases.

This file is loaded by the separately isolated comparison runner. It performs
no model/network calls and never edits the frozen test or module files.
"""
import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pure_answer_grounding_v2_tests", BASE / "app/services/answer_grounding_v2.py")
v2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v2)
check = v2.check_answer_grounding

# Execute the original test functions/parametrizations unchanged against v2.
# Function globals point at this in-memory legacy module; no disk mutation.
legacy_spec = importlib.util.spec_from_file_location("frozen_v1_assertions_for_v2", BASE / "tests/test_answer_grounding.py")
legacy = importlib.util.module_from_spec(legacy_spec)
legacy_spec.loader.exec_module(legacy)
legacy.check = check
legacy.grounding = v2
for _name, _function in list(vars(legacy).items()):
    if _name.startswith("test_") and callable(_function):
        globals()["test_v1_compat_" + _name[5:]] = _function


@pytest.mark.parametrize("answer", [
    "无法确定，因为未提供相关资料。",
    "无法确定，因为未提供相关故事资料。",
    "无法确定，因为未提供关于该日记本的具体资料（如材质、设计、用途等），且根据规则，给定资料不能覆盖的问题应拒答。",
    "无法确定，因为未提供相关剧本或创作资料，无法判断剧情安排是否可行。",
    "资料不足，所以无法确定。",
    "由于当前信息不足，因此无法判断。",
    "无法确定，因权限错误导致没有可用资料。",
    "无法确定产品的销售价格，因为提供的资料中未包含相关价格信息。",
    "无法确定，因为未提供任何外部资料。",
    "无法回答，因为没有相关信息。",
    "信息缺失，因此不能确认。",
])
def test_v2_local_missing_input_explanations_are_not_world_assertions(answer):
    result = check(answer, [], [])
    assert result["status"] == "NOT_CHECKED"
    assert result["answer"] == answer and not result["semantic_acceptance"]
    assert all(c["state"] != "NEED_REVIEW" for c in result["claims"])


@pytest.mark.parametrize("answer", [
    "无法确定，因为未提供相关资料。但需关好窗帘以防海面光影让人迷失方向。",
    "无法确定，因为未提供相关资料，但窗帘能防止光影让人迷失方向。",
    "资料不足，所以无法确定；但该产品保证治好失眠。",
    "无法确定，因为未提供相关资料（不过需关好窗帘以防海面光影让人迷失方向）。",
    "无法确定，因为未提供相关资料，‘关窗帘以防海面光影让人迷失方向’是确定的。",
    "关好窗帘以防海面光影让人迷失方向。其余问题因资料不足无法确定。",
    "无法确定，因为未提供相关资料；资料不足会导致角色失明。",
    "无法确定，因为未提供相关资料，但该杯保证治疗失眠（这一点已证实）。",
    "无法确定，因为未提供相关资料（关窗帘可以保证绝对安全）。",
    "资料不足，所以关窗帘就绝对安全。",
    "无法确定是否关帘能够防止迷失，所以关帘保证绝对安全。",
    "无法确定，因为未提供相关资料，护罩可以防止烫伤。",
    "无法确定具体机制，因为未提供相关资料且“关窗帘保证绝对安全”属于已知信息。",
    "无法确定价格（该杯保证治疗失眠）。",
])
def test_v2_mixed_refusals_do_not_hide_unsupported_world_relations(answer):
    result = check(answer, [], [])
    assert result["status"] == "NEED_REVIEW"
    assert any(c["state"] == "NEED_REVIEW" for c in result["claims"])
    assert result["answer"] == answer and result["automatic_refusal"] is False


def test_v2_unknown_citations_are_not_skipped_by_meta_refusal():
    result = check("无法确定，因为未提供相关资料。", [], ["K99"])
    assert result["status"] == "NEED_REVIEW"
    assert result["citation_issues"] == [{"citation_id": "K99", "reason": "unknown_citation"}]


@pytest.mark.parametrize("answer", [
    "无法确定‘该杯保证治疗失眠’这一说法是否正确。",
    "尚不能确定关窗帘可以防止迷失的效果。",
])
def test_v2_true_unknown_propositions_are_not_stripped_into_positive_claims(answer):
    result = check(answer, [], [])
    assert result["status"] == "NOT_CHECKED"
    assert all(c["state"] == "NOT_ASSERTED" for c in result["claims"])
    assert result["answer"] == answer


def test_v2_ambiguous_citations_remain_visible():
    sources = [{"citation_id": "K1", "text": "一段。"}, {"citation_id": "K1", "text": "另一段。"}]
    result = check("无法确定，因为未提供相关资料。", sources, ["K1"])
    assert result["status"] == "NEED_REVIEW" and result["citation_issues"][0]["reason"] == "ambiguous_citation"


@pytest.mark.parametrize("answer", [
    "无法确定，未提供关于疗效的资料，不能编造保证疗效的广告语。",
    "无法撰写该动作戏，因为资料规定没有私人机动车。",
    "因为小时候落水的经历让她害怕深水。",
])
def test_v2_does_not_expand_to_policy_refusals_or_general_paraphrases(answer):
    assert check(answer, [], [])["status"] == "NEED_REVIEW"


def test_v2_no_answer_level_abstained_switch_or_fact_mutation():
    answer = "无法确定，因为未提供相关资料。护罩保证绝对安全。"
    sources = [{"citation_id": "K1", "text": "建议佩戴护罩。", "abstained": True}]
    result = check(answer, sources, ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert result["answer"] == answer and sources[0]["abstained"] is True
