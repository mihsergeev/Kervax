"""Износ SSD: когда диск дойдет до 90% и до 100%.

Helper diskhealth-setup отдает износ в процентах (у NVMe Percentage Used, у SATA - нормализованный
атрибут остатка ресурса) и наработку в часах. Алерт по износу был только с 90%, а это впритык,
чтобы договориться с хостером о замене. На одной ноде оба диска RAID1 за 12 дней прошли с 75 до
77%: при таком темпе до 90% два с половиной месяца, и дойдут они туда вместе.

Скорость. Пока своей истории мало - средняя за всю жизнь диска (износ / наработка). Износ целый,
поэтому замеренная скорость считается только между моментами, когда процент сменился: от первой
увиденной смены до последней, если между ними хотя бы MEASURE_MIN_DAYS. Первое наблюдение в счет
не идет: процент мог смениться за минуту до него или за месяц.

Раз в час планировщик (refresh) обновляет servers.disk_wear:
{серийник: {"dev", "wear", "poh", "t": [[дата, износ], ...], "seen", "rate", "src", "d90", "d100",
"soon"}}. soon - до 90% меньше SOON_DAYS (снимается после SOON_OFF_DAYS, чтобы прогноз не мигал
на границе): строка уровня 1 в алерте "диск" и в "Что сломано". До 90% меньше полугода - пункт
"запланируйте замену" в "Требует действий" (считает главная).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Server

log = logging.getLogger("kervax.disk_wear")

WARN = 90
SOON_DAYS = 60
SOON_OFF_DAYS = 90
MEASURE_MIN_DAYS = 10
# средняя за жизнь по первым дням наработки ничего не говорит
_LIFETIME_MIN_H = 24 * 30
_KEEP_T = 60
_FORGET_DAYS = 30
_EVERY = timedelta(hours=1)
_last_run: datetime | None = None


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        return None


def rate(t: list, wear: float, poh: float | None) -> tuple[float | None, str]:
    """Сколько процентов износа в сутки и откуда это известно ("measured" или "lifetime")."""
    steps = t[1:]  # первое наблюдение - не смена процента
    if len(steps) >= 2:
        (d0, w0), (d1, w1) = steps[0], steps[-1]
        a, b = _day(d0), _day(d1)
        if a and b and (b - a).days >= MEASURE_MIN_DAYS and w1 > w0:
            return (w1 - w0) / (b - a).days, "measured"
    if poh and poh >= _LIFETIME_MIN_H and wear > 0:
        return wear / (poh / 24), "lifetime"
    return None, ""


def update(prev: dict | None, block: dict, now: datetime) -> dict:
    """Новое значение servers.disk_wear по свежему блоку здоровья дисков."""
    prev = prev if isinstance(prev, dict) else {}
    today = now.date()
    out: dict[str, dict] = {}
    for d in block.get("disks") or []:
        if not isinstance(d, dict):
            continue
        ser, wear = str(d.get("serial") or ""), _num(d.get("wear"))
        if not ser or wear is None:
            continue
        p = prev.get(ser) if isinstance(prev.get(ser), dict) else {}
        t = [x for x in p.get("t") or [] if isinstance(x, list) and len(x) == 2]
        if not t or t[-1][1] != wear:
            t.append([today.isoformat(), wear])
        t = t[-_KEEP_T:]
        poh = _num(d.get("poh"))
        r, src = rate(t, wear, poh)
        rec = {"dev": str(d.get("dev") or ""), "wear": wear, "poh": poh, "t": t, "seen": today.isoformat(),
               "rate": round(r, 4) if r else None, "src": src, "d90": None, "d100": None, "soon": False}
        if r and r > 0:
            if wear < WARN:
                days90 = (WARN - wear) / r
                rec["d90"] = (today + timedelta(days=round(days90))).isoformat()
                rec["soon"] = days90 <= SOON_DAYS or (bool(p.get("soon")) and days90 <= SOON_OFF_DAYS)
            if wear < 100:
                rec["d100"] = (today + timedelta(days=round((100 - wear) / r))).isoformat()
        out[ser] = rec
    # диск не виден (заменили или helper ненадолго замолчал): историю помним месяц
    for ser, p in prev.items():
        if ser not in out and isinstance(p, dict):
            seen = _day(p.get("seen"))
            if seen and (today - seen).days <= _FORGET_DAYS:
                out[ser] = p
    return out


def _ddmm(iso: str) -> str:
    d = _day(iso)
    return d.strftime("%d.%m.%Y") if d else str(iso)


def soon_text(name: str, rec: dict, short: bool = False) -> str:
    w = rec.get("wear")
    base = f"диск {name}: износ {w:g}%, 90% примерно к {_ddmm(rec.get('d90') or '')}"
    if short:
        return base
    how = "по средней за всю жизнь диска" if rec.get("src") == "lifetime" else "по замеру"
    return f"{base} ({float(rec.get('rate') or 0):.2f}% в сутки, {how}) - пора заказывать замену"


def card(block: dict | None, hist: dict | None) -> list[dict]:
    """Прогноз по дискам, которые сейчас в отчете: для карточки и "Требует действий"."""
    if not block or not isinstance(hist, dict):
        return []
    out = []
    for d in block.get("disks") or []:
        if not isinstance(d, dict):
            continue
        rec = hist.get(str(d.get("serial") or ""))
        if isinstance(rec, dict) and rec.get("rate"):
            out.append({k: rec.get(k) for k in ("wear", "rate", "src", "d90", "d100", "soon")}
                       | {"dev": str(d.get("dev") or ""), "serial": str(d.get("serial") or "")})
    return out


async def refresh(session_factory: async_sessionmaker[AsyncSession], now: datetime, block_of) -> None:
    """Раз в час: история износа по свежим блокам здоровья дисков. block_of - функция,
    достающая свежий блок из отчета (collector.disk_health_block)."""
    global _last_run
    if _last_run is not None and now - _last_run < _EVERY:
        return
    _last_run = now
    async with session_factory() as s:
        for srv in await s.scalars(select(Server).where(Server.enabled.is_(True))):
            hb = block_of(srv.last_report or {})
            if hb is None:
                continue  # нет свежего блока - не повод забывать историю
            new = update(srv.disk_wear, hb, now)
            if new != (srv.disk_wear or {}):
                srv.disk_wear = new
        await s.commit()
