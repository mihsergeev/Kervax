"""Штатная остановка сервера: перезагрузка не алертит.

Повод: железный сервер перезагрузили руками, POST и загрузка заняли чуть больше пяти минут
выдержки "недоступен" - пришло "не на связи", через минуту "снова на связи". Человек сам
перезагружал, реагировать не на что.

Агент 2.47+ по SIGTERM от systemd (перезагрузка, выключение) успевает отправить панели
POST /api/agent/going-down с видом остановки, и отметка ложится в отчет (KEY). Пока она свежая,
"недоступен" не поднимается до WAIT: сервер грузится. Вернулся - алерта нет, в ленте сервера
плановая перезагрузка с тем, сколько он был без связи (os_support.planned_reboot). Не вернулся
за WAIT - алерт, что не вернулся после перезагрузки.

Остановку одного агента (systemctl stop) панель тоже узнает, но не ждет: сервер жив, а агент
молчит - про это алерт в обычный срок, с пометкой, что агента остановили. Старые агенты и жесткий
reset (kernel panic, питание) ничего не присылают - для них все как раньше.
"""
from __future__ import annotations

from datetime import datetime, timezone

KEY = "going_down"
KINDS = ("reboot", "poweroff", "shutdown", "stop")
# Сколько ждать сервер после штатной остановки: железу с долгим POST и проверкой памяти нужно
# минут пять-десять, ВМ - минута
WAIT = 900
# Отчет, отправленный до остановки, может прийти позже отметки: столько она переживает обычный
# отчет без перезагрузки
RACE = 30


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def make(kind: str, now: datetime) -> dict:
    return {"at": now.isoformat(), "kind": kind if kind in KINDS else "shutdown"}


def notice(rep: dict | None) -> dict | None:
    """Отметка штатной остановки из отчета: {"at": datetime, "kind": str} или None."""
    v = (rep or {}).get(KEY)
    if not isinstance(v, dict) or v.get("kind") not in KINDS:
        return None
    try:
        at = _aware(datetime.fromisoformat(str(v.get("at") or "")))
    except ValueError:
        return None
    return {"at": at, "kind": v["kind"]}


def system(n: dict | None) -> bool:
    """Останавливалась система целиком (перезагрузка, выключение), а не один агент."""
    return bool(n) and n["kind"] != "stop"


def holds(rep: dict | None, now: datetime) -> bool:
    """Сервер ушел на перезагрузку недавно: "недоступен" пока не поднимаем."""
    n = notice(rep)
    return system(n) and (now - n["at"]).total_seconds() < WAIT


def carry(prev: dict | None, now: datetime, rebooted: bool):
    """Отметку переносим в следующий отчет, только если этот отчет ушел до остановки и пришел
    после нее (гонка в пару секунд). Перезагрузился или отчитывается дальше - отметка своё
    отработала."""
    raw = (prev or {}).get(KEY)
    n = notice(prev)
    if rebooted or n is None or (now - n["at"]).total_seconds() >= RACE:
        return None
    return raw


def _dur(sec: float) -> str:
    m = int(sec // 60)
    if m < 1:
        return "меньше минуты"
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин" if m else f"{h} ч"


def boot_why(prev: dict | None, base: str, last_seen: datetime | None, now: datetime) -> str:
    """Причина плановой перезагрузки для ленты, если агент о ней предупредил: что ждали (base,
    os_support.reboot_why) и сколько сервер был без связи. "" - не предупреждал."""
    n = notice(prev)
    if not system(n):
        return base
    head = base or ("выключали штатно" if n["kind"] == "poweroff" else "штатная")
    since = _aware(last_seen) if last_seen is not None else n["at"]
    return f"{head}, без связи {_dur((now - since).total_seconds())}"


def short(n: dict | None) -> str:
    """Заголовок алерта "недоступен" в чате, если агент предупредил об остановке."""
    if not n:
        return ""
    return {"reboot": "Не вернулся после перезагрузки",
            "poweroff": "Выключен штатно и не включился",
            "shutdown": "Не вернулся после остановки системы",
            "stop": "Агент остановлен, данных нет"}[n["kind"]]


def note(n: dict | None, now: datetime) -> str:
    """Строка в полный текст алерта: что сказал агент перед уходом."""
    if not n:
        return ""
    ago = _dur((now - n["at"]).total_seconds())
    if n["kind"] == "stop":
        return f"агента остановили штатно {ago} назад (systemctl stop), сервер мог остаться жив"
    what = {"reboot": "перезагрузку", "poweroff": "выключение", "shutdown": "остановку системы"}[n["kind"]]
    return f"агент предупредил про {what} {ago} назад, с тех пор сервер не вернулся"


def view(rep: dict | None, online: bool, now: datetime) -> dict | None:
    """Для карточки и "Что сломано": сервер молчит после штатной остановки. wait - еще ждем."""
    n = notice(rep)
    if online or n is None:
        return None
    return {"kind": n["kind"], "at": n["at"].isoformat(),
            "wait": system(n) and (now - n["at"]).total_seconds() < WAIT}
