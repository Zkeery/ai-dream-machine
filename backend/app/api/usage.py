"""Account-isolated generation budget summaries."""
from fastapi import APIRouter, Depends

from app.api.deps import get_current_user, get_admin_user
from app.services import cost_control

router = APIRouter()


@router.get("/usage")
async def account_usage(month: str | None = None, user_id: str = Depends(get_current_user)):
    return cost_control.usage(user_id, month)


@router.get("/admin/usage")
async def platform_usage(month: str | None = None, user_id: str = Depends(get_admin_user)):
    return cost_control.usage(None, month)
