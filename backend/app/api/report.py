"""Отчет за период: что ломалось, сколько лежало и что шумело. Только в панели - в Telegram не
уходит, это не алерт, а то, что смотрят раз в неделю.

Источники уже есть, отдельно ничего не копим:
- сайты: снимки проверок (аптайм) и инциденты (простой), живут sample_retention_days;
- серверы: пропуски в минутных метриках. Не по алертам: заглушенный или не доставленный алерт
  простой не отменяет. Пропуск, который был сразу у большинства серверов, - это лежала сама
  панель (деплой, рестарт), а не серверы: его вычитаем и показываем отдельно;
- шум и бэкапы: история доставленных алертов (180 дней);
- окна обслуживания: их вхождения за период.
Права как в разделах: учетка видит только свои группы и разделы.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from sqlalchemy import case, func, or_, select

from app import maint, settings_store
from app.api.alert_history import _label, _scope as _alert_scope
from app.collector import _OFFLINE_ALERT_EXTRA, seen_online
from app.config import get_settings
from app.deps import CurrentUser, SessionDep, scope_query, user_sections
from app.models import AlertEvent, Check, CheckIncident, CheckSample, MaintenanceWindow, Server, ServerMetric

router = APIRouter(prefix="/report", tags=["report"])

# сколько строк в таблицах отчета: дальше это уже не обзор, а история
_TOP = 50
# пропуск метрик сразу у такой доли серверов (и не меньше чем у трех) - лежала панель
_PANEL_SHARE = 0.6


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _overlap(s: datetime, e: datetime, a: datetime, b: datetime) -> float:
    return max(0.0, (min(e, b) - max(s, a)).total_seconds())


def _iso(ts: datetime | None) -> str | None:
    return _aware(ts).isoformat() if ts else None


async def _site_stats(session, ids: list[int], since: datetime, until: datetime) -> tuple[dict, dict]:
    """Аптайм по снимкам и простой по инцидентам "лежит" за [since, until)."""
    up_n = func.sum(case((CheckSample.status == "up", 1), else_=0))
    up: dict[int, float] = {}
    if ids:
        for cid, total, ups in (await session.execute(
            select(CheckSample.check_id, func.count(), up_n)
            .where(CheckSample.ts >= since, CheckSample.ts < until, CheckSample.check_id.in_(ids))
            .group_by(CheckSample.check_id)
        )).all():
            if total:
                up[cid] = (ups or 0) / total * 100
    inc: dict[int, dict] = {}
    if ids:
        for i in await session.scalars(
            select(CheckIncident).where(
                CheckIncident.check_id.in_(ids), CheckIncident.status == "down",
                CheckIncident.started_at < until,
                or_(CheckIncident.ended_at.is_(None), CheckIncident.ended_at > since),
            )
        ):
            s, e = _aware(i.started_at), _aware(i.ended_at) or until
            d = _overlap(s, e, since, until)
            x = inc.setdefault(i.check_id, {"n": 0, "down": 0.0, "longest": 0.0, "last": None, "open": False})
            x["n"] += 1
            x["down"] += d
            x["longest"] = max(x["longest"], (e - s).total_seconds())
            x["last"] = max(x["last"], s) if x["last"] else s
            x["open"] = x["open"] or i.ended_at is None
    return up, inc


async def _sites(session, user, since, until, prev_since, prev_ok) -> dict:
    checks = list(await session.scalars(
        scope_query(user, select(Check).where(Check.enabled.is_(True)), Check, "sites")))
    ids = [c.id for c in checks]
    up, inc = await _site_stats(session, ids, since, until)
    items = []
    for c in checks:
        x = inc.get(c.id)
        u = up.get(c.id)
        if not x and (u is None or u >= 99.995):
            continue
        items.append({
            "id": c.id, "name": c.name, "group": c.group_name or "",
            "uptime": round(u, 3) if u is not None else None,
            "incidents": x["n"] if x else 0, "downtime_s": int(x["down"]) if x else 0,
            "longest_s": int(x["longest"]) if x else 0, "last_at": _iso(x["last"]) if x else None,
            "open": bool(x and x["open"]),
        })
    items.sort(key=lambda r: (-r["downtime_s"], r["uptime"] if r["uptime"] is not None else 100, r["name"]))

    def totals(up_m: dict, inc_m: dict) -> dict:
        ups = list(up_m.values())
        return {
            "uptime": round(sum(ups) / len(ups), 3) if ups else None,
            "incidents": sum(v["n"] for v in inc_m.values()),
            "downtime_s": int(sum(v["down"] for v in inc_m.values())),
        }

    prev = None
    if prev_ok:
        p_up, p_inc = await _site_stats(session, ids, prev_since, since)
        prev = totals(p_up, p_inc)
    return {"total": len(checks), "affected": len(items), **totals(up, inc), "prev": prev,
            "items": items[:_TOP], "more": max(len(items) - _TOP, 0)}


def _gap_seconds(dialect: str, a, b):
    if dialect == "postgresql":
        return func.extract("epoch", a - b)
    return (func.julianday(a) - func.julianday(b)) * 86400.0


async def _offline_gaps(session, servers, since, until, now, tail: bool = True) -> tuple[dict[int, list], list]:
    """Простои серверов по пропускам в минутных метриках: {id: [(начало, конец)]} и общие
    простои панели. Порог - как у алерта "недоступен": offline_after плюс выдержка."""
    by_id = {s.id: s for s in servers}
    thr = {s.id: max(s.offline_after_seconds or 120, 30) + _OFFLINE_ALERT_EXTRA for s in servers}
    gaps: dict[int, list] = {s.id: [] for s in servers}
    if servers:
        dialect = session.bind.dialect.name
        # тип явно: в SQLite lag() иначе отдает строку
        prev = func.lag(ServerMetric.ts, type_=ServerMetric.ts.type).over(
            partition_by=ServerMetric.server_id, order_by=ServerMetric.ts)
        sub = (select(ServerMetric.server_id.label("sid"), ServerMetric.ts.label("ts"), prev.label("prev"))
               .where(ServerMetric.server_id.in_(list(by_id)), ServerMetric.ts >= since - timedelta(days=1),
                      ServerMetric.ts < until)
               .subquery())
        rows = (await session.execute(
            select(sub.c.sid, sub.c.prev, sub.c.ts).where(
                sub.c.prev.is_not(None), _gap_seconds(dialect, sub.c.ts, sub.c.prev) > min(thr.values()))
        )).all()
        for sid, a, b in rows:
            a, b = _aware(a), _aware(b)
            if (b - a).total_seconds() > thr[sid] and b > since:
                gaps[sid].append((max(a, since), min(b, until)))
        # молчит прямо сейчас: хвост от последнего отчета до конца периода (только для текущего)
        for s in servers if tail else []:
            ls = _aware(s.last_seen)
            if ls is not None and not seen_online(s, now) and ls < until and (until - ls).total_seconds() > thr[s.id]:
                gaps[s.id].append((max(ls, since), until))
    # Пропуск сразу у большинства серверов - лежала панель, а не они
    reporting = [sid for sid in gaps if by_id[sid].last_seen is not None]
    need = max(3, int(len(reporting) * _PANEL_SHARE + 0.999))
    panel: list[tuple[datetime, datetime]] = []
    flat = [(sid, a, b) for sid, lst in gaps.items() for a, b in lst]
    for sid, a, b in flat:
        n = len({o for o, oa, ob in flat if oa < b and ob > a})
        if n >= need:
            panel.append((a, b))
    if panel:
        for sid in gaps:
            gaps[sid] = [(a, b) for a, b in gaps[sid] if not any(pa < b and pb > a for pa, pb in panel)]
    merged: list[list[datetime]] = []
    for a, b in sorted(panel):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return gaps, [(a, b) for a, b in merged]


async def _servers(session, user, since, until, prev_since, now) -> dict:
    servers = list(await session.scalars(
        scope_query(user, select(Server).where(Server.enabled.is_(True)), Server, "servers")))
    gaps, panel = await _offline_gaps(session, servers, since, until, now)
    p_gaps, _p_panel = await _offline_gaps(session, servers, prev_since, since, now, tail=False)
    # алерты по серверу за период: сколько и каких видов (только доставленные)
    names = [s.name for s in servers]
    alerts: dict[str, dict[str, int]] = {}
    if names:
        q = _alert_scope(user, select(AlertEvent.target, AlertEvent.kind, func.count()).where(
            AlertEvent.ts >= since, AlertEvent.ts < until, AlertEvent.recovery.is_(False),
            AlertEvent.target.in_(names), AlertEvent.section != "sites", AlertEvent.kind != "",
        )).group_by(AlertEvent.target, AlertEvent.kind)
        for tgt, kind, n in (await session.execute(q)).all():
            alerts.setdefault(tgt, {})[kind] = n
    items = []
    for s in servers:
        g = gaps.get(s.id) or []
        off = sum((b - a).total_seconds() for a, b in g)
        al = alerts.get(s.name) or {}
        if not g and not al:
            continue
        top = sorted(al.items(), key=lambda kv: -kv[1])[:4]
        items.append({
            "id": s.id, "name": s.name, "group": s.group_name or "", "offline_s": int(off), "gaps": len(g),
            "longest_s": int(max(((b - a).total_seconds() for a, b in g), default=0)),
            "alerts": sum(al.values()), "kinds": [{"kind": k, "label": _label(k), "n": n} for k, n in top],
        })
    items.sort(key=lambda r: (-r["offline_s"], -r["alerts"], r["name"]))
    total_off = int(sum((b - a).total_seconds() for lst in gaps.values() for a, b in lst))
    prev_off = int(sum((b - a).total_seconds() for lst in p_gaps.values() for a, b in lst))
    return {
        "total": len(servers), "with_gaps": sum(1 for v in gaps.values() if v), "offline_s": total_off,
        "prev_offline_s": prev_off, "panel_s": int(sum((b - a).total_seconds() for a, b in panel)),
        "panel_gaps": len(panel), "items": items[:_TOP], "more": max(len(items) - _TOP, 0),
    }


async def _noise(session, user, since, until, prev_since) -> dict:
    """Что шумело: срабатывания (отбои шумом не считаем) по виду и объекту."""
    def fired(a, b):
        return _alert_scope(user, select(AlertEvent).where(
            AlertEvent.ts >= a, AlertEvent.ts < b, AlertEvent.recovery.is_(False))).subquery()

    cur = fired(since, until)
    n = func.count().label("n")
    total = await session.scalar(select(func.count()).select_from(cur)) or 0
    prev = await session.scalar(select(func.count()).select_from(fired(prev_since, since))) or 0
    top = (await session.execute(
        select(cur.c.kind, cur.c.target, n).where(cur.c.kind != "", cur.c.target != "")
        .group_by(cur.c.kind, cur.c.target).order_by(n.desc(), cur.c.target).limit(10)
    )).all()
    backups = (await session.execute(
        select(cur.c.target, cur.c.kind, n).where(cur.c.kind.like("backup%"), cur.c.target != "")
        .group_by(cur.c.target, cur.c.kind)
    )).all()
    bk: dict[str, dict] = {}
    for tgt, kind, cnt in backups:
        x = bk.setdefault(tgt, {"target": tgt, "n": 0, "kinds": []})
        x["n"] += cnt
        x["kinds"].append(_label(kind))
    return {
        "fires": total, "prev_fires": prev,
        "top": [{"kind": k, "label": _label(k), "target": t, "n": c} for k, t, c in top],
        "backups": sorted(bk.values(), key=lambda x: -x["n"])[:_TOP],
    }


async def _maint(session, user, since, until, srv_ids: set[int], chk_ids: set[int], tz: str) -> list[dict]:
    out = []
    for w in await session.scalars(select(MaintenanceWindow)):
        s_ids = set(w.server_ids or []) & srv_ids
        c_ids = set(w.check_ids or []) & chk_ids
        if user.role != "admin" and not (s_ids or c_ids):
            continue  # чужое окно: в нем ни одного объекта, который учетка видит
        seen: set[tuple] = set()
        probe = since
        while probe < until + timedelta(days=7):
            for a, b in maint.occurrences(w, probe, tz):
                if a < until and b > since and (a, b) not in seen:
                    seen.add((a, b))
            probe += timedelta(days=7)
        if not seen:
            continue
        # повторяющееся окно - одной строкой: сколько раз было и когда последний
        occ = sorted(seen)
        out.append({"title": w.title, "repeat": w.repeat or "", "count": len(occ),
                    "start": occ[-1][0].isoformat(), "end": occ[-1][1].isoformat(),
                    "servers": len(s_ids), "checks": len(c_ids)})
    return sorted(out, key=lambda x: x["start"], reverse=True)


@router.get("")
async def report(user: CurrentUser, session: SessionDep, days: int = Query(7, ge=1, le=30)) -> dict:
    now = datetime.now(timezone.utc)
    until = now
    since = now - timedelta(days=days)
    prev_since = since - timedelta(days=days)
    keep = get_settings().sample_retention_days
    sections = set(user_sections(user))
    cfg = await settings_store.get_alert_config(session, get_settings())
    out: dict = {"days": days, "since": since.isoformat(), "until": until.isoformat(),
                 # снимки сайтов живут keep дней: прошлый период целиком есть только для коротких
                 "prev_ok": days * 2 <= keep}
    srv_ids: set[int] = set()
    chk_ids: set[int] = set()
    if "sites" in sections:
        out["sites"] = await _sites(session, user, since, until, prev_since, out["prev_ok"])
        chk_ids = {i for (i,) in (await session.execute(
            scope_query(user, select(Check.id), Check, "sites"))).all()}
    if "servers" in sections:
        out["servers"] = await _servers(session, user, since, until, prev_since, now)
        # минутные метрики живут server_metric_retention_days: иначе прошлый период пустой
        out["servers"]["prev_ok"] = days * 2 <= get_settings().server_metric_retention_days
        srv_ids = {i for (i,) in (await session.execute(
            scope_query(user, select(Server.id), Server, "servers"))).all()}
    out["noise"] = await _noise(session, user, since, until, prev_since)
    if "backups" not in sections:
        out["noise"]["backups"] = []
    out["maint"] = await _maint(session, user, since, until, srv_ids, chk_ids, cfg.get("tz") or "Europe/Moscow")
    return out
