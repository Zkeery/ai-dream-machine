"""Offline v4: distinguish refusal behavior from source-bound factual reasons.

Refusing to invent a guarantee is not making that guarantee. Facts used as a
reason still require complete cited text evidence. This finite lexical helper
does not certify general semantics, source truth or a whole answer's quality.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import unicodedata

_spec = importlib.util.spec_from_file_location(
    "_frozen_answer_grounding_v3_for_v4", Path(__file__).with_name("answer_grounding_v3.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

VERSION = "answer-grounding-lexical-v4-policy-scope"
_V2 = _base._base
_V1 = _V2._base
_STRONG = re.compile(r"但是|然而|不过|可是|但|[。；;！？!?\n]")
_FIRST_SEPARATOR = re.compile(r"因为|由于|而且|并且|且|[，,]")
_ATOM_SEPARATOR = re.compile(r"而且|并且|且|[，,。；;！？!?\n]")
_CAUSE = re.compile(r"因为|由于|所以|因此|因而|导致|使得|造成|带来|引发|使(?!用)")
_CONDITION = re.compile(r"只要|如果|除非|一旦|仅当|只有|仅在|只在")
_HEAD = re.compile(r"^(?:(?:我|我们)(?:也|仍然|仍)?)?"
                   r"(?P<operator>不能|无法|不会|拒绝|不应|不得)"
                   r"(?P<action>编造|撰写|写|生成|提供|创作|描写|执行)(?P<object>.+)$")
_OBJECT_END = re.compile(r"(?:广告语|宣传文案|宣传语|文案|广告|保证|承诺)$")
_CREATIVE_END = re.compile(r"(?:场景|情节|动作戏|安排|片段|故事|广告|广告语|文案)$")
_PROMISE = re.compile(r"保证|承诺|确保|绝对安全|百分之百|100\s*%")
_PROHIBITION = re.compile(r"^(?:(?:品牌)?规范|规则|约束|品牌)?"
                          r"(?:禁止|不得|不允许|不能宣称|不得宣称|不可宣称)")
_COPULA = re.compile(r"^(?P<subject>[\w\u4e00-\u9fff-]{1,32})是(?P<classification>.+)$")
_QUANTIFIED_COPULA = re.compile(r"^[\w\u4e00-\u9fff-]{1,32}是一(?:款|本|种|个|台|件).+$")
_BARE_ATTRIBUTE = re.compile(r"^(?:没有|不具备|具备|具有|拥有|不拥有|缺少|采用|使用|属于|不属于)")
_EXPLICIT_ATTRIBUTE = re.compile(r"^(?P<subject>[\w\u4e00-\u9fff-]{1,32})"
                                 r"(?:没有|不具备|具备|具有|拥有|不拥有|缺少|采用|使用|属于|不属于).+$")
# Generic document attributes are not new product/entity declarations. This
# tiny grammar list is not a product, brand, character or evaluation-case list.
_ATTRIBUTE_SUBJECTS = frozenset(("卖点", "封面", "内页", "特点", "功能", "用途", "目的", "规格"))
_REPORT_ONLY = frozenset(("根据资料", "依据资料", "根据所给资料", "根据规则", "依据规则"))


def _plain(text):
    return re.sub(r"\s+|[，,。；;：:、\"'“”‘’（）()]", "", unicodedata.normalize("NFKC", text))


def _normalize(text, *, creative=False, constraint=False):
    text = text.strip()
    changes = []
    cleaned = re.sub(r"^(?:且|并且|而且)", "", text)
    if cleaned != text:
        changes.append("leading_coordination_removed")
    text = cleaned
    cleaned = re.sub(r"^(?:资料规定|资料说明|根据资料|依据资料|根据所给资料)", "", text)
    if cleaned != text:
        changes.append("explicit_report_prefix_removed")
    text = cleaned
    for pattern, replacement, name in (
        (r"是一(?:款|本|种|个)", "是", "classification_counter"),
        (r"不具备", "没有", "same_object_negative_possession"),
        (r"不得出现", "禁止出现", "explicit_prohibition_operator"),
    ):
        cleaned = re.sub(pattern, replacement, text)
        if cleaned != text:
            changes.append(name)
        text = cleaned
    cleaned = re.sub(r"^(?:品牌规范|规范)(?=禁止|不得)", "", text)
    if cleaned != text:
        changes.append("generic_rule_report_prefix_removed")
    text = cleaned
    if creative and constraint:
        cleaned = text.replace("禁止安排角色", "禁止")
        if cleaned != text:
            changes.append("creative_constraint_role_operator_only")
        text = cleaned
    return _plain(text), changes


def _units(text):
    """Whole causal/conditional units; otherwise split explicit factual lists."""
    visible = _base._visible_scope(text)
    if _CAUSE.search(visible) or _CONDITION.search(visible):
        value = text.strip(" \t\n。；;！？!?")
        return [value] if value else []
    units = []
    start = 0
    for match in _ATOM_SEPARATOR.finditer(visible):
        value = text[start:match.start()].strip()
        if value:
            units.append(value)
        start = match.end()
    value = text[start:].strip()
    if value:
        units.append(value)
    return units


def _source_units(sources, citations):
    selected = set(citations)
    by_id = {}
    for source in sources:
        cid = str(source.get("citation_id", source.get("id", "")))
        if cid in selected and isinstance(source.get("text"), str):
            by_id.setdefault(cid, []).append(source)
    result = []
    for cid, rows in by_id.items():
        signatures = {(row["text"], row.get("document_id"), row.get("version_id"), row.get("version_number"),
                       row.get("is_constraint")) for row in rows}
        if len(signatures) != 1:
            continue
        source = rows[0]
        entity_anchor = None
        entity_anchor_text = None
        first_anchor = None
        first_anchor_text = None
        for index, text in enumerate(_units(source["text"])):
            normalized, _ = _normalize(text)
            if first_anchor is None:
                first_anchor = normalized
                first_anchor_text = text
            declaration = _COPULA.fullmatch(text) or _EXPLICIT_ATTRIBUTE.fullmatch(text)
            if (not _BARE_ATTRIBUTE.match(text) and declaration
                    and declaration.group("subject") not in _ATTRIBUTE_SUBJECTS):
                entity_anchor = normalized
                entity_anchor_text = text
            required_anchor = None
            required_anchor_text = None
            if _BARE_ATTRIBUTE.match(text):
                # Bare attributes inherit their document's explicit entity;
                # they cannot be borrowed from a different cited product.
                required_anchor = entity_anchor or "__unbound_attribute_subject__"
                required_anchor_text = entity_anchor_text
            elif index > 0 and re.match(r"^(?:禁止|不得|不允许)", text):
                required_anchor = first_anchor
                required_anchor_text = first_anchor_text
            result.append({"citation_id": cid, "text": text,
                           "full_text": source["text"],
                           "is_constraint": source.get("is_constraint") is True,
                           "required_anchor": required_anchor,
                           "required_anchor_text": required_anchor_text,
                           "document_id": source.get("document_id"),
                           "version_id": source.get("version_id"),
                           "version_number": source.get("version_number")})
    return result


def _first_prohibition_member(source, claim_key):
    """Only the first explicit item of an unconditional negative list.

    Preserve the original full list as evidence. This never extracts a positive
    list item, drops an exception/condition, or treats one item as the full list.
    """
    full_scope = _base._visible_scope(source["full_text"])
    if (_CAUSE.search(full_scope) or _CONDITION.search(full_scope) or
            re.search(r"并非|并不是|不是|不代表|不意味着|例外|除外|但是|但|然而|不过|仅", full_scope)):
        return None
    text = unicodedata.normalize("NFKC", source["text"]).strip()
    text = re.sub(r"^(?:品牌规范|规范)(?=不得出现|禁止出现)", "", text)
    match = re.fullmatch(r"(?:不得出现|禁止出现)([^、]+)、(.+)", text)
    if not match:
        return None
    first = match.group(1).strip()
    member_key = _normalize("禁止出现" + first)[0]
    if member_key != claim_key:
        return None
    return {"member": first, "key": member_key, "original_list": source["text"]}


def _context_match(text, indexed_sources, matched_anchors, creative):
    claim_key, claim_changes = _normalize(text)
    evidence = []
    keys = []
    for source in indexed_sources:
        required = source["required_anchor"]
        if required is not None and required not in matched_anchors:
            continue
        source_key, source_changes = _normalize(source["text"], creative=creative,
                                                constraint=source["is_constraint"])
        member = None
        if claim_key != source_key:
            member = _first_prohibition_member(source, claim_key)
            if member is None:
                continue
            source_key = member["key"]
            source_changes = source_changes + ["first_explicit_unconditional_prohibition_list_item"]
        changes = list(dict.fromkeys(claim_changes + source_changes))
        evidence.append({"citation_id": source["citation_id"], "text": source["text"],
                         "document_id": source["document_id"], "version_id": source["version_id"],
                         "version_number": source["version_number"],
                         "required_anchor": required,
                         "subject_anchor_text": source["required_anchor_text"],
                         "prohibition_list_member": member,
                         "controlled_variants": changes})
        keys.append(source_key)
    if evidence:
        matched_anchors.update(keys)
    limited = any(row["controlled_variants"] for row in evidence)
    return {"text": text, "state": "LIMITED_LEXICAL_SUPPORT" if limited else
            "DIRECT_TEXT_SUPPORT" if evidence else "NEED_REVIEW",
            "reason": "whole_cited_atom_and_subject_context" if evidence else
            "unwritten_or_changed_fact_or_unresolved_subject_scope", "evidence": evidence}


def _refusal_head(text):
    match = _HEAD.fullmatch(text.strip())
    if not match:
        return None
    obj = match.group("object")
    if _PROMISE.search(obj) and _OBJECT_END.search(obj):
        return {"kind": "behavioral_policy_refusal", "creative": match.group("action") in
                ("撰写", "写", "生成", "创作", "描写")}
    if _CREATIVE_END.search(obj) and match.group("action") in ("撰写", "写", "生成", "创作", "描写", "执行"):
        return {"kind": "creative_or_action_refusal", "creative": match.group("action") != "执行"}
    return None


def _pieces(answer):
    visible = _base._visible_scope(answer)
    begin = 0
    for match in _STRONG.finditer(visible):
        if answer[begin:match.start()].strip():
            yield begin, match.start()
        begin = match.end()
    if answer[begin:].strip():
        yield begin, len(answer)


def _frames(answer):
    for piece_start, piece_end in _pieces(answer):
        text = answer[piece_start:piece_end]
        visible = _base._visible_scope(text)
        starts = [0] + [m.end() for m in re.finditer(r"[，,]|而且|并且|且", visible)]
        for start in starts:
            separator = _FIRST_SEPARATOR.search(visible, start)
            head_end = separator.start() if separator else len(text)
            head = text[start:head_end].strip()
            kind = _refusal_head(head)
            if kind is None:
                continue
            rest = text[head_end:].strip()
            rest = rest.lstrip("，, \t")
            explanation = False
            if rest.startswith(("因为", "由于")):
                rest = rest[2:].strip()
                explanation = True
            else:
                rest = re.sub(r"^(?:而且|并且|且)", "", rest).strip()
            yield {"start": piece_start + start, "end": piece_end,
                   "text": text[start:].strip(), "head": head, "body": rest,
                   "is_explanation": explanation, **kind}
            break


def _evaluate_frame(frame, sources, citations, indexed_sources):
    atoms = []
    world_claims = []
    matched_anchors = set()
    for text in _units(frame["body"]):
        if text in _REPORT_ONLY:
            continue
        if _refusal_head(text) or _V2._meta_explanation(text):
            atoms.append({"text": text, "state": "NOT_ASSERTED", "evidence": [],
                          "reason": "refusal_behavior_or_local_answerability_only"})
            continue
        normalized = re.sub(r"^(?:且|并且|而且)", "", text).strip()
        if _V1._RELATION.search(normalized) and not _PROHIBITION.match(normalized):
            checked = _base.check_answer_grounding(normalized, sources, citations)
            relevant = [c for c in checked["claims"] if c["state"] in
                        ("NEED_REVIEW", "DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT")]
            world_claims.extend(relevant)
            atoms.append({"text": text, "state": "NEED_REVIEW" if checked["status"] == "NEED_REVIEW" else
                          "DIRECT_TEXT_SUPPORT" if checked["status"] == "TEXT_EVIDENCE_FOUND" else "NOT_ASSERTED",
                          "reason": "world_relation_kept_intact_not_atom_stitched",
                          "evidence": [e for c in relevant for e in c.get("evidence", [])]})
        else:
            atoms.append(_context_match(text, indexed_sources, matched_anchors, frame["creative"]))
    return {"text": frame["body"], "head": frame["head"], "atoms": atoms,
            "resolved": all(atom["state"] != "NEED_REVIEW" for atom in atoms),
            "scope": "local_policy_reason_only_not_whole_answer_certification"}, world_claims


def check_answer_grounding(answer, sources, citations) -> dict:
    result = _base.check_answer_grounding(answer, sources, citations)
    result["version"] = VERSION
    result["policy_refusals"] = []
    result["policy_explanations"] = []
    indexed_sources = _source_units(sources, result["citations"])
    for frame in _frames(answer):
        result["policy_refusals"].append({"text": frame["head"], "state": "NOT_ASSERTED",
                                          "kind": frame["kind"], "start": frame["start"], "end": frame["end"]})
        explanation, world_claims = _evaluate_frame(frame, sources, result["citations"], indexed_sources)
        if frame["body"]:
            result["policy_explanations"].append(explanation)
        if explanation["resolved"]:
            for claim in result["claims"]:
                if claim["state"] == "NEED_REVIEW" and claim["text"] in frame["text"]:
                    claim["previous_state"] = claim["state"]
                    claim["state"] = "NOT_ASSERTED"
                    claim["reason"] = "refused_behavior_with_separately_bound_context_not_a_world_guarantee"
        elif not any(c["state"] == "NEED_REVIEW" and c["text"] in frame["text"] for c in result["claims"]):
            for atom in explanation["atoms"]:
                if atom["state"] == "NEED_REVIEW":
                    result["claims"].append(dict(atom, local_scope="unresolved_policy_context"))
        for claim in world_claims:
            if claim["state"] == "NEED_REVIEW" and not explanation["resolved"]:
                # Existing unresolved frame flags stay; avoid double-counting
                # the same issue merely because its explanation was inspected.
                continue
            if not any(c["state"] == claim["state"] and c["text"] == claim["text"] for c in result["claims"]):
                result["claims"].append(dict(claim, local_scope="independent_relation_after_policy_refusal"))
    needs_review = bool(result["citation_issues"]) or any(c["state"] == "NEED_REVIEW" for c in result["claims"])
    supported = any(c["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for c in result["claims"])
    result["status"] = "NEED_REVIEW" if needs_review else "TEXT_EVIDENCE_FOUND" if supported else "NOT_CHECKED"
    result["limitations"].append("v4 policy-reason evidence is local text evidence, never general factual or causal certification")
    return result
