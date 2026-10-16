"""История отправленных алертов: что приходило, про какой сервер или монитор и как часто.

Права те же, что у персональной рассылки (alerts.wants): админ видит все, остальные - свои
разделы и группы. Алерт без раздела (сводки) общий и виден всем.

Что шумело - не вид алерта со счетчиком, а что именно и где: какой юнит упал, какой под
перезапускается (из текста алерта, app/alert_what.py). Так же считает отчет (api/report.py).
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import and_, func, or_, select

from app import alert_what, settings_store
from app.deps import CurrentUser, SessionDep, scope_query, user_sections
from app.models import AlertEvent, Check, Server

router = APIRouter(prefix="/alert-history", tags=["alerts"])

# сколько строк "что шумело" отдавать, остальное - числом
_ITEMS_MAX = 150


def _scope(user, query):
    """Оставить в запросе только то, что учетка видит в панели."""
    if user.role == "admin":
        return query
    conds = [AlertEvent.section == ""]
    for sec in user_sections(user):
        groups = (user.site_groups if sec == "sites" else user.server_groups) or []
        conds.append(and_(AlertEvent.section == sec, AlertEvent.grp.in_(groups)) if groups
                     else AlertEvent.section == sec)
    return query.where(or_(*conds))


def _label(kind: str) -> str:
    """Подпись вида алерта, как в настройках правил."""
    for kinds in (settings_store.SERVER_ALERT_KINDS, settings_store.SITE_ALERT_KINDS):
        if kind in kinds:
            return kinds[kind][0]
    return kind


def _aware(ts: datetime) -> datetime:
    # SQLite отдает время без пояса, а пишем мы его в UTC
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _iso(ts: datetime) -> str:
    return _aware(ts).isoformat()


def group_fires(rows, day_of=None) -> list[dict]:
    """Срабатывания (ts, kind, target, section, text) по возрастанию времени - в строки "что
    именно и где": объект, вид, что, сколько раз, первый и последний раз, дни (day_of дает номер
    дня). Сообщения без объекта (про саму панель) - не беда объекта, их пропускаем."""
    groups: dict[tuple, dict] = {}
    for ts, kind, target, section, text in rows:
        if not target:
            continue
        ts = _aware(ts)
        day = day_of(ts) if day_of else -1
        for what, why in alert_what.items(kind, text) or [(_label(kind), "")]:
            g = groups.setdefault((target, kind, alert_what.key(what)), {
                "target": target, "section": section, "kind": kind, "label": _label(kind),
                "n": 0, "first": ts, "notes": [], "days": set()})
            g["n"] += 1
            g["what"], g["last"] = what, ts
            g["days"].add(day)
            if why:
                g["notes"].append(why)
    return sorted(groups.values(), key=lambda g: (-g["n"], -g["last"].timestamp()))


async def target_ids(session, user, sections: set[str]) -> tuple[dict[str, int], dict[str, int]]:
    """Имя в истории - имя сервера или монитора: по нему ссылка на карточку."""
    srv: dict[str, int] = {}
    chk: dict[str, int] = {}
    if "servers" in sections:
        srv = {n: i for i, n in (await session.execute(
            scope_query(user, select(Server.id, Server.name), Server, "servers"))).all()}
    if "sites" in sections:
        chk = {n: i for i, n in (await session.execute(
            scope_query(user, select(Check.id, Check.name), Check, "sites"))).all()}
    return srv, chk


def ids_of(target: str, section: str, srv: dict, chk: dict) -> dict:
    return {"srv_id": srv.get(target) if section != "sites" else None,
            "chk_id": chk.get(target) if section == "sites" else None}


def noise_item(g: dict, srv: dict, chk: dict) -> dict:
    return {
        "target": g["target"], "section": g["section"], "kind": g["kind"], "label": g["label"],
        "what": g["what"], "note": alert_what.merge_notes(g["notes"]), "n": g["n"],
        "days": len(g["days"]), "first": g["first"].isoformat(), "last": g["last"].isoformat(),
        **ids_of(g["target"], g["section"], srv, chk),
    }


@router.get("")
async def alert_history(
    user: CurrentUser,
    session: SessionDep,
    days: int = Query(7, ge=1, le=180),
    target: str = Query("", max_length=255),
    kind: str = Query("", max_length=40),
    q: str = Query("", max_length=100),
    only: str = Query("all", pattern="^(all|fires|recoveries)$"),
    before_id: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = _scope(user, select(AlertEvent).where(AlertEvent.ts >= since))
    if target:
        base = base.where(AlertEvent.target == target)
    if kind:
        base = base.where(AlertEvent.kind == kind)
    if q:
        base = base.where(AlertEvent.text.ilike(f"%{q}%"))
    if only == "fires":
        base = base.where(AlertEvent.recovery.is_(False))
    elif only == "recoveries":
        base = base.where(AlertEvent.recovery.is_(True))
    page = base.where(AlertEvent.id < before_id) if before_id else base
    rows = list(await session.scalars(page.order_by(AlertEvent.id.desc()).limit(limit)))

    # Сводка за весь период под тем же фильтром, без постраничности: ради нее история и
    # нужна - что шумит больше всего. Считаем срабатывания, отбои шумом не являются.
    sub = base.subquery()
    fired = sub.c.recovery.is_(False)
    total = await session.scalar(select(func.count()).select_from(sub)) or 0
    fires = await session.scalar(select(func.count()).select_from(sub).where(fired)) or 0
    n = func.count().label("n")
    # сводки по всему парку (без сервера) и общие сообщения (без вида) в "кто шумит" не идут
    kinds = (await session.execute(
        select(sub.c.kind, n).where(fired, sub.c.kind != "")
        .group_by(sub.c.kind).order_by(n.desc(), sub.c.kind).limit(15)
    )).all()
    targets = (await session.execute(
        select(sub.c.target, func.max(sub.c.section), n).where(fired, sub.c.target != "")
        .group_by(sub.c.target).order_by(n.desc(), sub.c.target).limit(15)
    )).all()
    groups = group_fires((await session.execute(
        select(sub.c.ts, sub.c.kind, sub.c.target, sub.c.section, sub.c.text).where(fired)
        .order_by(sub.c.ts, sub.c.id))).all())
    srv, chk = await target_ids(session, user, set(user_sections(user)))
    return {
        "events": [
            {"id": r.id, "ts": _iso(r.ts), "kind": r.kind, "label": _label(r.kind),
             "target": r.target, "section": r.section, "recovery": r.recovery, "text": r.text}
            for r in rows
        ],
        "more": len(rows) == limit,
        "summary": {
            "total": total, "fires": fires, "recoveries": total - fires,
            "kinds": [{"kind": k, "label": _label(k), "n": c} for k, c in kinds],
            "targets": [{"target": t, "section": sec, "n": c, **ids_of(t, sec, srv, chk)}
                        for t, sec, c in targets],
            "items": [noise_item(g, srv, chk) for g in groups[:_ITEMS_MAX]],
            "more_items": max(len(groups) - _ITEMS_MAX, 0),
        },
    }
