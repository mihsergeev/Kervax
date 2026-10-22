"""Зависимости между объектами: от какого сервера зависит монитор или другой сервер.

Сервер замолчал - все, что на нем держится, лежит вместе с ним, и десяток алертов про его
сайты ничего не добавляет к одному "сервер недоступен". Пока родитель молчит, алерты детей
придерживаются: не теряются, а ждут. Если ребенок все еще лежит, когда родитель вернулся,
алерт придет; поднялся вместе с родителем - тишина, про него и не писали. Сами дети
перечисляются строкой в сообщении про родителя.

Родителя монитора панель находит сама только по однозначным признакам, иначе легко
заглушить настоящую аварию из-за постороннего сервера:
- монитор проверяется изнутри сервера (probe_local): без агента его просто некому проверить;
- домен монитора смотрит в DNS только на адреса одного сервера: туда и идут запросы, даже
  если сайт с тем же именем остался в конфиге другого сервера, а веб-сервер этого агент не
  разбирает (панели хостинга);
- домен монитора обслуживает ровно один сервер (web_services из отчетов агентов);
- цель монитора - внешний IP ровно одного сервера.
Домен на нескольких серверах (фронт и бэкенд, два балансируемых узла) сам не связываем.
Домен за адресом не из панели (Cloudflare, чужой балансировщик) - только по web_services.
Остальное человек задает руками: parent_mode="server" и parent_server_id, или "off" -
монитор ни от чего не зависит. У сервера родитель только ручной (parent_id): шлюз, VPN,
гипервизор, за которым он стоит.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlparse

from sqlalchemy import select

# откуда взялся родитель - для подписи в карточке монитора
WHY_LOCAL = "local"    # проверяется изнутри этого сервера
WHY_SITE = "site"      # домен обслуживает этот сервер
WHY_IP = "ip"          # цель - адрес этого сервера
WHY_MANUAL = "manual"  # задан руками

# сколько детей перечислять в сообщении про родителя, остальное - "и еще N"
LIST_MAX = 5

# Сервер опаздывает с отчетом (агент шлет раз в 15 с): сайт, упавший в эту минуту, скорее
# всего лежит вместе с ним. Его алерт придерживаем, пока не станет ясно, замолчал ли сервер:
# иначе сайты уходили отдельными сообщениями раньше "не на связи" про сам сервер
LATE_S = 60

# адреса доменов мониторов: ответ живет полчаса, неудачу повторяем через пять минут, ждем
# резолвер не дольше трех секунд - на тике сборщика и в запросе карточки
DNS_TTL = 1800
DNS_RETRY = 300
DNS_WAIT = 3.0
_dns: dict[str, tuple[frozenset[str], float]] = {}


def target_host(check) -> str:
    """Хост, который монитор реально проверяет. У push адреса нет, а DNS-монитор спрашивает
    резолвер про записи домена - от веб-сервера сайта он не зависит."""
    target = (getattr(check, "target", "") or "").strip().lower()
    typ = getattr(check, "type", "") or "http"
    if not target or typ in ("push", "dns"):
        return ""
    if typ == "http":
        if "://" not in target:
            target = "http://" + target
        try:
            return (urlparse(target).hostname or "").strip(".")
        except ValueError:
            return ""
    # tcp_port / cert: "host" или "host:port"; голый IPv6 оставляем целиком
    t = target.split("://")[-1].split("/")[0]
    if t.startswith("["):
        return t[1:].split("]")[0]
    if t.count(":") > 1:
        return t
    return t.split(":")[0].strip(".")


def norm_domain(raw: str) -> str:
    d = (raw or "").strip().lower().rstrip(".")
    if "://" in d:
        d = d.split("://", 1)[1]
    d = d.split("/")[0]
    return d.split(":", 1)[0]


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class Index:
    """Серверы панели, разложенные для поиска родителей: имена, кто обслуживает какой домен,
    чей это адрес. Строится один раз на тик или на запрос."""

    def __init__(self, servers) -> None:
        self.names: dict[int, str] = {}
        self.domains: dict[str, set[int]] = {}
        self.ips: dict[str, set[int]] = {}
        self.parent: dict[int, int] = {}
        # адреса доменов мониторов из DNS (resolve_for); пусто - родителя ищем без них
        self.resolved: dict[str, frozenset[str]] = {}
        for s in servers:
            self.names[s.id] = s.name
            if getattr(s, "parent_id", None):
                self.parent[s.id] = s.parent_id
            for web in (getattr(s, "last_report", None) or {}).get("web_services") or []:
                for raw in web.get("sites") or []:
                    d = norm_domain(raw)
                    if d and "*" not in d and not d.startswith("~"):
                        self.domains.setdefault(d, set()).add(s.id)
            # внутренний адрес (local_ip) не берем: 10.0.0.5 бывает у серверов в разных сетях
            for ip in {(getattr(s, "external_ip", "") or "").strip(), (getattr(s, "agent_ip", "") or "").strip()}:
                if ip and _is_ip(ip):
                    self.ips.setdefault(ip, set()).add(s.id)


def parents_of(check, idx: Index) -> list[tuple[int, str]]:
    """Родители монитора: [(id сервера, откуда взялся)]. Пусто - ни от чего не зависит."""
    mode = getattr(check, "parent_mode", None) or "auto"
    if mode == "off":
        return []
    if mode == "server":
        pid = getattr(check, "parent_server_id", None)
        return [(pid, WHY_MANUAL)] if pid in idx.names else []
    pid = getattr(check, "probe_server_id", None)
    if getattr(check, "probe_local", False) and pid in idx.names:
        return [(pid, WHY_LOCAL)]
    host = target_host(check)
    if not host:
        return []
    if _is_ip(host):
        ids, why = idx.ips.get(host), WHY_IP
    elif (owner := _dns_owner(host, idx)) and idx.domains.get(host) != {owner}:
        # DNS ведет не туда, где сайт в конфиге, или конфига агент не видит: решает адрес
        ids, why = {owner}, WHY_IP
    else:
        ids, why = idx.domains.get(host), WHY_SITE
    if ids and len(ids) == 1:
        return [(next(iter(ids)), why)]
    return []


def _dns_owner(host: str, idx: Index) -> int | None:
    """Сервер, на адреса которого смотрит домен. Все адреса домена должны быть его: второй
    адрес у постороннего узла - уже балансировка, сам сервер сайт целиком не роняет."""
    ips = idx.resolved.get(host)
    if not ips:
        return None
    owners: set[int] = set()
    for ip in ips:
        ids = idx.ips.get(ip)
        if not ids or len(ids) != 1:
            return None
        owners |= ids
    return next(iter(owners)) if len(owners) == 1 else None


def _dns_host(check, idx: Index) -> str:
    """Домен монитора, адрес которого нужен для поиска родителя, или ""."""
    if (getattr(check, "parent_mode", None) or "auto") != "auto":
        return ""
    if getattr(check, "probe_local", False) and getattr(check, "probe_server_id", None) in idx.names:
        return ""
    host = target_host(check)
    return "" if not host or _is_ip(host) else host


async def _lookup(host: str) -> frozenset[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    out = set()
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        # внутренние адреса не сравниваем: 10.0.0.5 бывает у серверов в разных сетях
        if ip.is_global:
            out.add(str(ip))
    return frozenset(out)


async def resolve_for(idx: Index, checks) -> Index:
    """Подставляет в индекс адреса доменов мониторов. Из кеша сразу; устаревшие спрашиваем
    разом, ждем не дольше DNS_WAIT. Не ответил резолвер - остается прошлый адрес: сбой DNS у
    панели не повод отвязать сайты от их серверов."""
    hosts = {h for c in checks if (h := _dns_host(c, idx))}
    mono = time.monotonic()
    stale = [h for h in hosts if h not in _dns or _dns[h][1] <= mono]
    if stale:
        tasks = {asyncio.ensure_future(_lookup(h)): h for h in stale}
        done, pending = await asyncio.wait(tasks, timeout=DNS_WAIT)
        for t in pending:
            t.cancel()
        for t, h in tasks.items():
            got = t.result() if t in done and not t.cancelled() and t.exception() is None else None
            if got:
                _dns[h] = (got, mono + DNS_TTL)
            else:
                _dns[h] = (_dns[h][0] if h in _dns else frozenset(), mono + DNS_RETRY)
    for h in hosts:
        if h in _dns and _dns[h][0]:
            idx.resolved[h] = _dns[h][0]
    return idx


def silent_ids(servers, now: datetime, seen_online, just_started: bool) -> set[int]:
    """Серверы, которые сейчас молчат: включены, хоть раз отчитывались и не на связи. Сразу
    после старта панели молчат все (агенты получали 502), это не повод глушить детей."""
    if just_started:
        return set()
    return {
        s.id for s in servers
        if getattr(s, "enabled", True) and getattr(s, "last_seen", None) is not None
        and not seen_online(s, now)
    }


def late_ids(servers, now: datetime, just_started: bool) -> set[int]:
    """Серверы, опоздавшие с отчетом дольше LATE_S (и молчащие): сбой их сайтов ждет, пока не
    станет ясно, лежит ли сам сервер. Порог не больше offline-порога сервера."""
    if just_started:
        return set()
    out = set()
    for s in servers:
        seen = getattr(s, "last_seen", None)
        if not getattr(s, "enabled", True) or seen is None:
            continue
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=now.tzinfo)
        lim = min(LATE_S, max(getattr(s, "offline_after_seconds", None) or 120, 30))
        if (now - seen).total_seconds() > lim:
            out.add(s.id)
    return out


def held_by(check, idx: Index, silent: set[int]) -> int | None:
    """Молчащий родитель, из-за которого алерт монитора придерживаем, или None."""
    for pid, _why in parents_of(check, idx):
        if pid in silent:
            return pid
    return None


def would_cycle(server_id: int, parent_id: int, parent_of: dict[int, int]) -> bool:
    """Родитель parent_id для server_id замкнет цепочку (A за B, B за A)?"""
    seen = {server_id}
    cur: int | None = parent_id
    while cur is not None:
        if cur in seen:
            return True
        seen.add(cur)
        cur = parent_of.get(cur)
    return False


def list_text(names: list[str], limit: int = LIST_MAX) -> str:
    shown = ", ".join(names[:limit])
    rest = len(names) - limit
    return shown + (f" и еще {rest}" if rest > 0 else "")


async def load_servers(session) -> list[SimpleNamespace]:
    """Серверы для поиска родителей и проверки "молчит ли". Из отчета берем только
    web_services, аптайм и возврат на связь JSON-оператором в запросе: отчеты целиком ради
    этого не тянем."""
    from app.models import Server

    rows = (await session.execute(select(
        Server.id, Server.name, Server.group_name, Server.parent_id, Server.external_ip,
        Server.agent_ip, Server.enabled, Server.last_seen, Server.offline_after_seconds,
        Server.last_report["web_services"], Server.last_report["uptime_seconds"], Server.last_report["back_at"],
    ))).all()
    # аптайм и возврат на связь - для прогрева после включения (app/boot_warmup.py)
    return [
        SimpleNamespace(id=i, name=n, group_name=g or "", parent_id=p, external_ip=e or "",
                        agent_ip=a or "", enabled=en, last_seen=ls, offline_after_seconds=oa or 120,
                        last_report={"web_services": w if isinstance(w, list) else [],
                                     "uptime_seconds": up if isinstance(up, (int, float)) else 0,
                                     "back_at": bk if isinstance(bk, str) else ""})
        for i, n, g, p, e, a, en, ls, oa, w, up, bk in rows
    ]
