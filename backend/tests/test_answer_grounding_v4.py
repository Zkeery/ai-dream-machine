"""Policy scope controls + 112 frozen assertions + one explicit migration.

Only the root's isolated runner executes these tests. All previous source and
test files stay unchanged; the superseded policy assertion is not hidden by a
checker wrapper, xfail, or silently deleted coverage.
"""
import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pure_answer_grounding_v4_tests", BASE / "app/services/answer_grounding_v4.py")
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)
check = v4.check_answer_grounding

compat_spec = importlib.util.spec_from_file_location("frozen_v3_assertions_for_v4", BASE / "tests/test_answer_grounding_v3.py")
compat = importlib.util.module_from_spec(compat_spec)
compat_spec.loader.exec_module(compat)
FROZEN_V3 = compat.v3
compat.check = check
compat.v3 = v4
compat.compat.check = check
compat.compat.v2 = v4
compat.compat.legacy.check = check
compat.compat.legacy.grounding = v4

_MIGRATION_FUNCTION = "test_v2_does_not_expand_to_policy_refusals_or_general_paraphrases"
POLICY_CONTRACT_MIGRATION = None


def _retain_original_assertion(function, values):
    @pytest.mark.parametrize("answer", values)
    def retained(answer):
        function(answer)
    return retained


for _name, _function in list(vars(compat).items()):
    if not _name.startswith("test_") or not callable(_function):
        continue
    if _function.__name__ == _MIGRATION_FUNCTION:
        _parameters = next(mark.args[1] for mark in _function.pytestmark if mark.name == "parametrize")
        assert len(_parameters) == 3
        assert POLICY_CONTRACT_MIGRATION is None, "migration must be unique"
        POLICY_CONTRACT_MIGRATION = {
            "superseded_function": _MIGRATION_FUNCTION,
            "superseded_v3_alias": _name,
            "answer": _parameters[0], "old_v3_expected": "NEED_REVIEW",
            "new_v4_expected": "NOT_CHECKED",
            "reason": "explicitly authorized pure policy-refusal contract change; other two assertions retained",
        }
        globals()["test_v3_compat_" + _name[5:]] = _retain_original_assertion(_function, _parameters[1:])
    else:
        globals()["test_v3_compat_" + _name[5:]] = _function
assert POLICY_CONTRACT_MIGRATION is not None


def test_v4_policy_contract_migration_preserves_explicit_old_and_new_expectations():
    migration = POLICY_CONTRACT_MIGRATION
    answer = migration["answer"]
    assert FROZEN_V3.check_answer_grounding(answer, [], [])["status"] == migration["old_v3_expected"]
    result = check(answer, [], [])
    assert result["status"] == migration["new_v4_expected"]
    assert result["answer"] == answer and not result["semantic_acceptance"]


@pytest.mark.parametrize("answer", [
    "不能编造保证疗效的广告语。",
    "无法撰写保证治疗失眠的广告语。",
    "我拒绝生成保证治愈的宣传文案。",
    "不会提供承诺百分之百安全的广告。",
    "不能编造‘只要戴上护罩就绝对安全’的保证。",
    "无法确定，资料不足，不能编造保证疗效的广告语。",
])
def test_v4_pure_behavioral_policy_refusals_are_not_affirmative_promises(answer):
    result = check(answer, [], [])
    assert result["status"] == "NOT_CHECKED"
    assert result["answer"] == answer and not result["automatic_refusal"]


@pytest.mark.parametrize("answer", [
    "无法确定‘不能编造保证治愈广告’是不是规则。",
    "尚不能确定‘拒绝写保证文案’是否只是人物台词。",
    "无法确定品牌是否禁止疗效保证。",
])
def test_v4_quoted_policy_questions_remain_unknown(answer):
    result = check(answer, [], [])
    assert result["status"] == "NOT_CHECKED"
    assert not any(c["state"] == "NEED_REVIEW" for c in result["claims"])


