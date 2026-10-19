"""Что хранит панель и сколько это занимает: окно "Хранение данных".

Две группы со сроком из настроек (метрики серверов и история проверок) и данные со своими
постоянными сроками. Размер таблиц в Postgres точный (pg_total_relation_size, с индексами и
TOAST). SQLite без dbstat знает только размер всей базы: таблицам он раскладывается по числу
строк с весом примерного размера строки, это оценка (estimated).

Постоянные сроки живут здесь, чистка (collector._prune) берет их отсюда: окно показывает ровно
то, что удаляется."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import settings_store
from app import timeline as timeline_mod
from app import traffic as traffic_mod
from app import panel_errors as panel_errors_mod
from app import usage as usage_mod
from app.config import Settings
from app.models import CheckSample, ServerMetric

ALERTS_KEEP = timedelta(days=180)        # история алертов маленькая, а нужна надолго
COMMANDS_KEEP = timedelta(days=7)        # команды Docker, Kubernetes, бэкапа (с логами)
WEB_LINES_KEEP = timedelta(days=7)       # строки 5xx: в них адреса клиентов и запросы целиком
DOMAIN_PROBES_KEEP = timedelta(days=7)   # проверки домена из мастера живут часы
PROBES_KEEP = timedelta(days=1)          # ручные проверки нужны на время ожидания ответа
REQUESTS_KEEP = timedelta(days=2)        # левые запросы к панели: алерту нужны 10 минут, окну - сутки

# группы со сроком из настроек: ключ срока и таблицы, первая - главная (самая старая запись)
GROUPS = {
    "srv": ("server_days", ServerMetric, ["server_metrics", "link_samples", "web_error_samples", "oom_events"]),
    "chk": ("sample_days", CheckSample, ["check_samples", "location_samples", "check_ip_samples", "check_incidents"]),
}
# постоянные сроки: ключ, срок, таблицы (строки 5xx - колонка в web_error_samples, своего размера нет)
FIXED = [
    ("alerts", ALERTS_KEEP, ["alert_events"]),
    ("timeline", timeline_mod.KEEP, ["server_events"]),
    ("traffic", traffic_mod.KEEP, ["server_traffic"]),
    ("usage", usage_mod.KEEP, ["server_usage"]),
    ("commands", COMMANDS_KEEP, ["docker_commands", "kube_commands", "backup_commands"]),
    ("web_lines", WEB_LINES_KEEP, []),
    ("domain_probes", DOMAIN_PROBES_KEEP, ["domain_probes"]),
    ("probes", PROBES_KEEP, ["probe_requests"]),
    ("panel_requests", REQUESTS_KEEP, ["request_stats"]),
    ("panel_errors", panel_errors_mod.KEEP, ["panel_errors"]),
]
# примерный размер строки с индексами, байт (по продам) - только для оценки в SQLite
_ROW = {"server_metrics": 1400, "check_samples": 150, "location_samples": 150, "check_ip_samples": 220,
        "web_error_samples": 700, "link_samples": 190, "server_events": 160, "check_incidents": 400}
_ROW_DEFAULT = 300


def _known() -> list[str]:
    out = [t for _k, _m, ts in GROUPS.values() for t in ts]
    return out + [t for _k, _d, ts in FIXED for t in ts]


async def _sizes(session: AsyncSession) -> tuple[int | None, dict[str, int], dict[str, int], bool]:
    """Размер базы, байты и строки по таблицам, оценка ли это."""
    if session.bind.dialect.name == "postgresql":
        total = (await session.execute(text("select pg_database_size(current_database())"))).scalar()
        rows = (await session.execute(text(
            "select relname, pg_total_relation_size(relid), n_live_tup from pg_stat_user_tables"))).all()
        return (int(total) if total is not None else None, {r[0]: int(r[1]) for r in rows},
                {r[0]: int(r[2] or 0) for r in rows}, False)
    # SQLite: строки считаем сами, размер - dbstat, если собран, иначе оценка
    pages = (await session.execute(text("pragma page_count"))).scalar() or 0
    psize = (await session.execute(text("pragma page_size"))).scalar() or 0
    total = int(pages) * int(psize)
    counts: dict[str, int] = {}
    for t in _known():
        try:
            counts[t] = int((await session.execute(text(f'select count(*) from "{t}"'))).scalar() or 0)
        except Exception:  # noqa: BLE001 - таблицы нет (старая база)
            counts[t] = 0
    try:
        rows = (await session.execute(text(
            "select m.tbl_name, sum(d.pgsize) from dbstat d join sqlite_master m on m.name = d.name "
            "group by m.tbl_name"))).all()
        return total, {r[0]: int(r[1] or 0) for r in rows}, counts, False
    except Exception:  # noqa: BLE001 - SQLite собран без dbstat
        pass
    est = {t: n * _ROW.get(t, _ROW_DEFAULT) for t, n in counts.items()}
    s = sum(est.values())
    cap = total * 0.95
    if s > cap > 0:
        est = {t: int(v * cap / s) for t, v in est.items()}
    return total, est, counts, True


def _aware(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


async def overview(session: AsyncSession, settings: Settings, now: datetime) -> dict:
    ret = await settings_store.get_retention(session, settings)
    total, size, rows, estimated = await _sizes(session)
    listed = 0
    groups = []
    for gid, (key, model, tables) in GROUPS.items():
        days = int(ret[key])
        parts = [{"key": t, "bytes": size.get(t, 0), "rows": rows.get(t)} for t in tables]
        b = sum(p["bytes"] for p in parts)
        listed += b
        oldest = _aware((await session.execute(select(func.min(model.ts)))).scalar())
        # прирост в сутки: за сколько дней накоплено то, что лежит (не больше срока)
        span = (now - oldest).total_seconds() / 86400 if oldest else 0
        covered = max(1.0, min(float(days), span))
        groups.append({"id": gid, "days": days, "oldest": oldest, "bytes": b, "span_days": round(min(days, span), 1),
                       "per_day": int(b / covered) if b else 0, "parts": parts})
    fixed = []
    for key, keep, tables in FIXED:
        b = sum(size.get(t, 0) for t in tables) if tables else None
        listed += b or 0
        fixed.append({"key": key, "days": keep.days, "bytes": b,
                      "rows": sum(rows.get(t, 0) for t in tables) if tables else None})
    return {
        "db_bytes": total,
        "estimated": estimated,
        "groups": groups,
        "fixed": fixed,
        "other_bytes": max(0, total - listed) if total is not None else None,
        "prune_every": int(settings.prune_interval_seconds),
    }
