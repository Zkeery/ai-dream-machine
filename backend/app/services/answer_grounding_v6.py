"""Offline v6: one complete, explicit causal pair with literal arguments.

The finite connector set is {because/due to} x {so/therefore/consequently} in
the Chinese forms below. Both arguments remain exact text. Whole source text
must be one asserted pair: no sentence clipping, transitivity, world-existence
inference, nested relation or conditional-scope rewriting is performed.
The frozen helper's other hints and metadata stay intact. This remains an
optional lexical hint, never semantic acceptance or an automatic refusal gate.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location(
    "_frozen_answer_grounding_v5_for_v6", Path(__file__).with_name("answer_grounding_v5.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

VERSION = "answer-grounding-lexical-v6-explicit-causal-pair"
_PAIR = re.compile(
    r"(?P<cause_operator>因为|由于)(?P<cause>[^，,。；;！？!?\n]+)[，,]"
    r"(?P<effect_operator>所以|因此|因而)(?P<effect>[^，,。；;！？!?\n]+)")
_NESTED_OR_CONDITIONAL = re.compile(
    r"因为|由于|所以|因此|因而|导致|造成|引发|致使|使得|使(?!用)|"
    r"如果|只要|除非|一旦|仅当|只有|仅在|只在")


def _flat_pair(text):
    # Only surrounding whitespace and one optional terminal period are
    # syntax. Argument characters, including negation/time/quantity, survive.
    body = text.strip()
    if body.endswith(("。", ".")):
        body = body[:-1]
    match = _PAIR.fullmatch(body)
    if not match:
        return None
    cause, effect = match.group("cause"), match.group("effect")
    if not cause.strip() or not effect.strip():
        return None
    if _NESTED_OR_CONDITIONAL.search(cause) or _NESTED_OR_CONDITIONAL.search(effect):
        return None
    if _base._SOURCE_QUALIFICATION.search(text):
        return None
    return {"cause": cause, "effect": effect,
            "connectors": [match.group("cause_operator"), match.group("effect_operator")]}


def _match_pair(claim, eligible):
    answer_pair = _flat_pair(claim)
    if not answer_pair:
        return None
    for identifier, source in eligible:
        # Parse the complete provided body, including any trailing scope or
        # retraction. No title, sentence, quotation or extra tail is removed.
        source_pair = _flat_pair(source["text"])
        if not source_pair or answer_pair["cause"] != source_pair["cause"] or answer_pair["effect"] != source_pair["effect"]:
            continue
        evidence = _base._evidence(
            identifier, source, source["text"], cause=answer_pair["cause"], effect=answer_pair["effect"],
            answer_connectors=answer_pair["connectors"], source_connectors=source_pair["connectors"])
        return {"text": claim, "state": "LIMITED_LEXICAL_SUPPORT", "resolved": True,
                "evidence": [evidence], "controlled_variants": ["explicit_causal_connector_pair"],
                "unresolved_relations": []}
    return None


def check_answer_grounding(answer, sources, citations) -> dict:
    result = _base.check_answer_grounding(answer, sources, citations)
    result["version"] = VERSION
    eligible = _base._eligible_sources(sources, result["citations"])
    for claim in result["claims"]:
        if claim["state"] != "NEED_REVIEW":
            continue
        supported = _match_pair(claim["text"], eligible)
        if not supported:
            continue
        claim["previous_state"] = claim["state"]
        claim["state"] = supported["state"]
        claim["reason"] = "same_explicit_flat_causal_pair_with_literal_arguments"
        claim["evidence"] = supported["evidence"]
        # Replace the prior unresolved record; do not append a contradictory
        # second interpretation of the same risk span.
        found = False
        for index, row in enumerate(result["paraphrase_checks"]):
            if row["text"] == claim["text"]:
                result["paraphrase_checks"][index] = supported
                found = True
        if not found:
            result["paraphrase_checks"].append(supported)
    needs_review = bool(result["citation_issues"]) or any(claim["state"] == "NEED_REVIEW" for claim in result["claims"])
    has_support = any(claim["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for claim in result["claims"])
    result["status"] = "NEED_REVIEW" if needs_review else "TEXT_EVIDENCE_FOUND" if has_support else "NOT_CHECKED"
    return result
