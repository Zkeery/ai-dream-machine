# -*- coding: utf-8 -*-
"""短管线任务路由：创建、列表、查询、进度订阅（SSE）、导出。"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import get_current_user
from app.core import config
from app.core.errors import AppError
from app.core.path_security import ensure_inside
from app.schemas.task import TaskCreate, TaskMeta, talking_input
from app.services import auth, execution_store, task_store, model_catalog, recovery, provider_jobs
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
    if set(resolved) & {"provider", "baseURL", "base_url", "api_key", "model_usage"}:
        raise AppError("INVALID_MODEL_SELECTION", "供应商与模型鉴权配置由服务端管理", 422)
    mode = talking_input(resolved)[0] if ptype == "talking_head" else "static"
    resolved["model_selection"] = model_catalog.validate_selection(resolved.get("model_selection"), task_type=ptype, talking_mode=mode)
    resolved["model_usage"] = model_catalog.task_usage(ptype, resolved)
    if ptype == "motion_transfer":
        if not isinstance(resolved.get("prompt"), str) or not resolved["prompt"].strip():
            raise AppError("VALIDATION_ERROR", "请填写动作描述", 422)
        for key in ("character_image", "motion_video"):
            name = resolved.get(key)
            if not name:
                if key == "motion_video":
                    continue
                raise AppError("VALIDATION_ERROR", f"缺少 {key}")
            resolved[key] = str(auth.require_upload_owner(name, user_id))
    elif ptype == "talking_head":
        name = resolved.get("person_image")
        if not name:
            raise AppError("VALIDATION_ERROR", "缺少 person_image")
        resolved["person_image"] = str(auth.require_upload_owner(name, user_id))
    return resolved


def _task_json(meta: TaskMeta) -> dict:
    return {**meta.model_dump(), "execution": execution_store.snapshot("task", meta.task_id)}


def _own_task(task_id: str, user_id: str) -> TaskMeta:
    meta = task_store.load_task(task_id)
    if meta.owner_id != user_id:
        raise AppError("TASK_NOT_FOUND", "任务不存在", 404)
    return meta


@router.post("/tasks")
async def create_task(req: TaskCreate, user_id: str = Depends(get_current_user),
                      idempotency_key: str | None = Header(None)) -> dict:
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 128):
        raise AppError("VALIDATION_ERROR", "Idempotency-Key 需为 1 到 128 个字符")
    inputs = _resolve_inputs(user_id, req.type, req.input)
    meta = TaskMeta(task_id=uuid.uuid4().hex, type=req.type, owner_id=user_id, input=inputs)
    task_id, execution_id, created = execution_store.create_task(meta, idempotency_key)
    if created:
        async def run() -> dict:
            recovery.capture(execution_id, recovery.task_input(meta, meta.input))
            result = await get_pipelines().run(meta.task_id, meta.type, meta.input,
                                               execution_store.progress(execution_id))
            task_store.mark_completed(meta, result)
            return {"type": "done", "task": meta.model_dump()}
        execution_store.launch(execution_id, run)
    return {"task_id": task_id}


@router.get("/tasks")
async def list_tasks(user_id: str = Depends(get_current_user)) -> list[dict]:
    return [_task_json(m) for m in task_store.list_tasks(user_id)]


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, user_id: str = Depends(get_current_user)) -> dict:
    return _task_json(_own_task(task_id, user_id))


@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, user_id: str = Depends(get_current_user),
                      last_event_id: str | None = Header(None)) -> StreamingResponse:
    meta = _own_task(task_id, user_id)
    execution = execution_store.snapshot("task", task_id)
    if execution:
        try:
            cursor = max(0, int(last_event_id or 0))
        except ValueError:
            raise AppError("VALIDATION_ERROR", "Last-Event-ID 必须是事件编号")
        return StreamingResponse(execution_store.stream(execution["execution_id"], cursor),
                                 media_type="text/event-stream")
    # Historical completed tasks remain readable without a new execution or model call.
    import json
    async def historical():
        ev = {"type": "done", "task": meta.model_dump()} if meta.status == "completed" else \
             {"type": "error", "error": {"code": "TASK_FAILED", "message": meta.error or "任务未完成，请查询状态"}}
        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    return StreamingResponse(historical(), media_type="text/event-stream")


@router.get("/tasks/{task_id}/recovery")
async def task_recovery(task_id: str, user_id: str = Depends(get_current_user)):
    return recovery.status(_own_task(task_id, user_id), "task")


@router.post("/tasks/{task_id}/resume")
async def resume_task(task_id: str, body: recovery.ResumeRequest,
                      user_id: str = Depends(get_current_user),
                      idempotency_key: str | None = Header(None)):
    meta = _own_task(task_id, user_id)
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 128):
        raise AppError("VALIDATION_ERROR", "Idempotency-Key 需为 1 到 128 个字符")
    _, request = recovery.prepare(meta, "task", body.execution_id)
    execution_id, created = execution_store.claim_resume(body.execution_id, user_id, idempotency_key)
    if created:
        async def run() -> dict:
            current = _own_task(task_id, user_id)
            recovery.verify(body.execution_id, recovery.task_input(current, request))
            recovery.copy_snapshot(body.execution_id, execution_id)
            with provider_jobs.resume_scope(body.execution_id, user_id, allow_new=True):
                result = await get_pipelines().run(task_id, current.type, request, execution_store.progress(execution_id))
            task_store.mark_completed(current, result)
            return {"type": "done", "task": current.model_dump()}
        execution_store.launch(execution_id, run)
    return {"task_id": task_id}


@router.get("/tasks/{task_id}/export")
async def export_task(task_id: str, user_id: str = Depends(get_current_user)) -> FileResponse:
    meta = _own_task(task_id, user_id)
    final = (meta.result or {}).get("final_video") or ""
    if not final:
        raise AppError("NO_FINAL", "成片尚未生成", 404)
    return FileResponse(final, media_type="video/mp4", filename="成片.mp4")


@router.get("/tasks/{task_id}/audio")
async def task_audio(task_id: str, user_id: str = Depends(get_current_user)) -> FileResponse:
    meta = _own_task(task_id, user_id)
    value = (meta.result or {}).get("audio")
    if not isinstance(value, str) or not value:
        raise AppError("NO_AUDIO", "该任务尚未生成可播放的声音", 404)
    try:
        path = ensure_inside(config.VIDEO_DIR / task_id, Path(value))
    except AppError:
        raise AppError("NO_AUDIO", "该任务声音文件不可用", 404) from None
    if not path.is_file() or path.suffix.lower() not in {".mp3", ".m4a"}:
        raise AppError("NO_AUDIO", "该任务声音文件不可用", 404)
    return FileResponse(path, media_type="audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/mp4")
