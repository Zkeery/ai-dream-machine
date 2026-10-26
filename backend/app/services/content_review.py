# -*- coding: utf-8 -*-
"""内容安全审查，以及可选的、按人物数量判定的图片结构审查。"""
from __future__ import annotations

import json
import re
from copy import deepcopy

from app.core import config
from app.core.errors import AppError
from app.models.vlm_client import VLMClient
from app.services import review_store

INSTRUCTION = (
    "请判断这张图片是否包含以下违规内容：真实公众人物或真人肖像、侵权 IP（影视/动漫/游戏角色）、"
    "违法或低俗内容。只返回 JSON：{\"safe\": true 或 false, \"reason\": \"简短原因\"}，不要输出其他文字。"
)

COMPLIANCE_TERMS = ("公众人物", "真人", "肖像", "侵权", "版权", "色情", "低俗", "暴力", "违法", "非法", "裸露", "血腥")
ENGLISH_COMPLIANCE = (
    ("public figure", r"(?:real[\s-]+)?public[\s-]+figure"),
    ("real person", r"real[\s-]+(?:person|people|human)"),
    ("celebrity", r"\bcelebrity\b"), ("copyright", r"\bcopyright(?:ed)?\b"),
    ("infringement", r"\binfring(?:ement|es|ing)\b"), ("trademark", r"\btrademark\b"),
    ("sexual content", r"(?:explicit[\s-]+)?sexual[\s-]+content"),
    ("pornography", r"\bporn(?:ography|ographic)?\b"), ("nudity", r"\b(?:nudity|nude)\b"),
    ("violence", r"\bviolen(?:ce|t)\b"), ("illegal", r"\b(?:illegal|illicit)\b"),
    ("gore", r"\b(?:gore|bloodshed|obscene)\b"),
)
_NEGATED_TERM = re.compile(
    r"(?:未发现|未检出|未检测到|未见|没有|不存在|不包含|不涉及|不含|不属于|并非|不是|非|无)"
    + r"(?:任何|明显|相关|可见|直接|实质性|可识别|已知|的|或|和|及|、|,|，|\s|IP|ip|人物|角色|内容|问题|风险|行为|图中|画面中|有|涉及|包含|"
    + "|".join(re.escape(term) for term in COMPLIANCE_TERMS) + r")*$"
)
_NEGATED_ENGLISH = re.compile(
    r"(?:\bno|\bnot|\bwithout|\bfree of|does not (?:contain|include)|not (?:detected|found))"
    r"(?:\s|a\b|an\b|any\b|known\b|identifiable\b|real\b|public\b|figure\b|person\b|people\b|human\b|"
    r"copyright(?:ed)?\b|infring(?:ement|ing|es)\b|trademark\b|porn(?:ography|ographic)?\b|"
    r"sexual\b|content\b|explicit\b|nudity\b|nude\b|violen(?:ce|t)\b|illegal\b|illicit\b|"
    r"gore\b|bloodshed\b|obscene\b|issue(?:s)?\b|risk(?:s)?\b|character(?:s)?\b|IP\b|or\b|and\b|of\b|[-/,])*$", re.I
)
_QUALITY_REASON = re.compile(
    r"风格|质量|构图|画质|清晰|模糊|色彩|光照|裁剪|分辨率|人物数量|人数|元素缺失|"
    r"\b(?:style|quality|composition|blur(?:red|ry)?|resolution|aesthetic|lighting|crop(?:ping|ped)?)\b", re.I
)


def _compliance_reason(reason: str) -> list[str]:
    affirmed = []
    patterns = [(term, re.escape(term)) for term in COMPLIANCE_TERMS] + list(ENGLISH_COMPLIANCE)
    for label, pattern in patterns:
        for match in re.finditer(pattern, reason, re.I):
            prefix = reason[max(0, match.start() - 160):match.start()]
            # Negation does not span contrastive clauses or a new statement.
            prefix = re.split(r"[。；;\n]|但是|然而|不过|但|\bbut\b|\bhowever\b", prefix, flags=re.I)[-1]
            suffix = reason[match.end():]
            suffix_negated = re.match(r"(?:\s*[- ]free\b|\s+(?:is|are) not present\b|风险不存在|不存在|不成立)", suffix, re.I)
            if not suffix_negated and not _NEGATED_TERM.search(prefix) and not _NEGATED_ENGLISH.search(prefix):
                affirmed.append(label)
                break
    return list(dict.fromkeys(affirmed))


