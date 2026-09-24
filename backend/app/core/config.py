# -*- coding: utf-8 -*-
"""配置加载：只从项目根目录 .env 读取，密钥不进代码。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录 = backend/app/core/config.py 上三级
PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = PROJECT_ROOT / "backend"
# 运行数据目录：线上用 DATA_DIR 环境变量指到可写路径（veFaaS 实例除 /tmp 外只读，需 DATA_DIR=/tmp/data）
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data"))).resolve()

# 运行数据目录（gitignore 已排除 data/）
SESSIONS_DIR = DATA_DIR / "sessions"
RESULT_DIR = DATA_DIR / "result"
IMAGE_DIR = RESULT_DIR / "image"
VIDEO_DIR = RESULT_DIR / "video"
SCRIPT_DIR = RESULT_DIR / "script"
UPLOAD_DIR = RESULT_DIR / "uploads"

# 只在 .env 存在时加载（测试环境无 .env 也不报错）
load_dotenv(PROJECT_ROOT / ".env")


class Settings:
    """集中读取配置；密钥只存内存，绝不写日志。

    模型网关：AIHubMix（https://aihubmix.com），OpenAI 兼容协议，
    统一一个 Key 调文本（qwen）、图片（qwen-image）、视频（wan）。
    """

    # AIHubMix 网关 Key（兼容旧变量名）
    aihubmix_api_key: str = os.getenv("AIHUBMIX_API_KEY", "") or os.getenv("DASHSCOPE_API_KEY", "")

    # 网关地址
    aihubmix_base: str = os.getenv("AIHUBMIX_BASE", "https://aihubmix.com")

    # 模型名（.env 可覆盖，默认按 AIHubMix 可用模型清单）
    llm_model: str = os.getenv("LLM_MODEL", "qwen3.5-plus")
    vlm_model: str = os.getenv("VLM_MODEL", "qwen3-vl-flash")
    image_t2i_model: str = os.getenv("IMAGE_T2I_MODEL", "qwen-image-2.0")
    video_first_frame_model: str = os.getenv("VIDEO_FIRST_FRAME_MODEL", "wan2.7-i2v")
    video_start_end_model: str = os.getenv("VIDEO_START_END_MODEL", "wan2.7-i2v")
    video_reference_model: str = os.getenv("VIDEO_REFERENCE_MODEL", "wan2.7-r2v")

    # 运行环境：dev / prod（prod 仅作标识与未来强制项，鉴权本就用 get_current_user 统一校验）
    env: str = os.getenv("ENV", "dev")

    # 内容安全：生成结果侧审查开关（默认开）
    content_review_enabled: bool = os.getenv("CONTENT_REVIEW_ENABLED", "1") == "1"

    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8030"))

    # 模型调用
    llm_timeout: float = float(os.getenv("LLM_TIMEOUT", "180"))
    image_timeout: float = float(os.getenv("IMAGE_TIMEOUT", "300"))
    video_timeout: float = float(os.getenv("VIDEO_TIMEOUT", "900"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "2"))

    # 登录令牌有效期（天）
    auth_token_ttl_days: float = float(os.getenv("AUTH_TOKEN_TTL_DAYS", "30"))

    # 上传限制（与 PRD 7.2 一致）
    max_image_bytes: int = 25 * 1024 * 1024
    max_video_bytes: int = 100 * 1024 * 1024

    def ensure_dirs(self) -> None:
        for d in (SESSIONS_DIR, IMAGE_DIR, VIDEO_DIR, SCRIPT_DIR, UPLOAD_DIR):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
