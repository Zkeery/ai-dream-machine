# -*- coding: utf-8 -*-
"""文件上传：校验格式/大小/安全文件名，保存到 uploads，并记录归属。"""
from __future__ import annotations

import asyncio
import shutil
import uuid

from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.core import config
from app.core.errors import AppError
from app.core.path_security import IMAGE_EXTS, VIDEO_EXTS, check_ext, safe_filename
from app.services import asset_library, auth

router = APIRouter()

_ALLOWED = IMAGE_EXTS | VIDEO_EXTS


@router.post("/upload")
async def upload_media(file: UploadFile, user_id: str = Depends(get_current_user)) -> dict:
    name = safe_filename(file.filename or "")
    ext = check_ext(name, _ALLOWED)
    size = 0
    data = await file.read()
    size = len(data)
    if size == 0:
        raise AppError("EMPTY_FILE", "文件为空")
    limit = config.settings.max_video_bytes if ext in VIDEO_EXTS else config.settings.max_image_bytes
    if size > limit:
        raise AppError("FILE_TOO_LARGE", f"文件超过大小上限 {limit // (1024 * 1024)}MB")
    # 安全文件名：用 uuid 重新命名，防路径遍历/同名覆盖
    dest_name = f"{uuid.uuid4().hex}{ext}"
    dest = config.UPLOAD_DIR / dest_name
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    auth.record_upload(dest_name, user_id, name)
    return {"filename": name, "file_path": str(dest)}


class AssetReuse(BaseModel):
    asset_id: str = Field(min_length=1, max_length=128)


@router.get("/assets")
async def list_assets(user_id: str = Depends(get_current_user)) -> list[dict]:
    return [asset_library.public_asset(item) for item in asset_library.owned_assets(user_id)]


@router.get("/assets/{asset_id}/media")
async def asset_media(asset_id: str, user_id: str = Depends(get_current_user)) -> FileResponse:
    item = asset_library.require_asset(asset_id, user_id)
    return FileResponse(item["_path"])


@router.post("/assets/reuse")
async def reuse_asset(body: AssetReuse, user_id: str = Depends(get_current_user)) -> dict:
    item = asset_library.require_asset(body.asset_id, user_id)
    source = item["_path"]
    if source.stat().st_size > config.settings.max_image_bytes:
        raise AppError("FILE_TOO_LARGE", "素材超过图片大小上限")
    dest_name = f"{uuid.uuid4().hex}{source.suffix.lower()}"
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = config.UPLOAD_DIR / dest_name
    try:
        await asyncio.to_thread(shutil.copyfile, source, dest)
        auth.record_upload(dest_name, user_id, item["name"])
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    return {"filename": dest_name, "original_name": item["name"], "file_path": str(dest),
            "asset": asset_library.public_asset(item)}
