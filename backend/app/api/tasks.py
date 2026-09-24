# -*- coding: utf-8 -*-
"""短管线任务路由：创建、列表、查询、进度订阅（SSE）、导出。"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import get_current_user
from app.core.errors import AppError
from app.schemas.task import TaskCreate, TaskMeta
from app.services import auth, task_store
from app.services.short_pipelines import ShortPipelines

router = APIRouter()

_pipelines: ShortPipelines | None = None


def get_pipelines() -> ShortPipelines:
    global _pipelines
    if _pipelines is None:
        _pipelines = ShortPipelines()
    return _pipelines


def _resolve_inputs(user_id: str, ptype: str, inputs: dict) -> dict:
    """把上传文件名解析为本地路径，并校验归属；返回解析后的 input。"""
    resolved = dict(inputs)
    if ptype == "motion_transfer":
        for key in ("character_image", "motion_video"):
            name = resolved.get(key)
            if not name:
                raise AppError("VALIDATION_ERROR", f"缺少 {key}")
            resolved[key] = str(auth.require_upload_owner(name, user_id))
    elif ptype == "talking_head":
        name = resolved.get("person_image")
        if not name:
            raise AppError("VALIDATION_ERROR", "缺少 person_image")
        resolved["person_image"] = str(auth.require_upload_owner(name, user_id))
    return resolved


async def _run_task(meta: TaskMeta, pipelines: ShortPipelines) -> None:
    """后台执行：推进状态、上报进度、持久化结果；结束放哨兵。"""
    queue = task_store.get_queue(meta.task_id)
    async def progress(stage: str, message: str, percent: int) -> None:
        await queue.put({"type": "progress", "stage": stage, "message": message, "percent": percent})

    try:
        task_store.mark_running(meta)
        result = await pipelines.run(meta.task_id, meta.type, meta.input, progress)
        task_store.mark_completed(meta, result)
        await queue.put({"type": "done", "task": meta.model_dump()})
    except AppError as e:
        task_store.mark_failed(meta, e.message)
        await queue.put({"type": "error", "error": {"code": e.code, "message": e.message}})
    except Exception:  # 兜底：不静默
        task_store.mark_failed(meta, "服务内部错误")
        await queue.put({"type": "error", "error": {"code": "INTERNAL_ERROR", "message": "服务内部错误"}})
    finally:
        await queue.put(None)


async def _sse_stream(task_id: str, meta: TaskMeta) -> AsyncIterator[str]:
    """SSE：历史进度 + 实时进度，直到 done/error 收尾。"""
    queue = task_store.get_queue(task_id)
    if meta.status in ("completed", "failed"):
        ev = {"type": "done", "task": meta.model_dump()} if meta.status == "completed" \
            else {"type": "error", "error": {"code": "TASK_FAILED", "message": meta.error or "任务失败"}}
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        return
    try:
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    finally:
        task_store.drop_queue(task_id)


def _own_task(task_id: str, user_id: str) -> TaskMeta:
    meta = task_store.load_task(task_id)
    if meta.owner_id != user_id:
        raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
    return meta


@router.post("/tasks")
async def create_task(req: TaskCreate, user_id: str = Depends(get_current_user)) -> dict:
    inputs = _resolve_inputs(user_id, req.type, req.input)
    meta = TaskMeta(task_id=str(int(time.time() * 1000)), type=req.type, owner_id=user_id, input=inputs)
    task_store.create_task(meta)
    # 后台启动
    asyncio.create_task(_run_task(meta, get_pipelines()))
    return {"task_id": meta.task_id}


@router.get("/tasks")
async def list_tasks(user_id: str = Depends(get_current_user)) -> list[dict]:
    return [m.model_dump() for m in task_store.list_tasks(user_id)]


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, user_id: str = Depends(get_current_user)) -> dict:
    return _own_task(task_id, user_id).model_dump()


@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, user_id: str = Depends(get_current_user)) -> StreamingResponse:
    meta = _own_task(task_id, user_id)
    return StreamingResponse(_sse_stream(task_id, meta), media_type="text/event-stream")


@router.get("/tasks/{task_id}/export")
async def export_task(task_id: str, user_id: str = Depends(get_current_user)) -> FileResponse:
    meta = _own_task(task_id, user_id)
    final = (meta.result or {}).get("final_video") or ""
    if not final:
        raise AppError("NO_FINAL", "成片尚未生成", 404)
    return FileResponse(final, media_type="video/mp4", filename="成片.mp4")
