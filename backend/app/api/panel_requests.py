"""Левые запросы к панели (окно Пользователи, раздел "Запросы к панели", только админ): что
сейчас в состоянии алерта и кто за сутки слал отказы или всплески. Счетчик и алерт -
app/request_watch.py."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from sqlalchemy import select

from app import request_watch as rw
from app.deps import AdminUser, SessionDep
from app.models import RequestStat, Server

router = APIRouter(prefix="/panel-requests", tags=["panel-requests"])

_SHOWN = 50


def _srv(s: Server) -> dict:
    return {"id": s.id, "name": s.name or f"#{s.id}"}


@router.get("")
async def panel_requests(_: AdminUser, session: SessionDep) -> dict:
    now = datetime.now(timezone.utc)
    servers = list(await session.scalars(select(Server)))
    by_ip = rw.servers_by_ip(servers)
    by_id = {s.id: s for s in servers}
    state = await rw.load_state(session)
    rows = list(await session.scalars(select(RequestStat).where(RequestStat.minute >= now - timedelta(days=1))))
    agg = rw.aggregate(rows)
    sources = []
    for key, a in agg.items():
        srvs = by_ip.get(key, [])
        sources.append({
            "key": key,
            "who": rw.who(key, srvs)[0],
            "servers": [_srv(s) for s in srvs],
            "bad": a["bad"],
            "peak": a["peak"],
            "codes": {str(c): n for c, n in sorted(a["codes"].items())},
            "paths": a["paths"].most_common(5),
            "first": rw._aware(a["first"]).isoformat(),
            "last": rw._aware(a["last"]).isoformat(),
            "active": key in state,
            "hint": rw.hint(a, len(srvs)) if a["bad"] >= rw.BAD_LIMIT or a["peak"] * rw.WINDOW_MIN >= rw.FLOOD_PER_SERVER else "",
        })
    sources.sort(key=lambda r: (not r["active"], -r["bad"], -r["peak"], r["key"]))
    active = []
    for key, cur in state.items():
        srvs = [by_id[i] for i in cur.get("servers") or [] if i in by_id]
        active.append({
            "key": key,
            "who": rw.who(key, srvs)[0],
            "servers": [_srv(s) for s in srvs],
            "since": cur.get("since"),
            "last_over": cur.get("last_over"),
            "what": cur.get("what", ""),
            "hint": cur.get("hint", ""),
            "notified": bool(cur.get("notified")),
        })
    return {
        "window_min": rw.WINDOW_MIN,
        "bad_limit": rw.BAD_LIMIT,
        "flood_per_server": rw.FLOOD_PER_SERVER,
        "typical_per_server": rw.TYPICAL_PER_SERVER,
        "write_floor": rw.WRITE_FLOOR,
        "active": active,
        "sources": sources[:_SHOWN],
        "more": max(0, len(sources) - _SHOWN),
    }
