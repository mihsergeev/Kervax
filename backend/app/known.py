"""Известные проблемы: человек отметил, что про беду знают ("диск под замену, тикет у хостера").

Чем это не мьют. Мьют молчит про вид алерта навсегда или до срока, и следующая такая же беда
через месяц тоже пройдет молча. Известная проблема привязана к тому, что сломано сейчас:
- алерт молчит, пока не стало хуже: уровень выше отмеченного или сломалось новое (второй диск
  при уже известном первом) - алерт уходит как обычно;
- на главной она не в "Что сломано", а отдельным серым блоком с текстом и автором;
- проблема ушла сама и не вернулась за GONE_S - отметка закрывается, и следующая такая же беда
  снова придет алертом;
- вышел срок, а проблема осталась - отметка снимается, и алерт уходит как обычно;
- раз в REMIND_S напоминание, что она все еще висит: известное легко забыть навсегда.

Запись: {text, by, at, until|None} пишет человек; level, sig (снимок того, что было сломано,
когда отметили), worse, gone_at, reminded_at ведет сборщик."""

from datetime import datetime, timedelta

GONE_S = 10 * 60
REMIND_S = 7 * 86400


def _ts(v) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def expired(entry: dict, now: datetime) -> bool:
    until = _ts(entry.get("until"))
    return until is not None and until <= now


def covers(entry: dict | None, level: int, sig=None) -> bool:
    """Глушит ли отметка алерт этого уровня. Уровень 0 (отбой) не глушит никогда: если алерт
    успел уйти до отметки, отбой должен прийти."""
    if not entry or level <= 0:
        return False
    known_level = entry.get("level")
    if known_level is None:
        return True  # отметили между тиками, снимок еще не снят: молчим
    if level > int(known_level):
        return False
    known_sig = entry.get("sig")
    if sig and known_sig is not None and not set(map(str, sig)) <= set(map(str, known_sig)):
        return False
    return True


def step(entry: dict, level: int, sig, now: datetime) -> tuple[dict | None, str]:
    """Один тик для одной отметки сервера: (новая запись или None - снять, что случилось).
    Что случилось: "" | "closed" (ушла сама) | "expired" (вышел срок) | "remind"."""
    if expired(entry, now):
        return None, "expired"
    new = dict(entry)
    if level > 0:
        new.pop("gone_at", None)
        if new.get("level") is None:
            new["level"] = int(level)
            new["sig"] = sorted(map(str, sig)) if sig else None
        new["worse"] = not covers(new, level, sig)
    else:
        gone = _ts(new.get("gone_at"))
        if gone is None:
            new["gone_at"] = now.isoformat()
        elif (now - gone).total_seconds() >= GONE_S:
            return None, "closed"
        new["worse"] = False
    return new, _remind(new, level, now)


def step_check(entry: dict, ok: bool, now: datetime) -> tuple[dict | None, str]:
    """То же для монитора: проблема - он не "работает" (лежит, тормозит, через раз)."""
    if expired(entry, now):
        return None, "expired"
    new = dict(entry)
    if not ok:
        new.pop("gone_at", None)
    else:
        gone = _ts(new.get("gone_at"))
        if gone is None:
            new["gone_at"] = now.isoformat()
        elif (now - gone).total_seconds() >= GONE_S:
            return None, "closed"
    return new, _remind(new, 0 if ok else 1, now)


def _remind(new: dict, level: int, now: datetime) -> str:
    if level <= 0:
        return ""
    last = _ts(new.get("reminded_at")) or _ts(new.get("at"))
    if last is None or (now - last).total_seconds() < REMIND_S:
        return ""
    new["reminded_at"] = now.isoformat()
    return "remind"


def age_text(entry: dict, now: datetime) -> str:
    at = _ts(entry.get("at"))
    if at is None:
        return ""
    days = max(int((now - at).total_seconds() // 86400), 1)
    return f"{days} дн"


def remind_text(what: str, entry: dict, now: datetime) -> str:
    """Тело напоминания: что висит, сколько, что про это написали и кто."""
    out = f"известная проблема висит {age_text(entry, now)}: {what}"
    if entry.get("text"):
        out += f" - {entry['text']}"
    if entry.get("by"):
        out += f" ({entry['by']})"
    return out


def new_entry(text: str, by: str, days: int, now: datetime) -> dict:
    return {
        "text": text.strip(),
        "by": by,
        "at": now.isoformat(),
        "until": (now + timedelta(days=days)).isoformat() if days > 0 else None,
    }
