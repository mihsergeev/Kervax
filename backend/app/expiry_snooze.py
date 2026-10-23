"""Отложенные сроки сайта: "напомнить за 7 дней", "напомнить за 1 день", "не следить".

Повод: домен истекает через 6 дней, его продлят в последний день, а пункт на главной и алерт
все это время висят. Отложить - значит убрать пункт с главной и из алертов, пока до конца
больше выбранного числа дней. На этом пороге пункт возвращается и алерт приходит, даже если
такого порога нет в настройках монитора: "напомнить за 7 дней" и есть просьба напомнить.

Отметка привязана к сроку, который видели, когда откладывали (days). Срок отодвинулся -
домен продлили, сертификат перевыпустили: отложка снимается сама, дальше обычные пороги.
"Не следить" (mode 0) продление не снимает: это решение про сам домен, а не про этот срок.

Срок домена один на весь домен, поэтому отметку домена API ставит всем мониторам на нем.

Запись в checks.expiry_snooze: {"domain"|"ssl": {mode, by, at, days}}; mode 7 или 1 - до скольких
дней отложено, 0 - не следим."""

from datetime import datetime

KINDS = ("ssl", "domain")
MODES = (7, 1, 0)
# Срок между пробами плавает на день (RDAP и WHOIS считают дату по-разному), а продление
# отодвигает его на месяцы. Продленным считаем срок, выросший больше чем на это число дней.
RENEW_SLACK = 2


def _days(row, kind: str) -> int | None:
    return getattr(row, f"{kind}_days", None)


def get(row, kind: str) -> dict | None:
    v = getattr(row, "expiry_snooze", None)
    e = v.get(kind) if isinstance(v, dict) else None
    return e if isinstance(e, dict) else None


def renewed(e: dict, days: int | None) -> bool:
    """Срок отодвинулся с тех пор, как откладывали: продлили."""
    try:
        was = int(e.get("days"))
    except (TypeError, ValueError):
        return False
    return days is not None and days > was + RENEW_SLACK


def hidden(row, kind: str, days: int | None = None) -> bool:
    """Пункт сейчас скрыт: не следим, или отложен и до конца больше выбранного."""
    e = get(row, kind)
    if e is None:
        return False
    days = _days(row, kind) if days is None else days
    mode = int(e.get("mode") or 0)
    if mode == 0:
        return True
    if renewed(e, days):
        return False
    return days is not None and days > mode


def thresholds(row, kind: str, thr: list[int], days: int | None = None) -> list[int]:
    """Пороги напоминаний с порогом отложки: "напомнить за 7 дней" алертит на 7, даже если
    у монитора пороги [30, 1]."""
    e = get(row, kind)
    if e is None or renewed(e, _days(row, kind) if days is None else days):
        return list(thr)
    mode = int(e.get("mode") or 0)
    return sorted({*thr, mode}, reverse=True) if mode > 0 else list(thr)


def entry(mode: int, days: int | None, by: str, now: datetime) -> dict:
    return {"mode": mode, "by": by, "at": now.isoformat(), "days": days}


def sync(row) -> bool:
    """Снять отложки, срок которых отодвинулся (продлили). "Не следить" остается. True - что-то
    сняли."""
    v = getattr(row, "expiry_snooze", None)
    if not isinstance(v, dict) or not v:
        return False
    cur = dict(v)
    for kind in KINDS:
        e = cur.get(kind)
        if isinstance(e, dict) and int(e.get("mode") or 0) > 0 and renewed(e, _days(row, kind)):
            cur.pop(kind)
    if cur == v:
        return False
    row.expiry_snooze = cur or None
    return True


def label(mode: int) -> str:
    return "не следить" if mode == 0 else f"напомнить за {mode} дн."
