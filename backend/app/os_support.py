"""Обновления и поддержка ОС: что сделать на ноде, а не что сломалось.

Агент 2.26 раз в час отдает os_upd: нужна ли перезагрузка и с каких пор, есть ли в /boot ядро
новее работающего, сколько обновлений и сколько из них безопасности, работают ли
автообновления, когда последний раз прошел apt update. Имя ОС (PRETTY_NAME) агент шлет давно,
по нему - срок поддержки дистрибутива.

Это не алерты: ни одно из этого не горит, но каждое со временем превращается в дыру. Поэтому
пункты идут в "Требует действий" на главной и в блок "Обновления ОС" карточки сервера.

Обновить ОС можно не всегда: старый сервер бэкапов, железо под софт, который на новой версии не
встанет. Тогда это решение человека "так и должно быть" (alert_mutes "osupd:<вид>"): пункт уходит
из "Требует действий", а в карточке виден приглушенным с кнопкой "вернуть". Не заглушенный алерт -
в "Заглушено" его нет, как и других решений.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

MUTE_PREFIX = "osupd:"

# Конец поддержки: у Ubuntu LTS - обычной (дальше обновления безопасности только по подписке
# Ubuntu Pro), у Debian - вместе с LTS (Debian LTS бесплатный), у RHEL-семейства - полной.
_EOL: list[tuple[str, str, date, bool]] = [
    # (регулярка по PRETTY_NAME, название, дата, есть ли платное продление)
    (r"^Ubuntu 14\.04", "Ubuntu 14.04", date(2019, 4, 30), True),
    (r"^Ubuntu 16\.04", "Ubuntu 16.04", date(2021, 4, 30), True),
    (r"^Ubuntu 18\.04", "Ubuntu 18.04", date(2023, 5, 31), True),
    (r"^Ubuntu 20\.04", "Ubuntu 20.04", date(2025, 5, 31), True),
    (r"^Ubuntu 22\.04", "Ubuntu 22.04", date(2027, 6, 1), True),
    (r"^Ubuntu 24\.04", "Ubuntu 24.04", date(2029, 5, 31), True),
    (r"^Ubuntu 26\.04", "Ubuntu 26.04", date(2031, 5, 31), True),
    (r"^Ubuntu 23\.10", "Ubuntu 23.10", date(2024, 7, 11), False),
    (r"^Ubuntu 24\.10", "Ubuntu 24.10", date(2025, 7, 10), False),
    (r"^Ubuntu 25\.04", "Ubuntu 25.04", date(2026, 1, 15), False),
    (r"^Ubuntu 25\.10", "Ubuntu 25.10", date(2026, 7, 9), False),
    (r"^Debian GNU/Linux 9\b", "Debian 9", date(2022, 6, 30), False),
    (r"^Debian GNU/Linux 10\b", "Debian 10", date(2024, 6, 30), False),
    (r"^Debian GNU/Linux 11\b", "Debian 11", date(2026, 8, 31), False),
    (r"^Debian GNU/Linux 12\b", "Debian 12", date(2028, 6, 30), False),
    (r"^Debian GNU/Linux 13\b", "Debian 13", date(2030, 6, 30), False),
    (r"^CentOS Linux 7\b", "CentOS 7", date(2024, 6, 30), False),
    (r"^CentOS Linux 8\b", "CentOS 8", date(2021, 12, 31), False),
    (r"^CentOS Stream 8\b", "CentOS Stream 8", date(2024, 5, 31), False),
    (r"^CentOS Stream 9\b", "CentOS Stream 9", date(2027, 5, 31), False),
    (r"^(AlmaLinux|Rocky Linux|Red Hat Enterprise Linux)[^0-9]*8\.", "EL 8", date(2029, 5, 31), False),
    (r"^(AlmaLinux|Rocky Linux|Red Hat Enterprise Linux)[^0-9]*9\.", "EL 9", date(2032, 5, 31), False),
]
_EOL_RX = [(re.compile(rx), name, d, paid) for rx, name, d, paid in _EOL]
# Ubuntu Pro (ESM): до какого числа после обычной поддержки выходят исправления безопасности
_ESM = {"Ubuntu 16.04": date(2026, 4, 30), "Ubuntu 18.04": date(2028, 4, 30), "Ubuntu 20.04": date(2030, 4, 30),
        "Ubuntu 22.04": date(2032, 4, 30), "Ubuntu 24.04": date(2034, 4, 30), "Ubuntu 26.04": date(2036, 4, 30)}
EOL_WARN_DAYS = 180  # за полгода - пора планировать переезд
REBOOT_AFTER_DAYS = 7  # перезагрузку после обновления ядра откладывают на ночь-другую, не на месяц
UNATT_STALE_DAYS = 3
APT_STALE_DAYS = 14


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _days(n: int) -> str:
    return f"{n} {_plural(n, 'день', 'дня', 'дней')}"


def eol_of(os_name: str) -> tuple[str, date, bool] | None:
    """(название, конец поддержки, есть ли платное продление) или None, если ОС не знаем."""
    for rx, name, d, paid in _EOL_RX:
        if rx.search(os_name or ""):
            if name.startswith("EL "):
                name = (os_name.split(" (")[0] or name).strip()
            return name, d, paid
    return None


def advice(rep: dict, now: datetime, mutes: list[str] | None = None) -> list[dict]:
    """Что сделать с ОС ноды: [{"kind", "level", "text", "key", "muted"}], уровень 2 - уже дыра, 1 -
    пора; muted - человек решил, что так и должно быть (ключ key в alert_mutes)."""
    out = _advice(rep, now)
    got = set(mutes or [])
    for x in out:
        x["key"] = MUTE_PREFIX + x["kind"]
        x["muted"] = x["key"] in got
    return out


def _advice(rep: dict, now: datetime) -> list[dict]:
    out: list[dict] = []
    today = now.astimezone(timezone.utc).date()
    eol = eol_of(str(rep.get("os") or ""))
    if eol:
        name, end, paid = eol
        left = (end - today).days
        when = end.strftime("%d.%m.%Y")
        esm = _ESM.get(name)
        if left < 0 and paid and esm and esm >= today:
            # что делать, если переезд невозможен: ESM в Ubuntu Pro (pro attach <токен>)
            out.append({"kind": "eol", "level": 2, "text": (
                f"обычная поддержка {name} кончилась {when}: исправления безопасности дальше только в Ubuntu Pro "
                f"(ESM до {esm.strftime('%d.%m.%Y')}, pro attach) или после переезда на новую LTS")})
        elif left < 0:
            out.append({"kind": "eol", "level": 2, "text": (
                f"поддержка {name} кончилась {when}, ESM тоже ({esm.strftime('%d.%m.%Y')}): обновлений безопасности больше нет"
                if paid and esm else f"поддержка {name} кончилась {when}: обновлений безопасности больше нет")})
        elif left <= EOL_WARN_DAYS:
            out.append({"kind": "eol", "level": 1, "text": f"поддержка {name} кончится {when}, через {_days(left)}"})
    u = rep.get("os_upd") if isinstance(rep.get("os_upd"), dict) else None
    if not u:
        return out
    ts_now = now.timestamp()

    def ago(ts) -> int | None:
        try:
            t = float(ts or 0)
        except (TypeError, ValueError):
            return None
        return int((ts_now - t) // 86400) if t > 0 else None

    if u.get("reboot"):
        d = ago(u.get("reboot_ts"))
        if d is not None and d >= REBOOT_AFTER_DAYS:
            pkgs = [str(p) for p in u.get("reboot_pkgs") or []][:4]
            out.append({"kind": "reboot", "level": 1, "text": f"нужна перезагрузка уже {_days(d)}"
                        + (f" (обновлены {', '.join(pkgs)})" if pkgs else "")})
    elif u.get("kernel_new"):
        out.append({"kind": "reboot", "level": 1,
                    "text": f"в /boot ядро {u['kernel_new']}, а работает {u.get('kernel') or '?'}: нужна перезагрузка"})
    try:
        sec = int(u.get("security") or 0)
    except (TypeError, ValueError):
        sec = 0
    unatt = str(u.get("unatt") or "")
    ud = ago(u.get("unatt_ts"))
    if sec > 0 and (unatt != "on" or ud is None or ud >= UNATT_STALE_DAYS):
        why = {"off": "автообновления выключены", "none": "unattended-upgrades не установлен"}.get(unatt)
        if why is None:
            why = f"автообновления не запускались {_days(ud)}" if ud is not None else "автообновления не запускались"
        out.append({"kind": "security", "level": 1, "text": (
            f"не установлено {sec} {_plural(sec, 'обновление', 'обновления', 'обновлений')} безопасности: {why}")})
    ad = ago(u.get("apt_ts"))
    if ad is not None and ad >= APT_STALE_DAYS:
        out.append({"kind": "apt", "level": 1,
                    "text": f"список пакетов не обновлялся {_days(ad)}: о новых обновлениях нода не знает"})
    return out
