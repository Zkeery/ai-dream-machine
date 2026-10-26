"""Optional offline v2: narrow answerability-explanation handling over frozen v1.

Only explanations about absent/inaccessible input information are reclassified.
World facts, policy refusals and semantic paraphrases keep v1's review behavior.
NOT_ASSERTED/NOT_CHECKED certify neither correctness nor acceptance. No model,
application configuration, network, rewrite, score or production integration.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location(
    "_frozen_answer_grounding_v1_for_v2", Path(__file__).with_name("answer_grounding.py"))
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

VERSION = "answer-grounding-lexical-v2-meta-scope"
_PARTS = re.compile(r"[，,]+|(?:所以|因此|因而|故而|但是|然而|不过|可是|但)")
_CAUSE_PREFIX = re.compile(r"^(?:且)?(?:因为|由于|因|原因是|这是因为)")
_WORLD_EFFECT = re.compile(
    r"以防|防止|避免|预防|阻止|抵御|防护|保护|免于|免受|"
    r"保证|确保|绝对安全|完全安全|万无一失|百分之百|100\s*%|永远不会|不受|不会.{0,12}(?:受|遭)|"
    r"\b(?:prevent\w*|protect\w*|guarantee\w*|always safe)\b", re.I)
_REFUSAL_START = re.compile(
    r"^(?:(?:资料|原文|上下文)(?:中|里)?)?(?:无法|不能|未能|尚不能)(?:确定|确认|判断|回答|作答|推断)|"
    r"^(?:(?:当前|本次|当次|给定|现有|相关|可用)的?)?(?:资料|信息|来源|证据)(?:不足|缺失|不可用)")
_INFO = r"(?:参考资料|外部资料|可用来源|资料|信息|证据|来源|上下文)"
_META_TRANSITION = re.compile(r"所以|因此|因而|故而|但是|然而|不过|可是|但|却|而且|并且|且|另外|同时")


def _safe_meta_part(part: str) -> bool:
    """Full local-clause grammar, never a substring or whole-answer whitelist."""
    part = part.strip()
    part = _CAUSE_PREFIX.sub("", part, count=1).strip()
    part = re.sub(r"^(?:并且|且)", "", part, count=1).strip()
    # Apply these exclusions before *every* permissive data-noun pattern.
    # A quoted/parenthesized affirmative effect is not input availability,
    # even when its surrounding clause happens to end in "information".
    if not part or _WORLD_EFFECT.search(part) or _META_TRANSITION.search(part):
        return False
    if re.fullmatch(r"(?:权限(?:错误|不足|问题|校验失败)|无权访问|访问被拒|没有访问权限|资料访问失败)"
                    r"(?:导致|使得)(?:没有|无|缺少|无法(?:读取|访问|取得|获得))"
                    r"(?:任何|相关|可用)?" + _INFO, part):
        return True
    consequence = re.split(r"(?:会)?(?:导致|使得|造成|带来|引发|使(?!用))", part, maxsplit=1)
    if len(consequence) == 2:
        # Both ends must themselves be input/answerability conditions. Do not
        # let a missing-data prefix absorb blindness, healing or other events.
        return _safe_meta_part(consequence[0]) and _safe_meta_part(consequence[1])
    # A simple uncertainty report can mention an unknown object, but this
    # extension never exempts an effect/guarantee phrase on that basis alone.
    if _REFUSAL_START.match(part) and not _WORLD_EFFECT.search(part):
        if re.fullmatch(r"(?:(?:资料|原文|上下文)(?:中|里)?)?(?:无法|不能|未能|尚不能)"
                        r"(?:确定|确认|判断|回答|作答|推断)[^。；;，,！？!?]*", part):
            return True
    if re.fullmatch(r"(?:(?:当前|本次|当次|给定|现有|相关|可用)的?)?" + _INFO +
                    r"(?:不足|缺失|不完整|不可用|未提供)", part):
        return True
    # The optional parenthesis may describe the missing information only;
    # arbitrary parenthesized assertions must not be swallowed.
    plain, suffix = part, ""
    parenthesis = re.fullmatch(r"(.*?)([（(]如([^（）()]*)[）)])", part)
    if parenthesis:
        plain, suffix = parenthesis.group(1), parenthesis.group(3)
        if _WORLD_EFFECT.search(suffix) or _META_TRANSITION.search(suffix):
            return False
    elif any(ch in part for ch in "（）()"):
        return False
    # Negated provision may describe the *question* whose information is
    # absent; the complete clause must still end in a data noun. No connective
    # can smuggle an additional affirmative proposition into this span.
    if re.fullmatch(r"(?:(?:当前|本次|当次|题中)的?)?(?:未|尚未|并未|没有)"
                    r"(?:提供|给出|得到|获得|检索到|找到)[^。；;，,！？!?]*" + _INFO, plain):
        return True
    if re.fullmatch(r"(?:没有|缺少|缺乏)(?:任何|足够|相关|可用|必要)?" + _INFO, plain):
        return True
    if re.fullmatch(r"(?:(?:提供|所给|现有|本次|给定)的)?" + _INFO + r"(?:中|里)?"
                    r"(?:未|没有|尚未)(?:包含|提供|记载|覆盖|涉及)[^。；;，,！？!?]*"
                    r"(?:信息|资料|事实|答案|内容|数值|姓名|价格|型号|设定)", plain):
        return True
    # Response-condition rule, not a policy statement about a world/product.
    if re.fullmatch(r"(?:且)?根据规则|(?:且)?按(?:照)?规则", plain):
        return True
    if re.fullmatch(r"(?:给定|当前|本次|所给)?" + _INFO +
                    r"(?:不能|无法|未)(?:覆盖|支持|支撑|回答)(?:的)?(?:问题|内容)"
                    r"(?:应|应该|需|需要|必须|就应)(?:拒答|停止作答|请求澄清)", plain):
        return True
    return False


def _meta_explanation(text: str) -> bool:
    parts = [p.strip() for p in _PARTS.split(text) if p.strip()]
    return bool(parts) and all(_safe_meta_part(part) for part in parts)


def _refusal_asides(part: str):
    """Extract only explicit parenthetical asides, preserving unknown objects.

    "Cannot determine (whether X protects)" and quoted unknown propositions
    remain inside the uncertainty scope. "Cannot determine price (X guarantees
    healing)" contains an independently worded aside to inspect separately.
    """
    for match in re.finditer(r"[（(]([^（）()]*)[）)]", part):
        prefix = part[:match.start()].strip()
        refusal = _REFUSAL_START.match(prefix)
        if not refusal:
            continue
        unknown_object = prefix[refusal.end():].strip()
        if not unknown_object or re.search(r"是否|能否|是不是|有没有", unknown_object):
            continue
        suffix = part[match.end():].strip()
        if re.match(r"(?:这一说法|该说法|的效果|的作用)?(?:是否|能否|无法|不能|尚不)", suffix):
            continue
        content = match.group(1).strip()
        if _WORLD_EFFECT.search(content):
            yield content


def check_answer_grounding(answer, sources, citations) -> dict:
    result = _base.check_answer_grounding(answer, sources, citations)
    result["version"] = VERSION
    result["meta_explanations"] = []
    for claim in result["claims"]:
        if claim["state"] == "NEED_REVIEW" and _meta_explanation(claim["text"]):
            claim["previous_state"] = claim["state"]
            claim["state"] = "NOT_ASSERTED"
            claim["reason"] = "local_answerability_explanation_only_no_world_relation_asserted"
            result["meta_explanations"].append({"text": claim["text"],
                                                "state": "NOT_ASSERTED", "scope": "input_availability_only"})

    # Preserve the known disclaimer protection and independently inspect an
    # affirmative effect after an earlier refusal/question clause. A refusal
    # cannot cover the following clause, including parenthesized/quoted tails.
    for sentence in _base._clauses(answer):
        seen_refusal = False
        for part in [p.strip() for p in _PARTS.split(sentence) if p.strip()]:
            if _safe_meta_part(part):
                seen_refusal = True
                continue
            if _REFUSAL_START.match(part):
                seen_refusal = True
            candidates = list(_refusal_asides(part))
            if seen_refusal and _WORLD_EFFECT.search(part):
                candidates.append(part)
            for candidate in candidates:
                tail = _base.check_answer_grounding(candidate, sources, citations)
                for claim in tail["claims"]:
                    if claim["state"] == "NEED_REVIEW" and not any(
                            c["state"] == "NEED_REVIEW" and c["text"] == claim["text"] for c in result["claims"]):
                        result["claims"].append(dict(claim, local_scope="after_refusal_clause"))

    needs_review = bool(result["citation_issues"]) or any(c["state"] == "NEED_REVIEW" for c in result["claims"])
    supported = any(c["state"] in ("DIRECT_TEXT_SUPPORT", "LIMITED_LEXICAL_SUPPORT") for c in result["claims"])
    result["status"] = "NEED_REVIEW" if needs_review else "TEXT_EVIDENCE_FOUND" if supported else "NOT_CHECKED"
    result["limitations"].append("v2 only narrows local missing-input explanations; policy refusals and paraphrase review remain")
    return result
