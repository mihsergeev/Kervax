"""Журнал действий: запись событий в audit_log."""

import re
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import impersonate
from app.models import AuditLog, Server
from app.schemas import AuditEntryOut

# Действия с контейнером, подом или бэкапом сервера пишут в подробности srv=N: имя сервера
# могут сменить, номер - нет. Показывать надо имя, а не номер.
SRV_REF = re.compile(r"srv=(\d+)")


async def record(
    session: AsyncSession,
    username: str,
    action: str,
    target: str = "",
    detail: str = "",
) -> None:
    """Пишет запись в журнал (best-effort, не должно ронять основную операцию).

    Во время входа админа под чужой учеткой (app/impersonate.py) запись от имени пользователя
    уходит на самого админа с пометкой as_user: действовал он, а не пользователь."""
    as_user = ""
    a = impersonate.acting()
    if a is not None and username == a.target:
        username, as_user = a.admin, a.target
    try:
        session.add(
            AuditLog(
                username=username or "",
                action=action,
                target=str(target)[:255],
                detail=str(detail)[:2000],
                as_user=as_user,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — журнал не критичен
        await session.rollback()


async def readable(session: AsyncSession, rows: list[AuditLog]) -> list[AuditEntryOut]:
    """Записи журнала для показа: srv=N - текущее имя сервера (удаленного - #N), время с поясом
    (SQLite отдает его без пояса, пишется оно в UTC)."""
    ids = {int(m.group(1)) for r in rows if (m := SRV_REF.fullmatch(r.detail or ""))}
    names: dict[int, str] = {}
    if ids:
        names = dict((await session.execute(select(Server.id, Server.name).where(Server.id.in_(ids)))).all())
    out = []
    for r in rows:
        e = AuditEntryOut.model_validate(r)
        upd: dict = {}
        if e.ts.tzinfo is None:
            upd["ts"] = e.ts.replace(tzinfo=timezone.utc)
        if m := SRV_REF.fullmatch(e.detail):
            upd["detail"] = names.get(int(m.group(1)), f"#{m.group(1)}")
        out.append(e.model_copy(update=upd) if upd else e)
    return out
