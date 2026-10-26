"""Мониторы одного сервера или одного адреса - одним сообщением.

Вечером 25.10 балансировщик перед несколькими сайтами начал вешать TLS-рукопожатие, и за
полчаса пришло пять отдельных "отвечает через раз": четыре сайта на одном адресе и сайт по
соседству. Беда одна, а сообщений пять. Свертка по домену (www и соседний поддомен в одном
тике) их не ловила: домены разные, и падали они в разные минуты.

Место монитора - сервер панели, от которого он зависит (app/parents.py), а если такого нет -
адреса, на которые смотрит его домен (тот же кеш DNS). Балансировщик или чужой хостинг в
панели не заведен, но адрес у сайтов общий, и это видно.

Как работает:
- сбой нескольких мониторов одного места в одном тике - одно сообщение "N мониторов отвечают
  через раз" со списком: адрес ссылкой на монитор и коротко, что не так;
- одиночный сбой уходит как обычно, но открывает группу места. Следующий монитор того же места
  в пределах JOIN от прошлого не шлет нового сообщения: отправленное правится (Telegram умеет
  editMessageText), монитор дописывается строкой, в историю алертов - своя строка;
- отбои членов группы молчат, пока в норму не придут все: тогда одно "снова отвечают". Если
  часть давно в норме (PARTIAL), а остальные нет - отбой поднявшимся, остальные ждут дальше;
- через раз, лежит и тормозит - разные группы: монитор, который из "через раз" упал совсем,
  о себе скажет, это хуже;
- сервер места молчит - сбои придерживаются, как и раньше (parents), и названы строкой в его
  "не на связи". Если у сервера идет своя тревога (CPU, память, диск...), это строка в
  сообщении про мониторы: беда, скорее всего, одна.

Состояние в app_settings (STATE_KEY): переживает перезапуск планировщика и выкладку.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import re
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

from app import parents as parents_mod, settings_store, tg_fmt

log = logging.getLogger("kervax.site_group")

STATE_KEY = "site_groups"
JOIN = timedelta(minutes=30)      # новый сбой места не позже стольких от прошлого - в ту же группу
PARTIAL = timedelta(hours=1)      # часть в норме столько, остальные нет - отбой поднявшимся
LIST_MAX = 8                      # строк мониторов в сообщении, остальное "и еще N"

# вид беды монитора -> группа: через раз, лежит и тормозит отдельно
LVL_TITLE = {"flaky": "отвечают через раз", "down": "не отвечают", "slow": "тормозят"}
LVL_ICON = {"flaky": "🟠🌐", "down": "🔴🌐", "slow": "⚠️🌐"}
LVL_BACK = {"flaky": "снова отвечают стабильно", "down": "снова отвечают", "slow": "снова отвечают быстро"}

# тревоги сервера, от которых сайты на нем и падают: строка "На сервере тоже: ..."
SERVER_CAUSE = {
    "cpu": "CPU", "cpu_spin": "CPU", "mem": "память", "oom": "OOM", "mem_limit": "лимит памяти",
    "pressure": "давление", "disk": "диск", "inode": "inode", "disk_health": "диски",
    "disk_slow": "медленный диск", "net_health": "сеть", "link": "связь", "conntrack": "conntrack",
    "web_5xx": "5xx", "fpm": "php-fpm", "db_conn": "база", "docker_down": "контейнер",
    "docker_loop": "контейнер", "kube_pod": "под", "kube_node": "нода кубера",
}


def lvl_of(kind: str, status: str) -> str | None:
    """Группа по виду сайтового алерта: "bad" + статус или "flaky"; остальное не группируем."""
    if kind == "flaky":
        return "flaky"
    if kind == "bad":
        return "slow" if status == "degraded" else "down"
    return None


def place(check, idx: "parents_mod.Index") -> tuple[str, str, int | None] | None:
    """Место монитора: (ключ, подпись, id сервера панели или None). None - не понять где.

    Сервер панели (parents_of: проверяется изнутри, домен в его конфиге, DNS на его адрес,
    ручной родитель) - лучше всего. Иначе все адреса домена из DNS: общий балансировщик или
    хостинг вне панели."""
    got = parents_mod.parents_of(check, idx)
    if got:
        pid = got[0][0]
        return f"srv:{pid}", idx.names.get(pid, str(pid)), pid
    host = parents_mod.target_host(check)
    if not host:
        return None
    if parents_mod._is_ip(host):  # noqa: SLF001
        ips = frozenset({host})
    else:
        ips = idx.resolved.get(host) or cached_ips(host)
    if not ips:
        return None
    ips_s = sorted(ips)
    return "ip:" + ",".join(ips_s), tg_fmt.names(ips_s, 2), None


def cached_ips(host: str) -> frozenset[str]:
    """Адреса домена из кеша parents (там они есть у мониторов, которые искали родителя)."""
    got = parents_mod._dns.get(host)  # noqa: SLF001
    return got[0] if got else frozenset()


# Подпись адреса: обратное имя (balancer.example.net) понятнее голого адреса. Кеш на сутки,
# ждем не дольше PTR_WAIT, не ответило - просто адрес
PTR_TTL = 86400
PTR_WAIT = 2.0
_ptr_cache: dict[str, tuple[str, float]] = {}


async def _ptr(ip: str) -> str:
    host, _port = await asyncio.get_running_loop().getnameinfo((ip, 0), socket.NI_NAMEREQD)
    return host


async def label_for(key: str, label: str) -> str:
    """Подпись группы: у одного адреса - "имя (адрес)", если обратное имя есть."""
    if not key.startswith("ip:") or "," in key:
        return label
    ip = key[3:]
    try:
        if not ipaddress.ip_address(ip).is_global:
            return label
    except ValueError:
        return label
    mono = time.monotonic()
    hit = _ptr_cache.get(ip)
    if hit is None or hit[1] <= mono:
        try:
            name = await asyncio.wait_for(_ptr(ip), PTR_WAIT)
        except Exception:  # noqa: BLE001 - нет имени или резолвер молчит: подпишем адресом
            name = ""
        name = (name or "").strip(".")
        hit = (name if name and name != ip else "", mono + PTR_TTL)
        _ptr_cache[ip] = hit
    return f"{hit[0]} ({ip})" if hit[0] else label


# Коротко, что не так у монитора: строка в списке группы. Причина из алерта (srv_short.site)
# бывает длинной ("соединение есть, но TLS-рукопожатие висит: 10 с без ответа")
_WHY = (
    (re.compile(r"TLS-рукопожатие висит(?:: (\d+ с))?"), lambda m: "TLS висит" + (f" {m.group(1)}" if m.group(1) else "")),
    (re.compile(r"не ответил(?: вовремя)?(?: за (\d+(?:\.\d+)? ?с))?"),
     lambda m: "не ответил за " + m.group(1) if m.group(1) else "не ответил"),
    (re.compile(r"отклонил соединение"), lambda m: "соединение отклонено"),
    (re.compile(r"оборвал"), lambda m: "обрыв соединения"),
    (re.compile(r"DNS не ответил"), lambda m: "DNS не ответил"),
    (re.compile(r"\bHTTP (\d{3})\b"), lambda m: f"HTTP {m.group(1)}"),
)
WHY_MAX = 60


def short_why(reason: str) -> str:
    text = tg_fmt.clean(reason or "").strip()
    for rx, fn in _WHY:
        m = rx.search(text)
        if m:
            return fn(m)
    return tg_fmt.field(text, WHY_MAX)


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "монитор"
    return "монитора" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "мониторов"


def verb(n: int, text: str) -> str:
    """"отвечают через раз" для одного - "отвечает через раз"."""
    if n % 10 == 1 and n % 100 != 11:
        return (text.replace("отвечают", "отвечает").replace("тормозят", "тормозит"))
    return text


def head_html(g: dict, base: str) -> str:
    label = g.get("label") or g.get("key", "")
    srv = g.get("srv")
    if srv and base:
        return tg_fmt.link(label, f"{base}/?server={srv}")
    return html.escape(tg_fmt.field(label))


NO_WHY = "без подробностей"


def by_why(mem: list[dict]) -> list[tuple[str, list[dict]]]:
    """Мониторы по причине, в порядке появления. Мониторы без причины - последними: у всех
    одна беда, а пишется она один раз (26.10 пять строк подряд "сервер не принимает соединение")."""
    out: dict[str, list[dict]] = {}
    for m in mem:
        out.setdefault(m.get("why") or "", []).append(m)
    return sorted(out.items(), key=lambda kv: not kv[0])


def alert_text(g: dict, base: str, cause: list[str] | None = None) -> tuple[str, str, str]:
    """Сообщение группы: (текст в чат, полный для истории, одна строка для пачки).

    Причина одна у всех - строкой под заголовком, ниже список. Разные - причина, под ней ее
    мониторы."""
    mem = list((g.get("members") or {}).values())
    n = len(mem)
    lvl = g.get("lvl", "flaky")
    what = f"{n} {_plural(n)} {verb(n, LVL_TITLE.get(lvl, 'с ошибками'))}"
    head = head_html(g, base)
    lines = [f"{LVL_ICON.get(lvl, '🟠🌐')} {head}", f"<b>{html.escape(what)}</b>"]
    parts = by_why(mem)
    shown = 0
    for why, ms in parts:
        if shown >= LIST_MAX:
            break
        if why or len(parts) > 1:
            lines.append(html.escape(tg_fmt.cap(why or NO_WHY)))
        for m in ms[:LIST_MAX - shown]:
            lines.append(tg_fmt.link(m.get("host") or m.get("name", ""), m.get("url", "")))
        shown += min(len(ms), LIST_MAX - shown)
    if n > shown:
        lines.append(f"и еще {n - shown}")
    if cause:
        lines.append(html.escape(tg_fmt.fact("На сервере тоже", tg_fmt.names(cause))))
    text = "\n".join(lines)
    full = text.replace(head, html.escape(g.get("label") or "")) if not g.get("srv") else text
    line = f"{LVL_ICON.get(lvl, '🟠🌐')} {head}: {html.escape(what)}"
    return text, text, line


def _left_rows(left: list[dict]) -> list[str]:
    """Кто еще не в норме: "Еще нет: a, b - причина", при разных причинах - строка на причину."""
    rows = [tg_fmt.names(m.get("host") or m.get("name", "") for m in ms) + (f" - {why}" if why else "")
            for why, ms in by_why(left)]
    if len(rows) == 1:
        return [tg_fmt.fact("Еще нет", rows[0])]
    return ["Еще нет:"] + [tg_fmt.field(r, tg_fmt.FACT_LEN) for r in rows]


def back_text(g: dict, base: str, names: list[str], left: list[dict]) -> str:
    """Отбой группы одной строкой; если ждем еще кого-то - строка "Еще нет: ..." с причиной."""
    n = len(names)
    what = f"{verb(n, LVL_BACK.get(g.get('lvl', 'flaky'), 'снова отвечают'))}"
    if left:
        what += " " + tg_fmt.names(names)
        text = tg_fmt.recovery(head_html(g, base), what)
        return "\n".join([text] + [html.escape(r) for r in _left_rows(left)])
    text = tg_fmt.recovery(head_html(g, base), f"{what} ({n} {_plural(n)})" if n > 1 else what)
    return text + "\n" + html.escape(tg_fmt.names(names))


def new_group(key: str, lvl: str, label: str, srv: int | None, now: datetime) -> dict:
    return {"id": uuid.uuid4().hex[:12], "key": key, "lvl": lvl, "label": label, "srv": srv,
            "at": now.isoformat(), "last": now.isoformat(), "members": {}, "ok": {}, "msgs": [],
            "first": "", "kind": "", "grp": "", "solo": True}


def ts(raw) -> datetime | None:
    try:
        t = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def open_for(groups: dict, key: str, lvl: str, now: datetime) -> dict | None:
    """Группа, в которую можно дописать новый сбой места: прошлый сбой не раньше JOIN."""
    best = None
    for g in groups.values():
        if g.get("key") != key or g.get("lvl") != lvl:
            continue
        last = ts(g.get("last"))
        if last is None or now - last > JOIN:
            continue
        if best is None or (ts(best.get("last")) or now) < last:
            best = g
    return best


def member_of(groups: dict, cid: int) -> list[dict]:
    """Группы, объявленные сообщением про несколько мониторов, где этот монитор ждет отбоя."""
    return [g for g in groups.values() if not g.get("solo") and str(cid) in (g.get("members") or {})]


def ok_since(g: dict, cid: str, ok: bool, now: datetime) -> datetime | None:
    """Отметить, с какого момента член группы в норме; None - не в норме."""
    oks = g.setdefault("ok", {})
    if not ok:
        oks.pop(cid, None)
        return None
    oks.setdefault(cid, now.isoformat())
    return ts(oks[cid]) or now


def stale(g: dict, now: datetime) -> bool:
    """Группа больше не нужна: пустая, или в ней одно сообщение одного монитора (его отбой
    обычный), а дописать в нее уже нельзя."""
    last = ts(g.get("last")) or now
    if not g.get("members"):
        return True
    return bool(g.get("solo")) and now - last > JOIN


async def load(session) -> dict:
    raw = await settings_store.get_raw(session, STATE_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


async def save(session, groups: dict) -> None:
    await settings_store.set_raw(session, STATE_KEY, json.dumps(groups, ensure_ascii=False) if groups else "")


async def any_open(session_factory) -> bool:
    """Есть ли группа, которая ждет отбоя: тогда цикл алертов смотрит на нее и без новых событий."""
    try:
        async with session_factory() as session:
            return bool(await load(session))
    except Exception:  # noqa: BLE001
        return False
