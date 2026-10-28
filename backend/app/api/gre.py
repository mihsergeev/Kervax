"""GRE до панели (app/gre.py): галочка в настройках сервера, ручки helper'а gre-setup на ноде и
замер, по которому агент узнает, что соединение до панели режут по объему."""

import os
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import IntegrityError

from app import audit, gre as gre_mod
from app.api.relay import _by_token
from app.api.servers import _get_or_404, _out
from app.deps import CurrentUser, SessionDep
from app.schemas import GreInstallOut, GreUpdate, ServerOut

router = APIRouter(prefix="/servers", tags=["gre"])
agent_router = APIRouter(prefix="/agent", tags=["agent"])

# замер: мелкий кусок заведомо меньше порога, после которого режут (~16 КБ), большой - сильно
# больше. Больше потолка не отдаем: ручка открыта любому токену агента
PROBE_MAX = 128 * 1024


@router.put("/{server_id}/gre", response_model=ServerOut)
async def set_gre(server_id: int, body: GreUpdate, user: CurrentUser, session: SessionDep) -> ServerOut:
    """Включить или выключить GRE до панели. Включение выделяет серверу /30 (она остается за ним
    и после выключения); ставит helper человек (он появится в "Требует действий"), выключенный
    туннель helper снимает сам, хостовый скрипт - свою сторону."""
    server = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    if body.enabled:
        problem = gre_mod.enable_problem(server)
        if problem:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
        try:
            await gre_mod.allocate(session, server)
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    was = server.gre_enabled
    server.gre_enabled = body.enabled
    try:
        await session.commit()
    except IntegrityError as e:
        # ту же /30 только что взял другой сервер: повторить сохранение - выделится следующая
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Сеть туннеля только что занял другой сервер, сохраните еще раз") from e
    await session.refresh(server)
    if was != body.enabled:
        await gre_mod.write_tunnels(session)
        await audit.record(session, user.username, "gre_on" if body.enabled else "gre_off", server.name,
                           server.gre_net or "")
    return _out(server, now)


@router.get("/{server_id}/gre-install", response_model=GreInstallOut)
async def gre_install(server_id: int, user: CurrentUser, session: SessionDep) -> GreInstallOut:
    """Окно установки, путь "Через GRE": пока хост панели не поднял свою сторону, окно спрашивает
    раз в несколько секунд и команду не выдает. Выбор пути в окне тоже спрашивает только это:
    включает GRE кнопка в окне (install-via), выключает PUT /gre."""
    server = await _get_or_404(server_id, session, user)
    return GreInstallOut(**gre_mod.install_view(server))


@agent_router.post("/gre")
async def agent_gre(session: SessionDep, body: dict = Body(default_factory=dict),
                    authorization: str = Header(default="")) -> dict:
    """Helper gre-setup раз в минуту: сообщает, как у него дела, и получает, что держать. Адрес
    хоста панели (remote) - тот, с которого хост ждет GRE от этой ноды; пока хост свою сторону
    не поднял, его нет, и helper туннель не поднимает."""
    server = await _by_token(session, authorization.removeprefix("Bearer ").strip())
    if not server.gre_enabled:
        return {"enabled": False}
    st = gre_mod.helper_status(body if isinstance(body, dict) else {})
    server.gre_state = st
    server.gre_seen_at = datetime.now(timezone.utc)
    # helper называет адрес, с которого нода шлет GRE: другой конец туннеля для хоста панели
    peer_changed = False
    if gre_mod.is_public(st["local_ip"]) and st["local_ip"] != server.gre_peer:
        server.gre_peer, peer_changed = st["local_ip"], True
    if not gre_mod.addrs(server.gre_net or "")[0]:
        await gre_mod.allocate(session, server)
        peer_changed = True
    await session.commit()
    if peer_changed:
        await gre_mod.write_tunnels(session)
    mine, node = gre_mod.addrs(server.gre_net or "")
    return {
        "enabled": True,
        "remote": gre_mod.panel_ip(server),
        "local_addr": node,
        "panel_addr": mine,
        "prefix": 30,
        "mtu": gre_mod.MTU,
    }


@agent_router.post("/gre-wanted", response_class=PlainTextResponse)
async def agent_gre_wanted(request: Request, session: SessionDep) -> str:
    """Условие установки helper'а gre-setup (KERVAX_SETUP_WHEN): включен ли GRE этой ноде. Токен
    в теле, как у relay-wanted."""
    token = (await request.body())[:512].decode("ascii", errors="ignore").strip()
    server = await _by_token(session, token)
    return "yes" if server.gre_enabled else "no"


@agent_router.get("/probe")
async def agent_probe(session: SessionDep, n: int = Query(default=8192, ge=1, le=PROBE_MAX),
                      authorization: str = Header(default="")) -> Response:
    """n случайных байт одним ответом: агент меряет, доходит ли большой кусок одним соединением.
    Случайные - чтобы сжатие по дороге не сделало из 100 КБ два."""
    await _by_token(session, authorization.removeprefix("Bearer ").strip())
    return Response(os.urandom(n), media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store"})
