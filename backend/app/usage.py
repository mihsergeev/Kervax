"""Кто качает трафик и кто пишет на диск - по часам за неделю.

Агент отдает топ за последний час: net_users (2.34) - сколько контейнер или под принял и отдал,
disk_writers (2.33) - сколько записал и прочитал, и за какое окно (s, у недавно запущенного меньше
часа). Раз в STEP панель раскладывает скорость каждого за время с прошлого раза по часовым
корзинам (UTC): скорость * время, но не дольше окна агента. Так сутки сходятся, даже если отчеты
шли неровно; перерыв дольше окна - в корзину только окно, раньше агент уже не помнит.

В топ агента попадают самые большие (по сети восемь по исходящему и восемь по входящему, по диску
восемь по записи), мелкие в истории недосчитаны: вопрос в том, кто больше всех.

Сетевые счетчики контейнера - его интерфейсы, туда входит и обмен с другими контейнерами этого
сервера: база, которая отдает приложению гигабайты, тариф не тратит. Поэтому алерта по ним нет,
только карточка с этой оговоркой.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.models import Server, ServerUsage

STEP = timedelta(minutes=10)
KEEP = timedelta(days=8)  # неделя по дням в часовом поясе зрителя и сутки запаса
NAME_MAX = 200
# что в a и что в b: по сети принято и отдано, по диску записано и прочитано
METRICS = {"net": ("rx", "tx"), "disk": ("w", "r")}
DAYS = 7
TOP = 12


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def spread(items, keys: tuple[str, str], start: datetime, end: datetime) -> dict[tuple[int, str], list]:
    """Доля каждого за [start, end] по часовым корзинам: {(час с 1970, имя): [a, b, kind]}.
    Скорость - из окна агента, время - не раньше начала окна."""
    out: dict[tuple[int, str], list] = {}
    t1 = end.timestamp()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        try:
            s = float(it.get("s") or 0)
            a, b = float(it.get(keys[0]) or 0), float(it.get(keys[1]) or 0)
        except (TypeError, ValueError):
            continue
        name = str(it.get("name") or "")[:NAME_MAX]
        if s <= 0 or not name or (a <= 0 and b <= 0):
            continue
        t0 = max(start.timestamp(), t1 - s)
        h = int(t0 // 3600)
        while h * 3600 < t1:
            sec = min(t1, (h + 1) * 3600) - max(t0, h * 3600)
            if sec > 0:
                acc = out.setdefault((h, name), [0.0, 0.0, str(it.get("kind") or "")[:16]])
                acc[0] += a * sec / s
                acc[1] += b * sec / s
            h += 1
    return out


async def ingest(session, server: Server, net_users, disk_writers, now: datetime) -> None:
    """Раз в STEP: топы из отчета - в часовые корзины."""
    last = _aware(server.usage_at) if server.usage_at else None
    if last is not None and now - last < STEP:
        return
    server.usage_at = now
    # первый раз - все окно агента: за последний час он знает
    start = last or now - timedelta(hours=2)
    rows: dict[tuple[str, int, str], list] = {}
    for metric, items in (("net", net_users), ("disk", disk_writers)):
        for (h, name), v in spread(items, METRICS[metric], start, now).items():
            rows[(metric, h, name)] = v
    if not rows:
        return
    hours = {h for _m, h, _n in rows}
    have = {(r.metric, r.hour, r.name): r for r in (await session.execute(
        select(ServerUsage).where(ServerUsage.server_id == server.id, ServerUsage.hour.in_(hours))
    )).scalars()}
    for (metric, h, name), (a, b, kind) in rows.items():
        r = have.get((metric, h, name))
        if r is None:
            r = ServerUsage(server_id=server.id, metric=metric, hour=h, name=name, kind=kind, a=0, b=0)
            session.add(r)
        r.a = (r.a or 0) + round(a)
        r.b = (r.b or 0) + round(b)
        if kind:
            r.kind = kind


async def view(session, server_id: int, metric: str, now: datetime, tz_min: int = 0) -> dict:
    """Для карточки: по каждому сумма за последние сутки и за неделю и разбивка по дням недели
    в часовом поясе зрителя (tz_min - как Date.getTimezoneOffset: минуты, к западу плюс)."""
    h_now = int(now.timestamp() // 3600)
    off = -int(tz_min) * 60  # секунды к UTC, чтобы получить местное время
    today = datetime.fromtimestamp(now.timestamp() + off, timezone.utc).date()
    days = [today - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]
    first = days[0]
    rows = (await session.execute(
        select(ServerUsage.name, ServerUsage.kind, ServerUsage.hour, ServerUsage.a, ServerUsage.b)
        .where(ServerUsage.server_id == server_id, ServerUsage.metric == metric,
               ServerUsage.hour >= h_now - DAYS * 24 - 24)
    )).all()
    per: dict[str, dict] = {}
    since = None
    for r in rows:
        day = datetime.fromtimestamp(r.hour * 3600 + off, timezone.utc).date()
        if day < first:
            continue
        since = r.hour if since is None else min(since, r.hour)
        x = per.setdefault(r.name, {"name": r.name, "kind": r.kind or "", "d24": [0, 0], "week": [0, 0],
                                     "days": [[0, 0] for _ in days]})
        a, b = int(r.a or 0), int(r.b or 0)
        if r.hour > h_now - 24:
            x["d24"][0] += a
            x["d24"][1] += b
        x["week"][0] += a
        x["week"][1] += b
        dd = x["days"][(day - first).days]
        dd[0] += a
        dd[1] += b
    # диск - по записи (от нее износ), сеть - по всему обмену
    names = sorted(per.values(), key=lambda x: -(x["week"][0] + (x["week"][1] if metric == "net" else 0)))[:TOP]
    return {
        "metric": metric, "days": [str(d) for d in days], "names": names,
        "since": datetime.fromtimestamp(since * 3600, timezone.utc).isoformat() if since is not None else None,
    }
