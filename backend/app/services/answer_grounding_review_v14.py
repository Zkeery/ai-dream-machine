"""Exact R03 context human disposition after the unchanged v13 checker.

Not a purpose-entailment rule. No checker calls or mutations, old-score
decisions, model clients, file access, environment access, or network.
"""
from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json

VERSION = 'answer-grounding-human-disposition-v14-r03-context'
REVIEW_SHA256 = '2c4644cafb948352f66613bbc61d1f0aa9c7853f1646c5e5d147234739611761'
BASELINE_SHA256 = '1239688791bfe988c4b2de5edc88b6ee0999a325fe682b23d3d0c3c9d8886698'
ROUND_ID = 'rag-r03-purpose-review-20261026-r1'
TARGET = 'R03-with_rag'


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _json_sha(value):
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())


def _trusted_json(data, expected):
    if not isinstance(data, bytes) or _sha(data) != expected:
        return None
    return json.loads(data)


def annotate_review(record, sources, review_bytes=None, baseline_bytes=None):
    """Return a separate exact-context association. Caller verifies file bytes.

The complete record includes the actual question and frozen v13 result.
Actual answer/source hashes are recomputed; stale declared hashes cannot
borrow an accepted disposition. Old human_comparison is intentionally ignored.
"""
    check = record.get('assistant_check') if isinstance(record, dict) else None
    automatic = check.get('status') if isinstance(check, dict) else None
    result = dict(schema=VERSION, status='NEED_REVIEW', reason='no_exact_trusted_human_disposition',
                  automatic_status=automatic, automatic_check_unchanged=True, human_acceptance_only=True,
                  generic_rule_changed=False, semantic_acceptance=False, original_human_labels_changed=False,
                  new_fabrication_or_used_labels_inferred=False)
    if automatic in ('NOT_CHECKED', 'TEXT_EVIDENCE_FOUND'):
        result.update(status='NOT_APPLICABLE', reason='no_automatic_review_hint')
        return result
    if automatic != 'NEED_REVIEW':
        result['reason'] = 'missing_or_unknown_automatic_status'
        return result
    try:
        review = _trusted_json(review_bytes, REVIEW_SHA256)
        envelope = _trusted_json(baseline_bytes, BASELINE_SHA256)
        if review is None or envelope is None:
            result['reason'] = 'missing_or_untrusted_snapshot'
            return result
        baseline = json.loads(gzip.decompress(base64.b64decode(envelope['full_result_gzip_base64'])))
        if (review['review_round_id'] != ROUND_ID or review['status'] != '已通过'
                or review['active_question_id'] is not None or review['completed_judgment_count'] != 3
                or baseline['status'] != 'OFFLINE_COMPARE_COMPLETE'):
            return result
        if record.get('item') != TARGET:
            result['reason'] = 'no_human_disposition_for_this_item'
            return result
        old = [r for r in baseline['records'] if r['item'] == TARGET]
        if len(old) != 1 or not isinstance(sources, list):
            return result
        original = old[0]
        if _json_sha({k:v for k,v in record.items() if k != 'human_comparison'}) != _json_sha({
                k:v for k,v in original.items() if k != 'human_comparison'}):
            result['reason'] = 'original_record_or_question_context_binding_mismatch'
            return result
        bound = review['material_binding']
        if (any(_json_sha(record.get(k)) != _json_sha(v) for k,v in bound.items())
                or _sha(record['answer'].encode()) != bound['answer_sha256']
                or _json_sha(sources) != bound['provided_sources_sha256']):
            result['reason'] = 'actual_answer_source_or_version_binding_mismatch'
            return result
        descriptors = [{k:v for k,v in s.items() if k != 'text'} |
                       dict(text_sha256=_sha(s['text'].encode())) for s in sources]
        if _json_sha(descriptors) != _json_sha(original['sources']):
            result['reason'] = 'source_descriptor_type_or_version_mismatch'
            return result
        questions, judgments = review['questions'], review['new_judgments']
        expected_ids = ['R03P-01', 'R03P-02', 'R03P-03']
        expected_answers = ['可合理推断', '接受', '可接受']
        fields = ('question_id', 'field', 'answer', 'normalized_value', 'reason', 'user_message', 'user_question_item_id')
        if (len(questions) != 3 or len(judgments) != 3 or review['sequence'] != expected_ids
                or [q['question_id'] for q in questions] != expected_ids
                or [j['question_id'] for j in judgments] != expected_ids
                or [j['answer'] for j in judgments] != expected_answers
                or any(q['status'] != '已记录' or _json_sha({k:q[k] for k in fields}) != _json_sha({
                    k:j[k] for k in fields}) for q,j in zip(questions, judgments))):
            return result
        result.update(status='HUMAN_ACCEPTED_THIS_VERSION', reason='three_explicit_judgments_and_exact_question_context',
            label='该原问题、回答与来源版本已由人工接受；机器目的提示仍保留',
            review_binding=dict(round_id=ROUND_ID, review_sha256=REVIEW_SHA256, baseline_sha256=BASELINE_SHA256,
                item=TARGET, question_ids=expected_ids, judgments=copy.deepcopy(judgments),
                scope='this_exact_original_query_answer_source_and_v13_record_only_not_general_purpose_entailment'),
            machine_whole_answer=copy.deepcopy(record['assistant_check']['status_expression']['whole_answer']))
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, OSError):
        result['reason'] = 'malformed_binding_no_disposition_applied'
    return result
