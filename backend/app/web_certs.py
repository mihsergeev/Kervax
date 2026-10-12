"""Сертификаты, которые отдает nginx (helper webserver-setup 0.24).

Раз в 15 минут helper собирает пути ssl_certificate из конфигов nginx на хосте и в nginx-контейнерах
и читает срок каждого сертификата, плюс смотрит, кто продлевает Let's Encrypt: таймер certbot,
строка в cron или никто. Публичные сайты и так проверяют мониторы, а сайты за белым списком и
внутренние - нет: про такой сертификат узнавали, когда браузеры уже ругались. Блок
extras["web-certs"].
"""
from __future__ import annotations

from datetime import datetime

WARN_DAYS = 14  # Let's Encrypt продлевает за 30 дней: за две недели продление уже точно не идет
CRIT_DAYS = 3
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
    """(уровень, текст, ключ): истек или истекает - 2 при трех днях и меньше, 1 при двух неделях."""
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
