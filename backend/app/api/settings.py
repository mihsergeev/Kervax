from fastapi import APIRouter

from app import audit, settings_store
from app.config import get_settings
from app.deps import AdminUser, SessionDep
from app.schemas import RetentionConfig

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/retention", response_model=RetentionConfig)
async def get_retention(_: AdminUser, session: SessionDep) -> RetentionConfig:
    ret = await settings_store.get_retention(session, get_settings())
    return RetentionConfig(**ret)


@router.put("/retention", response_model=RetentionConfig)
async def put_retention(
    body: RetentionConfig, user: AdminUser, session: SessionDep
) -> RetentionConfig:
    await settings_store.set_retention(session, body.server_days, body.sample_days)
    await audit.record(session, user.username, "retention_config", "")
    ret = await settings_store.get_retention(session, get_settings())
    return RetentionConfig(**ret)
