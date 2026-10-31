"""Поиск по парку (Ctrl+K) - см. app/search.py. Права как в разделах: учетка находит только
свои группы и только то, что видно в ее разделах."""

from fastapi import APIRouter, Query
from sqlalchemy import select

from app import search as search_mod
from app.deps import CurrentUser, SessionDep, scope_query, user_sections
from app.models import Check, Server
from app.secret_masks import mask_target

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
async def search(user: CurrentUser, session: SessionDep, q: str = Query("", max_length=200)) -> dict:
    sections = set(user_sections(user))
    if len(q.strip()) < search_mod.MIN_LEN:
        return search_mod.find([], q, sections)
    entries: list[search_mod.Entry] = []
    if sections & {"servers", "docker", "kuber", "services"}:
        rows = (await session.execute(
            scope_query(user, select(*search_mod._SRV_COLS), Server, "servers")
        )).all()
        entries += await search_mod.server_entries(session, rows, full=not (user.server_groups or []))
        if "servers" in sections:
            entries += await search_mod.package_entries(session, rows)
    if "sites" in sections:
        entries += [search_mod.check_entry(
            c, target=c.target if user.role in ("admin", "editor") else mask_target(c.target),
        ) for c in (await session.execute(
            scope_query(user, select(Check.id, Check.name, Check.type, Check.target, Check.group_name, Check.note),
                        Check, "sites")
        )).all()]
    return search_mod.find(entries, q, sections)
