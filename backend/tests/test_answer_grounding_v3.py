"""Frozen v2's 65 instances plus independent-conjunct scope regressions.

Only the root's isolated runner executes this file. Legacy function assertions
and parametrization marks are reused in memory, without disk modifications.
"""
import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pure_answer_grounding_v3_tests", BASE / "app/services/answer_grounding_v3.py")
v3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v3)
check = v3.check_answer_grounding

compat_spec = importlib.util.spec_from_file_location("frozen_v2_assertions_for_v3", BASE / "tests/test_answer_grounding_v2.py")
compat = importlib.util.module_from_spec(compat_spec)
compat_spec.loader.exec_module(compat)
compat.check = check
compat.v2 = v3
compat.legacy.check = check
compat.legacy.grounding = v3
for _name, _function in list(vars(compat).items()):
    if _name.startswith("test_") and callable(_function):
        globals()["test_v2_compat_" + _name[5:]] = _function


@pytest.mark.parametrize("answer", [
    "无法确定具体机制而且我保证关好窗帘绝对安全。",
    "尚不能确定来源并且我们保证护罩绝对安全。",
    "无法确定价格而且这款杯子保证治疗失眠。",
    "无法确定情况并且护罩能够防止烫伤。",
    "无法确定具体机制且窗帘可以防止迷失方向。",
    "资料没有说明机制而且我保证关帘绝对安全。",
    "无法确定机制而且我保证‘关帘绝对安全’是事实。",
    "无法确定机制（而且我保证护罩绝对安全）。",
    "无法确定价格并且我可以保证该杯治好失眠。",
    "资料不足无法判断且我保证戴护罩百分之百安全。",
    "无法确定因为未提供资料而且我保证关窗帘以防光影迷失。",
])
def test_v3_explicit_independent_affirmations_are_reviewed(answer):
    result = check(answer, [], [])
    assert result["status"] == "NEED_REVIEW"
    assert any(c["state"] == "NEED_REVIEW" for c in result["claims"])
    assert result["answer"] == answer and not result["automatic_refusal"]


@pytest.mark.parametrize("answer", [
    "无法确定机制而且我保证护罩绝对安全并且我们保证窗帘防止迷失。",
    "无法确定机制而且护罩能防止烫伤并且窗帘能防止迷失方向。",
])
def test_v3_multiple_independent_affirmations_are_not_collapsed_into_one_unknown(answer):
    result = check(answer, [], [])
    reviewed = [c["text"] for c in result["claims"] if c["state"] == "NEED_REVIEW"]
    assert result["status"] == "NEED_REVIEW"
    assert any("护罩" in text for text in reviewed)
    assert any("窗帘" in text for text in reviewed)
    assert len(set(reviewed)) >= 2


def test_v3_later_negative_guarantee_does_not_replace_the_affirmative_risk():
    answer = "无法确定机制而且我保证护罩绝对安全但我不保证价格信息。"
    result = check(answer, [], [])
    assert result["status"] == "NEED_REVIEW"
    assert any(c["state"] == "NEED_REVIEW" and "护罩绝对安全" in c["text"] for c in result["claims"])


@pytest.mark.parametrize("answer", [
    "无法确定机制而且我不保证护罩绝对安全。",
    "无法确定机制而且我不能保证绝对安全。",
    "无法确定机制并且我无法保证能防止烫伤。",
    "无法确定机制而且我从未保证绝对安全。",
    "无法确定机制而且我保证不了绝对安全。",
    "无法确定机制而且不是说我保证绝对安全。",
    "无法确定机制而且我不可能保证绝对安全。",
    "无法确定机制并且我没有办法保证护罩绝对安全。",
    "无法确定机制而且我不能保证只要护罩已就位并且正确密封就绝对安全。",
])
def test_v3_negative_guarantees_are_not_positive_independent_promises(answer):
    result = check(answer, [], [])
    assert result["status"] == "NOT_CHECKED"
    assert not any(c["state"] == "NEED_REVIEW" for c in result["claims"])


