"""GRE до панели: туннель с ноды до хоста панели для агентов, чье соединение с панелью режут.

На некоторых каналах (DPI у провайдера ноды) TCP-соединение до панели замерзает, отдав ~16 КБ в
любую сторону: отчет сжат и проходит, а обновление агента идет кусками по 8 КБ по полчаса, большой
ответ команды не доходит. Голый GRE до хоста панели такой DPI не трогает: внутри него то же
TLS-соединение до панели, только адрес назначения внутренний.

Как устроено:
  * галочка "GRE до панели" в настройках сервера - панель выделяет серверу свою /30 из пула
    (gre_pool, по умолчанию 10.254.0.0/16): первый адрес - сторона хоста панели, второй - ноды.
    Выделенная сеть остается за сервером и после выключения: включили снова - те же адреса;
  * панель пишет data/gre_tunnels - что держать хосту панели; ops/gre-sync.sh вне docker
    поднимает GRE-интерфейсы и правила фаервола и отвечает в data/gre_tunnels.applied, что вышло
    и с какого своего адреса (local) он ждет GRE от ноды;
  * helper gre-setup на ноде раз в минуту спрашивает панель (POST /api/agent/gre, токеном агента),
    поднимает свою сторону, проверяет, что панель через туннель отвечает, и пишет агенту
    connect=<адрес панели в туннеле> - агент (2.56) ходит по нему с SNI на имя панели, а не
    отвечает туннель - по-прежнему напрямую;
  * отчет через туннель приходит с внутреннего адреса: панель подставляет настоящий адрес ноды
    (gre_peer), иначе в карточке, стране и проверках снаружи был бы 10.254.x.x.

Подсказка "этому серверу нужен GRE" - по замерам агента (net_probe): мелкие куски с панели
проходят, а 100 КБ одним соединением раз за разом замерзают.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import relay as relay_mod
from app.config import get_settings
from app.models import Server

log = logging.getLogger(__name__)

HELPER = "gre-setup"
MTU = 1476
# интерфейс на хосте панели: свой на каждый сервер, по id (имя интерфейса - до 15 символов)
IFACE_PREFIX = "kvxg"
TUNNELS = "gre_tunnels"
HEADER = "# kervax-gre v1"
# helper спрашивает панель раз в минуту: дольше этого молчит - не работает или не достает
STALE = timedelta(minutes=5)
# отчет агента через туннель: свежее этого - агент ходит туннелем
AGENT_FRESH = timedelta(minutes=5)
# хостовый скрипт отвечает при каждом прогоне (path-юнит и крон раз в 2 минуты)
HOST_STALE = 15 * 60

# Подсказка по замерам агента: из последних замеров большой кусок (100 КБ одним соединением)
# не прошел почти ни разу, а мелкий проходит. Один-два сбоя - не повод: бывает и просто плохой час
NEED_MIN_BIG = 4
NEED_MIN_SMALL = 3


def pool() -> ipaddress.IPv4Network:
    try:
        net = ipaddress.ip_network(get_settings().gre_pool, strict=False)
        if isinstance(net, ipaddress.IPv4Network) and net.prefixlen <= 30:
            return net
    except ValueError:
        pass
    return ipaddress.IPv4Network("10.254.0.0/16")


def iface_of(server: Server) -> str:
    return f"{IFACE_PREFIX}{server.id}"


def addrs(net: str) -> tuple[str, str]:
    """(адрес хоста панели, адрес ноды) в /30 сервера; ('', '') - сети нет или она битая."""
    try:
        n = ipaddress.IPv4Network(net or "", strict=True)
    except ValueError:
        return "", ""
    if n.prefixlen != 30:
        return "", ""
    hosts = list(n.hosts())
    return str(hosts[0]), str(hosts[1])


async def allocate(session: AsyncSession, server: Server) -> str:
    """/30 для сервера: уже выделенная остается, новая - первая свободная в пуле. Пересечений нет:
    сравниваются сети всех серверов, и на gre_net уникальный индекс (две вкладки сразу не
    получат одну сеть, вторая упадет на коммите)."""
    if addrs(server.gre_net or "")[0]:
        return server.gre_net
    taken = []
    for v in await session.scalars(select(Server.gre_net).where(Server.gre_net.is_not(None))):
        with contextlib.suppress(ValueError):
            taken.append(ipaddress.IPv4Network(v, strict=False))
    for net in pool().subnets(new_prefix=30):
        if not any(net.overlaps(t) for t in taken):
            server.gre_net = str(net)
            return server.gre_net
    raise ValueError("в пуле адресов GRE не осталось свободных /30")


# Не годятся концом туннеля: частные сети и CGNAT (за ними NAT, а GRE его не проходит) и служебные.
# Документационные 192.0.2/24, 198.51.100/24, 203.0.113/24 сюда не входят: NAT за ними не бывает
_NOT_PUBLIC = tuple(ipaddress.IPv4Network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/3"))


def is_public(ip: str) -> bool:
    """Публичный IPv4: GRE здесь только поверх IPv4, и не из сетей за NAT."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and not any(a in n for n in _NOT_PUBLIC)


