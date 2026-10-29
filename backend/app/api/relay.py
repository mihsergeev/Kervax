"""Ретранслятор для агентов (app/relay.py): галочка в настройках сервера, выбор пути в окне
команды установки и ручки helper'а relay-setup на самом ретрансляторе."""

from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import audit, gre as gre_mod, install_watch, relay as relay_mod
from app.api.servers import _get_or_404, _is_online, _out, _write_agent_allowlist
from app.config import get_settings
from app.deps import CurrentUser, SessionDep
from app.models import Location, Server
from app.schemas import (GreInstallOut, InstallStatusOut, InstallViaIn, InstallViaOut, RelayUpdate, ServerOut,
                         agent_ip_valid)
from app.security import hash_agent_token

router = APIRouter(prefix="/servers", tags=["relay"])
agent_router = APIRouter(prefix="/agent", tags=["agent"])


@router.put("/{server_id}/relay", response_model=ServerOut)
async def set_relay(server_id: int, body: RelayUpdate, user: CurrentUser, session: SessionDep) -> ServerOut:
    """Включить или выключить ретранслятор на сервере. Включение - только если сервер сам
    достает до панели напрямую; ставит helper человек (он появится в "Требует действий"),
    выключенный ретранслятор helper снимает сам."""
    server = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    if body.enabled:
        problem = relay_mod.enable_problem(server, _is_online(server, now))
        if problem:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
        relay_mod.ensure_credentials(server)
    was = server.relay_enabled
    server.relay_enabled = body.enabled
    if body.port is not None:
        server.relay_port = body.port
    await session.commit()
    await session.refresh(server)
    if was != body.enabled:
        await audit.record(session, user.username, "relay_on" if body.enabled else "relay_off", server.name,
                           f"порт {relay_mod.port_of(server)}" if body.enabled else "")
    return _out(server, now)


@router.post("/{server_id}/install-via", response_model=InstallViaOut)
async def install_via(server_id: int, body: InstallViaIn, user: CurrentUser, session: SessionDep) -> InstallViaOut:
    """Путь агента в панель для команды установки. Ретранслятор - сервер сразу попадает в список
    пропускаемых им адресов (helper откроет порт в течение минуты), в ответе адрес прокси с
    логином и паролем. Локация - ее адрес прокси. Напрямую и свой прокси - ретранслятор снимается,
    свой прокси панель не видит вовсе: браузер дописывает его в команду сам.

    GRE - включает серверу GRE до панели, как галочка в настройках (своя /30, строка хосту панели),
    в ответе готов ли хост панели и чем начать команду (gre). Окно зовет его только кнопкой
    "Включить GRE", а не выбором пути. Другие пути GRE не трогают: включенный в этом окне GRE
    окно выключает само (PUT /gre), когда уходят на другой путь или закрывают окно, так и не
    поставив агента, а включенный раньше выключают только кнопкой."""
    server = await _get_or_404(server_id, session, user)
    ip = body.ip.strip()
    if ip and not agent_ip_valid(ip):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "IP сервера указан неверно")
    if body.mode == "gre" and ip and not gre_mod.is_public(ip):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Для GRE нужен публичный IPv4 сервера: туннель ведется на этот адрес")
    if ip:
        server.agent_ip = ip
    peer_was, gre_was = gre_mod.peer_of(server), server.gre_enabled
    proxy = ""
    via: int | None = None
    if body.mode == "relay":
        if body.relay_id is None or body.relay_id == server.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Выберите ретранслятор")
        relay = await _get_or_404(body.relay_id, session, user)
        if not relay.relay_enabled:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "На этом сервере ретранслятор выключен")
        if not relay_mod.address_of(server):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Укажите внешний IP сервера: ретранслятор пускает только известные адреса")
        relay_mod.ensure_credentials(relay)
        proxy, via = relay_mod.proxy_url(relay), relay.id
    elif body.mode == "location":
        loc = await session.get(Location, body.location_id) if body.location_id is not None else None
        if loc is None or not loc.enabled or not loc.url.strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Выберите локацию с адресом прокси")
        proxy = loc.url.strip()
    elif body.mode == "gre":
        # адрес из окна главнее названного helper'ом раньше: ставят заново, и человек знает лучше
        if ip:
            server.gre_peer = ip
        if not gre_mod.peer_of(server):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Укажите внешний IP сервера: GRE-туннель ведется на его адрес")
        try:
            await gre_mod.allocate(session, server)
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    if body.mode == "gre":
        if not gre_was:
            # туннель после включения еще не работал: алерт gre_down ждет первого отчета через него
            server.gre_agent_at = None
        server.gre_enabled = True
    changed = via != server.relay_via_id
    server.relay_via_id = via
    try:
        await session.commit()
    except IntegrityError as e:
        # ту же /30 только что взял другой сервер: повторный запрос выделит следующую
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Сеть туннеля только что занял другой сервер, повторите") from e
    if gre_was != server.gre_enabled or (server.gre_enabled and gre_mod.peer_of(server) != peer_was):
        await gre_mod.write_tunnels(session)
    if gre_was != server.gre_enabled:
        await audit.record(session, user.username, "gre_on" if server.gre_enabled else "gre_off", server.name,
                           server.gre_net or "")
    if via is not None and (changed or ip):
        install_watch.via_relay(server.id)
    if ip:
        await _write_agent_allowlist(session)
    if changed or ip:
        await audit.record(session, user.username, "server_install_via", server.name, body.mode)
    gre = GreInstallOut(**gre_mod.install_view(server)) if body.mode == "gre" else None
    return InstallViaOut(proxy=proxy, ip=relay_mod.address_of(server), gre=gre)