@pytest.mark.parametrize("answer", [
    "无法确定护罩是否防止烫伤且保护眼睛。",
    "无法确定护罩是否能够防止烫伤而且保护眼睛。",
    "无法确定该杯保证治疗失眠这一说法是否正确。",
    "无法确定‘我保证关帘绝对安全’而且‘护罩防烫伤’这两句话是否真实。",
    "尚不能确定关帘防迷失和护罩防烫伤的效果。",
    "无法确定（护罩是否防止烫伤且保护眼睛）。",
    "无法确定护罩是否防止烫伤而且护目镜是否保护眼睛。",
    "无法确定该说法是否属实而且我也不清楚护罩是否防止烫伤。",
    "无法确定价格而且我是否保证护罩安全仍无从确认。",
    "无法确定机制而且‘我保证护罩绝对安全’是否只是台词。",
    "无法确定护罩能否防止烫伤并且保护眼睛。",
    "无法确定‘机制不明而且我保证绝对安全’这句话是否属实。",
    "无法确定护罩是否防止烫伤而且也能保护眼睛。",
    "无法确定护罩能否防止烫伤并且还能保护眼睛。",
    "无法确定机制而且我不清楚只要护罩已就绪并且正确密封就能防止烫伤是否正确。",
])
def test_v3_unknown_quote_question_and_object_scopes_are_preserved(answer):
    result = check(answer, [], [])
    assert result["status"] == "NOT_CHECKED"
    assert not any(c["state"] == "NEED_REVIEW" for c in result["claims"])
    assert result["answer"] == answer


def test_v3_directly_sourced_independent_effect_retains_its_citation_evidence():
    answer = "无法确定别的问题而且护罩可以防止烫伤。"
    sources = [{"citation_id": "K1", "text": "护罩可以防止烫伤。"}]
    result = check(answer, sources, ["K1"])
    assert result["status"] == "TEXT_EVIDENCE_FOUND"
    assert any(c["state"] == "DIRECT_TEXT_SUPPORT" and c["evidence"][0]["citation_id"] == "K1" for c in result["claims"])
    assert not result["semantic_acceptance"]


def test_v3_and_inside_a_conditional_antecedent_is_preserved_with_direct_source():
    relation = "我保证只要护罩没有裂纹并且正确密封就绝对安全"
    answer = "无法确定别的问题而且" + relation + "。"
    result = check(answer, [{"citation_id": "K1", "text": relation + "。"}], ["K1"])
    assert result["status"] == "TEXT_EVIDENCE_FOUND"
    assert any(c["state"] == "DIRECT_TEXT_SUPPORT" and c["text"] == relation
               and "没有裂纹并且正确密封" in c["text"] for c in result["claims"])


@pytest.mark.parametrize("source", [
    "正确密封就绝对安全。",
    "我保证只要护罩已有裂纹并且正确密封就绝对安全。",
])
def test_v3_shortened_or_changed_antecedent_cannot_support_full_promise(source):
    answer = "无法确定别的问题而且我保证只要护罩没有裂纹并且正确密封就绝对安全。"
    result = check(answer, [{"citation_id": "K1", "text": source}], ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert not any(c["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for c in result["claims"])


@pytest.mark.parametrize("setup", ["已就位", "已就绪"])
def test_v3_and_inside_a_conditional_setup_word_does_not_close_antecedent(setup):
    relation = f"我保证只要护罩{setup}并且正确密封就绝对安全"
    answer = "无法确定别的问题而且" + relation + "。"
    result = check(answer, [{"citation_id": "K1", "text": relation + "。"}], ["K1"])
    assert result["status"] == "TEXT_EVIDENCE_FOUND"
    assert any(c["state"] == "DIRECT_TEXT_SUPPORT" and c["text"] == relation for c in result["claims"])
    assert not any(c["state"] == "NEED_REVIEW" for c in result["claims"])


@pytest.mark.parametrize("setup", ["已就位", "已就绪"])
def test_v3_shortened_or_changed_antecedent_setup_prefix_is_not_support(setup):
    prefix = f"我保证只要护罩{setup}"
    answer = "无法确定别的问题而且" + prefix + "并且正确密封就绝对安全。"
    result = check(answer, [{"citation_id": "K1", "text": prefix + "。"}], ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert not any(c["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for c in result["claims"])


def test_v3_unknown_citation_is_not_hidden_by_genuine_uncertainty():
    result = check("无法确定护罩是否防止烫伤且保护眼睛。", [], ["K99"])
    assert result["status"] == "NEED_REVIEW"
    assert result["citation_issues"] == [{"citation_id": "K99", "reason": "unknown_citation"}]


def test_v3_ambiguous_citation_remains_visible_with_independent_affirmation():
    sources = [{"citation_id": "K1", "text": "护罩可以防止烫伤。"}, {"citation_id": "K1", "text": "护罩不能防止烫伤。"}]
    result = check("无法确定机制而且护罩可以防止烫伤。", sources, ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert result["citation_issues"][0]["reason"] == "ambiguous_citation"