def peer_of(server: Server) -> str:
    """Публичный адрес ноды - другой конец туннеля: тот, что назвал helper (с него нода шлет
    GRE), иначе тот, с которого приходил агент, или заданный руками."""
    p = (server.gre_peer or "").strip()
    if is_public(p):
        return p
    a = relay_mod.address_of(server)
    return a if is_public(a) else ""


def in_pool(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in pool()
    except ValueError:
        return False


def real_ip(server: Server, ext: str, now: datetime) -> str:
    """Адрес ноды для карточки по адресу запроса. С туннельного адреса этого сервера - настоящий
    публичный адрес ноды (и отметка, что агент ходит туннелем); другой адрес из пула - '': чужой
    туннельный адрес не адрес ноды, прежний не трогаем."""
    if not ext or not in_pool(ext):
        return ext
    if server.gre_net and ext == addrs(server.gre_net)[1]:
        server.gre_agent_at = now
        return peer_of(server)
    return ""


def enable_problem(server: Server) -> str:
    """Почему GRE этому серверу не включить, '' - можно."""
    if (server.last_report or {}).get("proxy"):
        return "Агент этого сервера ходит в панель через прокси: GRE до панели ему не поможет"
    if not peer_of(server):
        return "Не известен публичный адрес сервера: укажите IP сервера в настройках"
    return ""


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


_host_cache: dict[str, tuple[tuple[int, int], dict]] = {}


def host_state(data_dir: str | None = None) -> dict:
    """Что ответил хостовый ops/gre-sync.sh: {fw, ts, ifaces: {iface: {peer, local, ok, error}}}.
    Нет файла - {} (скрипт на хосте панели не стоит или еще не запускался)."""
    path = os.path.join(data_dir or get_settings().data_dir, TUNNELS + ".applied")
    try:
        st = os.stat(path)
        hit = _host_cache.get(path)
        # список серверов зовет это на каждый сервер: файл перечитываем, только когда он сменился
        if hit and hit[0] == (st.st_mtime_ns, st.st_size):
            return hit[1]
        with open(path, encoding="utf-8") as f:
            lines = f.read(256 * 1024).splitlines()
    except OSError:
        return {}
    out: dict = {"fw": "", "ts": 0, "ifaces": {}}
    for ln in lines:
        if ln.startswith("#"):
            for tok in ln[1:].split():
                k, _, v = tok.partition("=")
                if k == "fw":
                    out["fw"] = v
                elif k == "ts":
                    with contextlib.suppress(ValueError):
                        out["ts"] = int(v)
            continue
        f = ln.split(None, 4)
        if len(f) >= 4 and f[0].startswith(IFACE_PREFIX):
            out["ifaces"][f[0]] = {"peer": f[1], "local": f[2], "ok": f[3] == "ok",
                                   "error": f[4][:200] if len(f) > 4 else ""}
    _host_cache[path] = ((st.st_mtime_ns, st.st_size), out)
    return out


def panel_ip(server: Server, host: dict | None = None) -> str:
    """С какого адреса хост панели ждет GRE от этой ноды: задан руками (gre_host_ip) или тот,
    что хостовый скрипт поставил интерфейсу этого сервера local."""
    fixed = get_settings().gre_host_ip.strip()
    if is_public(fixed):
        return fixed
    i = ((host if host is not None else host_state()).get("ifaces") or {}).get(iface_of(server)) or {}
    loc = str(i.get("local") or "")
    return loc if i.get("ok") and is_public(loc) else ""


async def write_tunnels(session: AsyncSession) -> None:
    """data/gre_tunnels: строка на сервер с включенным GRE - интерфейс, адрес ноды, адрес хоста
    панели в туннеле /30, адрес ноды в туннеле. Первая строка - заголовок: по нему хостовый
    скрипт отличает список (пусть и пустой) от чужого или недописанного файла. Пишется через
    свой временный файл, как agent_allow_ips."""
    lines = [HEADER]
    for s in await session.scalars(select(Server).where(Server.gre_enabled.is_(True)).order_by(Server.id)):
        mine, node = addrs(s.gre_net or "")
        peer = peer_of(s)
        if mine and peer:
            lines.append(f"{iface_of(s)} {peer} {mine}/30 {node}")
    data_dir = get_settings().data_dir
    tmp = ""
    try:
        os.makedirs(data_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=TUNNELS + ".")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, os.path.join(data_dir, TUNNELS))
    except OSError:
        log.warning("не удалось записать %s", TUNNELS, exc_info=True)
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _count(v) -> int:
    try:
        return max(0, min(int(v or 0), 1000))
    except (TypeError, ValueError):
        return 0