def _ts(dt: datetime | None) -> float | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


@router.get("/{server_id}/install-status", response_model=InstallStatusOut)
async def install_status(server_id: int, user: CurrentUser, session: SessionDep) -> InstallStatusOut:
    """Шаг 3 окна установки, пока агент молчит: открыт ли адрес ноды в фаерволе панели (или на
    ретрансляторе) и что нода уже скачала. Окно спрашивает раз в несколько секунд."""
    server = await _get_or_404(server_id, session, user)
    ip = (server.agent_ip or "").strip()
    fw, age = install_watch.firewall_state(ip, get_settings().data_dir)
    out = InstallStatusOut(ip=ip, firewall=fw, fw_age=age, connected=server.last_seen is not None)
    if server.relay_via_id is not None:
        relay = await session.get(Server, server.relay_via_id)
        out.relay = install_watch.relay_state(server.id, _ts(relay.relay_seen_at) if relay is not None else None)
        # адрес ретранслятора в фаерволе панели и так открыт, а запросы ноды идут с него:
        # ни фаервол, ни скачанное ей не приписать
        out.firewall, out.fw_age = "", None
        return out
    since = _ts(server.created_at) or 0
    seen = install_watch.seen(relay_mod.address_of(server), since)
    if server.gre_enabled:
        # через туннель нода приходит со своего адреса в нем
        for what, ts in install_watch.seen(gre_mod.addrs(server.gre_net or "")[1], since).items():
            seen[what] = max(ts, seen.get(what, 0))
    for what, ts in seen.items():
        setattr(out, f"{what}_at", datetime.fromtimestamp(ts, timezone.utc))
    return out


async def _by_token(session, token: str) -> Server:
    server = await session.scalar(select(Server).where(Server.token_hash == hash_agent_token(token))) if token else None
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен агента")
    return server


@agent_router.post("/relay")
async def agent_relay(session: SessionDep, body: dict = Body(default_factory=dict),
                      authorization: str = Header(default="")) -> dict:
    """Helper relay-setup раз в минуту: сообщает, как у него дела, и получает, что держать.
    Куда пускать, в ответе нет: helper берет адрес панели из конфига своего агента, так что
    даже подмененный ответ не превратит ретранслятор в открытый прокси."""
    server = await _by_token(session, authorization.removeprefix("Bearer ").strip())
    if not server.relay_enabled:
        return {"enabled": False}
    server.relay_state = relay_mod.helper_status(body if isinstance(body, dict) else {})
    server.relay_seen_at = datetime.now(timezone.utc)
    relay_mod.ensure_credentials(server)
    await session.commit()
    return {
        "enabled": True,
        "port": relay_mod.port_of(server),
        "user": server.relay_user,
        "password": server.relay_password,
        "clients": await relay_mod.clients(session, server),
    }


@agent_router.post("/relay-wanted", response_class=PlainTextResponse)
async def agent_relay_wanted(request: Request, session: SessionDep) -> str:
    """Условие установки helper'а relay-setup (KERVAX_SETUP_WHEN): нужен ли ретранслятор этой
    ноде. Токен в теле, а не в заголовке: условие пишется без кавычек и обратных слэшей, а
    curl --data-binary @- берет его прямо из конфига агента."""
    token = (await request.body())[:512].decode("ascii", errors="ignore").strip()
    server = await _by_token(session, token)
    return "yes" if server.relay_enabled else "no"
