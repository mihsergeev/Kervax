from datetime import datetime, timezone

from fastapi import APIRouter

from app import audit, settings_store
from app.config import get_settings
from app.deps import AdminUser, SessionDep
from app.schemas import AnsibleAccessOut, AnsibleTokenOut, RetentionConfig
from app.security import generate_agent_token, hash_agent_token

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


# Доступ ansible: он спрашивает у панели список нод (GET /api/ansible/servers) и сам
# собирает группы kervax / kervax_outdated. Так одна команда обновляет helper'ы на
# нодах всех панелей сразу, без копирования списка хостов из каждой.
@router.get("/ansible", response_model=AnsibleAccessOut)
async def get_ansible_access(_: AdminUser, session: SessionDep) -> AnsibleAccessOut:
    acc = await settings_store.get_ansible_access(session)
    return AnsibleAccessOut(enabled=bool(acc.get("hash")), created_at=acc.get("created_at"),
                            used_at=acc.get("used_at"))


@router.post("/ansible", response_model=AnsibleTokenOut)
async def issue_ansible_token(user: AdminUser, session: SessionDep) -> AnsibleTokenOut:
    """Новый токен. Старый при этом перестает работать."""
    token = generate_agent_token()
    now = datetime.now(timezone.utc).isoformat()
    await settings_store.set_ansible_access(session, {"hash": hash_agent_token(token),
                                                      "created_at": now})
    await audit.record(session, user.username, "ansible_token_issue", "")
    return AnsibleTokenOut(token=token, created_at=now)


@router.delete("/ansible", status_code=204)
async def revoke_ansible_token(user: AdminUser, session: SessionDep) -> None:
    await settings_store.set_ansible_access(session, None)
    await audit.record(session, user.username, "ansible_token_revoke", "")
