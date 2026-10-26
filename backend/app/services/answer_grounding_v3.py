"""Offline v3: inspect explicit independent affirmative conjuncts over v2.

This is limited lexical scope assistance, not semantic acceptance. Whole quoted
unknowns, coordinated objects and conditional antecedents are not globally split.
The frozen v1/v2 code, source selection and human labels remain untouched.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location(
    "_frozen_answer_grounding_v2_for_v3", Path(__file__).with_name("answer_grounding_v2.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

VERSION = "answer-grounding-lexical-v3-independent-conjunct"
_AND = frozenset(("而且", "并且", "且"))
_TOKENS = re.compile(r"而且|并且|且|但是|然而|不过|可是|但|[。；;！？!?\n]")
_CONTRAST = frozenset(("但是", "然而", "不过", "可是", "但"))
_REFUSAL_CONTEXT = re.compile(
    r"(?:无法|不能|尚不能|未能)(?:确定|确认|判断|回答|作答|说明)|不清楚|不知道|不确定|"
    r"(?:资料|原文|来源|信息|证据).{0,8}(?:不足|缺失|不完整|没有说明|未说明|未证实)|"
    r"(?:未|没有)提供.{0,12}(?:资料|信息)")
_QUESTION = re.compile(
    r"是否|能否|是不是|有没有|不清楚|不确定|无从(?:确认|确定)|"
    r"(?:无法|不能|尚不能|未能)(?:确定|确认|判断|保证)|(?:吗|么)$")
_NEGATION = re.compile(
    r"^(?:并非|并不是|不是说|不是|不意味着|并不意味着|不代表)|"
    r"(?:不可能|没有办法|没办法|不|未|没有|从未|不曾|未曾|不能|无法|不会|并不)"
    r"(?:明确|真正|完全|能够|可以|能|可)?(?:保证|确保|承诺|担保|防止|避免|保护|抵御|预防)|"
    r"(?:保证|确保|承诺|担保)(?:不了|不住|不上)")
_PREDICATE = re.compile(
    r"保证|确保|承诺|担保|(?:能够|可以|能|可|会)?(?:防止|避免|预防|阻止|抵御|保护|免受|免于)|"
    r"绝对安全|完全安全|万无一失|百分之百安全|100\s*%安全")
_QUOTE_PAIRS = {"‘": "’", "“": "”", "\"": "\"", "'": "'"}
_BRACKETS = {"（": "）", "(": ")", "[": "]", "【": "】"}


def _visible_scope(text: str) -> str:
    """Keep positions, but hide quoted/bracketed interiors from boundary scans."""
    visible = list(text)
    quotes = []
    brackets = []
    for index, char in enumerate(text):
        escaped = index > 0 and text[index - 1] == "\\"
        if quotes:
            visible[index] = " "
            if char == quotes[-1] and not escaped:
                quotes.pop()
            elif char in _QUOTE_PAIRS and char != quotes[-1] and not escaped:
                quotes.append(_QUOTE_PAIRS[char])
            continue
        if char in _QUOTE_PAIRS and not escaped:
            quotes.append(_QUOTE_PAIRS[char])
            visible[index] = " "
            continue
        if char in _BRACKETS:
            brackets.append(_BRACKETS[char])
            visible[index] = " "
            continue
        if brackets:
            visible[index] = " "
            if char == brackets[-1]:
                brackets.pop()
            continue
    return "".join(visible)


def _boundaries(visible: str):
    """Locate visible boundaries; conditional candidates are retained as ranges."""
    events = []
    for token in _TOKENS.finditer(visible):
        word = token.group()
        if word in _AND or word in _CONTRAST:
            events.append((token.start(), token.end(), "and" if word in _AND else "contrast"))
        else:
            events.append((token.start(), token.end(), "sentence"))
    return events


def _independent_affirmation(text: str):
    visible = _visible_scope(text).strip()
    # Classification considers the first local head; the *entire* candidate
    # remains intact for source comparison, including conditions and qualifiers.
    head = re.split(r"[，,]", visible, maxsplit=1)[0].strip()
    predicate = _PREDICATE.search(head)
    negation_scope = head[:predicate.end() + 2] if predicate else head
    if _NEGATION.search(negation_scope):
        return False, "negative_or_denied_assurance"
    if _QUESTION.search(head):
        return False, "question_or_uncertainty_scope"
    if not predicate:
        return False, "no_explicit_effect_predicate"
    subject = head[:predicate.start()].strip()
    subject = re.sub(r"(?:能够|可以|能|可|会)$", "", subject).strip()
    if re.fullmatch(r"(?:也|还|亦|又|仍|仍然|同样|同时|照样|甚至|并|而)+", subject):
        return False, "adverb_or_modal_without_independent_subject"
    if not subject or not re.fullmatch(r"[\w\u4e00-\u9fff\s-]{1,32}", subject):
        return False, "coordinated_object_or_unresolved_subject"
    if re.match(r"^(?:只要|如果|若|除非|一旦)", subject):
        return False, "conditional_subject_scope_not_resolved"
    return True, "explicit_subject_and_affirmative_effect"


def check_answer_grounding(answer, sources, citations) -> dict:
    result = _base.check_answer_grounding(answer, sources, citations)
    result["version"] = VERSION
    result["independent_conjuncts"] = []
    visible = _visible_scope(answer)
    events = _boundaries(visible)
    clause_start = 0
    covered_until = 0
    for index, (start, end, kind) in enumerate(events):
        if kind != "and":
            clause_start = end
            continue
        if start < covered_until:
            continue
        if not _REFUSAL_CONTEXT.search(visible[clause_start:start]):
            continue
        stop = events[index + 1][0] if index + 1 < len(events) else len(answer)
        candidate = answer[end:stop].strip()
        if "只要" in _visible_scope(candidate):
            # A complete independent conditional relation is one comparison
            # unit, conservatively through the next sentence/contrast. Do not
            # guess where a single character closes the antecedent, and never
            # recheck its internal ANDs as shortened independent assertions.
            stop = next((event_start for event_start, _, event_kind in events[index + 1:]
                         if event_kind != "and"), len(answer))
            candidate = answer[end:stop].strip()
            covered_until = stop
        # Range comes before mood: negative/question promises must not expose
        # an inner condition as a new affirmative world assertion either.
        affirmative, reason = _independent_affirmation(candidate)
        audit = {"text": candidate, "start": end, "end": stop,
                 "scope_reason": reason, "complete_candidate_retained": True}
        if not affirmative:
            audit["state"] = "NOT_ASSERTED" if reason in (
                "negative_or_denied_assurance", "question_or_uncertainty_scope") else "NOT_CHECKED"
            result["independent_conjuncts"].append(audit)
            continue
        checked = _base.check_answer_grounding(candidate, sources, citations)
        audit["state"] = checked["status"]
        result["independent_conjuncts"].append(audit)
        for claim in checked["claims"]:
            if claim["state"] not in ("NEED_REVIEW", "DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT"):
                continue
            if not any(c["state"] == claim["state"] and c["text"] == claim["text"] for c in result["claims"]):
                result["claims"].append(dict(claim, local_scope="independent_affirmative_conjunct",
                                             candidate_start=end, candidate_end=stop))
    needs_review = bool(result["citation_issues"]) or any(c["state"] == "NEED_REVIEW" for c in result["claims"])
    supported = any(c["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for c in result["claims"])
    result["status"] = "NEED_REVIEW" if needs_review else "TEXT_EVIDENCE_FOUND" if supported else "NOT_CHECKED"
    result["limitations"].append("v3 only resolves selected explicit independent conjuncts; complex grammar remains unverified")
    return result
