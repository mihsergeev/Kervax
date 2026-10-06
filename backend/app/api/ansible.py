"""Список нод для инвентаря ansible.

Helper'ы на нодах выкатывает человек плейбуком kervax_helpers.yml: панель сама root-
скрипты не запускает (её взлом означал бы root на всем парке). Раньше хосты для
плейбука брались копированием из "Требует действий" каждой панели. Теперь плагин
инвентаря в репо ansible спрашивает их здесь, у всех панелей сразу.

Доступ - отдельным токеном только на чтение (выпускает админ в настройках). Токен
открывает ровно этот список: имена и адреса нод, без секретов и без управления.
"""

from datetime import datetime, timezone
import hmac

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app import docker_exposure, settings_store
from app.api.servers import _helper_advice, _is_online, helper_rollout
from app.config import get_settings
from app.deps import SessionDep
from app.models import Server
from app.schemas import AnsibleServerOut, AnsibleServersOut
from app.security import hash_agent_token
from app.setup_scripts import current_setup_versions

router = APIRouter(prefix="/ansible", tags=["ansible"])


@router.get("/servers", response_model=AnsibleServersOut)
async def ansible_servers(request: Request, session: SessionDep) -> AnsibleServersOut:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    acc = await settings_store.get_ansible_access(session)
    want = acc.get("hash") or ""
    if not token or not want or not hmac.compare_digest(hash_agent_token(token), want):
        raise HTTPException(401, "Токен ansible не подошел или доступ выключен")

    now = datetime.now(timezone.utc)
    cur = current_setup_versions()
    out: list[AnsibleServerOut] = []
    for s in await session.scalars(select(Server).order_by(Server.name)):
        rep = s.last_report or {}
        if not rep:
            continue  # агент ни разу не прислал отчет - ставить helper'ы некуда
        advice = _helper_advice(s, cur)
        online = _is_online(s, now)
        ips = [ip for ip in dict.fromkeys([s.external_ip, s.agent_ip, s.local_ip]) if ip]
        out.append(AnsibleServerOut(
            name=s.name, hostname=str(rep.get("hostname") or ""), ips=ips,
            online=online, enabled=bool(s.enabled),
            rollout=online and helper_rollout(s, advice),
            outdated=[a.name for a in advice],
            issues=["docker_sock"] if online and docker_exposure.exposed(rep) else [],
        ))
    acc["used_at"] = now.isoformat()
    await settings_store.set_ansible_access(session, acc)
    return AnsibleServersOut(version=get_settings().version, servers=out)