@pytest.mark.parametrize("answer", [
    "我不拒绝保证该产品绝对安全。",
    "不能编造保证广告，但是我保证该产品绝对安全。",
])
def test_v4_negative_refusal_does_not_hide_an_actual_guarantee(answer):
    assert check(answer, [], [])["status"] == "NEED_REVIEW"


def test_v4_refusal_context_requires_direct_cited_fact_support():
    answer = "无法执行该安排，因为规则禁止人员离开室内。"
    result = check(answer, [{"citation_id": "K1", "text": "规则禁止人员离开室内。"}], ["K1"])
    assert result["status"] == "NOT_CHECKED"
    assert any(atom["state"] == "DIRECT_TEXT_SUPPORT" and atom["evidence"][0]["citation_id"] == "K1"
               for explanation in result["policy_explanations"] for atom in explanation["atoms"])
    assert not result["semantic_acceptance"]


def test_v4_explicit_product_and_prohibition_variants_have_separate_context_evidence():
    answer = "无法撰写保证治愈的广告语，因为本品是纸质记录册，不具备医疗功能，且规范禁止出现疗效保证。"
    sources = [{"citation_id": "K1", "text": "不得出现疗效保证。", "is_constraint": True},
               {"citation_id": "K2", "text": "本品是一款纸质记录册，没有医疗功能。"}]
    result = check(answer, sources, ["K1", "K2"])
    assert result["status"] == "NOT_CHECKED"
    atoms = [atom for row in result["policy_explanations"] for atom in row["atoms"]]
    assert len(atoms) == 3
    assert all(atom["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") and atom["evidence"] for atom in atoms)
    assert not result["semantic_acceptance"]


def test_v4_creative_constraint_variant_is_local_to_refusal_and_constraint_source():
    answer = "无法描写该场景，因为根据资料，居民必须留在室内，禁止在街上闲逛。"
    sources = [{"citation_id": "K1", "text": "居民必须留在室内，禁止安排角色在街上闲逛。", "is_constraint": True}]
    result = check(answer, sources, ["K1"])
    assert result["status"] == "NOT_CHECKED"
    assert all(atom["evidence"] for row in result["policy_explanations"] for atom in row["atoms"])


def test_v4_creative_role_variant_is_not_general_world_fact_matching():
    answer = "无法描写该场景，因为居民必须留在室内，禁止在街上闲逛。"
    sources = [{"citation_id": "K1", "text": "居民必须留在室内，禁止安排角色在街上闲逛。", "is_constraint": False}]
    assert check(answer, sources, ["K1"])["status"] == "NEED_REVIEW"


@pytest.mark.parametrize("source", [
    "另一产品是一款纸质记录册，没有医疗功能。",
    "本品是一款纸质记录册，具备医疗功能。",
])
def test_v4_product_subject_and_negative_attributes_are_not_normalized_away(source):
    answer = "无法撰写保证广告，因为本品是纸质记录册，不具备医疗功能。"
    assert check(answer, [{"citation_id": "K1", "text": source}], ["K1"])["status"] == "NEED_REVIEW"


def test_v4_constraint_variant_keeps_explicit_temporal_scope():
    answer = "无法描写该场景，因为居民必须留在室内，禁止在街上闲逛。"
    source = "红月夜居民必须留在室内，禁止安排未成年角色在街上闲逛。"
    assert check(answer, [{"citation_id": "K1", "text": source, "is_constraint": True}], ["K1"])["status"] == "NEED_REVIEW"


@pytest.mark.parametrize("source", ["规则建议备伞。", "规则允许人员离开室内。", "规则禁止人员留在室内。"])
def test_v4_refusal_context_insufficient_or_opposite_sources_need_review(source):
    answer = "无法执行该安排，因为规则禁止人员离开室内。"
    assert check(answer, [{"citation_id": "K1", "text": source}], ["K1"])["status"] == "NEED_REVIEW"


