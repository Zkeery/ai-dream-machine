"""Opt-in offline v9: bounded, whole-source multisentence evidence over v8.

Finite grammars only: literal cause/effect arguments, one explicitly bounded
tail, or one two-edge blockage chain. No source clipping, lookup of reviewed
case IDs, human labels, semantic acceptance, generation, network or IO.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location(
    "_frozen_grounding_v8_for_v9", Path(__file__).with_name("answer_grounding_v8.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
_text, _core = _base._text, _base._core

VERSION = "answer-grounding-lexical-v9-bounded-multisentence"
_ATOM = r"[^\s，,。；;！？!?\n“”‘’\"'：:]{1,100}?"
_UNSAFE = re.compile(
    r"因为|由于|所以|因此|因而|导致|造成|引发|致使|使(?!用)|"
    r"如果|只要|除非|一旦|只有|仅当|仅在|只在|所有|任何|"
    r"但是|然而|不过|仍|没有|不|未|撤回|复核|核实|确认|"
    r"保证|确保|绝对|以防|防止|保护|免受|是否|能否|可能|也许|或许")
_PAIR_SOURCE = re.compile(
    r"(?:因为|由于)(?P<cause>" + _ATOM + r")[，,]"
    r"(?:(?:所以|因此|因而))?(?P<effect>" + _ATOM + r")。(?P<tail>.*)")
_PAIR_ANSWER = re.compile(
    r"(?P<cause>" + _ATOM + r")[，,](?:所以|因此|因而)(?P<effect>" + _ATOM + r")")
_COUNT_TAIL = re.compile(r"值班员在[^\s，,。；;！？!?]{1,20}记录了[^\s，,。；;！？!?]{1,30}数量。")
_STOP_TAIL = re.compile(r"温度降到[0-9]{1,3}℃时[，,](?P<unit>" + _ATOM + r")停止。")
_REASON_SOURCE = re.compile(
    r"这次(?P<location>" + _ATOM + r")的(?P<unit>" + _ATOM + r")"
    r"(?P<state>停止计量|停止运行)。(?P=state)的原因是(?P<cause>" + _ATOM + r")。"
    r"维修员随后检查了(?P<object>" + _ATOM + r")。")
_CHAIN_SOURCE = re.compile(
    r"(?P<cause>" + _ATOM + r")堵住(?P<middle>" + _ATOM + r")。"
    r"(?P=middle)堵塞使(?P<effect>" + _ATOM + r")。")
_CHAIN_ANSWER = re.compile(r"(?P<cause>" + _ATOM + r")导致(?P<effect>" + _ATOM + r")")
_MODE_SOURCE = re.compile(
    r"(?:因为|由于)(?P<cause>" + _ATOM + r")[，,](?P<owner>" + _ATOM + r")的"
    r"(?P<unit>" + _ATOM + r")启动。这条自动启动规则只在(?P<mode>" + _ATOM + r")下生效；"
    r"(?P<other>" + _ATOM + r")下[，,](?P<trigger>" + _ATOM + r")也不会使(?P=unit)自动启动。")
_MODE_ANSWER = re.compile(
    r"(?P<cause>" + _ATOM + r")[，,](?P<unit>" + _ATOM + r")就会在所有模式下自动启动")


def _body(text):
    return text.strip().removesuffix("。")


def _safe(*atoms):
    return all(atom and not _UNSAFE.search(atom) and not _text._SOURCE_QUALIFICATION.search(atom)
               for atom in atoms)


def _support(clause, eligible):
    # With several cited bodies we cannot rule out a scoped correction or a
    # conflicting authority. Missing version metadata is equally unresolved.
    if len(eligible) != 1:
        return None
    source = eligible[0][1]
    if (not source.get("document_id") or not source.get("version_id")
            or type(source.get("version_number")) is not int or source["version_number"] < 1):
        return None
    pair, chain = _PAIR_ANSWER.fullmatch(clause), _CHAIN_ANSWER.fullmatch(clause)
    if not pair and not chain:
        return None
    for identifier, source in eligible:
        raw = source["text"]
        if _text._SOURCE_QUALIFICATION.search(raw):
            continue
        rule, arguments = None, {}
        flat, reason, link = (_PAIR_SOURCE.fullmatch(raw), _REASON_SOURCE.fullmatch(raw),
                              _CHAIN_SOURCE.fullmatch(raw))
        if pair and _safe(pair["cause"], pair["effect"]):
            if flat and _safe(flat["cause"], flat["effect"]):
                tail = flat["tail"]
                stop = _STOP_TAIL.fullmatch(tail)
                bounded_tail = (not tail or
                                (bool(_COUNT_TAIL.fullmatch(tail)) and _safe(tail)) or
                                (stop is not None and flat["effect"] == stop["unit"] + "启动"
                                 and _safe(stop["unit"])))
                if (bounded_tail and pair["cause"] == flat["cause"]
                        and pair["effect"] == flat["effect"]):
                    rule = "literal_causal_pair_with_bounded_complete_tail"
                    arguments = dict(cause=pair["cause"], effect=pair["effect"], retained_tail=tail)
            if reason and _safe(*reason.groups()):
                # This grammar gives one explicitly scoped device, never a
                # free pronoun, a second device or a guessed causal edge.
                if ("的" not in reason["location"] and "的" not in reason["unit"]
                        and pair["cause"] == reason["cause"]
                        and pair["effect"] == reason["unit"] + reason["state"]):
                    rule = "explicit_reason_across_adjacent_complete_sentences"
                    arguments = dict(cause=pair["cause"], effect=pair["effect"],
                                     location=reason["location"], device=reason["unit"])
        elif chain and link and _safe(*link.groups(), chain["cause"], chain["effect"]):
            if chain["cause"] == link["cause"] and chain["effect"] == link["effect"]:
                rule = "literal_two_edge_blockage_chain"
                arguments = dict(cause=chain["cause"], intermediate=link["middle"], effect=chain["effect"])
        if rule:
            return dict(rule_id=rule, state="LIMITED_LEXICAL_SUPPORT", resolved=True,
                        evidence=[_text._evidence(identifier, source, raw, arguments=arguments)],
                        controlled_variants=[rule], unresolved_relations=[],
                        semantic_acceptance=False, fabrication_judgment=None)
    return None


def _mode_hints(clause, eligible):
    answer = _MODE_ANSWER.fullmatch(clause)
    if not answer:
        return []
    rows = []
    for identifier, source in eligible:
        raw = source["text"]
        match = _MODE_SOURCE.fullmatch(raw)
        if (match and match["cause"] == answer["cause"] and match["unit"] == answer["unit"]
                and match["mode"] != match["other"] and not _text._SOURCE_QUALIFICATION.search(raw)):
            rows.append(dict(text=clause, rule_id="limited_mode_to_all_modes",
                state="NEED_REVIEW", resolved=False,
                evidence=[_text._evidence(identifier, source, raw,
                    allowed_mode=match["mode"], excluded_mode=match["other"], device=match["unit"])],
                unresolved_relations=["answer expands the explicitly limited mode scope"],
                semantic_acceptance=False, fabrication_judgment=None))
    return rows


def check_answer_grounding(answer, sources, citations):
    result = _base.check_answer_grounding(answer, sources, citations)
    result["version"] = VERSION
    result["multisentence_checks"], result["boundary_scope_checks"] = [], []
    eligible = _text._eligible_sources(sources, result["citations"])
    for clause in _core._clauses(answer):
        support = _support(clause, eligible)
        if support:
            for claim in result["claims"]:
                if claim["state"] != "NEED_REVIEW" or claim["text"] not in (clause, _core._candidate(clause)):
                    continue
                row = dict(support, text=claim["text"], full_answer_relation=clause)
                claim.update(previous_state=claim["state"], state=row["state"],
                             reason=row["rule_id"], evidence=row["evidence"])
                result["multisentence_checks"].append(row)
                found = False
                for i, old in enumerate(result["paraphrase_checks"]):
                    if old["text"] == claim["text"]:
                        result["paraphrase_checks"][i] = row
                        found = True
                if not found:
                    result["paraphrase_checks"].append(row)
        rows = _mode_hints(clause, eligible)
        result["boundary_scope_checks"].extend(rows)
        if rows and not any(c["state"] == "NEED_REVIEW" and c["text"] == clause for c in result["claims"]):
            result["claims"].append(dict(text=clause, kind="mode_scope_extension", state="NEED_REVIEW",
                                        reason="limited_mode_to_all_modes",
                                        evidence=[e for row in rows for e in row["evidence"]]))
    needs = (bool(result["citation_issues"]) or bool(result["scope_extension_checks"])
             or bool(result["boundary_scope_checks"])
             or any(c["state"] == "NEED_REVIEW" for c in result["claims"]))
    supported = any(c["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for c in result["claims"])
    result["status"] = "NEED_REVIEW" if needs else "TEXT_EVIDENCE_FOUND" if supported else "NOT_CHECKED"
    return result

