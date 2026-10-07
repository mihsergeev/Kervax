"""История отправленных алертов: что приходило, про какой сервер или монитор и как часто.

Права те же, что у персональной рассылки (alerts.wants): админ видит все, остальные - свои
разделы и группы. Алерт без раздела (сводки) общий и виден всем.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import and_, func, or_, select

from app import settings_store
from app.deps import CurrentUser, SessionDep, user_sections
from app.models import AlertEvent

router = APIRouter(prefix="/alert-history", tags=["alerts"])


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


def _iso(ts: datetime) -> str:
    # SQLite отдает время без пояса, а пишем мы его в UTC
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).isoformat()


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
        select(sub.c.target, n).where(fired, sub.c.target != "")
        .group_by(sub.c.target).order_by(n.desc(), sub.c.target).limit(15)
    )).all()
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
            "targets": [{"target": t, "n": c} for t, c in targets],
        },
    }