@pytest.mark.parametrize("answer,source", [
    ("无法写保证广告，因为品牌规定该品可以治好失眠。", "品牌不得出现疗效保证。"),
    ("无法撰写该广告，因为本品是玻璃茶杯，不具备医疗功能。", "本品是纸质日记本，没有医疗功能。"),
    ("无法描写该场景，因为红月每年出现一次，所有居民必须留在室内。", "红月每四十天出现一次，所有居民必须留在室内。"),
    ("无法写出租车戏，因为城中没有任何机动车。", "城中没有私人机动车，交通画面只能出现步行、渡船或有轨电车。"),
])
def test_v4_fact_explanations_are_not_exempted_by_refusal_headers(answer, source):
    assert check(answer, [{"citation_id": "K1", "text": source}], ["K1"])["status"] == "NEED_REVIEW"


def test_v4_closed_creative_constraints_do_not_prove_world_absence():
    answer = "无法写出租车戏，因为不存在出租汽车。"
    source = "没有私人机动车；交通画面只能出现步行、渡船或有轨电车。"
    assert check(answer, [{"citation_id": "K1", "text": source}], ["K1"])["status"] == "NEED_REVIEW"


@pytest.mark.parametrize("answer", [
    "不能编造保证疗效的广告语但关好窗帘以防海面光影让人迷失方向。",
    "无法撰写保证广告而且我保证护罩绝对安全。",
    "不能编造保证广告（关帘能防止迷失）。",
    "不能编造保证广告，‘护罩绝对安全’是事实。",
    "不能编造保证广告，因为缺乏资料，而且关帘以防海面光影迷失。",
])
def test_v4_mixed_refusals_preserve_unsupported_protection_or_promises(answer):
    result = check(answer, [], [])
    assert result["status"] == "NEED_REVIEW"
    assert any(c["state"] == "NEED_REVIEW" for c in result["claims"])


@pytest.mark.parametrize("sources,citations", [
    ([], ["K99"]),
    ([{"citation_id": "K1", "text": "一段。"}, {"citation_id": "K1", "text": "另一段。"}], ["K1"]),
])
def test_v4_pure_refusal_does_not_skip_citation_anomalies(sources, citations):
    result = check("不能编造保证疗效的广告语。", sources, citations)
    assert result["status"] == "NEED_REVIEW" and result["citation_issues"]


def test_v4_context_support_does_not_borrow_uncited_material():
    result = check("无法执行该安排，因为规则禁止人员离开室内。",
                   [{"citation_id": "K1", "text": "规则禁止人员离开室内。"}], [])
    assert result["status"] == "NEED_REVIEW"


def test_v4_two_supported_facts_do_not_create_a_new_causal_explanation():
    answer = "无法写保证疗效广告，因为产品是纸质日记本所以不具备医疗功能。"
    sources = [{"citation_id": "K1", "text": "产品是纸质日记本，没有医疗功能。"}]
    assert check(answer, sources, ["K1"])["status"] == "NEED_REVIEW"


def test_v4_product_context_cannot_borrow_present_but_uncited_product_source():
    answer = "无法撰写保证广告，因为本品是纸质记录册，不具备医疗功能，且规范禁止出现疗效保证。"
    sources = [{"citation_id": "K1", "text": "不得出现疗效保证。", "is_constraint": True},
               {"citation_id": "K2", "text": "本品是纸质记录册，没有医疗功能。"}]
    assert check(answer, sources, ["K1"])["status"] == "NEED_REVIEW"


def test_v4_bare_attribute_cannot_inherit_the_wrong_product_from_another_source():
    answer = "无法撰写保证疗效广告，因为本品是纸质记录册，不具备医疗功能。"
    sources = [{"citation_id": "K1", "text": "本品是一款纸质记录册。"},
               {"citation_id": "K2", "text": "另一产品是一款玻璃茶杯，没有医疗功能。"}]
    assert check(answer, sources, ["K1", "K2"])["status"] == "NEED_REVIEW"


