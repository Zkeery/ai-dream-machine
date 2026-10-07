# -*- coding: utf-8 -*-
"""会话路由：创建、列表、查询、继续、执行阶段（SSE）、干预、产物、导出。
第三刀起全部会话接口要求登录，并强制账号隔离（只能访问自己的会话）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Header
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.api.deps import get_current_user, get_orchestrator
from app.core import config
from app.core.errors import AppError
from app.core.path_security import safe_filename, ensure_inside
from app.schemas.session import SessionCreate, SessionMeta, SessionKnowledgeUpdate
from app.schemas.models import SessionModelsUpdate
from app.services import execution_store, recovery, provider_jobs
from app.services.orchestrator import Orchestrator

router = APIRouter()


def _public_session_dump(meta: SessionMeta) -> dict:
    """对外会话快照：只有完整生成成功才算版本。"""
    from app.services.orchestrator import Orchestrator
    data = meta.model_dump()
    data["agent_runs"] = {
        stage: [{**{key: run[key] for key in ("execution_id", "status", "started_at", "decisions", "plan", "events", "reviews") if key in run},
                 "tasks": {task_id: {key: task[key] for key in ("status", "handoff") if key in task}
                           for task_id, task in run.get("tasks", {}).items()}}
                for run in runs]
        for stage, runs in meta.agent_runs.items()
    }
    data["model_selection"] = meta.model_selection.model_dump(exclude_none=True)
    data["stage_order"] = Orchestrator.stage_order(meta)
    public = Orchestrator.public_versions(meta)
    data["artifact_versions"] = public
    data["selected_versions"] = {
        stage: version_id
        for stage, version_id in (data.get("selected_versions") or {}).items()
        if version_id in {item["version_id"] for item in public.get(stage, [])}
    }
    return data


def _session_json(meta: SessionMeta) -> dict:
    from app.services.knowledge_retrieval import context_changes
    warnings = []
    for stage in ("script_generation", "storyboard", "comic_storyboard"):
        artifact = meta.artifacts.get(stage) or {}
        context = artifact.get("knowledge_context")
        if context and meta.owner_id:
            warnings.extend(context_changes(meta.owner_id, context))
        if artifact and set((context or {}).get("library_ids", [])) != set(meta.knowledge_library_ids):
            warnings.append("当前知识库选择与已有产物不同，重新生成后才会使用新选择。")
    return {**_public_session_dump(meta), "stage_order": Orchestrator.stage_order(meta),
            "execution": execution_store.snapshot("session", meta.session_id),
            "knowledge_status": {"changed": bool(warnings), "warnings": list(dict.fromkeys(warnings))}}


def _not_running(session_id: str) -> None:
    if execution_store.active("session", session_id):
        raise AppError("SESSION_RUNNING", "当前项目正在执行，请等待结束后再操作", 409)


def _validate_request_key(key: str | None) -> None:
    if key is not None and (not key.strip() or len(key) > 128):
        raise AppError("VALIDATION_ERROR", "Idempotency-Key 需为 1 到 128 个字符")


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
    return _session_json(meta)


@router.get("/comic/voices")
async def comic_voices(user_id: str = Depends(get_current_user)):
    from app.models.tts_client import TTSClient
    return {"voices": TTSClient.list_voices(), "emotion_supported": False}


@router.get("/sessions")
async def list_sessions(user_id: str = Depends(get_current_user),
                        orch: Orchestrator = Depends(get_orchestrator)):
    return [_session_json(m) for m in orch.list_sessions(user_id)]


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, user_id: str = Depends(get_current_user),
                      orch: Orchestrator = Depends(get_orchestrator)):
    return _session_json(_own_session(orch, session_id, user_id))


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, user_id: str = Depends(get_current_user)):
    from app.services import session_store
    session_store.delete_session(session_id, user_id)
    return Response(status_code=204)


@router.post("/sessions/{session_id}/continue")
async def continue_session(session_id: str, user_id: str = Depends(get_current_user),
                           orch: Orchestrator = Depends(get_orchestrator)):
    _own_session(orch, session_id, user_id)
    _not_running(session_id)
    return _session_json(orch.continue_session(session_id))


@router.patch("/sessions/{session_id}/models")
async def update_models(session_id: str, req: SessionModelsUpdate,
                        user_id: str = Depends(get_current_user)):
    from app.services import session_store
    return _session_json(session_store.update_models(session_id, user_id, req.model_selection))


@router.patch("/sessions/{session_id}/knowledge")
async def update_knowledge(session_id: str, req: SessionKnowledgeUpdate,
                           user_id: str = Depends(get_current_user),
                           orch: Orchestrator = Depends(get_orchestrator)):
    from app.services import knowledge_retrieval, session_store
    meta = _own_session(orch, session_id, user_id)
    _not_running(session_id)
    ids = list(dict.fromkeys(req.knowledge_library_ids))
    knowledge_retrieval.validate_libraries(user_id, ids)
    meta.knowledge_library_ids = ids
    session_store.touch(meta)
    return _session_json(meta)


@router.post("/sessions/{session_id}/execute/{stage}")
async def execute_stage(session_id: str, stage: str, user_id: str = Depends(get_current_user),
                        orch: Orchestrator = Depends(get_orchestrator),
                        idempotency_key: str | None = Header(None)):
    meta = _own_session(orch, session_id, user_id)
    if stage not in Orchestrator.stage_order(meta):
        raise AppError("UNKNOWN_STAGE", "未知阶段", 404)
    _validate_request_key(idempotency_key)
    execution_id, created = execution_store.claim_session(session_id, stage, "generate", {}, idempotency_key)
    if created:
        async def run() -> dict:
            recovery.capture(execution_id, recovery.session_input(orch.get(session_id), stage, {}))
            result = await orch.execute_stage(session_id, stage, execution_store.progress(execution_id))
            return {"type": "done", "session": _public_session_dump(result)}
        execution_store.launch(execution_id, run)
    return StreamingResponse(execution_store.stream(execution_id), media_type="text/event-stream")


@router.post("/sessions/{session_id}/intervene")
async def intervene(session_id: str, body: dict, user_id: str = Depends(get_current_user),
                    orch: Orchestrator = Depends(get_orchestrator),
                    idempotency_key: str | None = Header(None)):
    meta = _own_session(orch, session_id, user_id)
    stage = body.get("stage", "")
    modifications = body.get("modifications", {})
    if not isinstance(modifications, dict):
        raise AppError("VALIDATION_ERROR", "modifications 必须是对象")
    operation = modifications.get("operation", "regenerate")
    if stage not in Orchestrator.stage_order(meta):
        raise AppError("UNKNOWN_STAGE", "未知阶段", 404)
    if operation not in ("save", "select", "regenerate"):
        raise AppError("VALIDATION_ERROR", "operation 必须为 save、select 或 regenerate")
    _validate_request_key(idempotency_key)
    execution_id, created = execution_store.claim_session(session_id, stage, operation, modifications, idempotency_key)
    if created:
        async def run() -> dict:
            if operation == "regenerate":
                recovery.capture(execution_id, recovery.session_input(orch.get(session_id), stage, modifications))
            result = await orch.intervene(session_id, stage, modifications, execution_store.progress(execution_id))
            return {"type": "done", "session": _public_session_dump(result)}
        execution_store.launch(execution_id, run)
    return StreamingResponse(execution_store.stream(execution_id), media_type="text/event-stream")


@router.get("/sessions/{session_id}/recovery")
async def session_recovery(session_id: str, user_id: str = Depends(get_current_user),
                           orch: Orchestrator = Depends(get_orchestrator)):
    return recovery.status(_own_session(orch, session_id, user_id), "session")


@router.post("/sessions/{session_id}/resume")
async def resume_session(session_id: str, body: recovery.ResumeRequest,
                         user_id: str = Depends(get_current_user),
                         orch: Orchestrator = Depends(get_orchestrator),
                         idempotency_key: str | None = Header(None)):
    meta = _own_session(orch, session_id, user_id)
    _validate_request_key(idempotency_key)
    source, request = recovery.prepare(meta, "session", body.execution_id)
    if source["operation"] not in {"generate", "regenerate"}:
        raise AppError("RESUME_NOT_AVAILABLE", "此操作不支持生成任务恢复", 409)
    execution_id, created = execution_store.claim_resume(body.execution_id, user_id, idempotency_key)
    if created:
        async def run() -> dict:
            # Recheck after the atomic claim: a model/input change racing the
            # preflight must stop before any provider request.
            recovery.verify(body.execution_id, recovery.session_input(orch.get(session_id), source["stage"], request))
            recovery.copy_snapshot(body.execution_id, execution_id)
            with provider_jobs.resume_scope(body.execution_id, user_id, allow_new=True,
                                            allow_empty=recovery._control_resume_allowed(meta, body.execution_id)):
                if source["operation"] == "generate":
                    result = await orch.execute_stage(session_id, source["stage"], execution_store.progress(execution_id))
                else:
                    result = await orch.intervene(session_id, source["stage"], request, execution_store.progress(execution_id))
            return {"type": "done", "session": _public_session_dump(result)}
        execution_store.launch(execution_id, run)
    return StreamingResponse(execution_store.stream(execution_id), media_type="text/event-stream")


@router.get("/sessions/{session_id}/stream")
async def stream_session(session_id: str, last_event_id: str | None = Header(None),
                         user_id: str = Depends(get_current_user),
                         orch: Orchestrator = Depends(get_orchestrator)):
    _own_session(orch, session_id, user_id)
    execution = execution_store.snapshot("session", session_id)
    if not execution:
        raise AppError("EXECUTION_NOT_FOUND", "此项目尚无执行记录", 404)
    try:
        cursor = max(0, int(last_event_id or 0))
    except ValueError:
        raise AppError("VALIDATION_ERROR", "Last-Event-ID 必须是事件编号")
    return StreamingResponse(execution_store.stream(execution["execution_id"], cursor),
                             media_type="text/event-stream")


@router.get("/sessions/{session_id}/artifact/{stage}")
async def get_artifact(session_id: str, stage: str, user_id: str = Depends(get_current_user),
                       orch: Orchestrator = Depends(get_orchestrator)):
    meta = _own_session(orch, session_id, user_id)
    if stage not in Orchestrator.stage_order(meta):
        raise AppError("UNKNOWN_STAGE", "当前项目类型不支持此阶段", 404)
    artifact = meta.artifacts.get(stage)
    if artifact is None:
        raise AppError("ARTIFACT_NOT_FOUND", f"阶段 {stage} 产物不存在", 404)
    return artifact


@router.get("/sessions/{session_id}/export")
async def export_session(session_id: str, version_id: str | None = None, user_id: str = Depends(get_current_user),
                         orch: Orchestrator = Depends(get_orchestrator)):
    meta = _own_session(orch, session_id, user_id)
    final_stage = Orchestrator.completion_stage(meta)
    post = meta.artifacts.get(final_stage) or {}
    if version_id:
        version = next((v for v in Orchestrator.public_versions(meta, final_stage)
                        if v.get("version_id") == version_id), None)
        if not version:
            raise AppError("VERSION_NOT_FOUND", "成片版本不存在", 404)
        post = version.get("artifact") or {}
    elif getattr(meta, "stale_stages", []):
        raise AppError("STALE_EXPORT", "成片依赖已更新，请重新生成成片，或导出明确的历史版本", 409)
    final = post.get("final_video") or ""
    if not final:
        raise AppError("NO_FINAL", "成片尚未生成", 404)
    final = ensure_inside((config.VIDEO_DIR / session_id).resolve(), Path(final))
    if not final.is_file() or final.suffix.lower() != ".mp4":
        raise AppError("NO_FINAL", "成片文件不存在", 404)
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
    path = ensure_inside((base / session_id).resolve(), base / session_id / filename)
    if not path.is_file():
        raise AppError("MEDIA_NOT_FOUND", "媒体文件不存在", 404)
    return FileResponse(path)
