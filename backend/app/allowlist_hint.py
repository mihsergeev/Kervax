"""Похоже ли, что сайт закрыт белым списком, - для карточки на главной.

Раньше хватало "лежит снаружи, а домен держит наша нода": под это попадал любой сбой
любого сайта парка, в том числе сервер, который принял запрос и завис, или 502. Карточка
советовала проверять изнутри сайт, который просто лежал, и висела, пока он не встанет.

Белый список виден так: снаружи соединение режут (таймаут или отказ на TCP, сброс) или
прокси отвечает 403, со всех точек проверки, и так часами. А сайт при этом жив: изнутри
его ноды открывается, или снаружи он не открывался ни разу. Сайт, который обычно
доступен, и сбой, при котором изнутри тоже не открылось, - не белый список, а авария.

Ничего не хранится: подсказка считается заново на каждый запрос главной, поэтому сайт
уходит из карточки сам, как только условия перестают выполняться.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Сбой должен держаться не меньше этого: короткий инцидент - авария, а не правило файрвола
STEADY = timedelta(hours=2)
# Аптайм снаружи выше этого и за 7, и за 30 дней - сайт обычно доступен, сбой временный
USUAL_UP = 90.0
# Ответ изнутри берем, только если он получен в этом же сбое (с запасом на задержку агента)
INSIDE_SLACK = timedelta(minutes=10)

# Снаружи не подключиться: сюда смотрит файрвол. "Принял запрос и молчит" сюда не входит:
# соединение есть, значит пускают
_CONNECT = (
    "connecttimeout", "не ответил на попытку соединения", "не принимает соединение",
    "all connection attempts failed", "не удалось подключиться ни по одному адресу",
    "connection refused", "отклонил соединение",
)
_RESET = ("connection reset", "оборвал соединение")


@dataclass
class Hint:
    reason: str   # inside_up - изнутри открывается; never_up - снаружи не открывался ни разу; code - 403
    outside: str  # connect | reset | code: чем кончается проверка снаружи
    since: datetime
    code: int = 0


def outside_kind(message: str, phases: dict | None, closed_code: int) -> str:
    """Чем режут снаружи: connect, reset, code или "" (не похоже на белый список)."""
    if closed_code:
        return "code"
    fail = (phases or {}).get("fail") if isinstance(phases, dict) else None
    # таймаут на TLS, ожидании ответа или посреди тела: соединение пустили - это не файрвол
    if fail in ("tls", "wait", "body", "dns"):
        return ""
    low = (message or "").lower()
    if fail == "connect" or any(x in low for x in _CONNECT):
        return "connect"
    if any(x in low for x in _RESET):
        return "reset"
    return ""


def _ts(v) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if isinstance(v, str) and v else None
    except ValueError:
        return None


def inside_status(last_inside, since: datetime) -> str:
    """up / down / "" - что ответил сайт изнутри своей ноды в этом сбое (пусто - не знаем)."""
    li = last_inside if isinstance(last_inside, dict) else {}
    ts = _ts(li.get("ts"))
    st = li.get("status") or ""
    if ts is None or not st or ts < since - INSIDE_SLACK:
        return ""
    return "up" if st in ("up", "degraded") else "down"


def judge(
    *, outside: str, since: datetime | None, now: datetime, locations: list[str],
    inside: str, ever_up: bool, up7: float | None, up30: float | None, code: int = 0,
) -> Hint | None:
    """Вердикт по собранным признакам. locations - статусы монитора из точек проверки."""
    if not outside or since is None or now - since < STEADY:
        return None
    # хоть одна точка видит сайт - снаружи он открывается, белого списка "для всех" нет
    if any(st in ("up", "degraded") for st in locations):
        return None
    # изнутри тоже не открылось - сайт лежит сам. Кроме 403: прокси, что закрывает сайт,
    # часто не пускает и изнутри, а ответ "не пущу" - это не авария
    if inside == "down" and outside != "code":
        return None
    # обычно доступный снаружи сайт: сбой временный, как бы долго он ни тянулся в пределах
    # недели
    if up7 is not None and up30 is not None and up7 >= USUAL_UP and up30 >= USUAL_UP:
        return None
    if inside == "up":
        return Hint("inside_up", outside, since, code)
    if not ever_up:
        return Hint("code" if outside == "code" else "never_up", outside, since, code)
    return None
