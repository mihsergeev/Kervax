"""Мигающий сайт: одна тревога и один отбой на весь эпизод.

Сайт, который то лежит, то отвечает, давал по сообщению на каждый поворот. Ночью 24.10 один
сайт за 10 минут прислал "недоступен", через 6 минут "снова отвечает" и еще через 4 минуты
"отвечает через раз", и следующее падение дало бы еще пару. Беда одна, а сообщений серия.

Теперь о сайте, про который уже сказали, повторно не пишем, пока он не придет в норму:

- тревога ("недоступен" или "через раз") открывает эпизод (Check.flap_state);
- пока эпизод открыт, новые падения и "через раз" молчат: о сайте уже знают, отбоя не было.
  Хуже бывает только из "через раз" в "лежит": тогда одно сообщение, когда падение подряд
  продержалось WORSE (и набрало свой порог неудач);
- отбой сразу, если падение было одно. Если за FLAP_WINDOW до подъема сбоев было несколько
  серий или сайт отвечает через раз, он мигает: отбой только после STABLE без сбоев и
  STABLE_N удачных проверок подряд, одним "снова отвечает стабильно";
- после отбоя "через раз" считает только новые проверки: сбои уже закрытого эпизода второй
  раз тревогу не дают.

Первое падение по-прежнему приходит сразу по порогу монитора: эпизода еще нет.

Пороги по 14 дням проверок трех панелей (10-24.10.2026, ~4 млн проверок, 544 монитора): у
мигающих сайтов следующее падение приходило через 13-18 минут после подъема, STABLE 20 минут
уже ловит все такие случаи, 45 не дает ничего сверх; взято 30 с запасом. Окно в час против
получаса ловит на два повтора больше. Всего сайтовых сообщений 169 -> 133, у мигавших
сайтов 6 -> 2 и 4 -> 2, одиночные падения и их отбои не изменились.
"""

from datetime import datetime, timedelta, timezone

FLAP_WINDOW = timedelta(hours=1)  # несколько серий сбоев за столько до подъема - мигает
STABLE = timedelta(minutes=30)    # отбой мигавшему сайту: столько без единого падения
STABLE_N = 3                      # и столько удачных проверок подряд
WORSE = timedelta(minutes=10)     # через раз -> лежит: падение подряд не короче этого

Hist = list[tuple[datetime, str]]  # (время, статус) от старых к новым


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)


def level(state: dict | None) -> str | None:
    """Открытый эпизод: "down" (сказали, что лежит) или "flaky" (что через раз); None - нет."""
    lvl = (state or {}).get("lvl")
    return lvl if lvl in ("down", "flaky") else None


def opened(lvl: str, now: datetime) -> dict:
    return {"lvl": lvl, "at": now.isoformat()}


def closed(now: datetime) -> dict:
    return {"closed": now.isoformat()}


def cutoff(state: dict | None) -> datetime | None:
    """Когда закрыли прошлый эпизод: "через раз" смотрит только на проверки после этого."""
    raw = (state or {}).get("closed")
    try:
        return _aware(datetime.fromisoformat(raw)) if raw else None
    except (TypeError, ValueError):
        return None


def runs(hist: Hist, since: datetime) -> int:
    """Сколько отдельных серий сбоев (лежит или медленно) среди проверок не раньше since."""
    n, inside = 0, False
    for ts, st in hist:
        if _aware(ts) < since:
            continue
        if st != "up":
            n += 0 if inside else 1
            inside = True
        else:
            inside = False
    return n


def flapping(hist: Hist, now: datetime) -> bool:
    """Падение было не одно: за час до подъема сбои шли несколькими сериями."""
    return runs(hist, now - FLAP_WINDOW) >= 2


def stable(hist: Hist, now: datetime) -> bool:
    """Мигавший сайт пришел в норму: последние STABLE_N проверок прошли и STABLE не падал.
    Медленный ответ - не падение: сайт отвечает, тормоза ловит свой алерт."""
    tail = [st for _, st in hist[-STABLE_N:]]
    if len(tail) < STABLE_N or any(st != "up" for st in tail):
        return False
    last = max((_aware(ts) for ts, st in hist if st == "down"), default=None)
    return last is None or now - last >= STABLE
