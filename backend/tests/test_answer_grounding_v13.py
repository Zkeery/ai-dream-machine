"""Finite motion/purpose separation and literal relative-clause effect tests."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/evidence/local-free-acceptance-20261025/rag-implicit-relations-v13-20261026'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


v13 = module('pure_v13_tests', ROOT / 'backend/app/services/answer_grounding_v13.py')
v12 = module('frozen_v12_implicit_tests', ROOT / 'backend/app/services/answer_grounding_v12.py')
compat = module('v13_frozen_business', ROOT / 'backend/tests/test_answer_grounding_v6.py')
current = compat
while True:
    if hasattr(current, 'check'):
        current.check = v13.check_answer_grounding
    for name in ('v6', 'v5', 'v4', 'v3', 'v2', 'grounding'):
        if hasattr(current, name):
            setattr(current, name, v13)
    if hasattr(current, 'compat'):
        current = current.compat
    elif hasattr(current, 'legacy'):
        current = current.legacy
    else:
        break
for name, function in list(vars(compat).items()):
    if name.startswith('test_') and callable(function):
        globals()['test_v13_inherited_' + name[5:]] = function

CONTROLS = json.loads((OUT / 'controls.json').read_text())
CASES, ANCHORS = CONTROLS['cases'], CONTROLS['anchors']


@pytest.mark.parametrize('case', CASES, ids=lambda x: x['case_id'])
def test_frozen_implicit_relation_contract_without_whole_answer_acceptance(case):
    args = (case['answer'], copy.deepcopy(case['sources']), copy.deepcopy(case['citations']))
    immutable = copy.deepcopy(args)
    old, out = v12.check_answer_grounding(*args), v13.check_answer_grounding(*args)
    assert args == immutable and out['answer'] == case['answer']
    assert out['status'] == case['expected_status']
    assert out['status_expression']['whole_answer'] == dict(status='NOT_ASSESSED', passed=None, semantic_acceptance=False)
    assert out['semantic_acceptance'] is out['automatic_refusal'] is out['production_integrated'] is False
    if old['status'] != 'NOT_CHECKED':
        assert {k:v for k,v in old.items() if k != 'version'} == {
            k:v for k,v in out.items() if k not in ('version', 'implicit_relation_checks')}
        assert out['implicit_relation_checks'] == []
    for row in out['implicit_relation_checks']:
        assert row['fabrication_judgment'] is None and row['semantic_acceptance'] is False
        if row['resolved']:
            cited = next(s for s in case['sources'] if s['citation_id'] == row['evidence'][0]['citation_id'])
            assert row['evidence'][0]['text'] == cited['text']
            assert row['evidence'][0]['version_id'] == cited['version_id']


@pytest.mark.parametrize('anchor', ANCHORS, ids=lambda x:x['item'])
@pytest.mark.parametrize('change', ['no_source', 'missing_document', 'missing_version', 'missing_number',
                                  'bool_number', 'float_number', 'no_citation', 'unknown_citation',
                                  'second_citation', 'duplicate_version'])
def test_unique_cited_complete_version_required(anchor, change):
    sources, citations = copy.deepcopy(anchor['sources']), copy.deepcopy(anchor['citations'])
    target = next(s for s in sources if s['citation_id'] == 'K2')
    if change == 'no_source':
        sources = []
    elif change.startswith('missing_'):
        target.pop({'missing_document':'document_id', 'missing_version':'version_id', 'missing_number':'version_number'}[change])
    elif change == 'bool_number':
        target['version_number'] = True
    elif change == 'float_number':
        target['version_number'] = 1.0
    elif change == 'no_citation':
        citations = []
    elif change == 'unknown_citation':
        citations = ['UNKNOWN']
    elif change == 'second_citation':
        citations = ['K1', 'K2']
    else:
        sources.append(dict(target, version_id='changed'))
    out = v13.check_answer_grounding(anchor['answer'], sources, citations)
    assert out['status'] == 'NEED_REVIEW'
    assert not any(x['resolved'] for x in out['implicit_relation_checks'])


@pytest.mark.parametrize('anchor', ANCHORS, ids=lambda x:x['item'])
@pytest.mark.parametrize('tail', ['尚未核实。', '后来撤回以上记录。', '只有特殊天气适用。', '尚有其他未记录条件。'])
def test_source_tail_is_preserved_not_clipped(anchor, tail):
    sources = copy.deepcopy(anchor['sources'])
    next(s for s in sources if s['citation_id'] == 'K2')['text'] += tail
    out = v13.check_answer_grounding(anchor['answer'], sources, anchor['citations'])
    assert out['status'] == 'NEED_REVIEW'
    assert not any(x['resolved'] for x in out['implicit_relation_checks'])


@pytest.mark.parametrize('anchor', ANCHORS, ids=lambda x:x['item'])
@pytest.mark.parametrize('prefix', ['可能', '无法确定是否', '不能断言', '假设', '并非'])
def test_question_or_uncertainty_does_not_create_affirmative_support(anchor, prefix):
    out = v13.check_answer_grounding(prefix + anchor['answer'], anchor['sources'], anchor['citations'])
    assert out['implicit_relation_checks'] == []


def test_motion_evidence_and_unconfirmed_goal_are_separate():
    a = ANCHORS[0]
    out = v13.check_answer_grounding(a['answer'], a['sources'], a['citations'])
    motion, purpose = out['implicit_relation_checks']
    assert motion['state'] == 'LIMITED_LEXICAL_SUPPORT' and motion['evidence']
    assert purpose['kind'] == 'purpose_relation' and purpose['state'] == 'NEED_REVIEW'
    assert purpose['evidence'] == [] and purpose['fabrication_judgment'] is None
    assert out['status'] == 'NEED_REVIEW'


def test_literal_world_effect_never_certifies_the_worry_or_protection():
    a = ANCHORS[1]
    out = v13.check_answer_grounding(a['answer'], a['sources'], a['citations'])
    row, = out['implicit_relation_checks']
    assert row['unassessed_spans'] == ['镇民担心']
    assert row['relation']['scope'] == 'world_effect_only_not_mental_state_or_protection'
    assert row['relation']['condition'] == '红月升起后'
    assert out['status_expression']['whole_answer']['passed'] is None


def test_h05_h06_and_r11_old_evidence_is_unchanged():
    cases = json.loads((ROOT / 'docs/evidence/local-free-acceptance-20261025/rag-policy-status-v11-20261026/controls.json').read_text())['cases']
    for ident in ('PS01', 'PS19'):
        c = next(x for x in cases if x['case_id'] == ident)
        old, new = v12.check_answer_grounding(c['answer'], c['sources'], c['citations']), v13.check_answer_grounding(c['answer'], c['sources'], c['citations'])
        assert {k:v for k,v in old.items() if k != 'version'} == {
            k:v for k,v in new.items() if k not in ('version','implicit_relation_checks')}
        assert new['implicit_relation_checks'] == []


@pytest.mark.parametrize('anchor', ANCHORS, ids=lambda x:x['item'])
def test_independent_guarantee_remains_prior_risk(anchor):
    answer = anchor['answer'].rstrip('。') + '而且我保证绝对不会受伤。'
    old = v12.check_answer_grounding(answer, anchor['sources'], anchor['citations'])
    new = v13.check_answer_grounding(answer, anchor['sources'], anchor['citations'])
    assert old['status'] == new['status'] == 'NEED_REVIEW'
    assert old['claims'] == new['claims'] and new['implicit_relation_checks'] == []
