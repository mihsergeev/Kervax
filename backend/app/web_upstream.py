"""Почему nginx отвечал 502/504: до какого upstream он не достучался и как (helper
webserver-setup 0.26). Helper берет строки error-лога nginx про upstream и отдает по каждому
логу "up": [{"u": адрес, "k": вид, "n": в минуту, "c": имя}], где имя - контейнер docker по его
адресу или upstream ingress-nginx. Алерт по 5xx без этого говорил только, сколько ошибок и по
каким путям, а за причиной надо было идти в error-лог на сервер."""

from __future__ import annotations

import re

# Вид отказа -> текст после имени upstream: "kervax-backend-1:8000 не принимает соединения"
KINDS: dict[str, str] = {
    "refused": "не принимает соединения",
    "busy": "не успевает принимать соединения: очередь сокета полна",
    "nofile": "не запущен: сокета нет",
    "perm": "не пускает: нет прав на сокет",
    "ctimeout": "не принял соединение вовремя",
    "timeout": "не ответил вовремя",
    "nolive": "без живых серверов",
    "unreach": "недоступен по сети",
    "closed": "закрыл соединение, не ответив",
    "reset": "сбросил соединение",
    "bighdr": "прислал заголовок больше буфера",
    "resolve": "не резолвится",
    "ssl": "не прошел TLS",
    "bad": "ответил не по протоколу",
    "other": "ошибка связи",
}

_ADDR = re.compile(r"^[A-Za-z0-9._:/@\[\]-]{1,120}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_IP_PORT = re.compile(r"^\d+\.\d+\.\d+\.\d+(:\d+)?$")
UP_MAX = 8  # столько пар "адрес + вид" храним в минуте лога


def clean(items) -> list[dict] | None:
    """"up" из отчета -> то, что храним: только понятные поля, вид из известных."""
    out = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        u = str(it.get("u") or "")
        if not _ADDR.match(u):
            continue
        try:
            n = int(it.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        x = {"u": u, "k": str(it.get("k") or "") if it.get("k") in KINDS else "other", "n": n}
        c = str(it.get("c") or "")
        if _NAME.match(c):
            x["c"] = c
        out.append(x)
    return out[:UP_MAX] or None


def add(acc: dict, items) -> None:
    """Сложить "up" минуты в накопитель {(адрес, вид): {"u","k","n","c"?}}."""
    for it in items or []:
        if not isinstance(it, dict) or not it.get("u"):
            continue
        k = (str(it["u"]), str(it.get("k") or "other"))
        a = acc.setdefault(k, {"u": k[0], "k": k[1], "n": 0})
        a["n"] += int(it.get("n") or 0)
        if it.get("c"):
            a["c"] = str(it["c"])  # имя - самое свежее: контейнер могли пересоздать


def top(acc: dict, n: int = 8) -> list[dict]:
    return sorted(acc.values(), key=lambda x: -x["n"])[:n]


def who(it: dict) -> str:
    """Имя для текста: контейнер с портом вместо адреса, если имя известно."""
    u, c = str(it.get("u") or "?"), str(it.get("c") or "")
    if not c:
        return u
    if _IP_PORT.match(u):
        return c + (u[u.index(":"):] if ":" in u else "")
    return f"{c} ({u})"


def item_text(it: dict) -> str:
    return f"{who(it)} {KINDS.get(str(it.get('k')), KINDS['other'])}"


def text(acc: dict, n: int = 2, counts: bool = True) -> str:
    """"kervax-backend-1:8000 не принимает соединения - 44, unix:/run/php/php8.3-fpm.sock не
    ответил вовремя - 3". Пусто - helper про причину ничего не сказал."""
    return ", ".join(item_text(x) + (f" - {x['n']}" if counts else "") for x in top(acc, n))


def block_acc(extras: dict | None, muted=frozenset(), label_key=None) -> dict:
    """Накопитель по последнему блоку web-rate (для "Что сломано"): заглушенные логи не в счет."""
    acc: dict = {}
    block = (extras or {}).get("web-rate") if isinstance(extras, dict) else None
    for x in (block or {}).get("logs") or [] if isinstance(block, dict) else []:
        if not isinstance(x, dict) or not x.get("up"):
            continue
        if label_key is not None and label_key(x) in muted:
            continue
        add(acc, clean(x.get("up")))
    return acc
