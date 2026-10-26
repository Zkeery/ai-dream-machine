"""Opt-in v12: three bounded condition/rule restatements over frozen v11.

Literal complete source grammars only. Text support never certifies a whole
answer, general necessary/sufficient logic, world existence, or capabilities.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location(
    '_frozen_grounding_v11_for_v12', Path(__file__).with_name('answer_grounding_v11.py'))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
VERSION = 'answer-grounding-v12-bounded-condition-and-rule'
_A = r'[^\s，,。；;！？!?：:、“”‘’\"\x27]{1,40}?'
_UNSAFE = re.compile(r'如果|只要|只有|除非|一旦|仅|任何|所有|或|和|与|并|且|不|未|无|可能|假设|据称|传闻|这|该|其|因为|所以|导致|因此')
_QUALIFIED = re.compile(r'可能|无法确定|不能确认|不能断言|假设|并非|能否|是否|[？?！!“”‘’\"\x27]')
_OP_SOURCE = re.compile(
    r'只有在(?P<subject>' + _A + r')(?P<ready>已经|已)(?P<state>' + _A + r')时[，,]'
    r'按下(?P<key>' + _A + r')才会(?P<effect>' + _A + r')。'
    r'(?P=subject)未(?P=state)时[，,](?P=key)无效。')
_OP_ANSWER = re.compile(
    r'(?P<condition>' + _A + r')时[，,]按下(?P<key>' + _A + r')会(?P<effect>' + _A + r')[；;]'
    r'(?P<negative>' + _A + r')时[，,](?P<key2>' + _A + r')不能(?P<effect2>' + _A + r')')
_TRAFFIC_SOURCE = re.compile(r'本片所有交通画面只允许出现(?P<a>' + _A + r')和(?P<b>' + _A + r')。')
_TRAFFIC_ANSWER = re.compile(r'骑(?P<excluded>' + _A + r')送货的镜头不符合本片交通画面要求[，,]'
                             r'可以改为用(?P<replacement>' + _A + r')送货')
_VEHICLES = frozenset(('手推车', '缆车', '摩托车'))
_TICKET_SOURCE = re.compile(r'入场凭证只接受(?P<a>' + _A + r')或(?P<b>' + _A + r')。'
                            r'(?P<excluded>' + _A + r')不能替代入场凭证。')
_TICKET_ANSWER = re.compile(r'可以用(?P<a>' + _A + r')或(?P<b>' + _A + r')入场[，,]'
                            r'(?P<excluded>' + _A + r')不能替代这两种凭证')


def _safe(values):
    return all(isinstance(v, str) and v and not _UNSAFE.search(v) for v in values)


def _kind(answer, sources):
    if _QUALIFIED.search(answer):
        return None
    if '按下' in answer and '键' in answer:
        return 'two_state_operation'
    if ('本片交通画面要求' in answer or
            ('不存在' in answer and any('本片' in s.get('text', '') and '交通画面' in s.get('text', '')
                                    for s in sources))):
        return 'film_depiction_replacement'
    if (_TICKET_ANSWER.fullmatch(answer.strip().rstrip('。')) or
            (('入场' in answer or '凭证' in answer or '替代' in answer)
             and any('入场凭证' in s.get('text', '') for s in sources))):
        return 'admission_credentials'
    return None


def _unique_source(sources, citations, issues):
    if len(sources) != 1 or len(citations) != 1 or issues:
        return None
    source = sources[0]
    if source.get('citation_id', source.get('id')) != citations[0]:
        return None
    if any(not isinstance(source.get(k), str) or not source[k].strip() for k in ('text', 'document_id', 'version_id')):
        return None
    if type(source.get('version_number')) is not int or source['version_number'] <= 0:
        return None
    return source


def _support(kind, answer, source):
    body = source['text'].strip()
    if kind == 'two_state_operation':
        s, a = _OP_SOURCE.fullmatch(body), _OP_ANSWER.fullmatch(answer)
        if not s or not a or not _safe(s.groupdict().values()):
            return None
        data, reply = s.groupdict(), a.groupdict()
        condition = data['subject'] + data['ready'] + data['state']
        negatives = ('未' + data['state'], data['subject'] + '未' + data['state'])
        if (reply['condition'] == condition and reply['negative'] in negatives
                and reply['key'] == reply['key2'] == data['key']
                and reply['effect'] == reply['effect2'] == data['effect']):
            return dict(arguments=data, scope='this_complete_two_state_operating_rule_only')
    elif kind == 'film_depiction_replacement':
        s, a = _TRAFFIC_SOURCE.fullmatch(body), _TRAFFIC_ANSWER.fullmatch(answer)
        if not s or not a:
            return None
        data, reply = s.groupdict(), a.groupdict()
        allowed = {data['a'], data['b']}
        if (len(allowed) == 2 and allowed <= _VEHICLES and reply['excluded'] in _VEHICLES - allowed
                and reply['replacement'] in allowed):
            return dict(arguments=dict(**data, **reply),
                        scope='film_depiction_and_literal_allowed_member_only_not_world_existence_or_vehicle_capability')
    else:
        s, a = _TICKET_SOURCE.fullmatch(body), _TICKET_ANSWER.fullmatch(answer)
        if not s or not a or not _safe(s.groupdict().values()):
            return None
        data, reply = s.groupdict(), a.groupdict()
        allowed = {data['a'], data['b']}
        if (len(allowed) == 2 and data['excluded'] not in allowed
                and {reply['a'], reply['b']} == allowed and reply['excluded'] == data['excluded']):
            return dict(arguments=data, scope='literal_two_credentials_qualifiers_and_non_substitution_only')
    return None


def check_answer_grounding(answer, sources, citations) -> dict:
    result = _base.check_answer_grounding(answer, sources, citations)
    result['version'] = VERSION
    result['bounded_rule_checks'] = []
    # Never rejudge or clear an already recognized risk/support from v11.
    if result['status'] != 'NOT_CHECKED':
        return result
    kind = _kind(answer, sources)
    if kind is None:
        return result
    text = answer.strip().rstrip('。')
    source = _unique_source(sources, result['citations'], result['citation_issues'])
    support = _support(kind, text, source) if source is not None else None
    evidence = []
    if support:
        evidence = [dict(citation_id=result['citations'][0], text=source['text'], document_id=source['document_id'],
                         version_id=source['version_id'], version_number=source['version_number'],
                         source_text_sha256=hashlib.sha256(source['text'].encode()).hexdigest())]
    state = 'LIMITED_LEXICAL_SUPPORT' if support else 'NEED_REVIEW'
    reason = 'complete_literal_rule_and_arguments_bound' if support else 'rule_condition_or_scope_not_fully_bound'
    result['bounded_rule_checks'].append(dict(text=text, kind=kind, state=state, resolved=bool(support),
        reason=reason, evidence=evidence, relation=support, semantic_acceptance=False, fabrication_judgment=None))
    result['claims'].append(dict(text=text, kind='bounded_condition_or_rule', state=state, reason=reason, evidence=evidence))
    result['status'] = 'TEXT_EVIDENCE_FOUND' if support else 'NEED_REVIEW'
    result['status_expression'] = _base._expression(result, sources, result['citations'])
    return result
