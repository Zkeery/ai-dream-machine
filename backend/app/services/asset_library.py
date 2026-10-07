# -*- coding: utf-8 -*-
"""本人生成图片的统一视图：元数据登记、归属和文件边界必须同时满足。"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterator

from app.core import config
from app.core.errors import AppError
from app.core.path_security import IMAGE_EXTS, ensure_inside, validate_session_id
from app.services import session_store, task_store


def _paths(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        if Path(value).suffix.lower() in IMAGE_EXTS:
            yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _paths(child)
    elif isinstance(value, list):
        for child in value:
            yield from _paths(child)


def _entry(source_type: str, source_id: str, title: str, stage: str,
           value: str, updated_at: float, stale: bool, version_id: str | None) -> dict | None:
    try:
        validate_session_id(source_id)
        base = (config.IMAGE_DIR / source_id).resolve()
        # 既有生成图片均由 IMAGE_DIR/<来源id> 管理；不读取上传或任意磁盘路径。
        ensure_inside(config.IMAGE_DIR.resolve(), base)
        candidate = Path(value)
        if not candidate.is_absolute():
            return None
        image = ensure_inside(base, candidate)
        if not image.is_file() or image.suffix.lower() not in IMAGE_EXTS:
            return None
    except (AppError, OSError, ValueError):
        return None
    identity = f"{source_type}:{source_id}:{stage}:{image}"
    asset_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "asset_id": asset_id, "source_type": source_type, "source_id": source_id,
        "source_title": title[:100], "stage": stage, "kind": "image", "name": image.name,
        "url": f"/api/assets/{asset_id}/media", "updated_at": updated_at,
        "stale": stale, "version_id": version_id, "_path": image,
    }


def owned_assets(owner_id: str) -> list[dict]:
    assets: dict[str, dict] = {}
    for meta in session_store.list_sessions(owner_id):
        title = str((meta.artifacts.get("script_generation") or {}).get("title") or meta.idea)
        selected = getattr(meta, "selected_versions", {})
        stale = set(getattr(meta, "stale_stages", []))
        for stage, artifact in meta.artifacts.items():
            for value in _paths(artifact):
                item = _entry("session", meta.session_id, title, stage, value,
                              meta.updated_at, stage in stale, selected.get(stage))
                if item:
                    assets.setdefault(item["asset_id"], item)
        # 历史素材同样保留来源；当前选中项优先，未选旧版本标为非当前。
        for stage, versions in getattr(meta, "artifact_versions", {}).items():
            for version in versions:
                version_id = version.get("version_id")
                for value in _paths(version.get("artifact", {})):
                    item = _entry("session", meta.session_id, title, stage, value,
                                  float(version.get("created_at") or meta.updated_at),
                                  stage in stale or version_id != selected.get(stage), version_id)
                    if item:
                        assets.setdefault(item["asset_id"], item)
    for meta in task_store.list_tasks(owner_id):
        title = str(meta.input.get("text") or meta.input.get("script") or "快捷短片")
        for value in _paths(meta.result or {}):
            item = _entry("task", meta.task_id, title, meta.type, value, meta.updated_at,
                          False, None)
            if item:
                assets.setdefault(item["asset_id"], item)
    return sorted(assets.values(), key=lambda item: item["updated_at"], reverse=True)


def public_asset(item: dict) -> dict:
    return {key: value for key, value in item.items() if key != "_path"}


def require_asset(asset_id: str, owner_id: str) -> dict:
    if len(asset_id) != 64 or any(char not in "0123456789abcdef" for char in asset_id):
        raise AppError("ASSET_NOT_FOUND", "素材不存在或无权访问", 404)
    for item in owned_assets(owner_id):
        if item["asset_id"] == asset_id:
            return item
    raise AppError("ASSET_NOT_FOUND", "素材不存在或无权访问", 404)
