"""Opt-in v13: finite action/purpose and relative-clause effect evidence.

An inferred goal is never created from a motion description. A world effect
is not evidence of a mental state or protective action. Whole answers remain
unassessed. No case IDs, human labels, model clients or runtime data IO.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location(
    '_frozen_grounding_v12_for_v13', Path(__file__).with_name('answer_grounding_v12.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
VERSION = 'answer-grounding-v13-bounded-implicit-relations'
_A = r'[^\s，,。；;！？!?：:、“”‘’\"\x27]{1,40}?'
_UNSAFE = re.compile(r'如果|只要|只有|除非|一旦|仅|任何|所有|或|和|与|并|且|但|不|未|无|可能|假设|据称|传闻|这|该|其|因为|所以|导致|因此|以防|保证')
_QUALIFIED = re.compile(r'可能|无法确定|不能确认|不能断言|假设|并非|能否|是否|[？?！!“”‘’\"\x27]')
_ACTION_SOURCE = re.compile(
    r'(?:(?P<city>' + _A + r')每天下午(?P<clock>' + _A + r')涨潮[，,]低处巷道会被浅水覆盖。'
    r'市民靠步行、渡船和有轨电车出行[，,]不存在私人机动车。)?'
    r'(?P<venue>' + _A + r')搭在(?P<platform>' + _A + r')上[，,]摊位随潮位缓慢(?P<action>抬升|下降)'
    r'(?:以适应(?P<purpose>' + _A + r'))?。')
_ACTION_ANSWER = re.compile(
    r'(?P<subject>' + _A + r')搭在(?P<platform>' + _A + r')上[，,]随(?P<trigger>' + _A + r')缓慢'
    r'(?P<action>抬升|下降)(?:以适应(?P<purpose>' + _A + r'))?')
_LET_SOURCE = re.compile(
    r'(?:(?P<town>' + _A + r')的(?P<prefix_moon>' + _A + r')每(?P<period>[一二三四五六七八九十百0-9]{1,6}天)出现一次。)?'
    r'(?P<moon>' + _A + r')升起后[，,](?P<place>' + _A + r')会投出让人迷失方向的(?P<phenomenon>' + _A + r')'
    r'[，,]所以所有居民都留在室内。'
    r'(?:(?P<official>' + _A + r')只通过屋内广播提醒人们(?P<reminder>' + _A + r')。)?')
_LET_RELATIVE = re.compile(
    r'(?P<people>' + _A + r')担心(?P<moon>' + _A + r')升起后[，,](?P<place>' + _A + r')投出的'
    r'让人迷失方向的(?P<phenomenon>' + _A + r')')
_LET_DIRECT = re.compile(
    r'(?P<moon>' + _A + r')升起后[，,](?P<place>' + _A + r')投出的(?P<phenomenon>' + _A + r')让人迷失方向')


def _safe(values):
    return all(v is None or (isinstance(v, str) and v and not _UNSAFE.search(v)) for v in values)


def _kind(answer):
    if _QUALIFIED.search(answer):
        return None
    if '搭在' in answer and ('随' in answer or '以适应' in answer):
        return 'motion_and_purpose'
    if '让' in answer and any(x in answer for x in ('迷失方向', '担心', '光影')):
        return 'relative_clause_effect'
    return None


def _unique_cited_source(sources, citations, issues):
    if len(citations) != 1 or issues:
        return None
    rows = [s for s in sources if s.get('citation_id', s.get('id')) == citations[0]]
    if len(rows) != 1:
        return None
    source = rows[0]
    if any(not isinstance(source.get(k), str) or not source[k].strip() for k in ('text', 'document_id', 'version_id')):
        return None
    if type(source.get('version_number')) is not int or source['version_number'] <= 0:
        return None
    return source


def _evidence(source, cid):
    return [dict(citation_id=cid, text=source['text'], document_id=source['document_id'],
                 version_id=source['version_id'], version_number=source['version_number'],
                 source_text_sha256=hashlib.sha256(source['text'].encode()).hexdigest())]


def _row(text, kind, supported, source, cid, reason, relation=None, unassessed=None):
    return dict(text=text, kind=kind, state='LIMITED_LEXICAL_SUPPORT' if supported else 'NEED_REVIEW',
                resolved=supported, reason=reason, evidence=_evidence(source, cid) if supported else [],
                relation=relation, unassessed_spans=unassessed or [],
                semantic_acceptance=False, fabrication_judgment=None)


def _motion(answer, source, cid):
    a = _ACTION_ANSWER.fullmatch(answer)
    s = _ACTION_SOURCE.fullmatch(source['text'].strip()) if source else None
    valid = bool(s and _safe(s.groupdict().values()))
    matched = bool(valid and a and a['subject'] == s['venue'] + '摊位'
                   and a['platform'] == s['platform'] and a['trigger'] == '潮位' and a['action'] == s['action'])
    relation = (dict(subject=a['subject'], platform=a['platform'], trigger='潮位', speed='缓慢', action=a['action'],
                     scope='single_venue_stall_motion_only_not_causal_motor_or_inferred_goal') if matched else None)
    motion_text = (a['subject'] + '随' + a['trigger'] + '缓慢' + a['action']) if a else answer
    rows = [_row(motion_text, 'motion_relation', matched, source, cid,
                 'complete_motion_arguments_bound' if matched else 'motion_arguments_or_source_scope_unresolved', relation)]
    if a and a['purpose']:
        goal = bool(matched and s['purpose'] is not None and s['purpose'] == a['purpose'])
        rows.append(_row('以适应' + a['purpose'], 'purpose_relation', goal, source, cid,
                         'purpose_explicit_in_complete_source' if goal else 'purpose_not_explicitly_supported_keep_review',
                         dict(purpose=a['purpose'], scope='explicit_purpose_only') if goal else None))
    return rows


def _effect(answer, source, cid):
    a = _LET_RELATIVE.fullmatch(answer) or _LET_DIRECT.fullmatch(answer)
    s = _LET_SOURCE.fullmatch(source['text'].strip()) if source else None
    valid = bool(s and _safe(s.groupdict().values())
                 and (s['prefix_moon'] is None or s['prefix_moon'] == s['moon']))
    matched = bool(valid and a and all(a[k] == s[k] for k in ('moon', 'place', 'phenomenon')))
    text = a['place'] + '投出的' + a['phenomenon'] + '让人迷失方向' if a else answer
    unassessed = [a.groupdict()['people'] + '担心'] if a and a.groupdict().get('people') else []
    return [_row(text, 'relative_clause_effect', matched, source, cid,
        'literal_effect_and_moon_place_bound' if matched else 'effect_direction_condition_or_source_scope_unresolved',
        dict(condition=a['moon'] + '升起后', place=a['place'], phenomenon=a['phenomenon'], effect='让人迷失方向',
             scope='world_effect_only_not_mental_state_or_protection') if matched else None, unassessed)]


def check_answer_grounding(answer, sources, citations) -> dict:
    result = _base.check_answer_grounding(answer, sources, citations)
    result['version'] = VERSION
    result['implicit_relation_checks'] = []
    if result['status'] != 'NOT_CHECKED':
        return result
    kind = _kind(answer)
    if kind is None:
        return result
    source = _unique_cited_source(sources, result['citations'], result['citation_issues'])
    text = answer.strip().rstrip('。')
    cid = result['citations'][0] if len(result['citations']) == 1 else None
    rows = _motion(text, source, cid) if kind == 'motion_and_purpose' else _effect(text, source, cid)
    result['implicit_relation_checks'] = rows
    for row in rows:
        result['claims'].append({k: row[k] for k in ('text', 'kind', 'state', 'reason', 'evidence')})
    result['status'] = 'NEED_REVIEW' if any(not r['resolved'] for r in rows) else 'TEXT_EVIDENCE_FOUND'
    result['status_expression'] = _base._base._expression(result, sources, result['citations'])
    return result
