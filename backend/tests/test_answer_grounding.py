"""Pure tests: no app/config/conftest/embedding or model imports."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "app/services/answer_grounding.py"
spec = importlib.util.spec_from_file_location("local_answer_grounding_tests", MODULE_PATH)
grounding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grounding)
check = grounding.check_answer_grounding

SOURCES = [
    {"citation_id": "K2", "text": "红月夜所有居民必须留在室内，禁止安排角色在街上自由闲逛。"},
    {"citation_id": "K3", "text": "陆遥左眼视力较弱，拍照时习惯用右眼观察取景器。他小时候曾被雷声惊醒。这段受惊经历使他害怕突发的巨大声响，遇到轰鸣、雷鸣等声音时会主动远离声源，躲到安静处。"},
    {"citation_id": "K4", "text": "镜海镇的红月每四十天出现一次。红月升起后，海面会投出让人迷失方向的光影，所以所有居民都留在室内。镇长只通过屋内广播提醒人们关好窗帘。"},
]
CITATIONS = ["K2", "K3", "K4"]
REFERENCE = "陆遥左眼较弱，习惯用右眼取景；他害怕突发巨大声响，会远离声源、寻找安静处。红月夜居民必须留在室内，镇长会通过屋内广播提醒关好窗帘。资料没有说明关窗帘能防止迷失方向。"


def test_h05_c01_original_bad_relation_is_reviewed_without_changing_answer():
    answer = "陆遥左眼视力较弱，拍照习惯用右眼观察；红月夜规则要求所有居民必须留在室内，禁止在街上闲逛，且需关好窗帘以防海面光影让人迷失方向。"
    result = check(answer, SOURCES, CITATIONS)
    assert result["status"] == "NEED_REVIEW"
    assert result["answer"] == answer and result["automatic_refusal"] is False
    assert any("关好窗帘以防" in c["text"] for c in result["claims"])


def test_h05_c02_facts_and_curtain_reminder_are_not_rejected():
    result = check("陆遥左眼视力较弱。红月夜必须留在室内。镇长提醒关好窗帘。", SOURCES, CITATIONS)
    assert result["status"] == "NOT_CHECKED" and result["claims"] == []


def test_h05_c03_directly_supported_cause_is_allowed():
    sentence = "这段受惊经历使他害怕突发的巨大声响，遇到轰鸣、雷鸣等声音时会主动远离声源，躲到安静处"
    result = check(sentence, SOURCES, CITATIONS)
    assert result["status"] == "TEXT_EVIDENCE_FOUND"
    assert result["claims"][0]["state"] == "DIRECT_TEXT_SUPPORT"


def test_h05_c04_absolute_protection_is_reviewed():
    result = check("只要关好窗帘，就绝对安全，不受任何红月影响。", SOURCES, CITATIONS)
    assert result["status"] == "NEED_REVIEW"
    assert result["claims"][0]["kind"] == "absolute_guarantee"
    assert "只要" in result["claims"][0]["text"]


def test_h05_c05_unknown_protection_is_not_an_affirmative_claim():
    result = check("资料无法确定关帘是否能保证不迷失方向。", SOURCES, CITATIONS)
    assert result["status"] == "NOT_CHECKED"
    assert result["claims"][0]["state"] == "NOT_ASSERTED"


def test_h05_c06_reference_preserves_facts_and_uncertainty():
    result = check(REFERENCE, SOURCES, CITATIONS)
    assert result["status"] == "NOT_CHECKED" and result["answer"] == REFERENCE
    assert result["semantic_acceptance"] is False


def test_h05_c07_hypothetical_direct_mechanism_uses_controlled_variant():
    sources = [{"citation_id": "NEW1", "text": "关闭遮光板可以防止强光刺伤眼睛。"}]
    result = check("合上遮光板能够避免强光刺伤眼睛。", sources, ["NEW1"])
    assert result["claims"][0]["state"] == "LIMITED_LEXICAL_SUPPORT"
    assert result["status"] == "TEXT_EVIDENCE_FOUND"


@pytest.mark.parametrize("sources", [
    [{"citation_id": "A", "text": "盖上盒盖。"}, {"citation_id": "B", "text": "强光会损伤胶片。"}],
    [{"citation_id": "A", "text": "盖上盒盖。强光会损伤胶片。"}],
    [{"citation_id": "A", "text": "强光损伤胶片，值守人提醒盖上盒盖。"}],
])
def test_no_stitching_separate_facts_into_protection(sources):
    result = check("盖上盒盖可以防止强光损伤胶片。", sources, [s["citation_id"] for s in sources])
    assert result["status"] == "NEED_REVIEW"


def test_matching_words_but_reversed_relation_need_review():
    result = check("隔热板可以防止火焰伤人。", [{"citation_id": "A", "text": "火焰可以防止隔热板伤人。"}], ["A"])
    assert result["status"] == "NEED_REVIEW"


def test_supported_relation_in_uncited_source_does_not_count():
    result = check("隔热板可以防止火焰伤人。", [{"citation_id": "A", "text": "隔热板可以防止火焰伤人。"}], [])
    assert result["status"] == "NEED_REVIEW"


def test_unknown_and_ambiguous_citation_bindings_need_review():
    assert check("护罩可以防止烫伤。", [], ["Z"])["citation_issues"][0]["reason"] == "unknown_citation"
    sources = [{"citation_id": "A", "text": "护罩可以防止烫伤。"}, {"citation_id": "A", "text": "护罩无法防止烫伤。"}]
    assert check("护罩可以防止烫伤。", sources, ["A"])["citation_issues"][0]["reason"] == "ambiguous_citation"


@pytest.mark.parametrize("text", ["资料未证实护罩可以防止烫伤。", "护罩不能防止烫伤。"])
def test_negated_source_never_supports_positive_protection(text):
    assert check("护罩可以防止烫伤。", [{"citation_id": "A", "text": text}], ["A"])["status"] == "NEED_REVIEW"


def test_conditional_source_does_not_support_unqualified_guarantee():
    result = check("戴护目镜就绝对安全。", [{"citation_id": "A", "text": "只要正确密封护目镜且没有裂纹，就绝对安全。"}], ["A"])
    assert result["status"] == "NEED_REVIEW"


def test_unresolved_paraphrase_is_review_not_fabrication():
    result = check("屏障能够保护眼部免受光线损害。", [{"citation_id": "A", "text": "正确关闭遮光板可以防止强光刺伤眼睛。"}], ["A"])
    assert result["status"] == "NEED_REVIEW"
    assert "fabrication" not in result and result["semantic_acceptance"] is False


def test_disclaimer_does_not_hide_following_affirmative_clause():
    result = check("资料无法确认，不过盖上盒盖就可以防止中毒。", [], [])
    assert result["status"] == "NEED_REVIEW"


@pytest.mark.parametrize("answer", [
    "资料没有说明该机制，盖上盒盖可以防止中毒。",
    "资料无法确定，只要盖上盒盖，就绝对安全。",
    "资料无法确定，但盖上盒盖可以防止中毒。",
])
def test_uncertainty_prefix_does_not_hide_an_added_relation(answer):
    assert check(answer, [], [])["status"] == "NEED_REVIEW"


@pytest.mark.parametrize("answer", [
    "关窗帘可以保证绝对安全，这不是说别的事。",
    "关窗帘可防止迷失方向，无法确定的是价格。",
])
def test_later_disclaimer_does_not_cancel_preceding_protection(answer):
    sources = [{"citation_id": "A", "text": "镇长提醒关窗帘。"}]
    assert check(answer, sources, ["A"])["status"] == "NEED_REVIEW"


def test_intended_precaution_is_not_equivalent_to_proven_effect():
    result = check("盖上盒盖可以防止中毒。", [{"citation_id": "A", "text": "盖上盒盖以防中毒。"}], ["A"])
    assert result["status"] == "NEED_REVIEW"


def test_required_precaution_is_not_automatically_actual_effect():
    result = check("护罩防止烫伤。", [{"citation_id": "A", "text": "需护罩防止烫伤。"}], ["A"])
    assert result["status"] == "NEED_REVIEW"


def test_explicit_guarantee_can_have_literal_source_evidence_not_truth_certification():
    text = "这个护罩保证绝对安全"
    result = check(text, [{"citation_id": "A", "text": text}], ["A"])
    assert result["status"] == "TEXT_EVIDENCE_FOUND" and not result["semantic_acceptance"]


def test_no_keywords_is_not_checked_not_supported():
    result = check("他今天穿蓝色外套。", [], [])
    assert result["status"] == "NOT_CHECKED" and result["claims"] == []


def test_empty_answer_is_not_checked_and_inputs_are_unchanged():
    sources = [{"citation_id": "A", "text": "关灯。"}]
    citations = ["A", "A"]
    result = check("", sources, citations)
    assert result["status"] == "NOT_CHECKED" and result["answer"] == ""
    assert sources == [{"citation_id": "A", "text": "关灯。"}] and citations == ["A", "A"]


@pytest.mark.parametrize("answer,sources,citations", [(None, [], []), ("x", "wrong", []), ("x", [], "A"), ("x", [None], [])])
def test_invalid_inputs_fail_explicitly_without_generation(answer, sources, citations):
    with pytest.raises(TypeError):
        check(answer, sources, citations)
