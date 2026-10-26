"""199 frozen v5 business assertions and 40 explicit causal/world controls.

Synthetic source statements stay in these controls. They never replace or
augment the 52 historical payloads. The root alone executes this test module.
"""
import importlib.util
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pure_answer_grounding_v6_tests", BASE / "app/services/answer_grounding_v6.py")
v6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v6)
check = v6.check_answer_grounding

compat_spec = importlib.util.spec_from_file_location("frozen_v5_assertions_for_v6", BASE / "tests/test_answer_grounding_v5.py")
compat = importlib.util.module_from_spec(compat_spec)
compat_spec.loader.exec_module(compat)
compat.check = check
compat.v5 = v6
compat.compat.check = check
compat.compat.v4 = v6
compat.compat.compat.check = check
compat.compat.compat.v3 = v6
compat.compat.compat.compat.check = check
compat.compat.compat.compat.v2 = v6
compat.compat.compat.compat.legacy.check = check
compat.compat.compat.compat.legacy.grounding = v6

POLICY_CONTRACT_MIGRATION = compat.POLICY_CONTRACT_MIGRATION
for _name, _function in list(vars(compat).items()):
    if _name.startswith("test_") and callable(_function):
        globals()["test_v5_compat_" + _name[5:]] = _function


@pytest.mark.parametrize("answer,source,cause,effect,answer_connectors,source_connectors", [
    ("因为每天下午四点潮水上涨，所以低处巷道被浅水覆盖。",
     "由于每天下午四点潮水上涨，因此低处巷道被浅水覆盖。",
     "每天下午四点潮水上涨", "低处巷道被浅水覆盖", ["因为", "所以"], ["由于", "因此"]),
    ("由于每天下午四点潮水上涨，因此低处巷道被浅水覆盖。",
     "因为每天下午四点潮水上涨，所以低处巷道被浅水覆盖。",
     "每天下午四点潮水上涨", "低处巷道被浅水覆盖", ["由于", "因此"], ["因为", "所以"]),
    ("因为他害怕突发的巨大声响，所以他会主动远离声源。",
     "由于他害怕突发的巨大声响，因而他会主动远离声源。",
     "他害怕突发的巨大声响", "他会主动远离声源", ["因为", "所以"], ["由于", "因而"]),
    ("由于护罩没有正确密封，因而热水会外漏。",
     "因为护罩没有正确密封，所以热水会外漏。",
     "护罩没有正确密封", "热水会外漏", ["由于", "因而"], ["因为", "所以"]),
    ("因为潮水上涨，因此低巷被浅水覆盖。", "由于潮水上涨，所以低巷被浅水覆盖。",
     "潮水上涨", "低巷被浅水覆盖", ["因为", "因此"], ["由于", "所以"]),
    ("因为潮水上涨，因而低巷被浅水覆盖。", "因为潮水上涨，因此低巷被浅水覆盖。",
     "潮水上涨", "低巷被浅水覆盖", ["因为", "因而"], ["因为", "因此"]),
    ("由于潮水上涨，所以低巷被浅水覆盖。", "因为潮水上涨，因而低巷被浅水覆盖。",
     "潮水上涨", "低巷被浅水覆盖", ["由于", "所以"], ["因为", "因而"]),
])
def test_v6_complete_explicit_single_causal_pairs_keep_literal_cause_and_effect(answer, source, cause, effect, answer_connectors, source_connectors):
    sources = [{"citation_id": "K2", "text": source, "document_id": "synthetic-only", "version_id": "synthetic-v1", "version_number": 1}]
    result = check(answer, sources, ["K2"])
    assert result["status"] == "TEXT_EVIDENCE_FOUND"
    assert result["answer"] == answer and not result["semantic_acceptance"]
    rows = [row for row in result["paraphrase_checks"] if row["text"] == answer.rstrip("。")]
    assert len(rows) == 1 and rows[0]["resolved"] and not rows[0]["unresolved_relations"]
    assert rows[0]["state"] == "LIMITED_LEXICAL_SUPPORT"
    assert rows[0]["controlled_variants"] and rows[0]["evidence"]
    assert all(e["text"] == source and e["citation_id"] == "K2" and e["document_id"] == "synthetic-only"
               and e["version_id"] == "synthetic-v1" for e in rows[0]["evidence"])
    assert all(e["cause"] == cause and e["effect"] == effect and e["answer_connectors"] == answer_connectors
               and e["source_connectors"] == source_connectors for e in rows[0]["evidence"])
    assert not any(c["state"] == "NEED_REVIEW" for c in result["claims"])