def ordinary_result(raw: str) -> dict:
    """Route ordinary safety observations, preserving the provider conclusion."""
    result = {"protocol": "ordinary-content-review-v2", "raw_response": raw,
              "safe": None, "reason": "", "review_valid": False, "route": "manual",
              "rule_reasons": []}
    data = None
    try:
        data = json.loads(raw, object_pairs_hook=_unique_json_object)
    except (ValueError, TypeError):
        pass
    if isinstance(data, dict):
        result["provider_safe"] = data.get("safe")
        if type(data.get("safe")) is bool:
            result["safe"] = data["safe"]
        if isinstance(data.get("reason"), str):
            result["reason"] = data["reason"]
    compliance_text = result["reason"] if isinstance(data, dict) else raw if isinstance(raw, str) else ""
    affirmed = _compliance_reason(compliance_text)
    if affirmed:
        result["route"] = "compliance_rejected"
        result["rule_reasons"] = affirmed
        # A contradictory or malformed safe field cannot override an explicit
        # compliance reason; the original provider value stays in the trace.
        result["review_valid"] = isinstance(data, dict) and type(data.get("safe")) is bool and isinstance(data.get("reason"), str)
        return result
    if not isinstance(data, dict) or type(data.get("safe")) is not bool or not isinstance(data.get("reason"), str):
        result["rule_reasons"] = ["审查输出无法解析为唯一字段、布尔safe及字符串reason"]
        return result
    result["review_valid"] = True
    if not result["reason"].strip():
        result["rule_reasons"] = ["审查理由为空"]
    elif result["safe"] is True:
        result["route"] = "accepted"
    else:
        if not _QUALITY_REASON.search(result["reason"]):
            result["route"] = "unclassified_rejected"
            result["rule_reasons"] = ["合法拒绝理由不能归类为明确的风格、质量或构图问题，已停止"]
    return result

STRUCTURAL_FLAGS = (
    "multi_view_layout",
    "floating_head",
    "detached_limbs",
    "text_or_signage",
)

PROP_STATE_CHECKS = (
    "person_count_matches",
    "structure_ok",
    "no_text_or_signage",
    "envelope_count_matches",
    "envelope_position_matches",
    "envelope_style_matches",
)


def _validated_prop_state(expected_person_count: int, prop_state: dict) -> dict:
    if type(expected_person_count) is not int or expected_person_count < 0:
        raise ValueError("expected_person_count 必须为非负整数，不能是布尔值")
    if not isinstance(prop_state, dict) or set(prop_state) != {"visible_count", "location", "style"}:
        raise ValueError("prop_state 必须且只能包含 visible_count、location、style")
    count = prop_state["visible_count"]
    if type(count) is not int or count < 0:
        raise ValueError("visible_count 必须为非负整数，不能是布尔值")
    if any(not isinstance(prop_state[key], str) or not prop_state[key].strip() for key in ("location", "style")):
        raise ValueError("location、style 必须是非空字符串")
    return {key: prop_state[key] for key in ("visible_count", "location", "style")}


