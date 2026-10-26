"""邀请码所属账号的内测配额状态（不返回凭证）。"""
from decimal import Decimal

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.services import trial_controls, f1_controls
from app.services.trial_events import consented, PLANS, open_invite

router = APIRouter()


@router.get("/trial/status")
async def status(user_id: str = Depends(get_current_user)) -> dict:
    open_invite(user_id)
    agreed = consented(user_id)
    current = trial_controls.overview(user_id)
    if f1_controls.enabled():
        estimates = ({"three_shot": trial_controls.estimate_for("three_shot", user_id)}
                     if f1_controls.allowed(user_id) else {})
    else:
        estimates = {"comic": trial_controls.estimate_for("comic"), "three_shot": trial_controls.estimate_for("three_shot")}
    budget_left = (Decimal(str(current["site_fuse_cny"])) - Decimal(str(current["site_actual_cny"]))
                   - Decimal(str(current["site_reserved_cny"])))
    can_afford = any(Decimal(str(value)) <= Decimal(str(current["max_estimate_cny"]))
                     and Decimal(str(value)) <= budget_left for value in estimates.values())
    generation_enabled = agreed and not current["blocked"] and can_afford
    return {**current, "quota_remaining": current["remaining"], "mode": "real" if f1_controls.enabled() else "mock",
            "participant_code": current.get("invite_label"), "consented": agreed, "consent_ok": agreed,
            "generation_enabled": generation_enabled,
            "blocked_message": current["blocked_message"] or (trial_controls.FULL_MESSAGE if agreed and not can_afford else None),
            "plans": PLANS, "estimated_charge_cny": estimates.get("comic", estimates.get("three_shot")),
            "actual_charge_cny": current["site_actual_cny"] if f1_controls.enabled() else 0,
            "estimate_kind": "conservative_reservation" if f1_controls.enabled() else "mock_fixture",
            "estimates": estimates}
