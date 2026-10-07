from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.services.model_catalog import public_catalog

router = APIRouter()


@router.get("/models")
async def list_models(user_id: str = Depends(get_current_user)):
    return public_catalog()
