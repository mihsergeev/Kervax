"""Связь между серверами и трафик за месяц (карточки в разделе "Сеть" сервера).

Связь: агент сервера раз в 30 секунд пробует открыть TCP-соединение с адресом (app/netlinks.py).
Панель тем самым может заставить агента стучаться куда угодно, поэтому адресов не больше 16 на
сервер, такт фиксированный, а добавить связь может только тот, кто может менять этот сервер.

Трафик: сутки по настоящим сетевым картам, сводка за расчетный период и лимит тарифа
(app/traffic.py). Лимит меняется обычным PATCH сервера.

Кто качает трафик и кто пишет на диск - по часам за неделю (app/usage.py).
"""
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete, select

from app import audit, netlinks, traffic as traffic_mod, usage as usage_mod
from app.api.servers import _get_or_404
from app.deps import CurrentUser, SessionDep, group_allowed
from app.models import LinkSample, Server, ServerLink, ServerTraffic

router = APIRouter(prefix="/servers", tags=["servers"])

LINK_AGENT = (2, 31)
SPARK_MIN = 60  # мини-график у каждой связи: последний час
SPARK_STEP = 120  # по точке на 2 минуты
TRAFFIC_DAYS = 62


def _ver(v: object) -> tuple[int, ...]:
    out = []
    for p in str(v or "").split("."):
        if not p.isdigit():
            break
        out.append(int(p))
    return tuple(out)


class LinkIn(BaseModel):
    target_server_id: int | None = None
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    note: str = Field(default="", max_length=64)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/{server_id}/links")
async def list_links(server_id: int, user: CurrentUser, session: SessionDep) -> dict:
    server = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    rows = list(await session.scalars(
        select(ServerLink).where(ServerLink.server_id == server.id).order_by(ServerLink.id)))
    names, silent = await netlinks.target_names(session, rows, now)
    spark = await _sparks(session, [r.id for r in rows], now)
    links = []
    for r in rows:
        fresh = r.last_ts is not None and now - _aware(r.last_ts) <= netlinks.FRESH
        links.append({
            "id": r.id, "target_server_id": r.target_server_id, "host": r.host, "port": r.port,
            "note": r.note, "name": netlinks.label(r, names),
            "target_name": names.get(r.target_server_id) if r.target_server_id else None,
            "loss": r.last_loss if fresh else None, "rtt": r.last_rtt if fresh else None,
            "err": r.last_err if fresh else "", "fresh": fresh,
            "last_ts": _aware(r.last_ts).isoformat() if r.last_ts else None,
            "held": r.target_server_id in silent,
            "spark": spark.get(r.id),
        })
    targets = [
        {"id": s.id, "name": s.name, "ip": s.external_ip, "local_ip": s.local_ip}
        for s in await session.scalars(select(Server).where(Server.id != server.id).order_by(Server.name))
        if group_allowed(user, s.group_name)
    ]
    return {"links": links, "targets": targets, "max": netlinks.MAX_PER_SERVER,
            "agent_ok": _ver(server.agent_version) >= LINK_AGENT}


