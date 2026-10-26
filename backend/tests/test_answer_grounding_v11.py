"""Policy evidence display controls; no new human/semantic acceptance labels."""
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


v11 = module('pure_v11_status_tests', ROOT / 'backend/app/services/answer_grounding_v11.py')
v10 = module('frozen_v10_status_tests', ROOT / 'backend/app/services/answer_grounding_v10.py')
compat = module('v11_frozen_business', ROOT / 'backend/tests/test_answer_grounding_v6.py')
current = compat
while True:
    if hasattr(current, 'check'):
        current.check = v11.check_answer_grounding
    for name in ('v6', 'v5', 'v4', 'v3', 'v2', 'grounding'):
        if hasattr(current, name):
            setattr(current, name, v11)
    if hasattr(current, 'compat'):
        current = current.compat
    elif hasattr(current, 'legacy'):
        current = current.legacy
    else:
        break
for name, function in list(vars(compat).items()):
    if name.startswith('test_') and callable(function):
        globals()['test_v11_inherited_' + name[5:]] = function

OUT = ROOT / 'docs/evidence/local-free-acceptance-20261025/rag-policy-status-v11-20261026'
CASES = json.loads((OUT / 'controls.json').read_text())['cases']


@pytest.mark.parametrize('case', CASES, ids=lambda c: c['case_id'])
def test_scoped_status_contract_and_original_output_retention(case):
    args = (case['answer'], copy.deepcopy(case['sources']), copy.deepcopy(case['citations']))
    immutable = copy.deepcopy(args)
    old, new = v10.check_answer_grounding(*args), v11.check_answer_grounding(*args)
    assert args == immutable
    assert {k: v for k, v in old.items() if k != 'version'} == {
        k: v for k, v in new.items() if k not in ('version', 'status_expression')}
    expression = new['status_expression']
    assert expression['primary_status'] == case['expected_primary_status']
    assert expression['policy_reason']['status'] == case['expected_policy_status']
    assert expression['lexical_status'] == old['status']
    assert expression['whole_answer'] == dict(status='NOT_ASSESSED', passed=None, semantic_acceptance=False)
    assert expression['human_judgment'] is False
    assert expression['original_risks_preserved'] is True
    assert new['semantic_acceptance'] is new['automatic_refusal'] is new['production_integrated'] is False
    assert '整条回答未判定' in expression['label']
    if old['status'] == 'NEED_REVIEW':
        assert expression['primary_status'] == 'NEED_REVIEW'


@pytest.mark.parametrize('change', ['empty_atoms', 'nonasserted_only', 'no_evidence', 'false_resolved',
                                  'wrong_span', 'wrong_document', 'wrong_version', 'float_version',
                                  'uncited', 'duplicate_source'])
def test_resolved_or_partial_metadata_cannot_manufacture_local_support(change):
    case = CASES[0]
    sources = copy.deepcopy(case['sources'])
    result = v10.check_answer_grounding(case['answer'], sources, case['citations'])
    frame = result['policy_explanations'][0]
    if change == 'empty_atoms':
        frame['atoms'] = []
    elif change == 'nonasserted_only':
        frame['atoms'] = [dict(text='无法确定', state='NOT_ASSERTED', evidence=[])]
    elif change == 'no_evidence':
        for atom in frame['atoms']:
            atom['evidence'] = []
    elif change == 'false_resolved':
        frame['resolved'] = False
    elif change == 'duplicate_source':
        sources.append(copy.deepcopy(sources[0]))
    else:
        for atom in frame['atoms']:
            for evidence in atom.get('evidence', []):
                if change == 'wrong_span':
                    evidence['text'] = '并未提供的来源片段'
                elif change == 'wrong_document':
                    evidence['document_id'] = 'other'
                elif change == 'wrong_version':
                    evidence['version_id'] = 'other'
                elif change == 'float_version':
                    evidence['version_number'] = 1.0
                else:
                    evidence['citation_id'] = 'UNCITED'
    expression = v11._expression(result, sources, case['citations'])
    assert expression['whole_answer']['passed'] is None
    if change in ('empty_atoms', 'nonasserted_only'):
        assert expression['policy_reason']['status'] == 'NOT_CHECKED'
        assert expression['policy_reason']['supported_atom_count'] == 0
    elif change == 'false_resolved':
        assert expression['policy_reason']['status'] == 'PARTIAL_TEXT_EVIDENCE_FOUND'
        assert expression['primary_status'] == 'NEED_REVIEW'
    else:
        assert expression['policy_reason']['status'] == 'NEED_REVIEW'
        assert expression['policy_reason']['supported_atom_count'] == 0


