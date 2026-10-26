"""Frozen v6 business assertions rerouted to v8 plus finite-scope controls.

Engineering expectations are not additional human labels or holdout evidence.
"""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


v8 = module("v8_pure_test", ROOT / "backend/app/services/answer_grounding_v8.py")
compat = module("v8_frozen_business_tests", ROOT / "backend/tests/test_answer_grounding_v6.py")
current = compat
while True:
    if hasattr(current, "check"):
        current.check = v8.check_answer_grounding
    # Tests refer to older helper namespaces for metadata/version assertions.
    for name in ("v6", "v5", "v4", "v3", "v2", "grounding"):
        if hasattr(current, name):
            setattr(current, name, v8)
    if hasattr(current, "compat"):
        current = current.compat
    elif hasattr(current, "legacy"):
        current = current.legacy
    else:
        break
for name, function in list(vars(compat).items()):
    if name.startswith("test_") and callable(function):
        globals()["test_v8_inherited_" + name[5:]] = function


CASES = [
    ("本片不允许出现无人机画面。", "白汀城不存在无人机。", "film_depiction_to_world_nonexistence"),
    ("周三上午，东廊不向普通访客开放；持当日巡检证的维修人员可以进入。其他时段按正常预约规则开放。",
     "周三上午，任何人都不能进入东廊。", "visitor_restriction_with_exception_to_everyone"),
    ("常见制作材料包括竹片、棉线和陶珠。", "麦环手作坊只允许使用竹片、棉线或陶珠。",
     "common_material_list_to_exclusive_permission"),
]


def source(text, **extra):
    return {"citation_id": "S1", "text": text, "document_id": "control", "version_id": "v1", "version_number": 1, **extra}


@pytest.mark.parametrize("text,answer,rule", CASES)
def test_finite_scope_extension_is_review_only_with_complete_bound_evidence(text, answer, rule):
    sources = [source(text)]
    old = copy.deepcopy((sources, answer))
    out = v8.check_answer_grounding(answer, sources, ["S1"])
    assert out["status"] == "NEED_REVIEW"
    assert (sources, answer) == old and out["answer"] == answer
    assert not out["semantic_acceptance"] and not out["automatic_refusal"]
    row, = out["scope_extension_checks"]
    assert row["rule_id"] == rule and row["resolved"] is False and row["fabrication_judgment"] is None
    assert row["evidence"][0]["text"] == text and row["evidence"][0]["version_id"] == "v1"


@pytest.mark.parametrize("prefix,suffix", [("有人说：“", "”"), ("无法确定是否", ""),
                                          ("", "这条规则不代表其他规定。"), ("如果晴天，", "")])
@pytest.mark.parametrize("text,answer,rule", CASES)
def test_quoted_qualified_conditional_or_extended_sources_are_not_clipped(prefix, suffix, text, answer, rule):
    out = v8.check_answer_grounding(answer, [source(prefix + text + suffix)], ["S1"])
    assert out["scope_extension_checks"] == []
    assert not any(c["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for c in out["claims"])


@pytest.mark.parametrize("text,answer,rule", CASES)
def test_uncited_and_conflicting_versions_are_not_selected_as_scope_evidence(text, answer, rule):
    for sources, citations in [([source(text)], []), ([source(text)], ["OTHER"]),
                               ([source(text), source(text, version_id="v2")], ["S1"])]:
        out = v8.check_answer_grounding(answer, sources, citations)
        assert out["scope_extension_checks"] == []
        assert not any(c["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for c in out["claims"])


@pytest.mark.parametrize("text,answer,rule", CASES)
def test_question_and_negation_are_not_new_affirmative_scope_claims(text, answer, rule):
    for qualified in ["无法确定是否" + answer, "并非" + answer, "“" + answer + "”",
                      "不能断言" + answer, "无法推断" + answer, "不是" + answer,
                      "没有证据" + answer, "我猜" + answer, "可能" + answer]:
        assert v8.check_answer_grounding(qualified, [source(text)], ["S1"])["scope_extension_checks"] == []


@pytest.mark.parametrize("text,answer,rule", CASES)
def test_additional_complete_literal_cited_restriction_is_not_ignored(text, answer, rule):
    sources = [source(text), source(answer, citation_id="S2", document_id="explicit")]
    assert v8.check_answer_grounding(answer, sources, ["S1", "S2"])["scope_extension_checks"] == []


@pytest.mark.parametrize("text,answer", [
    (CASES[0][0], "本片不允许出现无人机画面。"),
    (CASES[0][0], "本片不存在无人机。"),
    (CASES[0][0], "这个镜头不存在无人机。"),
    (CASES[1][0], "周四上午，任何人都不能进入东廊。"),
    (CASES[1][0], "周三上午，任何人都不能进入西廊。"),
    (CASES[2][0], "麦环手作坊只允许使用木片、棉线或陶珠。"),
])
def test_scope_templates_require_same_literal_objects_time_place_and_items(text, answer):
    assert v8.check_answer_grounding(answer, [source(text)], ["S1"])["scope_extension_checks"] == []


def test_partial_activity_clause_cannot_hide_new_injury_claim():
    out = v8.check_answer_grounding("访客只能在展示区活动，不能进入设备间；待在展示区就绝对不会受伤。",
                                    [source("访客活动范围仅限展示区，设备间不对访客开放。")], ["S1"])
    assert out["status"] == "NEED_REVIEW"
    assert any(c["state"] == "NEED_REVIEW" and "不会受伤" in c["text"] for c in out["claims"])


def test_h05_protective_cause_remains_reviewable():
    out = v8.check_answer_grounding("关好窗帘以防海面光影让人迷失方向。",
                                    [source("室内规则：夜间应关好窗帘。海面会出现光影。")], ["S1"])
    assert out["status"] == "NEED_REVIEW"
    assert any(c["state"] == "NEED_REVIEW" and "以防" in c["text"] for c in out["claims"])


CONTROLS = json.loads((ROOT / "docs/evidence/local-free-acceptance-20261025/rag-calibration-v8/calibration-controls.json").read_text())


@pytest.mark.parametrize("case", CONTROLS["cases"], ids=lambda c: c["case_id"])
def test_reviewed_material_engineering_contract_is_separate_from_human_acceptability(case):
    # Only answer/sources/citations enter the checker, never human judgments.
    out = v8.check_answer_grounding(case["answer"], case["sources"], case["citations"])
    assert out["status"] == case["machine_expectation"]["status"]
    assert not out["semantic_acceptance"]
    assert bool(out["scope_extension_checks"]) == (case["case_id"] in {"J08", "J09", "J11"})
    if case["case_id"] == "J12":
        assert any(c["state"] == "NEED_REVIEW" and "不会受伤" in c["text"] for c in out["claims"])
