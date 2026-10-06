"""Прогноз заполнения дисков и inode по истории заполнения (ServerMetric.disks).

Тренд берется по минимумам трех суточных окон. Временные всплески (дамп записался и удалился,
логи ротировались ночью) минимумы не трогают, а устойчивый рост поднимает их каждый день.
Прогноз есть, только если минимум рос оба дня подряд: разовый скачок (кто-то скопировал
архив) - не тренд. Заполнение за часы ловят обычные пороги диска, прогноз - про дни: на
реальной истории парка 05.10.2026 он нашел ровно один такой раздел (бэкапы ClickHouse на
backup-a, +3% в сутки) и ни одного ложного у бэкап-серверов с ротацией.

Рост обоих дней должен быть заметным и сопоставимым. Иначе разовый скачок проходил как тренд,
если в соседние сутки раздел подрос на шум: на admin-a сборка образов разом добавила
+7.1% к /app, накануне было +0.1%, и панель обещала заполнение через неделю при ровном графике.
Неделя истории трех панелей (07.10.2026): так отсекается только этот случай, настоящий рост
(backup-a, корень backup-c) остается при любых порогах от 0.2 до 0.5 и отношении 3-4.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Server, ServerMetric

log = logging.getLogger(__name__)

DAY = 86400.0
WINDOWS = 3
# Минутных точек в каждом окне (6 часов из 24): меньше - нода молчала, судить не по чему
MIN_POINTS = 360
# Суточный прирост меньше этого (процентных пунктов) - шум, а не рост; и сутки не могут
# отличаться больше чем в STEP_RATIO раз, иначе это скачок, а не тренд
MIN_STEP = 0.3
STEP_RATIO = 4
# Дальше месяца прогноз не показываем: при таком росте это еще не новость
SHOW_HOURS = 30 * 24
REFRESH = timedelta(minutes=30)
# Серверов за тик: история трех суток - тысячи строк на сервер, весь парк разом не нужен
BATCH = 8
# Прогноз старше этого для алерта не годится: нода молчит или планировщик стоял
MAX_AGE = timedelta(hours=2)
# Нода, не отчитавшаяся столько, не пересчитывается: новых точек нет, прогноз был бы старым
ONLINE = timedelta(minutes=10)


def _epoch(ts: datetime) -> float:
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).timestamp()


def growth_rate(points: list[tuple[float, float]], now: float) -> float | None:
    """Рост в процентных пунктах в сутки по минимумам трех суточных окон, None - если
    устойчивого роста нет или данных мало."""
    mins = []
    for d in range(WINDOWS):
        lo, hi = now - (d + 1) * DAY, now - d * DAY
        vals = [v for t, v in points if lo <= t < hi]
        if len(vals) < MIN_POINTS:
            return None
        mins.append(min(vals))
    m0, m1, m2 = mins
    older, newer = m1 - m2, m0 - m1
    if older < MIN_STEP or newer < MIN_STEP:
        return None
    if min(older, newer) * STEP_RATIO < max(older, newer):
        return None
    return (m0 - m2) / (WINDOWS - 1)


def build(rows, rep: dict, now: float) -> list[dict]:
    """Прогноз по каждому разделу: rows - история [(ts, disks)], rep - последний отчет (из
    него текущее заполнение). Свободное место берем доступное обычным процессам (avail,
    агент 2.15+): ext4 держит резерв для root, и сервисы встают раньше 100%."""
    series: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for ts, disks in rows:
        t = _epoch(ts)
        for d in disks or []:
            if not isinstance(d, dict) or not d.get("mount"):
                continue
            if d.get("pct") is not None:
                series.setdefault((d["mount"], "space"), []).append((t, float(d["pct"])))
            if d.get("ipct") is not None:
                series.setdefault((d["mount"], "inode"), []).append((t, float(d["ipct"])))
    cur = {d.get("mount"): d for d in rep.get("disks") or [] if isinstance(d, dict)}
    items = []
    for (mount, kind), pts in series.items():
        d = cur.get(mount)
        if not d or not d.get("total"):
            continue  # раздела больше нет
        rate = growth_rate(pts, now)
        if rate is None:
            continue
        total, used = float(d["total"]), float(d.get("used") or 0)
        if kind == "space":
            pct = used / total * 100
            avail = d.get("avail")
            left = (float(avail) if avail is not None else total - used) / total * 100
        else:
            itot = float(d.get("inodes") or 0)
            if itot <= 0:
                continue
            pct = float(d.get("inodes_used") or 0) / itot * 100
            left = 100 - pct
        eta_h = max(0.0, left / rate * 24)
        if eta_h > SHOW_HOURS:
            continue
        items.append({"mount": mount, "kind": kind, "pct": round(pct, 1),
                      "rate": round(rate, 2), "eta_h": round(eta_h, 1)})
    items.sort(key=lambda i: i["eta_h"])
    return items


def fresh_items(fc: dict | None, now: datetime) -> list[dict] | None:
    """Пункты прогноза, если он свежий; None - прогноза нет или он протух."""
    if not isinstance(fc, dict) or not fc.get("ts"):
        return None
    if now.timestamp() - float(fc["ts"]) > MAX_AGE.total_seconds():
        return None
    return [i for i in fc.get("items") or [] if isinstance(i, dict) and i.get("eta_h") is not None]


def eta_text(h: float) -> str:
    """Срок словами: через 14 ч или через 3 дн; до двух суток - часами, дальше днями."""
    if h < 1:
        return "меньше чем через час"
    if h < 48:
        return f"через {round(h)} ч"
    return f"через {round(h / 24)} дн"


def _num(x: float) -> str:
    return f"{x:.1f}".rstrip("0").rstrip(".") if x < 10 else str(round(x))


def item_text(i: dict) -> str:
    """Строка прогноза: диск /hdd заполнится примерно через 3 дн: сейчас 91%, растет на ~3% в сутки."""
    when = eta_text(float(i["eta_h"]))
    what = (f"диск {i['mount']} заполнится примерно {when}" if i.get("kind") != "inode"
            else f"inode на {i['mount']} закончатся примерно {when}")
    return f"{what}: сейчас {_num(float(i['pct']))}%, растет на ~{_num(float(i['rate']))}% в сутки"


def forecast_text(items: list[dict], horizon_h: float) -> str:
    """Самый скорый раздел - строкой алерта, остальные в пределах горизонта - коротко."""
    if not items:
        return ""
    first, rest = items[0], [i for i in items[1:] if float(i["eta_h"]) <= horizon_h]
    txt = item_text(first)
    if rest:
        txt += "; еще " + ", ".join(
            f"{'inode ' if i.get('kind') == 'inode' else ''}{i['mount']} {eta_text(float(i['eta_h']))}"
            for i in rest[:3])
    return txt


def mount_eta(fc: dict | None, mount: str | None, kind: str, now: datetime) -> float | None:
    """Через сколько часов кончится место (или inode) на этом разделе, если прогноз есть."""
    for i in fresh_items(fc, now) or []:
        if i.get("mount") == mount and i.get("kind", "space") == kind:
            return float(i["eta_h"])
    return None


async def update_disk_forecasts(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    """Пересчитать прогноз у серверов, где он старше получаса: по BATCH за тик, самые
    давние первыми, только у нод на связи (у молчащей новых точек нет)."""
    async with session_factory() as s:
        rows = (await s.execute(select(Server.id, Server.disk_forecast, Server.last_seen)
                                .where(Server.enabled.is_(True)))).all()
        due = []
        for sid, fc, seen in rows:
            if seen is None or now - (seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc)) > ONLINE:
                continue
            ts = float((fc or {}).get("ts") or 0)
            if now.timestamp() - ts >= REFRESH.total_seconds():
                due.append((ts, sid))
        due.sort()
        n = 0
        for _ts, sid in due[:BATCH]:
            srv = await s.get(Server, sid)
            if srv is None:
                continue
            hist = (await s.execute(
                select(ServerMetric.ts, ServerMetric.disks)
                .where(ServerMetric.server_id == sid,
                       ServerMetric.ts >= now - timedelta(days=WINDOWS, minutes=5))
                .order_by(ServerMetric.ts))).all()
            srv.disk_forecast = {"ts": int(now.timestamp()),
                                 "items": build(hist, srv.last_report or {}, now.timestamp())}
            n += 1
        if n:
            await s.commit()
        return n