def test_local_support_records_complete_source_version_but_not_source_truth():
    case = CASES[0]
    out = v11.check_answer_grounding(case['answer'], case['sources'], case['citations'])
    reason = out['status_expression']['policy_reason']
    assert reason['supported_atom_count'] == 2
    assert reason['unresolved_count'] == 0
    evidence = [e for f in reason['frames'] for a in f['atoms'] for e in a['bound_evidence']]
    assert all(e['version_id'] and type(e['version_number']) is int and len(e['source_text_sha256']) == 64 for e in evidence)
    assert 'not_source_truth' in reason['scope']


def test_unchecked_tail_can_only_keep_local_evidence_never_whole_answer_pass():
    case = next(c for c in CASES if c['case_id'] == 'PS20')
    out = v11.check_answer_grounding(case['answer'], case['sources'], case['citations'])
    assert out['status_expression']['primary_status'] == 'LOCAL_POLICY_REASON_EVIDENCE_FOUND'
    assert out['status_expression']['whole_answer']['passed'] is None
    assert not any('隐形塔' in c['text'] for c in out['claims'])  # Deliberately remains an uncovered fact.


def test_h05_entire_original_risk_output_is_preserved():
    case = next(c for c in CASES if c['case_id'] == 'PS19')
    old = v10.check_answer_grounding(case['answer'], case['sources'], case['citations'])
    new = v11.check_answer_grounding(case['answer'], case['sources'], case['citations'])
    assert old['status'] == new['status'] == 'NEED_REVIEW'
    assert old['claims'] == new['claims']
    assert new['status_expression']['primary_status'] == 'NEED_REVIEW'
    assert new['status_expression']['policy_reason']['status'] == 'NOT_CHECKED'


@pytest.mark.parametrize('opening,closing', [('“', '”'), ('‘', '’'), ('"', '"'), ("'", "'")])
@pytest.mark.parametrize('scope', ['unrelated_name', 'evidence_quote', 'unbalanced'])
def test_quoted_names_and_quoted_rules_have_distinct_scopes(opening, closing, scope):
    case = CASES[0]
    sources = copy.deepcopy(case['sources'])
    body = sources[0]['text']
    if scope == 'unrelated_name':
        body += '品牌名始终写作' + opening + '新苇' + closing + '。'
    elif scope == 'evidence_quote':
        body = opening + body + closing
    else:
        body += opening + '未闭合'
    sources[0]['text'] = body
    out = v11.check_answer_grounding(case['answer'], sources, case['citations'])
    expression = out['status_expression']
    if scope == 'unrelated_name':
        assert expression['primary_status'] == 'LOCAL_POLICY_REASON_EVIDENCE_FOUND'
        assert expression['policy_reason']['supported_atom_count'] == 2
    else:
        assert expression['primary_status'] == 'NEED_REVIEW'
    assert expression['whole_answer']['passed'] is None


def test_disjoint_name_quotes_do_not_hide_later_retraction_or_condition():
    case = CASES[0]
    for tail in ('品牌名为“新苇”。复核撤回上述规则。', '品牌名为“新苇”。这条规则只在检修时生效。'):
        sources = copy.deepcopy(case['sources'])
        sources[0]['text'] += tail
        out = v11.check_answer_grounding(case['answer'], sources, case['citations'])
        assert out['status_expression']['primary_status'] == 'NEED_REVIEW'
