"""Finite conditions/rules: keep existing assertions and require explicit scope."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/evidence/local-free-acceptance-20261025/rag-condition-rule-v12-20261026'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


v12 = module('pure_v12_tests', ROOT / 'backend/app/services/answer_grounding_v12.py')
v11 = module('frozen_v11_rule_tests', ROOT / 'backend/app/services/answer_grounding_v11.py')
compat = module('v12_frozen_business', ROOT / 'backend/tests/test_answer_grounding_v6.py')
current = compat
while True:
    if hasattr(current, 'check'):
        current.check = v12.check_answer_grounding
    for name in ('v6', 'v5', 'v4', 'v3', 'v2', 'grounding'):
        if hasattr(current, name):
            setattr(current, name, v12)
    if hasattr(current, 'compat'):
        current = current.compat
    elif hasattr(current, 'legacy'):
        current = current.legacy
    else:
        break
for name, function in list(vars(compat).items()):
    if name.startswith('test_') and callable(function):
        globals()['test_v12_inherited_' + name[5:]] = function

CONTROLS = json.loads((OUT / 'controls.json').read_text())
CASES, ANCHORS = CONTROLS['cases'], CONTROLS['anchors']


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['case_id'])
def test_frozen_engineering_rule_contract(case):
    args = (case['answer'], copy.deepcopy(case['sources']), copy.deepcopy(case['citations']))
    immutable = copy.deepcopy(args)
    old, out = v11.check_answer_grounding(*args), v12.check_answer_grounding(*args)
    assert args == immutable and out['answer'] == case['answer']
    assert out['status'] == case['expected_status']
    assert out['semantic_acceptance'] is out['automatic_refusal'] is out['production_integrated'] is False
    assert out['status_expression']['whole_answer'] == dict(status='NOT_ASSESSED', passed=None, semantic_acceptance=False)
    if old['status'] != 'NOT_CHECKED':
        assert {k: v for k, v in old.items() if k != 'version'} == {
            k: v for k, v in out.items() if k not in ('version', 'bounded_rule_checks')}
        assert out['bounded_rule_checks'] == []
    if out['status'] == 'TEXT_EVIDENCE_FOUND':
        row, = out['bounded_rule_checks']
        assert row['resolved'] and row['state'] == 'LIMITED_LEXICAL_SUPPORT'
        assert row['fabrication_judgment'] is None and row['semantic_acceptance'] is False
        assert row['evidence'][0]['text'] == case['sources'][0]['text']
        assert row['evidence'][0]['version_id'] == case['sources'][0]['version_id']


@pytest.mark.parametrize('anchor', ANCHORS, ids=lambda a: a['case_id'])
@pytest.mark.parametrize('change', ['none', 'missing_document', 'missing_version', 'missing_number',
                                  'bool_number', 'float_number', 'unknown_citation', 'no_citation',
                                  'extra_uncited', 'same_id_other_version'])
def test_complete_single_source_version_and_citation_binding_required(anchor, change):
    case = anchor['material']
    sources, citations = copy.deepcopy(case['sources']), copy.deepcopy(case['citations'])
    if change == 'none':
        sources = []
    elif change.startswith('missing_'):
        sources[0].pop({'missing_document': 'document_id', 'missing_version': 'version_id',
                        'missing_number': 'version_number'}[change])
    elif change == 'bool_number':
        sources[0]['version_number'] = True
    elif change == 'float_number':
        sources[0]['version_number'] = 1.0
    elif change == 'unknown_citation':
        citations = ['UNKNOWN']
    elif change == 'no_citation':
        citations = []
    elif change == 'extra_uncited':
        sources.append(dict(sources[0], citation_id='S2'))
    else:
        sources.append(dict(sources[0], version_id='other'))
    out = v12.check_answer_grounding(case['answer'], sources, citations)
    assert out['status'] == 'NEED_REVIEW'
    assert not any(x['resolved'] for x in out['bounded_rule_checks'])


@pytest.mark.parametrize('anchor', ANCHORS, ids=lambda a: a['case_id'])
@pytest.mark.parametrize('tail', ['尚未核实。', '另有例外。', '只有维修员适用。', '后来撤回上述说明。'])
def test_unknown_source_tail_is_never_clipped(anchor, tail):
    case = anchor['material']
    sources = copy.deepcopy(case['sources'])
    sources[0]['text'] += tail
    out = v12.check_answer_grounding(case['answer'], sources, case['citations'])
    assert out['status'] == 'NEED_REVIEW'
    assert not any(x['resolved'] for x in out['bounded_rule_checks'])


@pytest.mark.parametrize('anchor', ANCHORS, ids=lambda a: a['case_id'])
@pytest.mark.parametrize('prefix', ['可能', '无法确定是否', '不能断言', '假设', '并非'])
def test_qualified_answer_is_not_new_affirmative_support(anchor, prefix):
    case = anchor['material']
    out = v12.check_answer_grounding(prefix + case['answer'], case['sources'], case['citations'])
    assert out['bounded_rule_checks'] == []


@pytest.mark.parametrize('anchor', ANCHORS, ids=lambda a: a['case_id'])
def test_extra_answer_claim_is_not_clipped_to_gain_support(anchor):
    case = anchor['material']
    out = v12.check_answer_grounding(case['answer'] + '这里还有十座塔。', case['sources'], case['citations'])
    assert out['status'] == 'NEED_REVIEW'
    assert not any(x['resolved'] for x in out['bounded_rule_checks'])
    assert out['status_expression']['whole_answer']['passed'] is None


def test_h05_entire_old_risk_and_h06_local_expression_remain():
    cases = json.loads((ROOT / 'docs/evidence/local-free-acceptance-20261025/rag-policy-status-v11-20261026/controls.json').read_text())['cases']
    for ident in ('PS01', 'PS19'):
        c = next(x for x in cases if x['case_id'] == ident)
        old, new = v11.check_answer_grounding(c['answer'], c['sources'], c['citations']), v12.check_answer_grounding(c['answer'], c['sources'], c['citations'])
        assert {k: v for k, v in old.items() if k != 'version'} == {
            k: v for k, v in new.items() if k not in ('version', 'bounded_rule_checks')}
        assert new['bounded_rule_checks'] == []
