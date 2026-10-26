"""Engineering boundary controls; none of these controls are new human labels."""
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


v9 = module("test_pure_v9", ROOT / "backend/app/services/answer_grounding_v9.py")
v8 = module("test_frozen_v8", ROOT / "backend/app/services/answer_grounding_v8.py")
compat = module("test_v9_frozen_business", ROOT / "backend/tests/test_answer_grounding_v6.py")
current = compat
while True:
    if hasattr(current, "check"):
        current.check = v9.check_answer_grounding
    for name in ("v6", "v5", "v4", "v3", "v2", "grounding"):
        if hasattr(current, name):
            setattr(current, name, v9)
    if hasattr(current, "compat"):
        current = current.compat
    elif hasattr(current, "legacy"):
        current = current.legacy
    else:
        break
for name, function in list(vars(compat).items()):
    if name.startswith("test_") and callable(function):
        globals()["test_v9_inherited_" + name[5:]] = function

EVIDENCE = ROOT / "docs/evidence/local-free-acceptance-20261025"
BND = json.loads((EVIDENCE / "rag-boundary-calibration-v9-20261026/controls.json").read_text())["cases"]
J = json.loads((EVIDENCE / "rag-new-synthetic-review-20261025/samples.json").read_text())["cases"]
POSITIVE = [c for c in BND if c["case_id"] in ("BND01", "BND05")] + [
    c for c in J if c["case_id"] in ("J01", "J04")]


@pytest.mark.parametrize("case", BND, ids=lambda c: c["case_id"])
def test_frozen_boundary_expectations_separate_human_fields_from_machine_state(case):
    args = (case["answer"], copy.deepcopy(case["sources"]), copy.deepcopy(case["citations"]))
    before = copy.deepcopy(args)
    out = v9.check_answer_grounding(*args)
    assert args == before and out["answer"] == case["answer"]
    assert out["status"] == case["machine_expectation"]["status"]
    assert out["semantic_acceptance"] is out["automatic_refusal"] is out["production_integrated"] is False
    if case["calibration_role"] == "negative" or case["case_id"] == "BND06":
        assert not out["multisentence_checks"]
    if case["case_id"] == "BND02":
        assert len(out["boundary_scope_checks"]) == 1
    for row in out["multisentence_checks"] + out["boundary_scope_checks"]:
        assert row["semantic_acceptance"] is False and row["fabrication_judgment"] is None


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
def test_whole_source_and_literal_arguments_supply_only_bounded_evidence(case):
    out = v9.check_answer_grounding(case["answer"], case["sources"], case["citations"])
    assert out["status"] == "TEXT_EVIDENCE_FOUND"
    assert len(out["multisentence_checks"]) == 1
    row = out["multisentence_checks"][0]
    evidence, = row["evidence"]
    assert evidence["text"] == case["sources"][0]["text"]
    assert evidence["version_id"] == case["sources"][0]["version_id"]
    assert row["resolved"] and row["state"] == "LIMITED_LEXICAL_SUPPORT"


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
@pytest.mark.parametrize("tail", ["这条规则只在检修时生效。", "复核撤回这项原因判断。",
                                  "尚未核实原因。", "也有人持不同意见。", "另有其他限制。"])
def test_unknown_limiting_retracted_or_unverified_tail_is_never_clipped(case, tail):
    sources = copy.deepcopy(case["sources"])
    sources[0]["text"] += tail
    assert not v9.check_answer_grounding(case["answer"], sources, case["citations"])["multisentence_checks"]


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
@pytest.mark.parametrize("prefix,suffix", [("访客称：“", "”"), ("无法确定是否", ""),
                                           ("如果晴天，", ""), ("并非", "")])
def test_quote_uncertainty_condition_and_negation_do_not_gain_support(case, prefix, suffix):
    sources = copy.deepcopy(case["sources"])
    sources[0]["text"] = prefix + sources[0]["text"] + suffix
    assert not v9.check_answer_grounding(case["answer"], sources, case["citations"])["multisentence_checks"]


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
@pytest.mark.parametrize("change", ["no_citation", "wrong_citation", "two_versions", "two_texts", "different_number"])
def test_citation_version_and_literal_number_bindings_are_required(case, change):
    sources, citations = copy.deepcopy(case["sources"]), copy.deepcopy(case["citations"])
    if change == "no_citation":
        citations = []
    elif change == "wrong_citation":
        citations = ["UNKNOWN"]
    elif change == "two_versions":
        sources.append(dict(sources[0], version_id="other"))
    elif change == "two_texts":
        sources.append(dict(sources[0], text=sources[0]["text"] + "其他记录。"))
    else:
        sources[0]["text"] = sources[0]["text"].replace("32", "33").replace("80", "81").replace("持续", "间歇")
        if sources[0]["text"] == case["sources"][0]["text"]:
            sources[0]["text"] = "另一" + sources[0]["text"]
    assert not v9.check_answer_grounding(case["answer"], sources, citations)["multisentence_checks"]


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
@pytest.mark.parametrize("prefix", ["不能断言", "可能", "不是", "无法确定是否"])
def test_answer_qualification_is_not_promoted_to_asserted_support(case, prefix):
    assert not v9.check_answer_grounding(prefix + case["answer"], case["sources"], case["citations"])["multisentence_checks"]


