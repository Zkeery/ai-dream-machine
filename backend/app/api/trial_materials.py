"""内测同意、埋点、假门与问卷；无需支付或第三方统计。"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from app.api.deps import get_current_user
from app.core.errors import AppError
from app.services import trial_events

router = APIRouter()


class ConsentBody(BaseModel):
    accepted: bool


class EventBody(BaseModel):
    event: str
    payload: dict = Field(default_factory=dict)


class SurveyBody(BaseModel):
    fields: dict


@router.get("/trial/plans")
async def plans(user_id: str = Depends(get_current_user)):
    return {"plans": trial_events.PLANS, "confirmation_required": any(p["confirmation_required"] for p in trial_events.PLANS)}


@router.post("/trial/consent")
async def consent(body: ConsentBody, user_id: str = Depends(get_current_user)):
    if not body.accepted:
        raise AppError("CONSENT_REQUIRED", "同意知情说明后才可进入创作", 403)
    trial_events.accept_consent(user_id)
    return {"ok": True, "consented": True, "policy_version": trial_events.POLICY_VERSION}


@router.post("/trial/events")
async def event(body: EventBody, user_id: str = Depends(get_current_user)):
    if body.event == "invite_open":
        trial_events.open_invite(user_id)
        return {"ok": True, "recorded": True}
    if not trial_events.consented(user_id):
        raise AppError("CONSENT_REQUIRED", "请先同意知情说明", 403)
    if body.event not in {"create_start", "estimate_shown", "paywall_click"}:
        raise AppError("TRIAL_EVENT_SERVER_ONLY", "该事件由服务端流程记录", 403)
    if body.event == "create_start" and "project_type" in body.payload:
        body.payload["kind"] = body.payload.pop("project_type")
    if body.event == "paywall_click" and body.payload.get("plan_id") not in {p["id"] for p in trial_events.PLANS}:
        raise AppError("TRIAL_PLAN_INVALID", "请选择已展示的套餐档位", 422)
    task_id = body.payload.get("task_id")
    if task_id:
        from app.services import trial_controls
        if not trial_controls.generation(task_id, user_id):
            raise AppError("TRIAL_TASK_NOT_FOUND", "作品不存在", 404)
    event_id = trial_events.record_event(user_id, body.event, body.payload)
    return {"ok": True, "recorded": True, "event_id": event_id, "message": "内测免费，没有扣费，谢谢你的选择" if body.event == "paywall_click" else "已记录"}


@router.post("/trial/survey")
async def survey(body: SurveyBody, user_id: str = Depends(get_current_user)):
    return {"ok": True, "survey_id": trial_events.save_survey(user_id, body.fields), "saved": True}


@router.post("/trial/deletion-request")
async def deletion(user_id: str = Depends(get_current_user)):
    return {"ok": True, "request_id": trial_events.request_deletion(user_id), "status": "pending",
            "message": "删除申请已记录，作者会处理你的内测数据删除请求"}