def prop_state_review_instruction(expected_person_count: int, prop_state: dict) -> str:
    """只索取六项观察；是否放行由代码计算，不要求模型给总结论。"""
    state = _validated_prop_state(expected_person_count, prop_state)
    schema = {"checks": {key: {"yes": True, "evidence": "一句画面依据"} for key in PROP_STATE_CHECKS}}
    return (
        f"逐项核对图片与冻结要求：人物表现恰好{expected_person_count}个；信封状态"
        + json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        + "。只输出六项观察，每项yes表示是否符合，evidence为一句1至25字画面依据。"
        "person_count_matches：人数符合；每个独立全身/半身/侧身及独立头像各计1，"
        "同一角色重复出现不得合并，依据中写实际数。"
        "structure_ok：无多视图、三视图、拼贴、悬浮头像或脱离主体肢体。"
        "no_text_or_signage：无文字、标牌、标志。"
        "envelope_count_matches：全画面可见信封数量符合visible_count；"
        "手中、投信口、地面均须检查，只露出一角或半截也计入，不因遮挡忽略。"
        "envelope_position_matches：可见位置符合location；要求尚未插入时，"
        "信封须完整可见、仍由手持且未进入投口。二维遮挡或前后重叠不等于已插入，"
        "不能仅据此判否；无法确定时yes=false，依据写明不确定，不捏造已插入。"
        "envelope_style_matches：样式符合style，检查彩边和文字。"
        "visible_count为0时，三项信封检查均须确认全画面任何位置都无信封；"
        "只要任何信封露出均为否，不能以不可判断或不适用代替。"
        "看不清时yes=false并简述，不猜测。只返回以下结构，各项布尔值按实际观察填写，"
        "不得增加safe、accepted、pass、fail、overall、reason或通过/不通过总结字段："
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def _prop_state_result(raw: str, expected_person_count: int, prop_state: dict) -> dict:
    result = {
        "protocol": "image-prop-state-v1",
        "expected_person_count": expected_person_count,
        "expected_prop_state": dict(prop_state),
        "review_valid": False,
        "accepted": False,
        "checks": {},
        "raw_response": raw,
        "rule_reasons": [],
    }
    errors = result["rule_reasons"]
    try:
        data = json.loads(raw, object_pairs_hook=_unique_json_object)
        if not isinstance(data, dict):
            raise ValueError("JSON顶层不是对象")
    except (ValueError, TypeError):
        errors.append("审查输出不是可解析且字段唯一的JSON对象")
        result["reason"] = "；".join(errors)
        return result

    if set(data) != {"checks"}:
        errors.append("顶层必须且只能包含checks，不允许模型总结论或其他字段")
    checks = data.get("checks")
    if not isinstance(checks, dict):
        errors.append("checks缺失或不是对象")
    else:
        # Preserve all model observations even when their schema is invalid.
        result["checks"] = checks
        if set(checks) != set(PROP_STATE_CHECKS):
            errors.append("checks必须完整且仅包含冻结的六项观察")
        for name in PROP_STATE_CHECKS:
            check = checks.get(name)
            if not isinstance(check, dict) or set(check) != {"yes", "evidence"}:
                errors.append(f"{name}必须且只能包含yes与evidence")
                continue
            if type(check["yes"]) is not bool:
                errors.append(f"{name}.yes不是布尔值")
            evidence = check["evidence"]
            if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 160:
                errors.append(f"{name}.evidence必须为1至160字的非空依据")
    if errors:
        result["reason"] = "；".join(errors)
        return result

    result["review_valid"] = True
    for name in PROP_STATE_CHECKS:
        if checks[name]["yes"] is False:
            errors.append(f"{name}不符合冻结要求")
    result["accepted"] = not errors
    result["reason"] = "；".join(errors) if errors else "六项观察均符合冻结要求，程序放行"
    return result


def structural_review_instruction(expected_person_count: int) -> str:
    """冻结严格协议；只检查所给图像，不依赖模型自报 accepted。"""
    if type(expected_person_count) is not int or expected_person_count < 0:
        raise ValueError("expected_person_count 必须为非负整数，不能是布尔值")
    return (
        "请逐项审查这张图片。先检查内容安全：是否包含真实公众人物或真人肖像、"
        "侵权 IP（影视/动漫/游戏角色）、违法或低俗内容；safe 只代表该安全检查。"
        f"本图剧本要求画面中恰好 {expected_person_count} 个人物表现。"
        "必须先数清整张图的可见人物数量，再检查结构，不能只识别角色身份。"
        "人物数量字段 person_count 必须是整数：每个独立全身、半身、侧身人物各算1个，"
        "每个悬浮/独立/插入头像也另算1个；同一个角色画了多次也须分别计数，"
        "例如两个侧身人物加一个独立头像必须报告3，不能因是同一角色而报告1。"
        "正常连接在单个人身体上的头不重复计数；单独肢体不作为完整人物计数，"
        "但必须将 detached_limbs 标为true。"
        "严格检查：multi_view_layout 表示存在多视图/三视图/设定图/分栏拼贴排版；"
        "floating_head 表示存在悬浮头像或插入的头像特写；"
        "detached_limbs 表示存在脱离人物主体的手臂、手、腿等肢体；"
        "text_or_signage 表示存在任何文字、标牌、招牌或标志。"
        "上述四项分别输出true或false，不得漏项。无法判断人物数量时输出null，"
        "无法判断某一布尔项时输出null，并在reason解释，绝不猜测为通过。"
        '只返回一个JSON对象，不要代码块或其他文字：{"safe": true或false, '
        '"person_count": 整数或null, "multi_view_layout": true或false或null, '
        '"floating_head": true或false或null, "detached_limbs": true或false或null, '
        '"text_or_signage": true或false或null, "reason": "逐项简短说明，包含人物数量"}。'
    )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON含重复字段")
        result[key] = value
    return result


def _strict_result(raw: str, expected_person_count: int) -> dict:
    result = {
        "protocol": "strict-image-structure-v1",
        "expected_person_count": expected_person_count,
        "review_valid": False,
        "accepted": False,
        # safe retains the conservative gate semantics for existing callers.
        "safe": False,
        "content_safe": None,
        "person_count": None,
        "人物数量": None,
        **{field: None for field in STRUCTURAL_FLAGS},
        "model_reason": "",
        "rule_reasons": [],
        "raw_response": raw,
    }
    errors = result["rule_reasons"]
    try:
        data = json.loads(raw, object_pairs_hook=_unique_json_object)
        if not isinstance(data, dict):
            raise ValueError("JSON顶层不是对象")
    except (ValueError, TypeError):
        errors.append("审查输出不是可解析且字段唯一的JSON对象")
        result["reason"] = "；".join(errors)
        return result

    if type(data.get("safe")) is bool:
        result["content_safe"] = data["safe"]
    else:
        errors.append("safe缺失或不是布尔值")

    # Accept the explicit Chinese field too, but reject conflicting aliases.
    count_keys = [key for key in ("person_count", "人物数量") if key in data]
    if not count_keys or any(type(data[key]) is not int or data[key] < 0 for key in count_keys):
        errors.append("人物数量缺失或不是非负整数（布尔值、字符串、小数、null均无效）")
    elif len({data[key] for key in count_keys}) != 1:
        errors.append("person_count与人物数量字段冲突")
    else:
        result["person_count"] = result["人物数量"] = data[count_keys[0]]

    for field in STRUCTURAL_FLAGS:
        if type(data.get(field)) is not bool:
            errors.append(f"{field}缺失或不是布尔值")
        else:
            result[field] = data[field]
    if not isinstance(data.get("reason"), str):
        errors.append("reason缺失或不是字符串")
    else:
        result["model_reason"] = data["reason"]

    if errors:
        result["reason"] = "；".join(errors)
        return result
    result["review_valid"] = True
    if not result["content_safe"]:
        errors.append("内容安全检查未通过")
    if result["person_count"] != expected_person_count:
        errors.append(f"人物数量为{result['person_count']}，要求恰好{expected_person_count}")
    labels = {
        "multi_view_layout": "存在多视图/三视图/拼贴排版",
        "floating_head": "存在悬浮或独立头像",
        "detached_limbs": "存在脱离主体的肢体",
        "text_or_signage": "存在文字、标牌或标志",
    }
    for field in STRUCTURAL_FLAGS:
        if result[field]:
            errors.append(labels[field])
    result["accepted"] = result["safe"] = not errors
    result["reason"] = "；".join(errors) if errors else "内容安全、人物数量及结构检查通过"
    return result


class ContentReviewer:
    def __init__(self, vlm: VLMClient | None = None) -> None:
        self.vlm = vlm or VLMClient()
        self.enabled = config.settings.content_review_enabled

    def _ask(self, path: str, instruction: str, protocol: str):
        cached = review_store.cached(path, instruction, protocol)
        if cached:
            review_store.reused(cached, path)
            result = deepcopy(cached["evidence"]["parsed_result"])
            result["review_id"] = cached["review_id"]
            result["review_reused"] = True
            if cached["status"] == "approved":
                # Effective human permission is separate from provider safe.
                result["manual_accepted"] = True
                result["route"] = "accepted"
            return None, None, result
        rid = review_store.begin(path, instruction, getattr(self.vlm, "model", config.settings.vlm_model), protocol)
        token = review_store.bind(rid)
        try:
            raw = self.vlm.review(path, instruction)
            # Real VLM records before settlement; this also covers local stubs.
            review_store.response(raw)
            return raw, rid, None
        except Exception as exc:
            review_store.error(rid, exc)
            raise
        finally:
            review_store.reset(token)

    @staticmethod
    def _finish(value: dict, rid: str | None, route: str) -> dict:
        value["route"] = route
        if rid:
            value["review_id"] = rid
        review_store.result(rid, value, route)
        return value

    def review_image(
        self, path: str, *, expected_person_count: int | None = None, prop_state: dict | None = None,
    ) -> dict:
        """prop_state启用六项观察协议；否则保留旧安全/严格人数协议。

        严格结果包含accepted、review_valid、人数、结构字段及原始输出。
        review_valid=False表示无法确定，不能计作成功拦截的回归证据。
        strict safe等于accepted；模型的内容安全判断单列为content_safe。
        道具协议只返回六项原观察及代码结论，不包含模型总safe字段。
        """
        if prop_state is not None:
            state = _validated_prop_state(expected_person_count, prop_state)
            instruction = prop_state_review_instruction(expected_person_count, state)
            raw, rid, cached = self._ask(path, instruction, "image-prop-state-v1")
            if cached is not None:
                return cached
            value = _prop_state_result(raw, expected_person_count, state)
            return self._finish(value, rid, "accepted" if value.get("accepted") is True else "structural_rejected")
        if expected_person_count is not None:
            instruction = structural_review_instruction(expected_person_count)
            raw, rid, cached = self._ask(path, instruction, "strict-image-structure-v1")
            if cached is not None:
                return cached
            value = _strict_result(raw, expected_person_count)
            return self._finish(value, rid, "accepted" if value.get("accepted") is True else "structural_rejected")
        if not self.enabled:
            return {"safe": True, "reason": "审查已关闭"}
        raw, rid, cached = self._ask(path, INSTRUCTION, "ordinary-content-review-v2")
        if cached is not None:
            return cached
        value = ordinary_result(raw)
        return self._finish(value, rid, value["route"])
