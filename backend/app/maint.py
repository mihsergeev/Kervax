"""Окна обслуживания: плановые работы, пока алерты по выбранным серверам и мониторам молчат.

Разовое окно - с и по. Повторяющееся - каждый день или по выбранным дням недели в то же время,
что и первое: время начала и длительность берутся из него, в часовом поясе алертов (переход на
летнее время не сдвигает "03:00-04:00").

Пока окно идет, сервер и монитор ведут себя как под снузом: алерты не шлются, состояние не
трогается, и то, что осталось сломанным после окна, придет алертом как обычно. Через
VERIFY_AFTER после конца - сверка одним сообщением: все ли вернулось в норму. Без нее окно
"на час" легко превращается в забытую поломку: работы кончились, а сервер так и не поднялся."""

from datetime import datetime, timedelta, timezone

from app.tg_buttons import zone

VERIFY_AFTER = 5 * 60
KEEP_DONE = 30 * 86400  # разовые окна, кончившиеся месяц назад, удаляем
_SPAN_DAYS = 8          # повторы ищем на неделю с запасом в обе стороны


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def occurrences(w, around: datetime, tz: str) -> list[tuple[datetime, datetime]]:
    """Вхождения окна около момента around: (начало, конец) в UTC, по возрастанию."""
    start, end = _aware(w.starts_at), _aware(w.ends_at)
    if not w.repeat:
        return [(start, end)]
    z = zone(tz)
    dur = end - start
    first = start.astimezone(z)
    days = set(w.weekdays or []) if w.repeat == "weekly" else set(range(7))
    base = around.astimezone(z).date()
    out = []
    for d in range(-_SPAN_DAYS, _SPAN_DAYS + 1):
        day = base + timedelta(days=d)
        if day < first.date() or day.weekday() not in days:
            continue
        s = datetime.combine(day, first.time(), tzinfo=z).astimezone(timezone.utc)
        out.append((s, s + dur))
    return out


def current(w, now: datetime, tz: str) -> tuple[datetime, datetime] | None:
    for s, e in occurrences(w, now, tz):
        if s <= now < e:
            return s, e
    return None


def last_ended(w, now: datetime, tz: str) -> tuple[datetime, datetime] | None:
    done = [(s, e) for s, e in occurrences(w, now, tz) if e <= now]
    return done[-1] if done else None


def next_start(w, now: datetime, tz: str) -> datetime | None:
    nxt = [s for s, _e in occurrences(w, now, tz) if s > now]
    return nxt[0] if nxt else None


def finished(w, now: datetime) -> bool:
    """Разовое окно, которое уже кончилось."""
    return not w.repeat and _aware(w.ends_at) <= now


def active(windows, now: datetime, tz: str) -> tuple[set[int], set[int]]:
    """Серверы и мониторы, у которых прямо сейчас идет обслуживание."""
    srv: set[int] = set()
    chk: set[int] = set()
    for w in windows:
        if current(w, now, tz) is not None:
            srv.update(int(x) for x in (w.server_ids or []))
            chk.update(int(x) for x in (w.check_ids or []))
    return srv, chk


def due_verify(w, now: datetime, tz: str) -> tuple[datetime, datetime] | None:
    """Вхождение, по которому пора сверку: кончилось VERIFY_AFTER назад (но не больше суток -
    после простоя панели не вываливаем сверки за всю неделю) и сверки по нему еще не было."""
    if not w.verify:
        return None
    last = last_ended(w, now, tz)
    if last is None:
        return None
    _s, e = last
    age = (now - e).total_seconds()
    if age < VERIFY_AFTER or age > 86400:
        return None
    done = w.verified_end
    if done is not None and _aware(done) >= e:
        return None
    return last


def verify_short(bad: list[str]) -> tuple[str, list[str]]:
    """Сверка после окна для чата (формат A): все вернулось или что так и не вернулось."""
    from app import tg_fmt

    if not bad:
        return "Обслуживание закончилось, все в норме", []
    return "Обслуживание закончилось, не все вернулось", [tg_fmt.names(bad, 2)]


def verify_text(title: str, bad: list[str], n_srv: int, n_chk: int) -> str:
    """Сверка после окна: все в норме или что так и не вернулось."""
    if not bad:
        what = ", ".join(x for x in (
            f"серверов {n_srv}" if n_srv else "", f"мониторов {n_chk}" if n_chk else "") if x)
        return f'после обслуживания "{title}" все в норме' + (f" ({what})" if what else "")
    return f'после обслуживания "{title}" не в норме: ' + "; ".join(bad)
