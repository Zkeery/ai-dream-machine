"""固定协议能力表与管理员公开名单的交集；不向用户暴露网关配置。"""
from __future__ import annotations

from app.core import config
from app.core.errors import AppError
from app.schemas.models import ModelSelection
from app.models.video_contracts import SEEDANCE, VEO, NAMES, capabilities

PROVIDER = "AIHubMix"
CATALOG = {
    "text": {"qwen3.5-plus": "通用创作与结构化剧本", "qwen3.5-flash": "轻量文本创作"},
    "image": {"qwen-image-2.0": "漫画与故事画面，支持角色参考图", "qwen-image-2.0-pro": "精细画面，支持角色参考图"},
    "video_first_frame": {"wan2.7-i2v": "由首帧生成视频", "wan2.6-i2v": "由首帧生成视频"},
    "video_start_end": {"wan2.7-i2v": "需要真实首帧与尾帧"},
    "video_reference": {"wan2.7-r2v": "根据实际参考图生成视频"},
    "video_speech": {"wan2.6-i2v": "人物图片与台词生成 10 秒 720P 声画同步视频，音色由模型生成；按表约 ¥7.23/次（非实付账单）"},
}
for _group in ("video_first_frame", "video_start_end", "video_reference"):
    CATALOG[_group].update({
        SEEDANCE: "5秒/片段；支持720P/1080P；多图参考最多30张。新接入，成片质量待实测。",
        VEO: "8秒/片段；支持720P/1080P与16:9、9:16；多图参考最多3张。新接入，成片质量待实测。",
    })
LABELS = {"text": "文本模型", "image": "图片模型", "video_first_frame": "首帧视频模型",
          "video_start_end": "首尾帧视频模型", "video_reference": "多图参考视频模型", "video_speech": "嘴型同步模型"}
DEFAULT_ATTRS = {"text": "llm_model", "image": "image_t2i_model", "video_first_frame": "video_first_frame_model",
                 "video_start_end": "video_start_end_model", "video_reference": "video_reference_model", "video_speech": "video_speech_model"}
TASK_KEYS = {"literary_video": {"text", "image"}, "motion_transfer": {"video_reference"}, "talking_head": set()}


def allowed(group: str) -> set[str]:
    return set(CATALOG[group]) & set(getattr(config.settings, "public_" + group + "_models"))


def public_catalog() -> dict:
    groups = {}
    for group, entries in CATALOG.items():
        available = allowed(group)
        default = getattr(config.settings, DEFAULT_ATTRS[group])
        groups[group] = {"label": LABELS[group], "default": default if default in available else None,
                         "options": [{"id": key, "label": NAMES.get(key, key), "description": description,
                                      **_video_details(group, key)}
                                     for key, description in entries.items() if key in available]}
    return {"provider": PROVIDER, "groups": groups}


def _video_details(group: str, model: str) -> dict:
    if group not in {"video_first_frame", "video_start_end", "video_reference"}:
        return {}
    from app.services.cost_control import estimate_cny
    caps = capabilities(model)
    estimates = {}
    for resolution in caps["resolutions"]:
        try:
            estimates[resolution] = estimate_cny(model, "video", {"seconds": caps["duration_seconds"], "resolution": resolution})
        except AppError:
            pass
    return {"video": {**caps, "estimated_clip_cny": estimates}}


def validate_selection(selection, *, project_type: str | None = None, task_type: str | None = None,
                       talking_mode: str = "static") -> dict[str, str]:
    try:
        value = (selection if isinstance(selection, ModelSelection) else ModelSelection.model_validate(selection or {})).model_dump(exclude_none=True)
    except Exception as error:
        raise AppError("INVALID_MODEL_SELECTION", "模型选择仅支持公开的模型类别与 ID", 422) from error
    scope = TASK_KEYS[task_type] if task_type else {"text", "image"} if project_type == "comic" else set(CATALOG) - {"video_speech"}
    if task_type == "talking_head" and talking_mode == "lip_sync":
        scope = {"video_speech"}
    for group, model in value.items():
        if group not in scope:
            raise AppError("MODEL_NOT_APPLICABLE", f"当前创作流程不使用{LABELS[group]}", 422)
        if model not in allowed(group):
            raise AppError("MODEL_NOT_AVAILABLE", f"{LABELS[group]}未公开或不兼容：{model}", 422)
    return value


def resolve(selection, keys) -> dict[str, str]:
    value = selection.model_dump(exclude_none=True) if isinstance(selection, ModelSelection) else selection or {}
    result = {}
    for group in keys:
        model = value.get(group) or getattr(config.settings, DEFAULT_ATTRS[group])
        if model not in allowed(group):
            raise AppError("MODEL_NOT_AVAILABLE", f"{LABELS[group]}当前不可用，请选择已公开的模型", 422)
        result[group] = model
    return result


def stage_usage(meta, stage: str) -> dict:
    key = "text" if stage in {"script_generation", "storyboard", "comic_storyboard"} else "image" if stage in {"character_design", "reference_generation", "comic_panels"} else "video_" + meta.video_generation_mode if stage == "video_generation" else None
    keys = {key} if key else set()
    if getattr(meta, "orchestration_mode", "workflow") == "multi_agent":
        keys.add("text")
    return {"provider": PROVIDER, "models": resolve(meta.model_selection, sorted(keys))}


def task_usage(task_type: str, inputs: dict) -> dict:
    keys = set(TASK_KEYS[task_type])
    if task_type == "talking_head" and inputs.get("talking_mode", "static") == "lip_sync":
        keys = {"video_speech"}
    if task_type == "literary_video" and len((inputs.get("text") or "").strip()) > 30:
        keys.discard("text")
    return {"provider": PROVIDER, "models": resolve(inputs.get("model_selection"), sorted(keys))}
