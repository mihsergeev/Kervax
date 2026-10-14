"""Трафик сервера за сутки и за расчетный период, прогноз до лимита тарифа.

Агент 2.31 отдает накопительные счетчики байт каждой сетевой карты и помечает настоящие карты
(phys). Панель складывает приросты с прошлого отчета в сутки (UTC, server_traffic). Сумма всех
интерфейсов не годится: на VPN-ноде один и тот же пакет проходит через карту, туннель и мост
docker, и трафик вышел бы в два-три раза больше настоящего.

Счетчик меньше прошлого - сервер перезагружался: в прирост идет все, что набежало с загрузки.
Агент не отчитывался - карта считала дальше, прирост придет со следующим отчетом целиком.

Лимит задается у сервера (ТБ в месяц, что считает хостер: исходящий или весь, с какого числа
период). Без лимита - только данные, без алертов.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Server, ServerTraffic

SUM_EVERY = timedelta(minutes=10)  # сводку за период пересчитываем не чаще
KEEP = timedelta(days=400)  # сутки трафика храним больше года: их мало, а сравнить с прошлым годом хочется
WARN_PCT = 90.0
FORECAST_MIN_DAYS = 3.0  # прогноз по меньшему числу суток слишком прыгает


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(v) -> datetime | None:
    try:
        return _aware(datetime.fromisoformat(v)) if isinstance(v, str) and v else None
    except ValueError:
        return None


async def ingest(session, server: Server, ifaces, now: datetime) -> None:
    """Приросты счетчиков настоящих карт с прошлого отчета - в сутки."""
    cur: dict[str, list[int]] = {}
    for i in ifaces or []:
        if not isinstance(i, dict) or not i.get("phys"):
            continue
        rx, tx = i.get("rx_total"), i.get("tx_total")
        if isinstance(rx, int) and isinstance(tx, int) and rx >= 0 and tx >= 0:
            cur[str(i.get("if") or "?")] = [rx, tx]
    if not cur:
        return
    st = server.traffic_ctr if isinstance(server.traffic_ctr, dict) else {}
    prev = st.get("c") if isinstance(st.get("c"), dict) else {}
    drx = dtx = 0
    for name, (rx, tx) in cur.items():
        p = prev.get(name)
        if not (isinstance(p, list) and len(p) == 2):
            continue  # карта появилась только что: считаем со следующего отчета
        drx += rx - p[0] if rx >= p[0] else rx
        dtx += tx - p[1] if tx >= p[1] else tx
    server.traffic_ctr = {"since": st.get("since") or now.isoformat(), "c": cur}
    if drx or dtx:
        day = now.astimezone(timezone.utc).date()
        row = await session.get(ServerTraffic, (server.id, day))
        if row is None:
            row = ServerTraffic(server_id=server.id, day=day, rx=0, tx=0)
            session.add(row)
        row.rx = (row.rx or 0) + drx
        row.tx = (row.tx or 0) + dtx
    old = server.traffic_sum if isinstance(server.traffic_sum, dict) else {}
    at = _iso(old.get("at"))
    if at is None or now - at >= SUM_EVERY or str(old.get("day")) != str(now.date()):
        await session.flush()
        server.traffic_sum = await summary(session, server, now)


def period(now: datetime, cycle_day: int) -> tuple[date, date]:
    """Расчетный период: с cycle_day числа до того же числа следующего месяца (не включая)."""
    cd = min(max(int(cycle_day or 1), 1), 28)
    d = now.astimezone(timezone.utc).date()
    y, m = (d.year, d.month) if d.day >= cd else ((d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1))
    start = date(y, m, cd)
    end = date(y + 1, 1, cd) if m == 12 else date(y, m + 1, cd)
    return start, end


async def summary(session, server: Server, now: datetime) -> dict:
    """Сводка за текущий период: сколько ушло, сколько из лимита, прогноз на конец периода."""
    start, end = period(now, server.traffic_cycle_day)
    rows = (await session.execute(
        select(ServerTraffic.rx, ServerTraffic.tx).where(
            ServerTraffic.server_id == server.id, ServerTraffic.day >= start, ServerTraffic.day < end)
    )).all()
    rx, tx = sum(r[0] or 0 for r in rows), sum(r[1] or 0 for r in rows)
    out_only = (server.traffic_count or "out") != "both"
    used = tx if out_only else rx + tx
    st = server.traffic_ctr if isinstance(server.traffic_ctr, dict) else {}
    p0 = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    p1 = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    since = _iso(st.get("since")) or now
    data_from = max(p0, since)
    days = max((now - data_from).total_seconds() / 86400, 0.0)
    left = max((p1 - now).total_seconds() / 86400, 0.0)
    forecast = round(used + used / days * left) if days >= FORECAST_MIN_DAYS else None
    return {
        "at": now.isoformat(), "day": str(now.date()), "start": str(start), "end": str(end),
        "rx": rx, "tx": tx, "used": used, "count": "out" if out_only else "both",
        "limit": int(server.traffic_limit_gb or 0) * 10**9, "forecast": forecast,
        "days": round(days, 2), "left": round(left, 2), "full": data_from <= p0,
    }


def fmt_bytes(n: float) -> str:
    """Десятичные единицы, как в тарифах хостеров: 20 ТБ = 20 * 10^12 байт."""
    for unit, k in (("ТБ", 10**12), ("ГБ", 10**9), ("МБ", 10**6)):
        if n >= k:
            v = n / k
            return f"{v:.1f} {unit}".replace(".0 ", " ") if v < 100 else f"{v:.0f} {unit}"
    return f"{n / 10**3:.0f} КБ"


def problem(ts, now: datetime) -> tuple[int, str]:
    """(уровень, текст) по сводке: лимит исчерпан - 2, израсходовано 90% или прогноз выше
    лимита - 1. Без лимита и без свежей сводки - ничего."""
    if not isinstance(ts, dict) or not ts.get("limit"):
        return 0, ""
    at = _iso(ts.get("at"))
    if at is None or now - at > timedelta(hours=2):
        return 0, ""
    used, limit = float(ts.get("used") or 0), float(ts["limit"])
    what = "исходящего трафика" if ts.get("count") == "out" else "трафика"
    till = _ddmm(ts.get("end"))
    if used >= limit:
        return 2, f"лимит {what} исчерпан: {fmt_bytes(used)} из {fmt_bytes(limit)}, период до {till}"
    pct = used / limit * 100
    if pct >= WARN_PCT:
        return 1, (f"израсходовано {pct:.0f}% {what}: {fmt_bytes(used)} из {fmt_bytes(limit)}, "
                   f"период до {till}")
    fc = ts.get("forecast")
    if isinstance(fc, (int, float)) and fc > limit:
        return 1, (f"к концу периода выйдет около {fmt_bytes(fc)} {what} при лимите {fmt_bytes(limit)} "
                   f"(сейчас {fmt_bytes(used)}, период до {till})")
    return 0, ""


def _ddmm(v) -> str:
    try:
        d = date.fromisoformat(str(v))
        return f"{d.day:02d}.{d.month:02d}"
    except ValueError:
        return "?"
