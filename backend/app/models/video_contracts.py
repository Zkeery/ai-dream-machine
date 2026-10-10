"""Reviewed AIHubMix story-video capabilities (2026-10-05).

Only the application's supported subset is advertised. Vendor schemas:
https://aihubmix.com/call/schema/models/{model}/endpoints
Durations are generation requests, not measured output-media durations.
"""
from app.core.errors import AppError

SEEDANCE = "doubao-seedance-2-5-260628"
VEO = "veo-3.1-generate-preview"
KLING = "kling-v3-omni"
NAMES = {SEEDANCE: "Seedance 2.5", VEO: "Veo 3.1", KLING: "Kling 3.0 Omni"}
VIDEO_MODE_MODELS = {
    "first_frame": frozenset({"wan2.7-i2v", "wan2.6-i2v", SEEDANCE, VEO}),
    "start_end": frozenset({"wan2.7-i2v", SEEDANCE, VEO}),
    "reference": frozenset({"wan2.7-r2v", SEEDANCE, VEO, KLING}),
}


def capabilities(model: str) -> dict:
    return {"duration_seconds": 8 if model == VEO else 5,
            "ratios": ["16:9", "9:16"] if model == VEO else ["16:9", "9:16", "1:1"],
            "resolutions": ["720P", "1080P"],
            "max_reference_images": 3 if model == VEO else 30 if model == SEEDANCE else 4 if model == KLING else None}


def validate(model: str, mode: str, ratio: str, resolution: str, *, reference_count: int = 1) -> dict:
    if model not in VIDEO_MODE_MODELS.get(mode, ()):
        raise AppError("VIDEO_MODEL_UNSUPPORTED", "所选视频模型不支持当前生成模式，请重新选择", 400)
    caps = capabilities(model)
    name = NAMES.get(model, model)
    if ratio not in caps["ratios"] or resolution.upper() not in caps["resolutions"]:
        raise AppError("VIDEO_FORMAT_UNSUPPORTED", f"{name} 支持 {'、'.join(caps['ratios'])} 与 {'/'.join(caps['resolutions'])}，请调整项目规格或选择其他模型", 400)
    limit = caps["max_reference_images"]
    if mode == "reference" and limit is not None and reference_count > limit:
        raise AppError("VIDEO_REFERENCE_LIMIT", f"{name} 每个镜头最多使用{limit}张参考图（包含分镜图、角色图与场景图），当前为{reference_count}张；请减少关联素材或选择其他模型", 400)
    return caps