async def _sparks(session, ids: list[int], now: datetime) -> dict[int, list]:
    """Мини-график связей за час: по точке на 2 минуты [среднее время подключения, худшая доля
    неудач] или None, если проверок не было."""
    if not ids:
        return {}
    since = now - timedelta(minutes=SPARK_MIN)
    n = SPARK_MIN * 60 // SPARK_STEP
    by: dict[int, dict[int, list]] = {}
    for lid, ts, loss, rtt in (await session.execute(
        select(LinkSample.link_id, LinkSample.ts, LinkSample.loss, LinkSample.rtt)
        .where(LinkSample.link_id.in_(ids), LinkSample.ts >= since)
    )).all():
        b = min(max(int((_aware(ts) - since).total_seconds() // SPARK_STEP), 0), n - 1)
        by.setdefault(lid, {}).setdefault(b, []).append((loss, rtt))
    out: dict[int, list] = {}
    for lid, bk in by.items():
        pts: list = []
        for b in range(n):
            g = bk.get(b)
            if not g:
                pts.append(None)
                continue
            rtts = [x[1] for x in g if x[1] is not None]
            pts.append([round(sum(rtts) / len(rtts), 1) if rtts else None, max(x[0] or 0 for x in g)])
        out[lid] = pts
    return out


@router.post("/{server_id}/links", status_code=status.HTTP_201_CREATED)
async def add_link(server_id: int, body: LinkIn, user: CurrentUser, session: SessionDep) -> dict:
    server = await _get_or_404(server_id, session, user)
    host = body.host.strip().lower()
    if not netlinks.host_ok(host):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Адрес - IP или имя хоста, без http:// и порта")
    if netlinks.host_forbidden(host):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "localhost и адреса метаданных облака проверять нельзя")
    # Связь - TCP с ноды на этот адрес: с любым адресом это сканер сети изнутри ноды. Не админу -
    # только адреса и имена серверов парка из своих групп
    if user.role != "admin":
        visible = [s for s in await session.scalars(select(Server)) if group_allowed(user, s.group_name)]
        if host not in netlinks.fleet_hosts(visible):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Связь с адресом не из серверов панели добавляет только администратор")
    target = None
    if body.target_server_id is not None:
        if body.target_server_id == server.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Связь сервера с самим собой не нужна")
        target = await _get_or_404(body.target_server_id, session, user)
    rows = list(await session.scalars(select(ServerLink).where(ServerLink.server_id == server.id)))
    if len(rows) >= netlinks.MAX_PER_SERVER:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"У сервера уже {netlinks.MAX_PER_SERVER} связей, больше агент не проверяет")
    if any(r.host == host and r.port == body.port for r in rows):
        raise HTTPException(status.HTTP_409_CONFLICT, "Такая связь уже есть")
    link = ServerLink(server_id=server.id, target_server_id=target.id if target else None,
                      host=host, port=body.port, note=body.note.strip())
    session.add(link)
    await session.commit()
    await audit.record(session, user.username, "link_add", server.name,
                       f"{target.name if target else host}:{body.port}")
    return {"id": link.id}


@router.delete("/{server_id}/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_link(server_id: int, link_id: int, user: CurrentUser, session: SessionDep) -> None:
    server = await _get_or_404(server_id, session, user)
    link = await session.get(ServerLink, link_id)
    if link is None or link.server_id != server.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Связь не найдена")
    what = f"{link.host}:{link.port}"
    await session.execute(sa_delete(LinkSample).where(LinkSample.link_id == link.id))
    await session.delete(link)
    await session.commit()
    await audit.record(session, user.username, "link_delete", server.name, what)


@router.get("/{server_id}/links/{link_id}/history")
async def link_history(server_id: int, link_id: int, user: CurrentUser, session: SessionDep,
                       hours: float = Query(24, gt=0, le=24 * 31)) -> dict:
    server = await _get_or_404(server_id, session, user)
    link = await session.get(ServerLink, link_id)
    if link is None or link.server_id != server.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Связь не найдена")
    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(LinkSample.ts, LinkSample.loss, LinkSample.rtt, LinkSample.rtt_max)
        .where(LinkSample.link_id == link.id, LinkSample.ts >= now - timedelta(hours=hours))
        .order_by(LinkSample.ts)
    )).all()
    step = max(30, int(hours * 3600 / 360))
    buckets: dict[int, list] = {}
    for r in rows:
        buckets.setdefault(int(_aware(r.ts).timestamp() // step) * step, []).append(r)
    points = []
    for b in sorted(buckets):
        grp = buckets[b]
        rtts = [x.rtt for x in grp if x.rtt is not None]
        mx = [x.rtt_max for x in grp if x.rtt_max is not None]
        points.append({
            "ts": datetime.fromtimestamp(b, timezone.utc).isoformat(),
            "loss": round(sum(x.loss for x in grp) / len(grp), 1),
            "rtt": round(sum(rtts) / len(rtts), 1) if rtts else None,
            "rtt_max": round(max(mx), 1) if mx else None,
        })
    return {"points": points, "step": step}


def _phys(server: Server) -> list[str]:
    return [str(i.get("if")) for i in ((server.last_report or {}).get("net_ifaces") or [])
            if isinstance(i, dict) and i.get("phys") and isinstance(i.get("rx_total"), int)]


@router.get("/{server_id}/traffic")
async def server_traffic(server_id: int, user: CurrentUser, session: SessionDep) -> dict:
    server = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(ServerTraffic.day, ServerTraffic.rx, ServerTraffic.tx)
        .where(ServerTraffic.server_id == server.id, ServerTraffic.day >= (now - traffic_mod.KEEP).date())
        .order_by(ServerTraffic.day)
    )).all()
    first = (now - timedelta(days=TRAFFIC_DAYS - 1)).date()
    days = [{"day": str(r.day), "rx": r.rx, "tx": r.tx} for r in rows if r.day >= first]
    # прошлые периоды тарифа: сколько ушло в каждом (последние 12)
    periods: dict[date, list[int]] = {}
    for r in rows:
        start, _end = traffic_mod.period(
            datetime(r.day.year, r.day.month, r.day.day, 12, tzinfo=timezone.utc), server.traffic_cycle_day)
        acc = periods.setdefault(start, [0, 0])
        acc[0] += r.rx or 0
        acc[1] += r.tx or 0
    hist = [{"start": str(k), "rx": v[0], "tx": v[1]} for k, v in sorted(periods.items())[-12:]]
    phys = _phys(server)
    return {
        "days": days, "periods": hist, "sum": await traffic_mod.summary(session, server, now) if phys or rows else None,
        "limit_gb": server.traffic_limit_gb or 0, "count": server.traffic_count or "out",
        "cycle_day": server.traffic_cycle_day or 1, "ifaces": phys,
        "agent_ok": _ver(server.agent_version) >= LINK_AGENT,
        "since": (server.traffic_ctr or {}).get("since") if isinstance(server.traffic_ctr, dict) else None,
    }


@router.get("/{server_id}/usage")
async def server_usage(
    server_id: int, user: CurrentUser, session: SessionDep,
    metric: str = Query("net", pattern="^(net|disk)$"),
    tz: int = Query(0, ge=-14 * 60, le=14 * 60),
) -> dict:
    """Кто качает трафик (net) или пишет на диск (disk): сутки, неделя и дни недели по каждому."""
    server = await _get_or_404(server_id, session, user)
    return await usage_mod.view(session, server.id, metric, datetime.now(timezone.utc), tz)
