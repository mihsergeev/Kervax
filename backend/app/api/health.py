from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(tags=["system"])


@router.get("/health")
async def health() -> dict[str, str]:
    settings = get_settings()
    return {"status": "ok", "version": settings.version}
