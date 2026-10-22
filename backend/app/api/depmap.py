"""Карта зависимостей (окно под шестеренкой): кто за кем стоит и какие мониторы на каком сервере.

Связи те же, по которым панель придерживает алерты (app/parents.py): ручной родитель сервера
(шлюз, VPN, гипервизор) и фактический родитель монитора - найденный сам по однозначным
признакам или заданный руками. Только чтение, только свои группы и разделы.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter
from sqlalchemy import select

from app import parents as parents_mod
from app.collector import seen_online
from app.deps import CurrentUser, SessionDep, group_allowed, scope_query, user_sections
from app.models import Check

router = APIRouter(prefix="/depmap", tags=["depmap"])


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


@router.get("")
async def depmap(user: CurrentUser, session: SessionDep) -> dict:
    sections = set(user_sections(user))
    now = datetime.now(timezone.utc)
    # индекс строим по всем серверам, как алерты: иначе домен "на одном сервере" для учетки с
    # частью групп оказался бы однозначным там, где на деле серверов два
    servers = await parents_mod.load_servers(session)
    idx = parents_mod.Index(servers)
    visible = [s for s in servers if group_allowed(user, s.group_name, "servers")] if "servers" in sections else []
    vis = {s.id for s in visible}
    out_s = [{
        "id": s.id, "name": s.name, "group": s.group_name,
        "parent_id": s.parent_id if s.parent_id in vis and s.parent_id != s.id else None,
        "online": bool(s.enabled) and seen_online(s, now), "enabled": bool(s.enabled),
        "last_seen": _iso(s.last_seen),
    } for s in sorted(visible, key=lambda x: x.name.lower())]
    out_c = []
    if "sites" in sections:
        rows = (await session.execute(scope_query(user, select(
            Check.id, Check.name, Check.type, Check.target, Check.last_status, Check.probe_local,
            Check.probe_server_id, Check.parent_mode, Check.parent_server_id,
        ).where(Check.enabled.is_(True)), Check, "sites"))).all()
        cs = [SimpleNamespace(id=r.id, target=r.target, type=r.type, probe_local=r.probe_local,
                              probe_server_id=r.probe_server_id, parent_mode=r.parent_mode,
                              parent_server_id=r.parent_server_id) for r in rows]
        await parents_mod.resolve_for(idx, cs)
        for r, c in zip(rows, cs):
            # mode: off - отвязан руками (ни от какого сервера не зависит), иначе auto или server
            out_c.append({
                "id": r.id, "name": r.name, "type": r.type, "target": r.target or "",
                "status": r.last_status or "unknown", "mode": r.parent_mode or "auto",
                "parents": [{"id": pid, "why": why} for pid, why in parents_mod.parents_of(c, idx) if pid in vis],
            })
        out_c.sort(key=lambda x: x["name"].lower())
    return {"servers": out_s, "checks": out_c}
