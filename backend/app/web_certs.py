"""Сертификаты, которые отдает nginx (helper webserver-setup 0.24).

Раз в 15 минут helper собирает пути ssl_certificate из конфигов nginx на хосте и в nginx-контейнерах
и читает срок каждого сертификата, плюс смотрит, кто продлевает Let's Encrypt: таймер certbot,
строка в cron или никто. Публичные сайты и так проверяют мониторы, а сайты за белым списком и
внутренние - нет: про такой сертификат узнавали, когда браузеры уже ругались. Блок
extras["web-certs"].
"""
from __future__ import annotations

import re
from datetime import datetime

from app import tg_fmt

# Алерт за неделю и за день, как у сроков сайтов и Kubernetes. Let's Encrypt продлевает за 30 дней:
# за неделю продление уже точно не идет, а времени разобраться еще хватает. Сертификаты ближе
# месяца видны в разделе "Сертификаты" карточки и в "Сервисах", но это не алерт
WARN_DAYS = 7
CRIT_DAYS = 1
STALE = 3600  # блок старше часа - helper не работает, не судим по старому


def block(rep: dict, now: datetime) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("web-certs")
    if not isinstance(b, dict) or b.get("v") != 1 or not isinstance(b.get("certs"), list):
        return None
    try:
        if now.timestamp() - float(b.get("ts") or 0) > STALE:
            return None
    except (TypeError, ValueError):
        return None
    return b


def _label(c: dict) -> str:
    names = str(c.get("names") or "").split()
    if names:
        return ", ".join(names[:2]) + (f" и еще {len(names) - 2}" if len(names) > 2 else "")
    return str(c.get("cn") or c.get("file") or "?")


def _where(c: dict) -> str:
    w = str(c.get("where") or "")
    return "на хосте" if w in ("", "host") else f"в контейнере {w}"


def _renew_note(b: dict, c: dict) -> str:
    if "letsencrypt" not in str(c.get("file") or ""):
        return ""
    return {"none": ", продление certbot не настроено", "failed": ", certbot.service упал"}.get(str(b.get("renew") or ""), "")


def problems(rep: dict, now: datetime) -> list[tuple[int, str, str]]:
    """(уровень, текст, ключ): истек или истекает - 2 при дне и меньше, 1 при неделе."""
    b = block(rep, now)
    out: list[tuple[int, str, str]] = []
    seen = set()
    for c in (b or {}).get("certs") or []:
        if not isinstance(c, dict):
            continue
        key = f"{c.get('where')}:{c.get('file')}"
        if key in seen:
            continue
        seen.add(key)
        try:
            days = (float(c.get("end") or 0) - now.timestamp()) / 86400
        except (TypeError, ValueError):
            continue
        if days > WARN_DAYS:
            continue
        where = f"{_where(c)}: {c.get('file')}"
        if days < 0:
            n = int(-days) or 1
            text = f"сертификат {_label(c)} истек {n} дн назад ({where}), сайты на нем уже отдают ошибку"
        else:
            text = f"сертификат {_label(c)} истекает через {int(days)} дн ({where})"
        out.append((2 if days <= CRIT_DAYS else 1, text + _renew_note(b, c), key))
    out.sort(key=lambda r: -r[0])
    return out


def view(rep: dict, now: datetime) -> list[dict] | None:
    """Для "Сервисов": все сертификаты nginx со сроком в днях"""
    b = block(rep, now)
    if b is None:
        return None
    out = []
    for c in b.get("certs") or []:
        if not isinstance(c, dict):
            continue
        try:
            days = int((float(c.get("end") or 0) - now.timestamp()) // 86400)
        except (TypeError, ValueError):
            continue
        out.append({"where": str(c.get("where") or ""), "file": str(c.get("file") or ""), "label": _label(c),
                    "days": days})
    return sorted(out, key=lambda x: x["days"])


# -- короткий текст для Telegram (формат A, app/tg_fmt.py) --
# Истекли сертификаты, продление не настроено
# admin.example.com, bp.example.com, example.org

_TEXT = re.compile(r"сертификат (.+?) (?:истек (\d+) дн назад|истекает через (\d+) дн)")


def tg_short(items) -> tuple[str, list[str]] | None:
    """(что случилось, конкретика) по problems(): истекшие важнее истекающих, имена - первым доменом
    сертификата, без путей к файлам."""
    certs = []
    for p in items or []:
        if not isinstance(p, (tuple, list)) or len(p) < 2:
            continue
        text = str(p[1] or "")
        m = _TEXT.match(text)
        if not m:
            continue
        name = m.group(1).split(",")[0].split(" и еще")[0].strip()
        certs.append({"name": name, "expired": m.group(2) is not None,
                      "days": int(m.group(3)) if m.group(3) is not None else -int(m.group(2)),
                      "renew": "none" if "продление certbot не настроено" in text
                      else "failed" if "certbot.service упал" in text else ""})
    if not certs:
        return None
    gone = [c for c in certs if c["expired"]]
    main = gone or certs
    one = len(main) == 1
    if gone:
        what = "Истек сертификат" if one else "Истекли сертификаты"
    else:
        d = min(c["days"] for c in main)
        when = "сегодня" if d < 1 else f"через {tg_fmt.days(d)}"
        what = f"{'Сертификат истекает' if one else 'Сертификаты истекают'} {when}"
    renew = {c["renew"] for c in main}
    if "none" in renew:
        what += ", продление не настроено"
    elif "failed" in renew:
        what += ", certbot упал"
    facts = [tg_fmt.names(c["name"] for c in main)]
    soon = [c["name"] for c in certs if not c["expired"]] if gone else []
    if soon:
        facts.append(tg_fmt.fact("Скоро истекает", tg_fmt.names(soon)))
    return what, facts
