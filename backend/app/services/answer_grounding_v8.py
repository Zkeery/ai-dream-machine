"""Opt-in offline v8: three finite source-scope extension hints over v6.

No new support/acceptance rule. Only complete, cited, unqualified templates
are compared; arguments stay literal. NEED_REVIEW is not fabrication and an
unmatched template is not correctness. No human labels or IO are inputs.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location(
    "_frozen_answer_grounding_v6_for_v8", Path(__file__).with_name("answer_grounding_v6.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)
_text = _base._base  # frozen v5 binding/evidence helpers
_core = _base
while hasattr(_core, "_base"):
    _core = _core._base

VERSION = "answer-grounding-lexical-v8-finite-scope-hints"
_A = r"[^\s，,。；;！？!?\n“”‘’\"'：:、和或]+"
_FILM = re.compile(r"本片不允许出现(?P<object>" + _A + r")画面")
_WORLD = re.compile(r"(?P<subject>" + _A + r"(?:城|镇|世界))不存在(?P<object>" + _A + r")")
_ACCESS = re.compile(r"(?P<time>" + _A + r")，(?P<place>" + _A + r")不向普通访客开放；"
                     r"(?P<exception>持" + _A + r"的" + _A + r")可以进入。其他时段按正常预约规则开放")
_EVERYONE = re.compile(r"(?P<time>" + _A + r")，任何人都不能进入(?P<place>" + _A + r")")
_LIST = r"(?P<a>" + _A + r")、(?P<b>" + _A + r")(?:和|或)(?P<c>" + _A + r")"
_COMMON = re.compile(r"常见制作材料包括" + _LIST)
_ONLY = re.compile(r"(?P<subject>" + _A + r")只允许使用" + _LIST)
_UNASSERTED_HEAD = re.compile(
    r"^(?:不能|无法|尚不能|不应|不可)(?:断言|证明|推断|认为|说)|"
    r"^(?:不是|并不是|没有依据|没有证据|未证明|未证实|不足以说明|不一定|未必|我猜|可能|或许|也许)")


def _body(text):
    return text.strip().removesuffix("。")


def _rows(claim, eligible):
    if (_core._is_nonassertion(claim) or _text._SOURCE_QUALIFICATION.search(claim)
            or _UNASSERTED_HEAD.search(claim)):
        return []
    # An additional complete, unqualified literal cited statement may supply
    # the asserted restriction. This extension never converts it to acceptance.
    if any(not _text._SOURCE_QUALIFICATION.search(s["text"])
           and _body(s["text"]) == claim for _, s in eligible):
        return []
    world, everyone, only = _WORLD.fullmatch(claim), _EVERYONE.fullmatch(claim), _ONLY.fullmatch(claim)
    found = []
    for identifier, source in eligible:
        raw = source["text"]
        if _text._SOURCE_QUALIFICATION.search(raw):
            continue
        body, rule, arguments = _body(raw), None, {}
        film, access, common = _FILM.fullmatch(body), _ACCESS.fullmatch(body), _COMMON.fullmatch(body)
        if world and film and world["object"] == film["object"]:
            rule = "film_depiction_to_world_nonexistence"
            arguments = {"object": world["object"], "world_subject": world["subject"]}
        elif everyone and access and all(everyone[k] == access[k] for k in ("time", "place")):
            rule = "visitor_restriction_with_exception_to_everyone"
            arguments = {k: access[k] for k in ("time", "place", "exception")}
        elif only and common and all(only[k] == common[k] for k in ("a", "b", "c")):
            rule = "common_material_list_to_exclusive_permission"
            arguments = {"items": [common[k] for k in ("a", "b", "c")]}
        if rule:
            found.append({"text": claim, "rule_id": rule, "state": "NEED_REVIEW", "resolved": False,
                          "evidence": [_text._evidence(identifier, source, raw, arguments=arguments)],
                          "unresolved_relations": ["source scope does not by itself establish answer scope"],
                          "semantic_acceptance": False, "fabrication_judgment": None})
    return found


def check_answer_grounding(answer, sources, citations) -> dict:
    result = _base.check_answer_grounding(answer, sources, citations)
    result["version"] = VERSION
    eligible = _text._eligible_sources(sources, result["citations"])
    result["scope_extension_checks"] = []
    for clause in _core._clauses(answer):
        rows = _rows(clause, eligible)
        result["scope_extension_checks"].extend(rows)
        if rows and not any(c["text"] == clause and c["state"] == "NEED_REVIEW" for c in result["claims"]):
            result["claims"].append({"text": clause, "kind": "source_scope_extension",
                                     "state": "NEED_REVIEW", "reason": "finite_source_scope_mismatch_requires_review",
                                     "evidence": [e for row in rows for e in row["evidence"]]})
    if result["scope_extension_checks"]:
        result["status"] = "NEED_REVIEW"
    return result