def source(text, **extra):
    return dict(citation_id="S1", text=text, document_id="control", version_id="v1", version_number=1) | extra


@pytest.mark.parametrize("text", [
    "淤泥堆积堵住进水口。排水口堵塞使抽水泵暂停运行。",
    "淤泥堆积堵住进水口。进水口堵塞与抽水泵暂停运行同时发生。",
    "淤泥堆积堵住进水口。只有备用泵失效时，进水口堵塞才使抽水泵暂停运行。",
    "淤泥堆积堵住进水口。进水口堵塞使抽水泵暂停运行。随后恢复运行。",
])
def test_causal_chain_requires_same_intermediate_and_complete_unqualified_body(text):
    out = v9.check_answer_grounding("淤泥堆积导致抽水泵暂停运行。", [source(text)], ["S1"])
    assert out["status"] == "NEED_REVIEW" and not out["multisentence_checks"]


def test_chain_never_joins_two_documents_or_reverses_direction():
    sources = [source("淤泥堆积堵住进水口。"), source("进水口堵塞使抽水泵暂停运行。", citation_id="S2")]
    out = v9.check_answer_grounding("淤泥堆积导致抽水泵暂停运行。", sources, ["S1", "S2"])
    assert out["status"] == "NEED_REVIEW" and not out["multisentence_checks"]
    text = "淤泥堆积堵住进水口。进水口堵塞使抽水泵暂停运行。"
    assert not v9.check_answer_grounding("抽水泵暂停运行导致淤泥堆积。", [source(text)], ["S1"])["multisentence_checks"]


def test_unreviewed_name_renaming_uses_grammar_not_case_identity():
    case = next(c for c in BND if c["case_id"] == "BND01")
    answer = case["answer"].replace("育苗室", "培育舱").replace("循环风扇", "通风机")
    sources = copy.deepcopy(case["sources"])
    sources[0]["text"] = sources[0]["text"].replace("育苗室", "培育舱").replace("循环风扇", "通风机")
    sources[0].update(document_id="unseen-engineering-control", version_id="other")
    assert v9.check_answer_grounding(answer, sources, ["S1"])["status"] == "TEXT_EVIDENCE_FOUND"


def test_matched_relation_does_not_hide_independent_guarantee_or_h05():
    case = POSITIVE[0]
    out = v9.check_answer_grounding(case["answer"] + "而且我保证人物绝对安全。", case["sources"], case["citations"])
    assert out["status"] == "NEED_REVIEW"
    answer = "关好窗帘以防海面光影让人迷失方向。"
    sources = [source("室内规则：夜间应关好窗帘。海面会出现光影。")]
    old = v8.check_answer_grounding(answer, sources, ["S1"])
    new = v9.check_answer_grounding(answer, sources, ["S1"])
    assert old["status"] == new["status"] == "NEED_REVIEW"
    assert old["claims"] == new["claims"] and not new["multisentence_checks"]


@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
def test_multiple_cited_bodies_cannot_silently_supply_or_override_a_cause(case):
    sources = copy.deepcopy(case["sources"])
    sources.append(dict(sources[0], citation_id="S2", document_id="correction", text="复核撤回此前的原因判断。"))
    out = v9.check_answer_grounding(case["answer"], sources, ["S1", "S2"])
    assert not out["multisentence_checks"] and out["status"] == "NEED_REVIEW"


@pytest.mark.parametrize("field", ["document_id", "version_id", "version_number"])
@pytest.mark.parametrize("case", POSITIVE, ids=lambda c: c["case_id"])
def test_missing_source_version_metadata_remains_unresolved(case, field):
    sources = copy.deepcopy(case["sources"])
    sources[0].pop(field)
    out = v9.check_answer_grounding(case["answer"], sources, ["S1"])
    assert not out["multisentence_checks"] and out["status"] == "NEED_REVIEW"


def test_boolean_version_number_is_not_an_integer_version_binding():
    case = POSITIVE[0]
    sources = copy.deepcopy(case["sources"])
    sources[0]["version_number"] = True
    assert not v9.check_answer_grounding(case["answer"], sources, ["S1"])["multisentence_checks"]
