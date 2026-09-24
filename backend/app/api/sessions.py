# -*- coding: utf-8 -*-
"""会话路由：创建、列表、查询、继续、执行阶段（SSE）、干预、产物、导出。
第三刀起全部会话接口要求登录，并强制账号隔离（只能访问自己的会话）。
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import get_current_user, get_orchestrator
from app.core import config
from app.core.errors import AppError
from app.core.path_security import safe_filename
from app.schemas.session import SessionCreate, SessionMeta
from app.services.orchestrator import Orchestrator

router = APIRouter()


def _progress(queue: asyncio.Queue) -> Callable[[str, str, int], Awaitable[None]]:
    async def cb(stage: str, message: str, percent: int) -> None:
        await queue.put({"type": "progress", "stage": stage, "message": message, "percent": percent})

    return cb


async def _sse_run(coro: Awaitable, queue: asyncio.Queue) -> AsyncIterator[str]:
    """把编排任务包成 SSE 流：progress* → done/error 收尾（二者必居其一）。"""
    async def worker() -> None:
        try:
            result = await coro
            await queue.put({"type": "done", "session": result.model_dump()})
        except AppError as e:
            await queue.put({"type": "error", "error": {"code": e.code, "message": e.message}})
        except Exception:  # 兜底：任何异常都不静默断流
            await queue.put({"type": "error", "error": {"code": "INTERNAL_ERROR", "message": "服务内部错误"}})
        finally:
            await queue.put(None)  # 结束哨兵

    task = asyncio.create_task(worker())
    try:
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    finally:
        if not task.done():
            task.cancel()


def _own_session(orch: Orchestrator, session_id: str, user_id: str) -> SessionMeta:
    """读取会话并强制归属校验；他人会话按不存在处理（404，不泄露）。"""
    meta = orch.get(session_id)
    if meta.owner_id != user_id:
        raise AppError("SESSION_NOT_FOUND", "会话不存在", 404)
    return meta


@router.post("/sessions")
async def create_session(req: SessionCreate, user_id: str = Depends(get_current_user),
                         orch: Orchestrator = Depends(get_orchestrator)):
    meta = orch.create(req, owner_id=user_id)
    return meta.model_dump()


@router.get("/sessions")
async def list_sessions(user_id: str = Depends(get_current_user),
                        orch: Orchestrator = Depends(get_orchestrator)):
    return [m.model_dump() for m in orch.list_sessions(user_id)]


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user_id: str = Depends(get_current_user),
                      orch: Orchestrator = Depends(get_orchestrator)):
    return _own_session(orch, session_id, user_id).model_dump()


@router.post("/sessions/{session_id}/continue")
async def continue_session(session_id: str, user_id: str = Depends(get_current_user),
                           orch: Orchestrator = Depends(get_orchestrator)):
    _own_session(orch, session_id, user_id)
    return orch.continue_session(session_id).model_dump()


@router.post("/sessions/{session_id}/execute/{stage}")
async def execute_stage(session_id: str, stage: str, user_id: str = Depends(get_current_user),
                        orch: Orchestrator = Depends(get_orchestrator)):
    _own_session(orch, session_id, user_id)
    queue: asyncio.Queue = asyncio.Queue()
    progress = _progress(queue)
    coro = orch.execute_stage(session_id, stage, progress)
    return StreamingResponse(_sse_run(coro, queue), media_type="text/event-stream")


@router.post("/sessions/{session_id}/intervene")
async def intervene(session_id: str, body: dict, user_id: str = Depends(get_current_user),
                    orch: Orchestrator = Depends(get_orchestrator)):
    _own_session(orch, session_id, user_id)
    stage = body.get("stage", "")
    modifications = body.get("modifications", {})
    if not isinstance(modifications, dict):
        raise AppError("VALIDATION_ERROR", "modifications 必须是对象")
    queue: asyncio.Queue = asyncio.Queue()
    progress = _progress(queue)
    coro = orch.intervene(session_id, stage, modifications, progress)
    return StreamingResponse(_sse_run(coro, queue), media_type="text/event-stream")


@router.get("/sessions/{session_id}/artifact/{stage}")
async def get_artifact(session_id: str, stage: str, user_id: str = Depends(get_current_user),
                       orch: Orchestrator = Depends(get_orchestrator)):
    meta = _own_session(orch, session_id, user_id)
    artifact = meta.artifacts.get(stage)
    if artifact is None:
        raise AppError("ARTIFACT_NOT_FOUND", f"阶段 {stage} 产物不存在", 404)
    return artifact


@router.get("/sessions/{session_id}/export")
async def export_session(session_id: str, user_id: str = Depends(get_current_user),
                         orch: Orchestrator = Depends(get_orchestrator)):
    meta = _own_session(orch, session_id, user_id)
    post = meta.artifacts.get("post_production") or {}
    final = post.get("final_video") or ""
    if not final:
        raise AppError("NO_FINAL", "成片尚未生成", 404)
    return FileResponse(final, media_type="video/mp4", filename="成片.mp4")


@router.get("/sessions/{session_id}/media/{kind}/{filename}")
async def get_media(session_id: str, kind: str, filename: str, user_id: str = Depends(get_current_user),
                    orch: Orchestrator = Depends(get_orchestrator)):
    """产物媒体文件服务：鉴权 + 归属校验 + 安全文件名。kind ∈ image/video/script。"""
    _own_session(orch, session_id, user_id)
    base_map = {"image": config.IMAGE_DIR, "video": config.VIDEO_DIR, "script": config.SCRIPT_DIR}
    base = base_map.get(kind)
    if base is None:
        raise AppError("INVALID_MEDIA_KIND", "未知媒体类型", 400)
    safe_filename(filename)
    path = base / session_id / filename
    if not path.is_file():
        raise AppError("MEDIA_NOT_FOUND", "媒体文件不存在", 404)
    return FileResponse(path)
