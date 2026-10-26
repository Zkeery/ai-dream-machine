"""Pure, optional answer-level review hints; never an acceptance or refusal gate.

This limited lexical check compares a *whole stated relation* with one cited
source sentence. It does not join words across sentences/documents, infer a
mechanism, judge source truth, or prove general semantic entailment. A mismatch
means NEED_REVIEW, not fabrication. No flag means NOT_CHECKED, not correctness.
The module imports only the standard library and is not wired into production.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence

VERSION = "answer-grounding-lexical-v1"
_RELATION = re.compile(
    r"以防|防止|避免|预防|阻止|抵御|防护|保护|免于|免受|"
    r"不会.{0,12}(?:受|遭)|不受|绝对安全|完全安全|万无一失|"
    r"百分之百|100\s*%|保证|确保|永远不会|"
    r"只要.{1,40}就|因为|由于|因此|所以|导致|因而|使得|使(?!用)|"
    r"\b(?:prevent\w*|protect\w*|guarantee\w*|because|therefore|always safe)\b",
    re.I,
)
_GUARANTEE = re.compile(
    r"绝对安全|完全安全|万无一失|百分之百|100\s*%|不受任何|"
    r"不会受到任何|永远不会|保证|确保|\b(?:guarantee\w*|always safe)\b", re.I
)
_NONASSERTION = re.compile(
    r"(?:资料|材料|来源|原文).{0,6}(?:没有|未|不).{0,4}(?:说明|证实|支持|提到|记载)|"
    r"无法(?:确定|确认|保证)|不能(?:确定|确认|保证|防止|避免)|尚不清楚|"
    r"是否|能否|不代表|不意味着|并非|不是说|"
    r"\b(?:unknown|not established|not confirmed|does not prove|cannot confirm)\b", re.I
)
_CLAUSE_BOUNDARY = re.compile(r"[。；;！？!?\n]+|(?:但是|然而|不过|可是|但)")
_CITATION = re.compile(r"\[(?:K\d+|[A-Za-z][A-Za-z0-9_-]*)\]")
# Explicit, small lexical variants only. No entity, object or mechanism aliases.
_VARIANTS = (("关好", "关闭"), ("合上", "关闭"), ("能够", "可"),
             ("可以", "可"), ("能防止", "可防止"), ("避免", "防止"),
             ("预防", "防止"))


def _canonical(text: str) -> str:
    text = _CITATION.sub("", unicodedata.normalize("NFKC", text).lower())
    text = re.sub(r"\s+|[，,。；;：:、\"'“”‘’（）()]", "", text)
    text = re.sub(r"^(?:并且|且)", "", text)
    for old, new in _VARIANTS:
        text = text.replace(old, new)
    return text


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in _CLAUSE_BOUNDARY.split(text) if part.strip()]


def _candidate(text: str) -> str:
    """Keep conditional antecedents; isolate a final protective assertion only."""
    match = _RELATION.search(text)
    if not match or "只要" in text:
        return text
    comma = max(text.rfind("，", 0, match.start()), text.rfind(",", 0, match.start()))
    # Splitting is for locating the risk span only, never for stitching support.
    return text[comma + 1:].strip() if comma >= 0 else text


def _is_nonassertion(text: str) -> bool:
    """A later disclaimer about something else cannot retract a relation."""
    qualification = _NONASSERTION.search(text)
    relation = _RELATION.search(text)
    if not qualification:
        return False
    if not relation or qualification.start() <= relation.start():
        return True
    between = text[relation.start():qualification.start()]
    if re.search(r"[，,]", between):
        return False
    # Postposed uncertainty must attach to this relation explicitly. Unknown
    # attachment remains reviewable; it does not establish a false assertion.
    return qualification.group() in ("是否", "能否") or bool(re.search(
        r"(?:的效果|的作用|的机制|这一说法|这项效果)\s*$", text[:qualification.start()]))


def check_answer_grounding(
    answer: str, sources: Sequence[Mapping], citations: Sequence[str]
) -> dict:
    """Return review hints while preserving the answer and all cited facts.

    Source IDs come from citation_id (or id). Only explicitly cited sources
    count. Identical complete relations can receive DIRECT_TEXT_SUPPORT;
    controlled lexical variants receive LIMITED_LEXICAL_SUPPORT. Other
    paraphrases, contradictory citations and absent direct support need human
    review. Nothing is scored, deleted, rewritten, rejected or called remotely.
    """
    if not isinstance(answer, str):
        raise TypeError("answer must be a string")
    if isinstance(sources, (str, bytes)) or isinstance(citations, (str, bytes)):
        raise TypeError("sources and citations must be sequences")
    cited = list(dict.fromkeys(str(item) for item in citations))
    by_id: dict[str, list[str]] = {}
    issues = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise TypeError("each source must be a mapping")
        identifier = source.get("citation_id", source.get("id"))
        text = source.get("text")
        if identifier is not None and isinstance(text, str):
            by_id.setdefault(str(identifier), []).append(text)
    eligible = []
    for identifier in cited:
        texts = list(dict.fromkeys(by_id.get(identifier, [])))
        if len(texts) != 1:
            issues.append({"citation_id": identifier, "reason":
                           "unknown_citation" if not texts else "ambiguous_citation"})
        else:
            eligible.append((identifier, texts[0]))
    claims = []
    for sentence in _clauses(answer):
        if not _RELATION.search(sentence):
            continue
        claim = _candidate(sentence)
        # A disclaimer in one clause must not hide an assertion in the next.
        # For a conditional guarantee, retain its entire antecedent.
        if "只要" in claim:
            prior, marker, following = claim.partition("只要")
            if marker and _NONASSERTION.search(prior) and prior.endswith(("，", ",")):
                claim = marker + following
        if _is_nonassertion(claim):
            claims.append({"text": claim, "state": "NOT_ASSERTED",
                           "reason": "explicit_question_or_uncertainty_not_a_positive_guarantee",
                           "evidence": []})
            continue
        evidence = []
        for identifier, body in eligible:
            for source_sentence in _clauses(body):
                if _is_nonassertion(source_sentence) or not _RELATION.search(source_sentence):
                    continue
                # Equality, not bag-of-words/substring support: conditions and
                # scope must survive; separate facts cannot create a relation.
                if _canonical(claim) == _canonical(source_sentence):
                    evidence.append({"citation_id": identifier, "text": source_sentence,
                                     "match": "literal" if claim == source_sentence else
                                     "controlled_lexical_variant"})
        claims.append({"text": claim,
                       "kind": "absolute_guarantee" if _GUARANTEE.search(claim) else "causal_or_protective_relation",
                       "state": ("DIRECT_TEXT_SUPPORT" if any(e["match"] == "literal" for e in evidence)
                                 else "LIMITED_LEXICAL_SUPPORT" if evidence else "NEED_REVIEW"),
                       "reason": "whole_relation_in_one_cited_sentence" if evidence else
                       "no_direct_text_support_or_paraphrase_not_resolved",
                       "evidence": evidence})
    supported = [c for c in claims if c["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT")]
    needs_review = bool(issues) or any(c["state"] == "NEED_REVIEW" for c in claims)
    return {"version": VERSION, "status": "NEED_REVIEW" if needs_review else
            "TEXT_EVIDENCE_FOUND" if supported else "NOT_CHECKED",
            "scope": "offline_auxiliary_lexical_relations_only_not_semantic_acceptance",
            "answer": answer, "citations": cited, "claims": claims,
            "citation_issues": issues, "semantic_acceptance": False,
            "automatic_refusal": False, "production_integrated": False,
            "limitations": ["No flag does not certify factual accuracy",
                            "Unrecognized wording or indirect causality can be missed",
                            "Controlled variants do not prove unrestricted semantic equivalence",
                            "Source truth, authority and contradictions need separate review"]}
