"""Почему nginx отвечал 502/504: до какого upstream он не достучался и как (helper
webserver-setup 0.26). Helper берет строки error-лога nginx про upstream и отдает по каждому
логу "up": [{"u": адрес, "k": вид, "n": в минуту, "c": имя}], где имя - контейнер docker по его
адресу или upstream ingress-nginx. Алерт по 5xx без этого говорил только, сколько ошибок и по
каким путям, а за причиной надо было идти в error-лог на сервер.

С helper 0.28 там же вид noep: ingress-nginx ответил 503 сам, потому что у сервиса нет ни одного
живого пода, - с "s" (namespace/сервис), "i" (Ingress) и "h" (хосты) из его nginx.conf. У нод со
старым helper тот же вид панель находит по строкам с 5xx (from_lines)."""

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
    # ingress-nginx ответил 503 сам, не спросив под (helper 0.28); текст - в item_text
    "noep": "без живых подов",
    "limit": "срезан лимитом запросов (limit_req)",
}

_ADDR = re.compile(r"^[A-Za-z0-9._:/@\[\]-]{1,120}$")
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_IP_PORT = re.compile(r"^\d+\.\d+\.\d+\.\d+(:\d+)?$")
_SVC = re.compile(r"^[A-Za-z0-9._-]{1,80}/[A-Za-z0-9._-]{1,80}$")
_HOST = re.compile(r"^[A-Za-z0-9.*_-]{1,120}$")
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
        _svc_fields(x, it)
        out.append(x)
    return out[:UP_MAX] or None


def _svc_fields(x: dict, it: dict) -> None:
    """Сервис за upstream ingress-nginx (helper 0.28): "s" - namespace/сервис, "i" - Ingress,
    "h" - до трех хостов через пробел."""
    sv, ing = str(it.get("s") or ""), str(it.get("i") or "")
    if _SVC.match(sv):
        x["s"] = sv
    if _NAME.match(ing):
        x["i"] = ing
    hosts = [h for h in str(it.get("h") or "").split() if _HOST.match(h)][:3]
    if hosts:
        x["h"] = " ".join(hosts)


# Строка ingress-nginx (формат по умолчанию), на которую он ответил сам: 503, а за
# [ns-svc-port] [альтернативный] вместо адреса пода "-"
_NOEP_LINE = re.compile(r'" 503 \d+ .*?\[([A-Za-z0-9_.-]{1,120})\] \[[^\]]*\] - ')


def from_lines(lines, codes, e5: int, namespaces=()) -> list[dict] | None:
    """Вид noep по сохраненным строкам с 5xx - для нод, где helper старше 0.28. Только если вся
    минута такая: все строки - 503 без upstream и других кодов нет. Строк до пяти, поэтому
    минуту, где есть и другие ошибки, не угадываем - она считается как раньше."""
    lines = [str(x) for x in lines or [] if x]
    if not lines or not e5 or set((codes or {}).keys()) - {"503"}:
        return None
    names = [m.group(1) if (m := _NOEP_LINE.search(x)) else None for x in lines]
    if None in names:
        return None
    out = []
    for name in dict.fromkeys(names):
        x = {"u": name, "k": "noep", "n": max(1, round(e5 * names.count(name) / len(names)))}
        sv = svc_of(name, namespaces)
        if sv:
            x["s"] = sv
        out.append(x)
    return out


def svc_of(name: str, namespaces) -> str:
    """Upstream ingress-nginx "ns-сервис-порт" -> "ns/сервис" по namespace'ам кластера (из отчета
    агента). Дефисы есть и в именах, поэтому без списка namespace'ов не делим."""
    ns = max((n for n in namespaces or () if n and name.startswith(n + "-")), key=len, default="")
    if not ns:
        return ""
    rest = name[len(ns) + 1:].rsplit("-", 1)[0]
    return f"{ns}/{rest}" if rest and _SVC.match(f"{ns}/{rest}") else ""


def noep_services(items, e5) -> list[str]:
    """Upstream'ы без живых подов, если ими объясняется минута: не меньше 80% ее 5xx - ответы
    ingress-nginx без upstream. Пусто - минута обычная."""
    got = [it for it in items if isinstance(it, dict) and it.get("k") == "noep"] if isinstance(items, list) else []
    n = sum(int(it.get("n") or 0) for it in got)
    if not got or not e5 or n < 0.8 * int(e5):
        return []
    return list(dict.fromkeys(str(it.get("u") or "") for it in got if it.get("u")))


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
        for f in ("s", "i", "h"):
            if it.get(f):
                a[f] = str(it[f])


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


def svc_text(it: dict) -> str:
    """"shop/shop-back (ingress shop, api.example.com)": сервис без живых подов и где он виден.
    Без helper 0.28 - имя upstream ingress-nginx, как в его логе."""
    where = ([f"ingress {it['i']}"] if it.get("i") else []) + str(it.get("h") or "").split()
    return str(it.get("s") or it.get("u") or "?") + (f" ({', '.join(where)})" if where else "")


def item_text(it: dict) -> str:
    if it.get("k") == "noep":
        return f"нет живых подов у сервиса {svc_text(it)}"
    if it.get("k") == "limit":
        return f"{svc_text(it)} {KINDS['limit']}"
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
