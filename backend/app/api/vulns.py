"""Уязвимости по всему парку (страница из меню): у каких серверов есть важные уязвимости и чем
они закрываются. Только чтение, только свои группы. Расчет - app/vulns.py."""
from fastapi import APIRouter
from sqlalchemy import select

from app import vulns as vulns_mod
from app.deps import CurrentUser, SessionDep, group_allowed, user_sections
from app.models import Server, ServerPackages

router = APIRouter(prefix="/vulns", tags=["vulns"])


@router.get("")
async def fleet_vulns(user: CurrentUser, session: SessionDep) -> dict:
    if "servers" not in set(user_sections(user)):
        return {"servers": [], "feeds": []}
    servers = [s for s in (await session.scalars(select(Server).where(Server.enabled.is_(True)))).all()
               if group_allowed(user, s.group_name, "servers")]
    ids = [s.id for s in servers]
    rows = {r.server_id: r for r in (await session.scalars(
        select(ServerPackages).where(ServerPackages.server_id.in_(ids)))).all()} if ids else {}
    out = []
    feeds: dict[str, dict] = {}
    for s in servers:
        v = await vulns_mod.for_server(session, s, rows.get(s.id))
        if v["release"] and v["release"] not in feeds:
            feeds[v["release"]] = {"release": v["release"], "ts": v["feed_ts"], "gen": v["feed_gen"],
                                   "err": v["feed_err"]}
        out.append({"id": s.id, "name": s.name, "group": s.group_name, **v})
    out.sort(key=lambda x: (-x["summary"]["kev"], -x["summary"]["critical"], -x["summary"]["pkgs"],
                            x["name"].lower()))
    return {"servers": out, "feeds": sorted(feeds.values(), key=lambda f: f["release"])}
