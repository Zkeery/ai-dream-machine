"""Opt-in offline v10: bounded batch/referent evidence over frozen v9.

Only complete finite source grammars can resolve a literal batch relation.
Recognized unresolved batch/time assertions remain reviewable. No human label,
case identity, source-hash lookup, model, IO or production gate is an input.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location(
    "_frozen_grounding_v9_for_v10", Path(__file__).with_name("answer_grounding_v9.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
_text, _core = _base._text, _base._core

VERSION = "answer-grounding-lexical-v10-bounded-batch-reference"
_B = r"[\u4e00-\u9fffA-Za-z0-9]{1,12}批"
_I = r"(?:新书|图册|画册|玻璃杯|陶杯|茶盒)"
_A = r"[^\s，,。；;！？!?\n“”‘’\"'：:]{1,60}?"
_D = r"(?:[一二两三四五六七八九十]{1,3}|[1-9][0-9]?)天"
_DATE = r"(?:(?P<date>周[一二三四五六日]到货的))?"
_RESULT = (_DATE + r"(?P<batch>" + _B + r")(?P<item>" + _I + r")"
           r"(?P<delay>延后|推迟)(?P<duration>" + _D + r")(?P<event>入库|上架)")
_CAUSAL = re.compile(r"(?P<cause>" + _A + r")导致" + _RESULT)
_REASON = re.compile(
    _DATE + r"(?P<batch>" + _B + r")(?P<item>" + _I + r")因(?P<cause>" + _A + r")"
    r"(?P<delay>延后|推迟)(?P<duration>" + _D + r")(?P<event>入库|上架)")
_SINGLE = re.compile(
    r"(?P<batch>" + _B + r")(?P<item>" + _I + r")的(?P<cause>" + _A + r")，"
    r"使这批(?P=item)需要重新包装。(?P<bridge>.*?)"
    r"重新包装使(?P=batch)(?P=item)的(?P<event>入库|上架)时间"
    r"(?P<delay>延后|推迟)(?P<duration>" + _D + r")。", re.S)
_BRIDGE = re.compile(
    r"另一批(?P<other>" + _B + r")(?P<item>" + _I + r")已按原计划(?P<event>入库|上架)。")
_COAT = re.compile(
    r"(?P<batch>" + _B + r")(?P<item>" + _I + r")的(?P<cause>" + _A + r")，"
    r"工人因此为该批(?P=item)重新覆膜。这项返工使该批(?P=item)的"
    r"(?P<event>入库|上架)时间(?P<delay>延后|推迟)(?P<duration>" + _D + r")。(?P<tail>.*)", re.S)
_TAIL = re.compile(
    r"(?P<other>" + _B + r")(?P<item>" + _I + r")按原计划(?P<event>入库|上架)。")
_PLURAL = re.compile(
    r"(?P<first>" + _B + r")和(?P<second>" + _B + r")(?P<item>" + _I + r")的"
    r"(?P<head>" + _A + r")均(?P<condition>受潮|破损)，使这两批(?P=item)都需要重新包装。"
    r"重新包装使这两批(?P=item)的(?P<event>入库|上架)时间均"
    r"(?P<delay>延后|推迟)(?P<duration>" + _D + r")。")
_UNASSERTED = re.compile(
    r"无法|不能|不应|尚不能|没有证据|没有依据|不一定|未必|可能|或许|也许|"
    r"我猜|假设|如果|有人说|据称|是否|能否|[“”‘’\"']")
_SUPPORT = {"DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT"}


def _name(value):
    return (value.count("批") == 1 and not any(x in value for x in ("和", "的", "其中", "这", "那", "该"))
            and _base._safe(value))


def _claim(clause):
    if _UNASSERTED.search(clause):
        return None
    match = _CAUSAL.fullmatch(clause) or _REASON.fullmatch(clause)
    if not match:
        return None
    value = match.groupdict()
    return value if _name(value["batch"]) and _base._safe(value["cause"]) else None


def _side_record(raw, pattern, batch, item, event):
    if not raw:
        return True
    match = pattern.fullmatch(raw)
    return bool(match and _name(match["other"]) and match["other"] != batch
                and match["item"] == item and match["event"] == event)


def _source_relation(raw):
    if _text._SOURCE_QUALIFICATION.search(raw):
        return None
    match = _SINGLE.fullmatch(raw)
    if match and _name(match["batch"]) and _base._safe(match["cause"]):
        if _side_record(match["bridge"], _BRIDGE, match["batch"], match["item"], match["event"]):
            return dict(batch=match["batch"], batches=[match["batch"]], item=match["item"],
                        cause=match["cause"], delay=match["delay"], duration=match["duration"],
                        event=match["event"], rule="single_batch_literal_chain", intermediate="重新包装")
    match = _COAT.fullmatch(raw)
    if match and _name(match["batch"]) and _base._safe(match["cause"]):
        if _side_record(match["tail"], _TAIL, match["batch"], match["item"], match["event"]):
            return dict(batch=match["batch"], batches=[match["batch"]], item=match["item"],
                        cause=match["cause"], delay=match["delay"], duration=match["duration"],
                        event=match["event"], rule="single_batch_explicit_rework_referents", intermediate="重新覆膜")
    match = _PLURAL.fullmatch(raw)
    if (match and all(_name(match[k]) for k in ("first", "second")) and match["first"] != match["second"]
            and _base._safe(match["head"])):
        return dict(batches=[match["first"], match["second"]], item=match["item"],
                    cause=match["head"] + match["condition"], delay=match["delay"],
                    duration=match["duration"], event=match["event"],
                    rule="two_explicit_batches_distributed_literal_chain", intermediate="重新包装")
    return None


def _evidence_support(claim, eligible, citations, issues):
    if len(eligible) != 1 or len(citations) != 1 or issues or claim.get("date") is not None:
        return None
    identifier, source = eligible[0]
    if (not isinstance(source.get("document_id"), str) or not source["document_id"]
            or not isinstance(source.get("version_id"), str) or not source["version_id"]
            or type(source.get("version_number")) is not int or source["version_number"] < 1):
        return None
    relation = _source_relation(source["text"])
    if (not relation or claim["batch"] not in relation["batches"]
            or any(claim[k] != relation[k] for k in ("item", "cause", "delay", "duration", "event"))):
        return None
    return dict(rule_id=relation["rule"], state="LIMITED_LEXICAL_SUPPORT", resolved=True,
                evidence=[_text._evidence(identifier, source, source["text"],
                    arguments=dict(claim), source_batches=relation["batches"],
                    intermediate=relation["intermediate"])],
                controlled_variants=[relation["rule"]], unresolved_relations=[],
                semantic_acceptance=False, fabrication_judgment=None)


def _paraphrase(result, row):
    for i, old in enumerate(result["paraphrase_checks"]):
        if old["text"] == row["text"]:
            result["paraphrase_checks"][i] = row
            return
    result["paraphrase_checks"].append(row)


def check_answer_grounding(answer, sources, citations):
    result = _base.check_answer_grounding(answer, sources, citations)
    result["version"] = VERSION
    result["batch_reference_checks"] = []
    eligible = _text._eligible_sources(sources, result["citations"])
    # A punctuation-stripped question is not an affirmative batch assertion.
    # The frozen checker still inspects all other independent risk clauses.
    if not re.search(r"[？?！!]", answer):
        for clause in _core._clauses(answer):
            claim = _claim(clause)
            if not claim:
                continue
            existing = [c for c in result["claims"] if c["text"] == clause]
            if any(c["state"] in _SUPPORT | {"NOT_ASSERTED"} for c in existing):
                continue
            row = _evidence_support(claim, eligible, result["citations"], result["citation_issues"])
            if row:
                row = dict(row, text=clause, full_answer_relation=clause)
                for c in existing:
                    if c["state"] == "NEED_REVIEW":
                        c.update(previous_state=c["state"], state=row["state"], reason=row["rule_id"],
                                 evidence=row["evidence"])
                if not existing:
                    result["claims"].append(dict(text=clause, kind="bounded_batch_relation",
                        state=row["state"], reason=row["rule_id"], evidence=row["evidence"]))
            else:
                row = dict(text=clause, rule_id="unresolved_batch_or_time_binding",
                    state="NEED_REVIEW", resolved=False,
                    evidence=[_text._evidence(identifier, source, source["text"]) for identifier, source in eligible],
                    controlled_variants=[], unresolved_relations=[
                        "no complete finite binding of the stated batch, referent, cause, event and delay"],
                    semantic_acceptance=False, fabrication_judgment=None)
                if not any(c["state"] == "NEED_REVIEW" for c in existing):
                    result["claims"].append(dict(text=clause, kind="bounded_batch_relation",
                        state="NEED_REVIEW", reason=row["rule_id"], evidence=row["evidence"]))
            result["batch_reference_checks"].append(row)
            _paraphrase(result, row)
    needs = (bool(result["citation_issues"]) or bool(result["scope_extension_checks"])
             or bool(result["boundary_scope_checks"])
             or any(c["state"] == "NEED_REVIEW" for c in result["claims"]))
    supported = any(c["state"] in _SUPPORT for c in result["claims"])
    result["status"] = "NEED_REVIEW" if needs else "TEXT_EVIDENCE_FOUND" if supported else "NOT_CHECKED"
    return result

