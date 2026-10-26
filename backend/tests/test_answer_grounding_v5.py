"""159 unchanged v4 business assertions plus scoped v5 text-evidence controls.

Only the root's isolated runner executes this file. A saved historical answer
is never a case whitelist, a new generated answer, or a human score change.
"""
import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pure_answer_grounding_v5_tests", BASE / "app/services/answer_grounding_v5.py")
v5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v5)
check = v5.check_answer_grounding

compat_spec = importlib.util.spec_from_file_location("frozen_v4_assertions_for_v5", BASE / "tests/test_answer_grounding_v4.py")
compat = importlib.util.module_from_spec(compat_spec)
compat_spec.loader.exec_module(compat)
compat.check = check
compat.v4 = v5
compat.compat.check = check
compat.compat.v3 = v5
compat.compat.compat.check = check
compat.compat.compat.v2 = v5
compat.compat.compat.legacy.check = check
compat.compat.compat.legacy.grounding = v5

# The v4 test's explicitly declared v3 -> v4 policy migration is preserved.
# FROZEN_V3 stays a frozen checker; the current side receives v5 through check.
POLICY_CONTRACT_MIGRATION = compat.POLICY_CONTRACT_MIGRATION
for _name, _function in list(vars(compat).items()):
    if _name.startswith("test_") and callable(_function):
        globals()["test_v4_compat_" + _name[5:]] = _function


def _supported_claims(result):
    return [claim for claim in result["claims"]
            if claim["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT")]


@pytest.mark.parametrize("answer,source,relation", [
    ("因为小时候落水的经历让她害怕深水。",
     "她常穿雨衣。小时候落水的经历让她害怕深水；遇到积水码头会先找绕行路线。",
     "小时候落水的经历让她害怕深水"),
    ("邮差不能私自看信，因为所有未拆封信件只能由收件人打开。",
     "城里使用编号筒。所有未拆封信件只能由收件人打开，投递员没有拆信权。",
     "所有未拆封信件只能由收件人打开"),
    ("因为早年受伤的经历使她害怕高处。",
     "早年受伤的经历使她害怕高处；她会先检查护栏。",
     "早年受伤的经历使她害怕高处"),
    ("因为所有密封包裹只能由登记人开启。",
     "所有密封包裹只能由登记人开启，搬运员没有开包权。",
     "所有密封包裹只能由登记人开启"),
])
def test_v5_exact_cited_reason_clause_has_local_text_evidence(answer, source, relation):
    sources = [{"citation_id": "K2", "text": source, "document_id": "doc-current", "version_id": "v-current", "version_number": 3}]
    result = check(answer, sources, ["K2"])
    assert result["status"] == "TEXT_EVIDENCE_FOUND"
    assert result["answer"] == answer and not result["semantic_acceptance"]
    assert _supported_claims(result)
    rows = [row for row in result["paraphrase_checks"] if row["resolved"]]
    assert rows and all(row["evidence"] and not row["unresolved_relations"] for row in rows)
    assert any(relation in evidence["text"] and evidence["citation_id"] == "K2"
               and evidence["document_id"] == "doc-current" and evidence["version_id"] == "v-current"
               for row in rows for evidence in row["evidence"])


@pytest.mark.parametrize("sources,citations", [
    ([{"citation_id": "K2", "text": "所有密封包裹只能由登记人开启。"}], []),
    ([{"citation_id": "K2", "text": "所有密封包裹只能由登记人开启。"}], ["K99"]),
    ([{"citation_id": "K2", "text": "所有密封包裹只能由登记人开启。"},
      {"citation_id": "K2", "text": "所有密封包裹只能由搬运员开启。"}], ["K2"]),
    ([{"citation_id": "K2", "text": "所有密封包裹只能由登记人开启。", "version_id": "old"},
      {"citation_id": "K2", "text": "所有密封包裹只能由登记人开启。", "version_id": "new"}], ["K2"]),
])
def test_v5_local_reason_requires_unambiguous_cited_source_binding(sources, citations):
    result = check("因为所有密封包裹只能由登记人开启。", sources, citations)
    assert result["status"] == "NEED_REVIEW"
    assert not any(row["resolved"] for row in result["paraphrase_checks"])