_PAIR_ANSWER = "因为所有成年人在雨天使用护罩，所以双手不会被热水烫伤。"
_PAIR_SOURCE = "由于所有成年人在雨天使用护罩，因此双手不会被热水烫伤。"


@pytest.mark.parametrize("source", [
    "所有成年人在雨天使用护罩，双手不会被热水烫伤。",
    "由于双手不会被热水烫伤，因此所有成年人在雨天使用护罩。",
    "所有成年人在雨天使用护罩。",
    "由于所有儿童在雨天使用护罩，因此双手不会被热水烫伤。",
    "由于所有成年人在雨天使用护罩，因此眼睛不会被热水烫伤。",
    "由于所有成年人在晴天使用护罩，因此双手不会被热水烫伤。",
    "由于部分成年人在雨天使用护罩，因此双手不会被热水烫伤。",
    "由于所有成年人在雨天不使用护罩，因此双手不会被热水烫伤。",
])
def test_v6_parallel_or_changed_causal_edges_are_not_connector_variants(source):
    result = check(_PAIR_ANSWER, [{"citation_id": "K1", "text": source}], ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert not any(row["resolved"] for row in result["paraphrase_checks"])


@pytest.mark.parametrize("sources", [
    [{"citation_id": "K1", "text": "仅当护罩经检验合格时，由于所有成年人在雨天使用护罩，因此双手不会被热水烫伤。"}],
    [{"citation_id": "K1", "text": "并非由于所有成年人在雨天使用护罩，因此双手不会被热水烫伤。"}],
    [{"citation_id": "K1", "text": "尚不能确定是否由于所有成年人在雨天使用护罩，因此双手不会被热水烫伤。"}],
    [{"citation_id": "K1", "text": "有人说：‘由于所有成年人在雨天使用护罩，因此双手不会被热水烫伤。’"}],
    [{"citation_id": "K1", "text": _PAIR_SOURCE + "这个说法是错的。"}],
    [{"citation_id": "K1", "text": _PAIR_SOURCE, "version_id": "old"},
     {"citation_id": "K1", "text": _PAIR_SOURCE, "version_id": "new"}],
])
def test_v6_source_must_be_one_whole_asserted_pair_with_its_scope_and_version(sources):
    result = check(_PAIR_ANSWER, sources, ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert not any(row["resolved"] for row in result["paraphrase_checks"])


@pytest.mark.parametrize("answer,source", [
    ("无法撰写该动作戏，因为海岬城不存在出租汽车。", "海岬城不存在出租汽车。"),
    ("无法撰写该动作戏，因为海岬城没有私人机动车，海岬城不存在出租汽车。",
     "海岬城没有私人机动车；海岬城不存在出租汽车。"),
])
def test_v6_world_absence_requires_the_explicit_same_world_object_and_scope(answer, source):
    result = check(answer, [{"citation_id": "K1", "text": source}], ["K1"])
    assert result["status"] == "NOT_CHECKED"
    assert result["policy_explanations"] and all(row["resolved"] for row in result["policy_explanations"])
    atoms = [atom for row in result["policy_explanations"] for atom in row["atoms"]]
    assert atoms and all(atom["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") and atom["evidence"] for atom in atoms)
    evidence = [item for atom in atoms for item in atom["evidence"]]
    assert all(item["citation_id"] == "K1" and item["text"] in source for item in evidence)
    assert any("海岬城不存在出租汽车" in item["text"] for item in evidence)
    assert not result["semantic_acceptance"]


@pytest.mark.parametrize("source", [
    "海岬城的交通画面只能出现步行、渡船或有轨电车。",
    "海岬城没有私人机动车。",
    "另一座城不存在出租汽车。",
    "海岬城仅在节庆期间不存在出租汽车。",
])
def test_v6_closed_images_private_vehicle_limits_or_other_scope_do_not_prove_world_absence(source):
    result = check("无法撰写该动作戏，因为海岬城不存在出租汽车。", [{"citation_id": "K1", "text": source}], ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert not result["semantic_acceptance"]


@pytest.mark.parametrize("sources,citations", [
    ([{"citation_id": "K1", "text": _PAIR_SOURCE}], []),
    ([{"citation_id": "K1", "text": _PAIR_SOURCE}], ["K99"]),
    ([{"citation_id": "K1", "text": "所有成年人在雨天使用护罩。"},
      {"citation_id": "K2", "text": "双手不会被热水烫伤。"}], ["K1", "K2"]),
])
def test_v6_unreferenced_or_separate_documents_cannot_supply_an_explicit_pair(sources, citations):
    result = check(_PAIR_ANSWER, sources, citations)
    assert result["status"] == "NEED_REVIEW"
    assert not any(row["resolved"] for row in result["paraphrase_checks"])


@pytest.mark.parametrize("answer,sources,citations", [
    ("阿澄投递时需兼顾自身怕深水（遇积水码头绕行）的设定，以及潮汐城下午四点涨潮导致低处巷道被浅水覆盖、且无私人机动车（仅能步行、乘渡船或有轨电车）的城市规则。",
     [{"citation_id": "K2", "text": "潮汐城没有私人机动车；任何交通画面只能出现步行、渡船或有轨电车。"},
      {"citation_id": "K3", "text": "阿澄二十六岁，是潮汐城的信件投递员。她右手装有铜色机械义肢，常穿深绿色雨衣。小时候落水的经历让她害怕深水；遇到积水码头会先找绕行路线。她嘴上冷淡，行动上总会保护小孩。"},
      {"citation_id": "K4", "text": "潮汐城每天下午四点涨潮，低处巷道会被浅水覆盖。市民靠步行、渡船和有轨电车出行，不存在私人机动车。夜市搭在升降木平台上，摊位随潮位缓慢抬升。"}], ["K2", "K3", "K4"]),
    (compat._HISTORY_ANSWER, [{"citation_id": "K2", "text": "陆遥人物档案\n身份与称谓：陆遥是镜海镇故事的男主角，三十一岁的成年男性，经营当地照相馆，以摄影师为职业。\n身体特征：左眼视力较弱，拍照时习惯用右眼观察取景器。\n经历与行为：他小时候曾被雷声惊醒。这段受惊经历使他害怕突发的巨大声响，遇到轰鸣、雷鸣等声音时会主动远离声源，躲到安静处。\n性格：不喜欢争吵；面对人际冲突，他常先保持沉默。"}], ["K2"]),
    ("无法撰写该动作戏，因为资料规定潮汐城没有私人机动车，交通画面只能出现步行、渡船或有轨电车，不存在出租汽车。",
     [{"citation_id": "K1", "text": "潮汐城没有私人机动车；任何交通画面只能出现步行、渡船或有轨电车。"}], ["K1"]),
], ids=["parallel_tide_original", "added_therefore_original", "closed_image_original"])
def test_v6_original_unresolved_edges_keep_review_without_changing_the_answer(answer, sources, citations):
    result = check(answer, sources, citations)
    assert result["status"] == "NEED_REVIEW"
    assert result["answer"] == answer and not result["semantic_acceptance"]
    assert any(row["unresolved_relations"] and not row["resolved"] for row in result["paraphrase_checks"])


def test_v6_scene_rules_keep_only_the_explicit_depiction_scope():
    source = "本片不得出现汽车。"
    result = check("无法描写该场景，因为本片不得出现汽车。", [{"citation_id": "K1", "text": source, "is_constraint": True}], ["K1"])
    assert result["status"] == "NOT_CHECKED"
    atoms = [atom for row in result["policy_explanations"] for atom in row["atoms"]]
    assert atoms and all(atom["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") and atom["evidence"] for atom in atoms)
    assert any(item["citation_id"] == "K1" and item["text"] == source.rstrip("。")
               for atom in atoms for item in atom["evidence"])
    assert not result["semantic_acceptance"]


@pytest.mark.parametrize("answer,source", [
    ("无法描写该场景，因为海岬城不存在汽车。", "本片不得出现汽车。"),
    ("无法描写该场景，因为本片不得出现汽车。", "本片并非不得出现汽车。"),
])
def test_v6_scene_rules_do_not_create_world_absence_or_reverse_a_negated_rule(answer, source):
    result = check(answer, [{"citation_id": "K1", "text": source, "is_constraint": True}], ["K1"])
    assert result["status"] == "NEED_REVIEW"


@pytest.mark.parametrize("source", [
    "由于水位升高，因此闸门关闭。闸门关闭时警灯同时亮起。",
    "由于水位升高，因此闸门关闭。由于闸门关闭，因此警灯亮起。",
])
def test_v6_same_document_relations_are_not_composed_transitively(source):
    result = check("因为水位升高，所以警灯亮起。", [{"citation_id": "K1", "text": source}], ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert not any(row["resolved"] for row in result["paraphrase_checks"])


@pytest.mark.parametrize("source", [
    "由于水位升高，因此闸门关闭吗？",
    "由于水位升高，因此闸门关闭，但紧急泄洪期间例外。",
])
def test_v6_question_or_explicit_exception_is_not_an_unconditional_pair(source):
    result = check("因为水位升高，所以闸门关闭。", [{"citation_id": "K1", "text": source}], ["K1"])
    assert result["status"] == "NEED_REVIEW"
    assert not any(row["resolved"] for row in result["paraphrase_checks"])
