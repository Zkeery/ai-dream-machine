"""Offline v5: cited whole-clause evidence and explicitly partial local anchors.

A leading reason marker can report a complete source clause. It cannot create
a new relation or drop a condition, exception, subject or negative qualifier.
Local narrative anchors never establish an added causal edge. This finite
helper neither generates answers nor certifies truth, human scores or quality.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import hashlib
import re
import unicodedata

_spec = importlib.util.spec_from_file_location(
    "_frozen_answer_grounding_v4_for_v5", Path(__file__).with_name("answer_grounding_v4.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

VERSION = "answer-grounding-lexical-v5-scoped-text-evidence"
_REASON = re.compile(r"^(?:因为|由于)")
_SENTENCE = re.compile(r"[^。；;！？!?\n]+[。；;！？!?]?")
_SOURCE_QUALIFICATION = re.compile(
    r"有人(?:说|称|问)|据称|传闻|传言|未经证实|尚未证实|未获证实|"
    r"无法(?:确定|确认)|尚不能|不属实|不正确|不真实|并非|不是说|"
    r"不代表|不意味着|是否|能否|[‘’“”\"']")
_LIMITING_SCOPE = re.compile(
    r"如果|只要|除非|一旦|仅当|只有|仅在|只在|但|然而|不过|"
    r"例外|除外|除了|是否|能否|只是|并不|不属实|传闻|传言")
_IDENTITY = re.compile(
    r"(?:^|\n)\s*身份与称谓[：:]\s*(?P<subject>[^，,。；;：:\n]{1,32}?)是[^\n]*")
_HISTORY = re.compile(r"(?:^|\n)\s*经历与行为[：:]\s*(?P<paragraph>[^\n]+)")
_CONSEQUENCE = re.compile(r"^(?:因此|所以|因而)")
_INDEPENDENT_PERMISSION = re.compile(
    r"^[\w\u4e00-\u9fff]{1,16}(?:没有|不具备|无)[\w\u4e00-\u9fff]{1,12}权(?:利|限)?$")
_LATER_RULE_REFERENCE = re.compile(
    r"[。；;]\s*(?:这条规则|上述规则|这个说法|该规则|这一规则|此规则|这一说法|上述说法)")
_BINDING_FIELDS = ("text", "document_id", "version_id", "version_number", "is_constraint", "chunk_id", "fixture_document")


def _plain(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).rstrip("。；;")


def _eligible_sources(sources, citations):
    """A cited ID must have exactly one text *and* version/role binding."""
    by_id = {}
    for source in sources:
        identifier = source.get("citation_id", source.get("id"))
        if identifier is not None and isinstance(source.get("text"), str):
            by_id.setdefault(str(identifier), []).append(source)
    eligible = []
    for identifier in citations:
        rows = by_id.get(identifier, [])
        signatures = {tuple((field, repr(row.get(field))) for field in _BINDING_FIELDS) for row in rows}
        if len(signatures) == 1:
            eligible.append((identifier, rows[0]))
    return eligible


def _evidence(identifier, source, text, **extra):
    return dict({"citation_id": identifier, "text": text,
                 "document_id": source.get("document_id"), "version_id": source.get("version_id"),
                 "version_number": source.get("version_number"),
                 "source_text_sha256": hashlib.sha256(source["text"].encode("utf-8")).hexdigest()}, **extra)


def _literal_reason(claim, eligible):
    marker = _REASON.match(claim)
    if not marker:
        return None
    body = claim[marker.end():].strip()
    for identifier, source in eligible:
        # Quoted, disputed or uncertain reporting is not an asserted source
        # rule. Keep the complete document scope; do not search inside quotes.
        if _SOURCE_QUALIFICATION.search(source["text"]) or _LATER_RULE_REFERENCE.search(source["text"]):
            continue
        for match in _SENTENCE.finditer(source["text"]):
            raw = match.group().strip()
            if raw.endswith(("?", "？", "!", "！")):
                continue
            sentence = raw.rstrip("。；;").strip()
            candidates = [(sentence, [])]
            # The sole prefix form is a rule followed by an explicit separate
            # subject's lack of permission. Unknown tails, including an
            # unrecognized retraction, are never assumed independent.
            if not _LIMITING_SCOPE.search(sentence):
                parts = re.split(r"[，,]", sentence)
                if len(parts) == 2 and _INDEPENDENT_PERMISSION.fullmatch(parts[1].strip()):
                    first = parts[0]
                    candidates.append((first, ["independent_source_tail_retained_as_evidence"]))
            for candidate, variants in candidates:
                if _plain(body) != _plain(candidate):
                    continue
                return {"text": claim, "state": "LIMITED_LEXICAL_SUPPORT", "resolved": True,
                        "evidence": [_evidence(identifier, source, raw, matched_text=candidate)],
                        "controlled_variants": ["leading_reason_report_marker"] + variants,
                        "unresolved_relations": []}
    return None


def _partial_history(answer, claim, eligible):
    """Record three literal anchors; do not remove or support a new therefore."""
    answer_start = re.split(r"[，,。；;]", answer, maxsplit=1)[0].strip()
    answer_parts = re.split(r"[，,]", claim)
    if len(answer_parts) != 2:
        return None
    connector = _CONSEQUENCE.match(answer_parts[1])
    if not connector:
        return None
    answer_behavior = answer_parts[1][connector.end():]
    for identifier, source in eligible:
        identity = list(_IDENTITY.finditer(source["text"]))
        histories = list(_HISTORY.finditer(source["text"]))
        if len(identity) != 1 or len(histories) != 1 or identity[0].start() >= histories[0].start():
            continue
        paragraph = histories[0].group("paragraph").strip()
        if _SOURCE_QUALIFICATION.search(paragraph) or _LIMITING_SCOPE.search(paragraph):
            continue
        experience, separator, relation = paragraph.partition("。")
        if not separator or not experience.startswith(("他", "她")):
            continue
        subject = identity[0].group("subject")
        if _plain(answer_start) != _plain(subject + experience[1:]):
            continue
        source_parts = re.split(r"[，,]", relation.rstrip("。"))
        if len(source_parts) < 2 or not source_parts[0].startswith("这段"):
            continue
        if _plain(answer_parts[0]) != _plain(source_parts[0]) or _plain(answer_behavior) != _plain(source_parts[1]):
            continue
        evidence = [
            _evidence(identifier, source, identity[0].group().strip(), anchor_role="subject"),
            _evidence(identifier, source, experience + "。", anchor_role="experience"),
            _evidence(identifier, source, relation, anchor_role="relation_and_behavior"),
        ]
        return {"text": claim, "state": "PARTIAL_TEXT_SUPPORT", "resolved": False,
                "evidence": evidence,
                "controlled_variants": ["explicit_identity_anchor_for_pronoun", "behavior_tail_not_certified"],
                "unresolved_relations": ["新增连接“" + connector.group() + "”的因果边未获原文直接支持：" + claim]}
    return None


def check_answer_grounding(answer, sources, citations) -> dict:
    result = _base.check_answer_grounding(answer, sources, citations)
    result["version"] = VERSION
    result["paraphrase_checks"] = []
    eligible = _eligible_sources(sources, result["citations"])
    for claim in result["claims"]:
        if claim["state"] != "NEED_REVIEW":
            continue
        check = _literal_reason(claim["text"], eligible)
        if check:
            claim["previous_state"] = claim["state"]
            claim["state"] = check["state"]
            claim["reason"] = "complete_cited_reason_clause_with_scope_preserved"
            claim["evidence"] = check["evidence"]
        else:
            check = _partial_history(answer, claim["text"], eligible)
        result["paraphrase_checks"].append(check or {
            "text": claim["text"], "state": "NEED_REVIEW", "resolved": False,
            "evidence": [], "controlled_variants": [], "unresolved_relations": [claim["text"]]})
    needs_review = bool(result["citation_issues"]) or any(claim["state"] == "NEED_REVIEW" for claim in result["claims"])
    supported = any(claim["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for claim in result["claims"])
    result["status"] = "NEED_REVIEW" if needs_review else "TEXT_EVIDENCE_FOUND" if supported else "NOT_CHECKED"
    result["limitations"].append("v5 local text evidence and partial narrative anchors do not establish an added causal edge or whole-answer correctness")
    return result
