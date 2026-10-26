"""Exact, opt-in human dispositions after the frozen lexical checker.

This is not a new entailment rule. Nothing here calls or alters a checker,
changes its status/evidence, or reads old human scores as expected answers.
The two complete trusted snapshots and every original input binding must
match. New text, sources, versions or contexts always need a fresh review.
No file, environment, provider or network access occurs in this module.
"""
from __future__ import annotations

import copy
import hashlib
import json

VERSION = "answer-grounding-human-disposition-v7"
REVIEW_SHA256 = "6bd9b8da73d25da5340af4dd1139386363b117f01219c748755bdfc9dbdae60b"
BASELINE_SHA256 = "9bbd5eadd394e2975998910233901f9f83d72697528407e314b9b09952abda5e"
ROUND_ID = "rag-human-adjudication-20261025-r1"
TARGETS = frozenset(("R07-with_rag", "H01-with_rag", "R10-with_rag"))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _json_sha(value):
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8"))


def _trusted_json(data, digest):
    if not isinstance(data, bytes) or _sha(data) != digest:
        return None
    return json.loads(data)


def annotate_review(record, sources, review_bytes=None, baseline_bytes=None):
    """Return a separate disposition; supplied objects are never modified.

    The offline caller binds physical payload/response hashes before calling.
    We independently bind the actual answer and complete source objects, so
    stale declared hashes cannot hide changes to the supplied evidence.
    """
    automatic = record.get("assistant_check", {}).get("status") if isinstance(record, dict) else None
    result = {
        "schema": VERSION,
        "status": "NEED_REVIEW",
        "reason": "no_exact_trusted_human_disposition",
        "automatic_status": automatic,
        "automatic_check_unchanged": True,
        "human_acceptance_only": True,
        "generic_rule_changed": False,
        "semantic_acceptance": False,
        "original_human_labels_changed": False,
    }
    if automatic in ("NOT_CHECKED", "TEXT_EVIDENCE_FOUND"):
        result.update(status="NOT_APPLICABLE", reason="no_automatic_review_hint")
        return result
    if automatic != "NEED_REVIEW":
        result["reason"] = "missing_or_unknown_automatic_status"
        return result
    try:
        review = _trusted_json(review_bytes, REVIEW_SHA256)
        baseline = _trusted_json(baseline_bytes, BASELINE_SHA256)
        if review is None or baseline is None:
            result["reason"] = "missing_or_untrusted_snapshot"
            return result
        if (review["review_round_id"] != ROUND_ID or review["status"] != "completed"
                or review["active_question_id"] is not None
                or review["source_baseline"]["sha256"] != BASELINE_SHA256):
            return result
        item = record.get("item")
        if item not in TARGETS:
            result["reason"] = "no_human_disposition_for_this_item"
            return result
        cases = [c for c in review["cases"] if c["item"] == item]
        old = [r for r in baseline["records"] if r["item"] == item]
        if len(cases) != 1 or len(old) != 1:
            return result
        case, original = cases[0], old[0]
        # Original scores are not an input to this new review disposition.
        # Every other original field, including the full raw checker output,
        # remains exactly bound to this known version.
        excluded = {"human_comparison"}
        # Canonical JSON distinguishes bool/int and int/float versions.
        # Python equality alone would equate True/1 and timestamp.0/timestamp.
        if (_json_sha({k: v for k, v in record.items() if k not in excluded})
                != _json_sha({k: v for k, v in original.items() if k not in excluded})):
            result["reason"] = "original_record_binding_mismatch"
            return result
        if (record["answer"] != case["answer"] or record["query"] != case["query"]
                or record["citations"] != case["citations"] or record["group"] != "with_rag"
                or _sha(record["answer"].encode("utf-8")) != case["binding"]["answer_sha256"]
                or _json_sha(sources) != case["binding"]["provided_sources_sha256"]):
            result["reason"] = "actual_answer_or_sources_binding_mismatch"
            return result
        descriptors = [{k: v for k, v in source.items() if k != "text"}
                       | {"text_sha256": _sha(source["text"].encode("utf-8"))} for source in sources]
        if descriptors != original["sources"]:
            result["reason"] = "source_descriptor_binding_mismatch"
            return result
        for key, value in case["binding"].items():
            if record.get(key) != value:
                result["reason"] = "review_version_binding_mismatch"
                return result
        questions = case["review_questions"]
        judgments = case["new_judgments"]
        expected_ids = [case["case_id"] + "-0" + str(i) for i in (1, 2, 3)]
        if (case["review_status"] != "completed" or len(questions) != 3 or len(judgments) != 3
                or [q["question_id"] for q in questions] != expected_ids
                or [j["question_id"] for j in judgments] != expected_ids
                or any(q["status"] != "answered" or q["answer"] != j["answer"]
                       for q, j in zip(questions, judgments))
                or questions[0]["answer"] not in ("原文明示", "可合理推断")
                or questions[1]["answer"] != "接受" or questions[2]["answer"] != "可接受"):
            return result
        scope = ("original_creative_request_and_traffic_depiction_only" if case["case_id"] == "R10"
                 else "this_exact_original_query_answer_and_source_versions_only")
        result.update(
            status="HUMAN_ACCEPTED_THIS_VERSION", reason="nine_judgments_and_exact_original_bindings",
            review_binding={"round_id": ROUND_ID, "review_sha256": REVIEW_SHA256,
                            "baseline_sha256": BASELINE_SHA256,
                            "case_id": case["case_id"], "item": item,
                            "question_ids": expected_ids, "judgments": copy.deepcopy(judgments),
                            "scope": scope})
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        result["reason"] = "malformed_binding_no_disposition_applied"
    return result
