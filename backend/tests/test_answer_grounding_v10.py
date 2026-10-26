"""Finite batch controls, separate from original human judgments and holdout evidence."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


v10 = module("pure_v10_tests", ROOT / "backend/app/services/answer_grounding_v10.py")
v9 = module("frozen_v9_batch_tests", ROOT / "backend/app/services/answer_grounding_v9.py")
compat = module("v10_frozen_business", ROOT / "backend/tests/test_answer_grounding_v6.py")
current = compat
while True:
    if hasattr(current, "check"):
        current.check = v10.check_answer_grounding
    for name in ("v6", "v5", "v4", "v3", "v2", "grounding"):
        if hasattr(current, name):
            setattr(current, name, v10)
    if hasattr(current, "compat"):
        current = current.compat
    elif hasattr(current, "legacy"):
        current = current.legacy
    else:
        break
for name, function in list(vars(compat).items()):
    if name.startswith("test_") and callable(function):
        globals()["test_v10_inherited_" + name[5:]] = function

OUT = ROOT / "docs/evidence/local-free-acceptance-20261025/rag-batch-reference-calibration-v10-20261026"
CASES = json.loads((OUT / "controls.json").read_text())["cases"]
POSITIVE = [c for c in CASES if c["calibration_role"] == "positive"]
NEGATIVE = [c for c in CASES if c["calibration_role"] == "negative"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["case_id"])
def test_reviewed_material_has_only_the_frozen_engineering_outcome(case):
    args = (case["answer"], copy.deepcopy(case["sources"]), copy.deepcopy(case["citations"]))
    before = copy.deepcopy(args)
    out = v10.check_answer_grounding(*args)
    assert args == before and out["answer"] == case["answer"]
    assert out["status"] == case["machine_expectation"]["status"]
    assert out["semantic_acceptance"] is out["automatic_refusal"] is out["production_integrated"] is False
    rows = out["batch_reference_checks"]
    assert len(rows) == 1
    assert bool([r for r in rows if r["resolved"]]) == (case["calibration_role"] == "positive")
    for row in rows:
        assert row["fabrication_judgment"] is None and row["semantic_acceptance"] is False
        assert row["evidence"][0]["text"] == case["sources"][0]["text"]


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
@pytest.mark.parametrize("tail", ["这条规则只在检修时适用。", "复核撤回该原因判断。",
                                  "另有其他条件。", "随后按原计划入库。", "尚未核实具体原因。"])
def test_unknown_retraction_limitation_or_changed_final_state_is_not_clipped(case, tail):
    sources = copy.deepcopy(case["sources"])
    sources[0]["text"] += tail
    out = v10.check_answer_grounding(case["answer"], sources, case["citations"])
    assert out["status"] == "NEED_REVIEW"
    assert not any(r["resolved"] for r in out["batch_reference_checks"])


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
@pytest.mark.parametrize("prefix,suffix", [("有人称：“", "”"), ("如果天气晴朗，", ""),
                                           ("无法确定是否", ""), ("并非", "")])
def test_quoted_uncertain_conditional_or_negated_whole_source_is_not_new_support(case, prefix, suffix):
    sources = copy.deepcopy(case["sources"])
    sources[0]["text"] = prefix + sources[0]["text"] + suffix
    out = v10.check_answer_grounding(case["answer"], sources, case["citations"])
    assert out["status"] == "NEED_REVIEW"
    assert not any(r["resolved"] for r in out["batch_reference_checks"])


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
@pytest.mark.parametrize("change", ["no_citation", "wrong_citation", "different_versions", "missing_document",
                                     "missing_version", "missing_number", "boolean_number", "multiple_cited"])
def test_unique_explicit_document_version_and_single_cited_scope_are_required(case, change):
    sources, citations = copy.deepcopy(case["sources"]), copy.deepcopy(case["citations"])
    if change == "no_citation":
        citations = []
    elif change == "wrong_citation":
        citations = ["UNKNOWN"]
    elif change == "different_versions":
        sources.append(dict(sources[0], version_id="different"))
    elif change.startswith("missing_"):
        sources[0].pop({"missing_document": "document_id", "missing_version": "version_id",
                        "missing_number": "version_number"}[change])
    elif change == "boolean_number":
        sources[0]["version_number"] = True
    else:
        sources.append(dict(sources[0], citation_id="S2", text="复核撤回此前的原因判断。"))
        citations = ["S1", "S2"]
    out = v10.check_answer_grounding(case["answer"], sources, citations)
    assert out["status"] == "NEED_REVIEW" and not any(r["resolved"] for r in out["batch_reference_checks"])


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
@pytest.mark.parametrize("prefix", ["可能", "不能断言", "无法确定是否", "假设", "并非"])
def test_answer_qualification_is_not_a_new_affirmative_batch_claim(case, prefix):
    out = v10.check_answer_grounding(prefix + case["answer"], case["sources"], case["citations"])
    assert out["batch_reference_checks"] == []


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
@pytest.mark.parametrize("change", ["duration", "event", "batch", "cause", "order"])
def test_answer_arguments_and_causal_order_are_literal(case, change):
    answer = case["answer"]
    if change == "duration":
        answer = answer.replace("两天", "四天").replace("一天", "四天")
    elif change == "event":
        answer = answer.replace("入库", "上架")
    elif change == "batch":
        for batch in ("霜叶批", "霁蓝批", "岚白批", "苇白批"):
            answer = answer.replace(batch, "另名批")
    elif change == "cause":
        answer = "印刷出错导致" + answer.split("导致")[1]
    else:
        cause, effect = answer.rstrip("。").split("导致")
        answer = effect + "导致" + cause + "。"
    out = v10.check_answer_grounding(answer, case["sources"], case["citations"])
    assert out["status"] == "NEED_REVIEW" and not any(r["resolved"] for r in out["batch_reference_checks"])


def test_plural_requires_two_distinct_named_members_and_all_distribution_markers():
    case = next(c for c in CASES if c["case_id"] == "BREF08")
    text = case["sources"][0]["text"]
    for changed in [text.replace("均受潮", "受潮"), text.replace("都需要", "需要"),
                    text.replace("时间均延后", "时间延后"), text.replace("竹青批", "苇白批"),
                    text.replace("这两批", "其中一批")]:
        sources = [dict(case["sources"][0], text=changed)]
        out = v10.check_answer_grounding(case["answer"], sources, ["S1"])
        assert out["status"] == "NEED_REVIEW" and not any(r["resolved"] for r in out["batch_reference_checks"])


def test_bridge_and_tail_cannot_change_the_batch_or_event():
    for cid in ("BREF03", "BREF05"):
        case = next(c for c in CASES if c["case_id"] == cid)
        text = case["sources"][0]["text"]
        changed = (text.replace("绛红批", "霁蓝批") if cid == "BREF03"
                   else text.replace("蓝砂批", "岚白批"))
        out = v10.check_answer_grounding(case["answer"], [dict(case["sources"][0], text=changed)], ["S1"])
        assert out["status"] == "NEED_REVIEW" and not any(r["resolved"] for r in out["batch_reference_checks"])


def test_source_is_not_joined_across_documents():
    case = POSITIVE[0]
    parts = case["sources"][0]["text"].split("。")
    sources = [dict(case["sources"][0], text=parts[0] + "。"),
               dict(case["sources"][0], citation_id="S2", text=parts[1] + "。")]
    out = v10.check_answer_grounding(case["answer"], sources, ["S1", "S2"])
    assert out["status"] == "NEED_REVIEW" and not any(r["resolved"] for r in out["batch_reference_checks"])


def test_renaming_unseen_entities_keeps_literal_grammar_without_case_lookup():
    case = POSITIVE[0]
    sources = copy.deepcopy(case["sources"])
    sources[0]["text"] = sources[0]["text"].replace("霜叶批", "新柳批")
    sources[0].update(document_id="unseen-control", version_id="other")
    answer = case["answer"].replace("霜叶批", "新柳批")
    assert v10.check_answer_grounding(answer, sources, ["S1"])["status"] == "TEXT_EVIDENCE_FOUND"


def test_question_punctuation_does_not_gain_new_affirmative_support():
    case = POSITIVE[0]
    for ending in ("？", "?", "！"):
        out = v10.check_answer_grounding(case["answer"].rstrip("。") + ending, case["sources"], ["S1"])
        assert out["batch_reference_checks"] == []


def test_new_batch_support_does_not_hide_an_independent_guarantee():
    case = POSITIVE[0]
    out = v10.check_answer_grounding(case["answer"] + "而且我保证绝对不会受伤。", case["sources"], ["S1"])
    assert out["status"] == "NEED_REVIEW"
    assert any(c["state"] == "NEED_REVIEW" and "保证" in c["text"] for c in out["claims"])


def test_h05_keeps_the_entire_frozen_risk_output():
    answer = "关好窗帘以防海面光影让人迷失方向。"
    sources = [dict(citation_id="S1", document_id="h05-control", version_id="v1", version_number=1,
                    text="室内规则：夜间应关好窗帘。海面会出现光影。")]
    old = v9.check_answer_grounding(answer, sources, ["S1"])
    new = v10.check_answer_grounding(answer, sources, ["S1"])
    assert old["status"] == new["status"] == "NEED_REVIEW"
    assert {k: v for k, v in old.items() if k != "version"} == {
        k: v for k, v in new.items() if k not in {"version", "batch_reference_checks"}}



def test_date_prefix_is_retained_and_never_dropped_to_gain_support():
    case = POSITIVE[0]
    cause, result = case["answer"].split("导致")
    answer = cause + "导致周一到货的" + result
    out = v10.check_answer_grounding(answer, case["sources"], ["S1"])
    assert out["status"] == "NEED_REVIEW"
    row, = out["batch_reference_checks"]
    assert not row["resolved"] and "周一到货的" in row["text"]
