"""Opt-in offline v11: express local policy evidence without answer acceptance.

The frozen v10 output is untouched except version and a new status_expression.
No new semantic inference, human labels, generation, network, or runtime IO.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location(
    '_frozen_grounding_v10_for_v11', Path(__file__).with_name('answer_grounding_v10.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

VERSION = 'answer-grounding-v11-local-policy-status-expression'
_SUPPORTED = frozenset(('DIRECT_TEXT_SUPPORT', 'LIMITED_LEXICAL_SUPPORT'))
_CAUTION = re.compile(r'撤回|尚未核实|未核实|未确认')
_QUOTES = re.compile(r'“[^“”]*”|‘[^‘’]*’|"[^"\n]*"|\x27[^\x27\n]*\x27')
_QUOTE_MARKS = re.compile(r'[“”‘’"\x27]')


def _qualified_source(body, evidence_text):
    # Qualifier words, limits and retractions remain whole-source guards.
    # A quoted name elsewhere is not a quotation of the supporting rule.
    plain = _QUOTE_MARKS.sub('', body)
    if (_base._text._SOURCE_QUALIFICATION.search(plain)
            or _base._text._LIMITING_SCOPE.search(body) or _CAUTION.search(body)):
        return True
    spans = list(_QUOTES.finditer(body))
    if _QUOTE_MARKS.search(_QUOTES.sub('', body)):
        return True  # Unbalanced/nested quote scope is unresolved.
    occurrences = list(re.finditer(re.escape(evidence_text), body))
    return any(q.start() < e.end() and e.start() < q.end() for q in spans for e in occurrences)


def _bound_evidence(evidence, sources, citations):
    """Bind an existing evidence span; never create new text evidence."""
    if not isinstance(evidence, dict):
        return None
    cid, text = evidence.get('citation_id'), evidence.get('text')
    if (not isinstance(cid, str) or cid not in citations
            or not isinstance(text, str) or not text.strip()):
        return None
    rows = [s for s in sources if isinstance(s, dict) and s.get('citation_id', s.get('id')) == cid]
    # Even identical duplicate IDs are not uniquely bound for this display.
    if len(rows) != 1:
        return None
    source = rows[0]
    body = source.get('text')
    if not isinstance(body, str) or text not in body or _qualified_source(body, text):
        return None
    for key in ('document_id', 'version_id'):
        value = source.get(key)
        if not isinstance(value, str) or not value.strip() or evidence.get(key) != value:
            return None
    number = source.get('version_number')
    if (type(number) is not int or number <= 0
            or type(evidence.get('version_number')) is not int or evidence['version_number'] != number):
        return None
    return dict(citation_id=cid, document_id=source['document_id'], version_id=source['version_id'],
                version_number=number, text=text,
                source_text_sha256=hashlib.sha256(body.encode('utf-8')).hexdigest())


def _expression(result, sources, citations):
    frames = []
    supported = unresolved = neutral = 0
    for frame in result.get('policy_explanations', []):
        atoms = []
        for atom in frame.get('atoms', []):
            state = atom.get('state')
            bound = []
            if state in _SUPPORTED:
                evidence = atom.get('evidence')
                if isinstance(evidence, list) and evidence:
                    bound = [_bound_evidence(e, sources, citations) for e in evidence]
                if bound and all(e is not None for e in bound):
                    display = 'LOCAL_TEXT_EVIDENCE_FOUND'
                    supported += 1
                else:
                    display = 'NEED_REVIEW'
                    unresolved += 1
            elif state == 'NOT_ASSERTED':
                display = 'NOT_ASSERTED'
                neutral += 1
            else:
                display = 'NEED_REVIEW'
                unresolved += 1
            atoms.append(dict(text=atom.get('text'), lexical_state=state, display_state=display,
                              bound_evidence=[e for e in bound if e is not None]))
        # resolved=True alone (including vacuous all([])) is never evidence.
        if frame.get('resolved') is not True and not any(a['display_state'] == 'NEED_REVIEW' for a in atoms):
            unresolved += 1
        frames.append(dict(text=frame.get('text'), head=frame.get('head'),
                           legacy_resolved=frame.get('resolved'), atoms=atoms))
    policy = ('PARTIAL_TEXT_EVIDENCE_FOUND' if supported and unresolved else
              'NEED_REVIEW' if unresolved else 'LOCAL_TEXT_EVIDENCE_FOUND' if supported else 'NOT_CHECKED')
    lexical = result['status']
    if lexical == 'NEED_REVIEW' or unresolved:
        primary = 'NEED_REVIEW'
        label = '存在需复核内容；整条回答未判定'
    elif supported:
        primary = 'LOCAL_POLICY_REASON_EVIDENCE_FOUND'
        label = '拒答理由有局部文本依据；整条回答未判定'
    else:
        primary = lexical
        label = ('已找到有限关系文本证据；整条回答未判定' if lexical == 'TEXT_EVIDENCE_FOUND'
                 else '当前未覆盖；整条回答未判定')
    return dict(schema='scoped-status-expression-v11', primary_status=primary, label=label,
                lexical_status=lexical, policy_reason=dict(status=policy, supported_atom_count=supported,
                    unresolved_count=unresolved, nonasserted_atom_count=neutral, frames=frames,
                    scope='existing_local_policy_reason_atoms_only_not_source_truth_or_answer_acceptance'),
                whole_answer=dict(status='NOT_ASSESSED', passed=None, semantic_acceptance=False),
                original_risks_preserved=True, human_judgment=False)


def check_answer_grounding(answer, sources, citations) -> dict:
    result = _base.check_answer_grounding(answer, sources, citations)
    result['version'] = VERSION
    result['status_expression'] = _expression(result, sources, result['citations'])
    return result
