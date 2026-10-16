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
from app.collector import bsrv_extra, bsrv_restic_old
from app.config import get_settings
from app.deps import SessionDep
from app.models import Server
from app.schemas import AnsibleNodeOut, AnsibleServerOut, AnsibleServersOut
from app.security import hash_agent_token
from app.setup_scripts import current_setup_versions

router = APIRouter(prefix="/ansible", tags=["ansible"])


def _issues(rep: dict, now: datetime) -> list[str]:
    """Что на ноде чинится плейбуком или командой, кроме helper'ов: группы kervax_docker_sock,
    kervax_proxy_old, kervax_restic_old."""
    out = []
    if docker_exposure.exposed(rep):
        out.append("docker_sock")
    if docker_exposure.outdated(rep, now.timestamp()):
        out.append("proxy_old")
    if bsrv_restic_old(rep):
        out.append("restic_old")
    return out


def node_of(s: Server, cur: dict[str, str], now: datetime) -> AnsibleNodeOut | None:
    """Нода, как ее видит инвентарь ansible, с подробностями для окна панели: что именно
    обновит плейбук и что за проблема в группах kervax_<проблема>. None - агент ни разу не
    прислал отчет, ставить helper'ы некуда."""
    rep = s.last_report or {}
    if not rep:
        return None
    advice = _helper_advice(s, cur)
    online = _is_online(s, now)
    issues = _issues(rep, now) if online else []
    restic = bsrv_restic_old(rep) if "restic_old" in issues else ""
    return AnsibleNodeOut(
        id=s.id, name=s.name, hostname=str(rep.get("hostname") or ""),
        ips=[ip for ip in dict.fromkeys([s.external_ip, s.agent_ip, s.local_ip]) if ip],
        online=online, enabled=bool(s.enabled),
        rollout=online and helper_rollout(s, advice),
        outdated=advice,
        watchdog_off=((rep.get("caps") or {}).get("watchdog") is False),
        issues=issues,
        sock=docker_exposure.exposed(rep) if "docker_sock" in issues else [],
        proxy_old=docker_exposure.outdated(rep, now.timestamp()) if "proxy_old" in issues else [],
        restic=restic,
        restic_target=str(bsrv_extra(rep).get("restic_target") or "") if restic else "",
    )


async def nodes(session, now: datetime) -> list[AnsibleNodeOut]:
    cur = current_setup_versions()
    out = []
    for s in await session.scalars(select(Server).order_by(Server.name)):
        n = node_of(s, cur, now)
        if n is not None:
            out.append(n)
    return out


@router.get("/servers", response_model=AnsibleServersOut)
async def ansible_servers(request: Request, session: SessionDep) -> AnsibleServersOut:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    acc = await settings_store.get_ansible_access(session)
    want = acc.get("hash") or ""
    if not token or not want or not hmac.compare_digest(hash_agent_token(token), want):
        raise HTTPException(401, "Токен ansible не подошел или доступ выключен")

    now = datetime.now(timezone.utc)
    out = [AnsibleServerOut(name=n.name, hostname=n.hostname, ips=n.ips, online=n.online, enabled=n.enabled,
                            rollout=n.rollout, outdated=[a.name for a in n.outdated], issues=n.issues)
           for n in await nodes(session, now)]
    acc["used_at"] = now.isoformat()
    await settings_store.set_ansible_access(session, acc)
    return AnsibleServersOut(version=get_settings().version, servers=out)