def test_v4_policy_head_before_conditional_promise_keeps_complete_source():
    relation = "我保证只要护罩没有裂纹并且正确密封就绝对安全"
    answer = "不能编造保证疗效的广告语而且" + relation + "。"
    result = check(answer, [{"citation_id": "K1", "text": relation + "。"}], ["K1"])
    assert result["status"] == "TEXT_EVIDENCE_FOUND"
    assert any(c["state"] == "DIRECT_TEXT_SUPPORT" and c["text"] == relation for c in result["claims"])


@pytest.mark.parametrize("source", [
    "我保证只要护罩没有裂纹。",
    "我保证只要护罩已有裂纹并且正确密封就绝对安全。",
])
def test_v4_policy_head_cannot_use_shortened_or_changed_condition_as_support(source):
    answer = "不能编造保证疗效的广告语而且我保证只要护罩没有裂纹并且正确密封就绝对安全。"
    result = check(answer, [{"citation_id": "K1", "text": source}], ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert not any(c["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for c in result["claims"])


def test_v4_refusal_does_not_create_h05_protective_mechanism_from_real_related_source():
    answer = "不能编造保证效果，因为镇长提醒关好窗帘，关帘可以防止海面光影让人迷失方向。"
    source = "红月升起后海面会投出让人迷失方向的光影，所以居民留在室内。镇长提醒关好窗帘。"
    assert check(answer, [{"citation_id": "K1", "text": source}], ["K1"])["status"] == "NEED_REVIEW"


def test_v4_explicit_prohibition_list_and_realistic_product_document_keep_all_three_facts():
    answer = "无法撰写保证治疗失眠的广告语，因为本品是纸质日记本，不具备医疗功能，且品牌规范禁止出现疗效保证。"
    sources = [{"citation_id": "K1", "text": "不得出现疗效保证、投资回报承诺或夸大功效；品牌名始终写作‘本品’。", "is_constraint": True},
               {"citation_id": "K2", "text": "本品是一款纸质日记本，封面使用再生纸，内页是空白页。卖点是书写和记录生活；没有医疗功能，不能宣称能治疗失眠或焦虑。"}]
    result = check(answer, sources, ["K1", "K2"])
    assert result["status"] == "NOT_CHECKED"
    atoms = [atom for row in result["policy_explanations"] for atom in row["atoms"]]
    assert len(atoms) == 3 and all(atom["evidence"] for atom in atoms)
    medical = next(atom for atom in atoms if "医疗功能" in atom["text"])
    assert medical["evidence"][0]["subject_anchor_text"] == "本品是一款纸质日记本"
    prohibition = next(atom for atom in atoms if "疗效保证" in atom["text"])
    listed = prohibition["evidence"][0]["prohibition_list_member"]
    assert listed["member"] == "疗效保证" and "投资回报承诺或夸大功效" in listed["original_list"]


@pytest.mark.parametrize("source", [
    "仅在未获许可时不得出现疗效保证、投资回报承诺或夸大功效。",
    "并非不得出现疗效保证、投资回报承诺或夸大功效。",
    "允许出现疗效保证、投资回报承诺或夸大功效。",
])
def test_v4_explicit_prohibition_list_does_not_drop_scope_or_reverse_operator(source):
    answer = "无法写保证广告，因为规范禁止出现疗效保证。"
    result = check(answer, [{"citation_id": "K1", "text": source, "is_constraint": True}], ["K1"])
    assert result["status"] == "NEED_REVIEW"


def test_v4_conflicting_source_role_metadata_does_not_select_the_first_constraint_flag():
    answer = "无法描写该场景，因为居民必须留在室内，禁止在街上闲逛。"
    text = "居民必须留在室内，禁止安排角色在街上闲逛。"
    sources = [{"citation_id": "K1", "text": text, "is_constraint": True},
               {"citation_id": "K1", "text": text, "is_constraint": False}]
    assert check(answer, sources, ["K1"])["status"] == "NEED_REVIEW"
