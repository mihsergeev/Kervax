"""Окна обслуживания: список, создание, правка, удаление (логика расписания - app/maint.py)."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app import audit, maint, settings_store
from app.config import get_settings
from app.deps import CurrentUser, SessionDep, group_allowed, section_allowed
from app.models import Check, MaintenanceWindow, Server
from app.schemas import MaintIn, MaintOut

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


async def _visible(session, user) -> tuple[set[int], set[int]]:
    """Видимые объекты: открытый раздел и своя группа обязательны одновременно."""
    srv = {i for i, g in (await session.execute(select(Server.id, Server.group_name))).all()
           if section_allowed(user, "/api/servers") and group_allowed(user, g, "servers")}
    chk = {i for i, g in (await session.execute(select(Check.id, Check.group_name))).all()
           if section_allowed(user, "/api/checks") and group_allowed(user, g, "sites")}
    return srv, chk


async def _tz(session) -> str:
    cfg = await settings_store.get_alert_config(session, get_settings())
    return cfg.get("tz") or "Europe/Moscow"


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _validate(body: MaintIn, vis: tuple[set[int], set[int]]) -> None:
    start, end = _utc(body.starts_at), _utc(body.ends_at)
    if end <= start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Конец окна должен быть позже начала")
    if body.repeat and end - start > timedelta(hours=24):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Повторяющееся окно - не длиннее суток")
    if not body.repeat and end - start > timedelta(days=31):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Окно - не длиннее месяца")
    if body.repeat == "weekly" and not body.weekdays:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Выберите дни недели")
    if any(d < 0 or d > 6 for d in body.weekdays):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "День недели - от 0 (пн) до 6 (вс)")
    if not body.server_ids and not body.check_ids:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Выберите хотя бы один сервер или монитор")
    if not set(body.server_ids) <= vis[0] or not set(body.check_ids) <= vis[1]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Среди выбранных есть недоступные вам серверы или мониторы")


def _fill(w: MaintenanceWindow, body: MaintIn) -> None:
    w.title = body.title.strip()
    w.server_ids = sorted(set(body.server_ids)) or None
    w.check_ids = sorted(set(body.check_ids)) or None
    w.starts_at = _utc(body.starts_at)
    w.ends_at = _utc(body.ends_at)
    w.repeat = body.repeat
    w.weekdays = sorted(set(body.weekdays)) if body.repeat == "weekly" else None
    w.verify = body.verify
    w.note = body.note.strip()


def _desc(w: MaintenanceWindow) -> str:
    rep = {"": "разово", "daily": "каждый день",
           "weekly": "по " + ",".join(_WEEKDAYS[d] for d in (w.weekdays or []))}[w.repeat or ""]
    return (f"{_utc(w.starts_at).isoformat(timespec='minutes')} - {_utc(w.ends_at).isoformat(timespec='minutes')}, "
            f"{rep}, серверов {len(w.server_ids or [])}, мониторов {len(w.check_ids or [])}")


def _out(w: MaintenanceWindow, now: datetime, tz: str, vis) -> MaintOut:
    cur = maint.current(w, now, tz)
    return MaintOut(
        id=w.id, title=w.title,
        server_ids=[i for i in (w.server_ids or []) if i in vis[0]],
        check_ids=[i for i in (w.check_ids or []) if i in vis[1]],
        starts_at=_utc(w.starts_at), ends_at=_utc(w.ends_at), repeat=w.repeat or "",
        weekdays=w.weekdays or [], verify=w.verify, note=w.note or "", created_by=w.created_by or "",
        active=cur is not None, cur_end=cur[1] if cur else None,
        next_start=maint.next_start(w, now, tz), finished=maint.finished(w, now),
    )


@router.get("", response_model=list[MaintOut])
async def list_windows(user: CurrentUser, session: SessionDep) -> list[MaintOut]:
    """Окна, где есть хоть один видимый учетке сервер или монитор (чужие id из окна убраны)."""
    now = datetime.now(timezone.utc)
    tz = await _tz(session)
    vis = await _visible(session, user)
    out = []
    for w in await session.scalars(select(MaintenanceWindow).order_by(MaintenanceWindow.starts_at)):
        o = _out(w, now, tz, vis)
        if o.server_ids or o.check_ids:
            out.append(o)
    return out


async def _get(session, wid: int) -> MaintenanceWindow:
    w = await session.get(MaintenanceWindow, wid)
    if w is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Окно не найдено")
    return w


@router.post("", response_model=MaintOut, status_code=status.HTTP_201_CREATED)
async def create_window(body: MaintIn, user: CurrentUser, session: SessionDep) -> MaintOut:
    vis = await _visible(session, user)
    _validate(body, vis)
    w = MaintenanceWindow(created_by=user.username)
    _fill(w, body)
    session.add(w)
    await session.commit()
    await session.refresh(w)
    await audit.record(session, user.username, "maint_create", w.title, _desc(w))
    return _out(w, datetime.now(timezone.utc), await _tz(session), vis)


@router.put("/{wid}", response_model=MaintOut)
async def update_window(wid: int, body: MaintIn, user: CurrentUser, session: SessionDep) -> MaintOut:
    """Правка. Окно с серверами или мониторами чужих групп учетка с группами не правит, как и
    не удаляет: раньше чужие объекты оставались в окне и получали новое расписание, и
    "каждый день 00:00-23:59" глушило чужие серверы."""
    vis = await _visible(session, user)
    w = await _get(session, wid)
    srv, chk = set(w.server_ids or []), set(w.check_ids or [])
    if not (srv & vis[0]) and not (chk & vis[1]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Окно не найдено")
    if not srv <= vis[0] or not chk <= vis[1]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "В окне есть серверы или мониторы чужих групп")
    _validate(body, vis)
    _fill(w, body)
    # новое расписание - новая сверка: старая отметка относилась к прошлому времени
    w.verified_end = None
    await session.commit()
    await session.refresh(w)
    await audit.record(session, user.username, "maint_update", w.title, _desc(w))
    return _out(w, datetime.now(timezone.utc), await _tz(session), vis)


@router.delete("/{wid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_window(wid: int, user: CurrentUser, session: SessionDep) -> None:
    vis = await _visible(session, user)
    w = await _get(session, wid)
    if not (set(w.server_ids or []) & vis[0]) and not (set(w.check_ids or []) & vis[1]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Окно не найдено")
    if not set(w.server_ids or []) <= vis[0] or not set(w.check_ids or []) <= vis[1]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "В окне есть серверы или мониторы чужих групп")
    title = w.title
    await session.delete(w)
    await session.commit()
    await audit.record(session, user.username, "maint_delete", title)