def need(rep: dict) -> dict | None:
    """Режут ли соединение до панели по объему (замеры агента 2.56, net_probe): мелкое проходит,
    большое одним соединением замерзает. None - не режут или замеров мало."""
    p = rep.get("net_probe")
    if not isinstance(p, dict):
        return None
    big_n, big_ok = _count(p.get("big_n")), _count(p.get("big_ok"))
    small_n, small_ok = _count(p.get("small_n")), _count(p.get("small_ok"))
    if big_n < NEED_MIN_BIG or small_ok < NEED_MIN_SMALL:
        return None
    # большое прошло не больше чем в четверти попыток, мелкое - хотя бы в двух третях
    if big_ok * 4 > big_n or small_ok * 3 < small_n * 2:
        return None
    return {"big_ok": big_ok, "big_n": big_n, "small_ok": small_ok, "small_n": small_n,
            "kb": _count(p.get("big_kb")) or 100}


def view(server: Server, now: datetime, host: dict | None = None) -> dict | None:
    """Состояние GRE для карточки. Коды, а не фразы: подписи делает интерфейс.

    state: no_helper - helper еще не поставлен (он в "Требует действий"); waiting - helper стоит,
    но панель еще не спрашивал; host - хост панели свою сторону еще не поднял (ops/gre-sync.sh не
    стоит или не запускался); nat - у ноды нет публичного адреса на интерфейсе; error - helper
    сообщил ошибку; stale - helper давно молчит; down - туннель не поднят или панель через него не
    отвечает; up - туннель поднят, ждем агента; ok - агент ходит туннелем."""
    if not server.gre_enabled:
        return None
    st = server.gre_state or {}
    installed = ((server.last_report or {}).get("setup_versions") or {}).get(HELPER)
    seen, agent_at = _aware(server.gre_seen_at), _aware(server.gre_agent_at)
    host = host if host is not None else host_state()
    hi = (host.get("ifaces") or {}).get(iface_of(server)) or {}
    host_ok = bool(hi.get("ok")) and time.time() - (host.get("ts") or 0) < HOST_STALE
    mine, node = addrs(server.gre_net or "")
    if seen is None:
        state = "waiting" if installed else "no_helper"
    elif now - seen > STALE:
        state = "stale"
    elif st.get("nat"):
        state = "nat"
    elif not host_ok and not get_settings().gre_host_ip.strip():
        state = "host"
    elif st.get("error"):
        state = "error"
    elif agent_at is not None and now - agent_at < AGENT_FRESH:
        state = "ok"
    else:
        state = "up" if st.get("up") else "down"
    return {
        "state": state,
        "net": server.gre_net or "",
        "panel_addr": mine,
        "node_addr": node,
        "peer": peer_of(server),
        "local_ip": str(st.get("local_ip") or ""),
        "firewall": str(st.get("firewall") or ""),
        "error": str(st.get("error") or hi.get("error") or "")[:300],
        "up": bool(st.get("up")),
        "host": host_ok,
        "host_fw": str(host.get("fw") or ""),
        "seen_at": seen.isoformat() if seen else None,
        "agent_at": agent_at.isoformat() if agent_at else None,
        "helper": str(installed) if installed else "",
    }


def helper_status(body: dict) -> dict:
    """Что helper сообщил о себе - только известные поля и в разумных размерах."""
    fw = str(body.get("firewall") or "")
    ip = str(body.get("local_ip") or "")[:64]
    try:
        ip = ipaddress.ip_address(ip).compressed
    except ValueError:
        ip = ""
    return {
        "version": str(body.get("version") or "")[:16],
        "up": bool(body.get("up")),
        "nat": bool(body.get("nat")),
        "local_ip": ip,
        "firewall": fw if fw in ("ufw", "firewalld", "none") else "",
        "error": str(body.get("error") or "")[:300],
    }
