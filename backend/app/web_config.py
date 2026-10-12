"""Конфиг веб-сервера не проходит проверку (helper webserver-setup 0.23).

Раз в 15 минут helper гоняет nginx -t на хосте и в nginx-контейнерах, apachectl -t, caddy
validate для Caddyfile и haproxy -c. Сейчас все работает на старом конфиге, но следующий reload,
перезапуск контейнера или перезагрузка ноды уронит сайты. С ноды уходит только первая строка
ошибки (файл и строка, на которые она указывает), сам конфиг - нет. Блок extras["web-config"].
"""
from __future__ import annotations

from datetime import datetime

STALE = 3600  # блок старше часа - helper не работает, не судим по старому
_NAMES = {"nginx": "nginx", "apache": "Apache", "caddy": "Caddy", "haproxy": "HAProxy"}


def block(rep: dict, now: datetime) -> list[dict] | None:
    b = ((rep or {}).get("extras") or {}).get("web-config")
    if not isinstance(b, dict) or b.get("v") != 1 or not isinstance(b.get("items"), list):
        return None
    try:
        if now.timestamp() - float(b.get("ts") or 0) > STALE:
            return None
    except (TypeError, ValueError):
        return None
    return [x for x in b["items"] if isinstance(x, dict)]


def _where(x: dict) -> str:
    w = str(x.get("where") or "")
    return "на хосте" if w in ("", "host") else f"в контейнере {w}"


def problems(rep: dict, now: datetime) -> list[tuple[int, str, str]]:
    """(уровень, текст, ключ). Предупреждение: сейчас сайты работают, упадут при следующем reload."""
    out = []
    for x in block(rep, now) or []:
        if x.get("ok") is not False:
            continue
        name = _NAMES.get(str(x.get("server") or ""), str(x.get("server") or "веб-сервер"))
        err = str(x.get("err") or "").strip()
        out.append((1, f"конфиг {name} {_where(x)} не проходит проверку" + (f": {err}" if err else "")
                       + ". Следующий reload или перезапуск уронит сайты", f"{x.get('server')}:{x.get('where')}"))
    return out


def view(rep: dict, now: datetime) -> list[dict] | None:
    """Для "Сервисов": что проверено и что не прошло"""
    items = block(rep, now)
    if items is None:
        return None
    return [{"server": str(x.get("server") or ""), "where": str(x.get("where") or ""), "ok": x.get("ok") is not False,
             "err": str(x.get("err") or "")} for x in items]
