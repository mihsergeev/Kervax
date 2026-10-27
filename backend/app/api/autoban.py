"""Автобан сканеров: белый список и сводка банов (только админ). Сами баны и кнопки разбана и режима
- в карточке сервера (api/servers.py, /servers/{id}/autoban), логика - app/autoban.py."""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app import audit
from app import autoban as autoban_mod
from app.deps import AdminUser, SessionDep
from app.models import Location, Server
from app.schemas import AutobanSettingsIn

router = APIRouter(prefix="/autoban", tags=["autoban"])


@router.get("/settings")
async def autoban_settings(_: AdminUser, session: SessionDep) -> dict:
    """Белый список и сводка. auto - что панель добавляет к списку сама: адреса своих серверов и
    выходы локаций проверок."""
    cfg = await autoban_mod.load_settings(session)
    servers = list(await session.scalars(select(Server)))
    locs = list(await session.scalars(select(Location)))
    mine = {x["net"] for x in cfg["allow"]}
    auto = [{"net": n, "label": lab} for n, lab in autoban_mod.panel_allow({"allow": []}, servers, locs) if n not in mine]
    return {**cfg, "auto": auto}


@router.put("/settings")
async def autoban_settings_put(body: AutobanSettingsIn, user: AdminUser, session: SessionDep) -> dict:
    allow, seen = [], set()
    for x in body.allow:
        try:
            net = autoban_mod.norm_net(x.net)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"не адрес и не сеть: {x.net}") from None
        if net in seen:
            continue
        seen.add(net)
        allow.append({"net": net, "label": x.label.strip()})
    await autoban_mod.save_settings(session, {"allow": allow, "digest": body.digest})
    await session.commit()
    await audit.record(session, user.username, "autoban_settings", f"{len(allow)}:{body.digest}", "")
    return await autoban_settings(user, session)