@pytest.mark.parametrize("source", [
    "如果登记人到场，所有密封包裹只能由登记人开启。",
    "仅在雨天，所有密封包裹只能由登记人开启。",
    "并非所有密封包裹只能由登记人开启。",
    "尚不能确定所有密封包裹只能由登记人开启。",
    "所有密封包裹只能由登记人开启，但获准的搬运员也能开启。",
    "有人问：‘所有密封包裹只能由登记人开启’是否正确？",
    "有人说：‘所有密封包裹只能由登记人开启’。",
    "所有密封包裹只能由登记人开启，只是传闻。",
    "所有密封包裹只能由登记人开启，并不属实。",
    "所有密封包裹只能由登记人开启，仅在雨天适用。",
    "所有密封包裹只能由登记人开启，这个说法是错的。",
    "所有密封包裹只能由登记人开启，上述规则不成立。",
    "所有密封包裹只能由登记人开启，仅为传闻。",
    "所有密封包裹只能由登记人开启。这条规则仅在雨天适用。",
    "所有密封包裹只能由登记人开启；上述规则不成立。",
    "所有密封包裹只能由登记人开启。这个说法是错的。",
])
def test_v5_source_scope_negation_time_conditions_and_exceptions_are_not_dropped(source):
    result = check("因为所有密封包裹只能由登记人开启。", [{"citation_id": "K1", "text": source}], ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert not any(row["resolved"] for row in result["paraphrase_checks"])


@pytest.mark.parametrize("answer", [
    "因为所有密封包裹只能由搬运员开启。",
    "因为所有包裹都能由登记人开启。",
    "因为所有密封包裹只能由登记人开启，所以包裹绝对安全。",
    "因为所有密封包裹只能由登记人开启，而且关好窗帘可以防止迷失方向。",
])
def test_v5_literal_overlap_never_covers_changed_or_added_relations(answer):
    result = check(answer, [{"citation_id": "K1", "text": "所有密封包裹只能由登记人开启。"}], ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert any(claim["state"] == "NEED_REVIEW" for claim in result["claims"])


@pytest.mark.parametrize("answer,source", [
    ("因为仅在雨天所有密封包裹只能由登记人开启。", "仅在雨天所有密封包裹只能由登记人开启。"),
    ("因为只要护罩没有裂纹并且正确密封就绝对安全。", "只要护罩没有裂纹并且正确密封就绝对安全。"),
])
def test_v5_complete_explicit_scope_is_retained_when_the_whole_cited_clause_matches(answer, source):
    result = check(answer, [{"citation_id": "K1", "text": source}], ["K1"])
    assert result["status"] == "TEXT_EVIDENCE_FOUND"
    assert any(source.rstrip("。") in evidence["text"] for row in result["paraphrase_checks"] for evidence in row["evidence"])


_HISTORY_SOURCE = (
    "陆遥人物档案\n身份与称谓：陆遥是镜海镇故事的男主角，三十一岁的成年男性。\n"
    "经历与行为：他小时候曾被雷声惊醒。这段受惊经历使他害怕突发的巨大声响，"
    "遇到轰鸣、雷鸣等声音时会主动远离声源，躲到安静处。"
)
_HISTORY_ANSWER = "陆遥小时候曾被雷声惊醒，这段受惊经历使他害怕突发的巨大声响，因此遇到轰鸣、雷鸣等声音时会主动远离声源。"


def test_v5_historical_experience_gets_three_local_anchors_but_added_cause_stays_unresolved():
    result = check(_HISTORY_ANSWER, [{"citation_id": "K2", "text": _HISTORY_SOURCE, "document_id": "person-v1", "version_id": "v1"}], ["K2"])
    assert result["status"] == "NEED_REVIEW"
    partial = [row for row in result["paraphrase_checks"] if row["evidence"] and not row["resolved"]]
    assert partial and any(row["unresolved_relations"] for row in partial)
    evidence = [item for row in partial for item in row["evidence"]]
    assert {item["anchor_role"] for item in evidence} >= {"subject", "experience", "relation_and_behavior"}
    assert all(item["citation_id"] == "K2" and item["document_id"] == "person-v1" and item["version_id"] == "v1" for item in evidence)
    assert any("因此" in relation for row in partial for relation in row["unresolved_relations"])
    assert result["answer"] == _HISTORY_ANSWER and not result["semantic_acceptance"]


@pytest.mark.parametrize("source", [
    _HISTORY_SOURCE.replace("陆遥是", "另一人是"),
    _HISTORY_SOURCE.replace("他小时候曾被雷声惊醒", "他小时候曾在高处摔倒"),
    _HISTORY_SOURCE.replace("这段受惊经历使他害怕", "这段受惊经历并未使他害怕"),
    _HISTORY_SOURCE.replace("他小时候曾被雷声惊醒", "如果他小时候曾被雷声惊醒"),
])
def test_v5_experience_anchors_do_not_borrow_a_different_person_past_or_scope(source):
    result = check(_HISTORY_ANSWER, [{"citation_id": "K2", "text": source}], ["K2"])
    assert result["status"] == "NEED_REVIEW"
    assert not any(row["resolved"] for row in result["paraphrase_checks"])
    assert not any({e.get("anchor_role") for e in row["evidence"]} >= {"subject", "experience", "relation_and_behavior"}
                   for row in result["paraphrase_checks"])


def test_v5_experience_anchors_are_not_stitched_across_cited_documents():
    sources = [{"citation_id": "K1", "text": "身份与称谓：陆遥是故事的男主角。"},
               {"citation_id": "K2", "text": "经历与行为：他小时候曾被雷声惊醒。这段受惊经历使他害怕突发的巨大声响，遇到轰鸣、雷鸣等声音时会主动远离声源。"}]
    result = check(_HISTORY_ANSWER, sources, ["K1", "K2"])
    assert result["status"] == "NEED_REVIEW"
    assert not any({e.get("anchor_role") for e in row["evidence"]} >= {"subject", "experience", "relation_and_behavior"}
                   for row in result["paraphrase_checks"])


@pytest.mark.parametrize("answer,source", [
    ("下午四点涨潮导致低处巷道被浅水覆盖。", "每天下午四点涨潮，低处巷道会被浅水覆盖。"),
    ("无法撰写该动作戏，因为交通画面只能出现步行、渡船或有轨电车，不存在出租汽车。",
     "没有私人机动车；任何交通画面只能出现步行、渡船或有轨电车。"),
    ("陆遥小时候曾被雷声惊醒，因此遇到轰鸣时会主动远离声源。",
     "陆遥小时候曾被雷声惊醒。他遇到轰鸣时会主动远离声源。"),
    ("且需关好窗帘以防海面光影让人迷失方向。", "海面会投出让人迷失方向的光影。镇长提醒关好窗帘。"),
])
def test_v5_unstated_causal_or_world_existence_edges_remain_reviewable(answer, source):
    result = check(answer, [{"citation_id": "K1", "text": source}], ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert not result["semantic_acceptance"]
