"""Что открыто в интернет: внешняя проверка портов нод.

Агент 2.26 сообщает слушающие порты (адрес привязки, владелец сокета), публичные адреса ноды,
опубликованные порты контейнеров Docker и NodePort-сервисы. Панель раз в 6 часов стучится к
этим портам снаружи и запоминает, что реально открыто, а что закрыто файрволом.

Откуда стучаться: с адреса самой панели часто разрешено больше, чем остальному интернету
(вайтлисты под мониторинг), и порт, закрытый для мира, выглядел бы открытым. Поэтому проверка
идет через первую включенную локацию-прокси (socks5 или HTTP CONNECT), а без нее - с панели,
и в результате записано, откуда смотрели. По проверке с самой панели не алертим: на одной ноде
она показала открытыми etcd, kubelet и node_exporter, а из интернета они были закрыты -
адрес панели стоял в белом списке файрвола.

Кто слушает порт: агент работает без root и видит только владельца сокета, имя процесса
присылает helper changes-setup с 0.2 (ss -ltnp).

Опасным считается сервис, которому не место в интернете: базы, очереди, Docker API, etcd,
kubelet, node_exporter. Служебные порты (Kubernetes и сети подов, агенты и экспортеры метрик)
не дыра сами по себе, но снаружи им делать нечего: их тоже надо закрыть, алерт - предупреждение.
Открытый SSH, веб и свои сервисы - норма, их просто видно в списке.

Порт сервиса LoadBalancer kube-proxy заворачивает в под правилом DNAT, сокета под ним нет, и в
listen его не видно: шлюз Envoy с сайтами на 80 и 443 в проверку не попадал. Теперь он
проверяется на адресах ноды - на наших кластерах адрес балансировщика и есть адрес ноды (MetalLB).

Через Cloudflare: если домены сайтов ноды смотрят в адреса Cloudflare, а 80 или 443 напрямую
закрыт, порт открыт только для Cloudflare (белый список его адресов в файрволе или
NetworkPolicy). Открыт напрямую - Cloudflare можно обойти, это видно отметкой в строке. Если
веб-сервер вдобавок верит CF-Connecting-IP или X-Forwarded-For от любого адреса, белые списки
обходятся поддельным заголовком: это красный признак (app/cf_realip.py).

Белый список с адресом проверки: прокси, через который смотрим, бывает и адресом мониторов, и его
добавляют в белые списки файрвола нод. Тогда порт, закрытый для интернета, проверке открыт: на
сервере бэкапов так пришел "в интернет открылся новый порт" про SSH, до которого снаружи не
достучаться. helper changes-setup 0.7 присылает, кого файрвол пускает и на какие порты (отдельно на
саму ноду и в контейнеры), панель запоминает адреса своей проверки (via_ips). Порт, открытый
проверке только по белому списку с ее адресом, не считается открытым в интернет: ни нового
порта, ни алерта, в таблице - "только белому списку".
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import socket
import struct
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import tg_fmt
from app.models import Location, Server

log = logging.getLogger("kervax.open_ports")

# порт -> (что это обычно, опасно ли держать его открытым в интернет)
KNOWN: dict[int, tuple[str, bool]] = {
    21: ("FTP", True), 22: ("SSH", False), 23: ("Telnet", True), 25: ("SMTP", False),
    53: ("DNS", False), 80: ("HTTP", False), 111: ("rpcbind", True), 135: ("MS RPC", True),
    139: ("SMB", True), 443: ("HTTPS", False), 445: ("SMB", True), 465: ("SMTP", False),
    587: ("SMTP", False), 873: ("rsync", True), 993: ("IMAP", False), 1433: ("MS SQL", True),
    1521: ("Oracle", True), 2049: ("NFS", True), 2181: ("ZooKeeper", True),
    2375: ("Docker API", True), 2376: ("Docker API", True), 2379: ("etcd", True), 2380: ("etcd", True),
    3306: ("MySQL", True), 3389: ("RDP", True), 4369: ("Erlang epmd", True),
    5432: ("PostgreSQL", True), 5601: ("Kibana", True),
    5672: ("RabbitMQ", True), 5900: ("VNC", True), 5984: ("CouchDB", True), 6379: ("Redis", True),
    6443: ("Kubernetes API", False), 8086: ("InfluxDB", True), 8123: ("ClickHouse HTTP", True),
    # служебные порты Kubernetes (SERVICE_PORTS): не дыра, но снаружи им делать нечего
    179: ("BGP сети подов (Calico, kube-router)", False), 8132: ("konnectivity (Kubernetes)", False),
    9443: ("k0s API или вебхук Kubernetes", False), 10249: ("kube-proxy, метрики", False),
    10256: ("kube-proxy, проверка здоровья", False), 10257: ("kube-controller-manager", False),
    10259: ("kube-scheduler", False), 20244: ("kube-router, проверка здоровья", False),
    8200: ("Vault", False), 8500: ("Consul", True), 9000: ("ClickHouse, MinIO или php-fpm", True),
    9042: ("Cassandra", True), 9090: ("Prometheus", True), 9092: ("Kafka", True),
    9093: ("Alertmanager", True), 9100: ("node_exporter", True), 9200: ("Elasticsearch", True),
    9300: ("Elasticsearch", True), 10250: ("kubelet", True), 10255: ("kubelet", True),
    11211: ("Memcached", True), 15672: ("RabbitMQ UI", True), 26257: ("CockroachDB", True),
    27017: ("MongoDB", True), 27018: ("MongoDB", True), 27019: ("MongoDB", True),
}
# Служебные: порты Kubernetes и сетей подов, агенты мониторинга. Снаружи должны быть закрыты
SERVICE_PORTS = {179, 4240, 4244, 5473, 6443, 8132, 9091, 9099, 9443, 10050, 10051, 10249, 10256, 10257,
                 10259, 20244}
SERVICE_PROCS = {"kube-apiserver", "kube-proxy", "kube-router", "konnectivity-server", "k0s", "k3s",
                 "kube-controller-manager", "kube-scheduler", "calico-node", "calico-typha", "cilium-agent",
                 "flanneld", "containerd", "zabbix_agent2", "zabbix_agentd", "zabbix_server"}
# владелец сокета или образ контейнера -> сервис, которому не место в интернете
# (целым словом: postgrest - веб-API, а не сама база)
_RISKY_NAMES = {
    "postgres": "PostgreSQL", "postgresql": "PostgreSQL", "postgis": "PostgreSQL",
    "timescaledb": "PostgreSQL", "mysql": "MySQL", "mariadb": "MySQL", "percona": "MySQL",
    "redis": "Redis", "valkey": "Redis", "mongo": "MongoDB", "mongodb": "MongoDB",
    "clickhouse": "ClickHouse", "elasticsearch": "Elasticsearch", "opensearch": "OpenSearch",
    "memcache": "Memcached", "memcached": "Memcached", "rabbitmq": "RabbitMQ", "influxdb": "InfluxDB",
    "etcd": "etcd", "zookeeper": "ZooKeeper", "kafka": "Kafka", "cassandra": "Cassandra",
    "couchdb": "CouchDB",
}
_RISKY_RE = re.compile(r"(?<![a-z])(" + "|".join(sorted(_RISKY_NAMES, key=len, reverse=True)) + r")(?![a-z])")
SCAN_EVERY = timedelta(hours=6)
FRESH = timedelta(days=2)  # старше - не судим: проверка давно не шла
_SEEN_KEEP = timedelta(days=30)  # порт, открытый когда-то за этот срок, - не новый
_MAX_PORTS = 64
_MAX_IPS = 4
_TIMEOUT = 4.0
_PARALLEL = 16
MUTE_PREFIX = "exposed:"
# Версия правил выбора портов. Новые правила (2 - порты LoadBalancer) добавляют порты, которые и
# раньше были открыты: первая проверка по ним - точка отсчета, а не "новый порт" на половине парка
RULES = 2

# Адреса Cloudflare (www.cloudflare.com/ips) и два, что он с 10.2026 отдает российским резолверам
_CF_NETS = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "8.6.112.0/24", "8.47.69.0/24",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32")]
CF_PORTS = {80, 443}
_CF_LOOK = 24  # доменов ноды за проверку: понять, идут ли сайты через Cloudflare, хватает и части
_DNS_TIMEOUT = 3.0


# не публичные: частные сети, CGNAT и меш (100.64/10), loopback, link-local, мультикаст. Как у
# агента (IsGlobalUnicast и не IsPrivate), а не is_global: тот считает частными и сети из
# документации, которых на нодах не бывает
_NOT_PUBLIC = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "224.0.0.0/3", "fc00::/7")]
_V6_GLOBAL = ipaddress.ip_network("2000::/3")


def is_public(ip: str) -> bool:
    """Адрес из интернета: не частный, не loopback, не CGNAT, не служебный."""
    return _public(ip)


def _public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a not in _V6_GLOBAL:
        return False
    return not any(a.version == n.version and a in n for n in _NOT_PUBLIC)


def targets(srv: Server) -> tuple[list[str], dict[int, str]]:
    """Публичные адреса ноды и порты для проверки: {порт: что за ним}. TCP, что слушает на всех
    адресах или на публичном, плюс опубликованные порты контейнеров и NodePort. У облачной ВМ
    за NAT публичного адреса на интерфейсах нет, и снаружи виден её основной внутренний адрес
    (local_ip): привязанное к нему тоже проверяем."""
    rep = srv.last_report or {}
    own = [ip for ip in rep.get("ips") or [] if isinstance(ip, str) and _public(ip)]
    ips: list[str] = []
    for ip in [*own, getattr(srv, "external_ip", None) or ""]:
        if isinstance(ip, str) and _public(ip) and ip not in ips:
            ips.append(ip)
    behind_nat = str(getattr(srv, "local_ip", "") or "") if not own else ""
    ports: dict[int, str] = {}
    # кто слушает порт (helper changes-setup 0.2): {"2221": "sshd"}
    procs = ((rep.get("extras") or {}).get("changes") or {}).get("listen")
    procs = procs if isinstance(procs, dict) else {}

    def put(port, what: str) -> None:
        try:
            p = int(port)
        except (TypeError, ValueError):
            return
        if 0 < p < 65536 and (p not in ports or (what and not ports[p])):
            ports[p] = what

    for c in ((rep.get("docker") or {}).get("containers") or []):
        if not isinstance(c, dict):
            continue
        for p in c.get("ports") or []:
            if isinstance(p, dict) and (p.get("proto") or "tcp") == "tcp" and (p.get("ip") or "") in ("", "0.0.0.0", "::"):
                put(p.get("pub"), f"контейнер {c.get('name') or '?'} ({c.get('image') or '?'})")
    for n in ((rep.get("kube") or {}).get("nodeports") or []):
        if isinstance(n, dict) and (n.get("proto") or "TCP").upper() == "TCP":
            put(n.get("node_port"), f"сервис Kubernetes {n.get('ns', '?')}/{n.get('name', '?')} ({n.get('type') or 'NodePort'})")
    for s in rep.get("listen") or []:
        if not isinstance(s, dict) or not str(s.get("p") or "").startswith("tcp"):
            continue
        bind = str(s.get("ip") or "")
        if bind in ("0.0.0.0", "::") or _public(bind) or (behind_nat and bind == behind_nat):
            put(s.get("port"), _proc_text(str(procs.get(str(s.get("port"))) or ""), str(s.get("u") or "")))
    # порт LoadBalancer на адресе ноды: сокета нет, kube-proxy заворачивает его в под (DNAT).
    # После listen: если порт все же слушает процесс (ingress-nginx в сети хоста), подпись - его
    for n in ((rep.get("kube") or {}).get("nodeports") or []):
        if isinstance(n, dict) and n.get("type") == "LoadBalancer" and (n.get("proto") or "TCP").upper() == "TCP":
            put(n.get("port"), f"сервис Kubernetes {n.get('ns', '?')}/{n.get('name', '?')} (LoadBalancer)")
    keep = sorted(ports)[:_MAX_PORTS]
    return ips[:_MAX_IPS], {p: ports[p] for p in keep}


# ss отдает имя процесса не длиннее 15 символов (comm): konnectivity-server -> konnectivity-se
_COMM_FULL = {"konnectivity-se": "konnectivity-server", "kube-controller": "kube-controller-manager"}


def _proc_text(proc: str, user: str) -> str:
    """'процесс kube-proxy', 'процесс mysqld, пользователь mysql'; без helper'а - только владелец.
    Пользователя оставляем: по нему classify узнает базу (mysqld -> пользователь mysql)."""
    proc = _COMM_FULL.get(proc, proc)
    if proc:
        return f"процесс {proc}" + (f", пользователь {user}" if user and user != proc else "")
    return f"процесс пользователя {user}" if user else ""


def classify(port: int, what: str) -> tuple[str, bool]:
    """(название сервиса, опасно ли в интернете) по порту, контейнеру или владельцу сокета.
    Веб и SSH на своих портах - норма, кто бы за ними ни стоял (postgrest на 443 - сайт)."""
    known = KNOWN.get(port)
    if known and not known[1]:
        return known
    low = what.lower()
    m = _RISKY_RE.search(low)
    if m and "exporter" not in low:
        return _RISKY_NAMES[m.group(1)], True
    if known:
        return known
    p = _PROC_RE.match(what)
    if p and _lb_health(port, p.group(1)):
        return "проверка здоровья балансировщика (kube-proxy)", False
    return _PROC_NAMES.get(p.group(1), ("", False)) if p else ("", False)


def _lb_health(port: int, proc: str) -> bool:
    """kube-proxy на порту из диапазона NodePort - healthCheckNodePort сервиса LoadBalancer:
    по нему внешний балансировщик узнает, есть ли на ноде живые поды. Часть сервиса, а не
    управления кластером: служебным не считаем"""
    return proc == "kube-proxy" and 30000 <= port <= 32767


def is_service(port: int, what: str) -> bool:
    """Служебный порт: Kubernetes, сеть подов, агент или экспортер метрик (kube-router на 8080)"""
    if port in SERVICE_PORTS:
        return True
    p = _PROC_RE.match(what)
    if p and _lb_health(port, p.group(1)):
        return False
    return bool(p and p.group(1) in SERVICE_PROCS) or "exporter" in what.lower()


def label(port: int, what: str) -> dict:
    """Подпись строки проверки: название, опасно ли (risk) и служебный ли порт (svc)"""
    name, risk = classify(port, what)
    svc = not risk and is_service(port, what)
    if svc and not name and "exporter" in what.lower():
        name = "экспортер метрик"
    return {"name": name, "risk": risk, "svc": svc}


# имя процесса (helper changes-setup 0.2) -> сервис: SSH на 2221 без него был бы безымянным
_PROC_NAMES: dict[str, tuple[str, bool]] = {
    "sshd": ("SSH", False), "dropbear": ("SSH", False), "nginx": ("nginx", False),
    "caddy": ("Caddy", False), "haproxy": ("HAProxy", False), "traefik": ("Traefik", False),
    "envoy": ("Envoy", False), "apache2": ("Apache", False), "httpd": ("Apache", False),
    "kube-apiserver": ("Kubernetes API", False), "kubelet": ("kubelet", True),
    "kube-proxy": ("kube-proxy", False), "kube-router": ("kube-router", False),
    "konnectivity-server": ("konnectivity (Kubernetes)", False), "k0s": ("k0s", False),
    "k3s": ("k3s", False), "dockerd": ("Docker API", True), "node_exporter": ("node_exporter", True),
    "zabbix_agent2": ("Zabbix agent", False), "zabbix_agentd": ("Zabbix agent", False),
}
_PROC_RE = re.compile(r"процесс (?!пользователя )([^,\s]+)")


# ---- проверка порта: напрямую, через socks5 или HTTP CONNECT ----

async def _direct(ip: str, port: int) -> str:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), _TIMEOUT)
    except ConnectionRefusedError:
        return "closed"
    except asyncio.TimeoutError:
        return "filtered"
    except OSError:
        return "unknown"  # нет маршрута (у панели нет IPv6) и подобное: не знаем
    w.close()
    return "open"


async def _via_proxy(proxy: str, ip: str, port: int) -> str:
    u = urlsplit(proxy)
    host, pport = u.hostname or "", u.port or (1080 if u.scheme.startswith("socks") else 3128)
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, pport), _TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        return "unknown"
    try:
        if u.scheme.startswith("socks5"):
            return await asyncio.wait_for(_socks5(r, w, u, ip, port), _TIMEOUT * 2)
        return await asyncio.wait_for(_http_connect(r, w, u, ip, port), _TIMEOUT * 2)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        return "filtered"
    finally:
        w.close()


async def _socks5(r, w, u, ip: str, port: int) -> str:
    auth = bool(u.username)
    w.write(b"\x05\x01" + (b"\x02" if auth else b"\x00"))
    await w.drain()
    ver, method = await r.readexactly(2)
    if method == 0x02 and auth:
        user, pw = unquote(u.username or "").encode(), unquote(u.password or "").encode()
        w.write(b"\x01" + bytes([len(user)]) + user + bytes([len(pw)]) + pw)
        await w.drain()
        if (await r.readexactly(2))[1] != 0:
            return "unknown"
    elif method != 0x00:
        return "unknown"
    a = ipaddress.ip_address(ip)
    addr = (b"\x01" + a.packed) if a.version == 4 else (b"\x04" + a.packed)
    w.write(b"\x05\x01\x00" + addr + struct.pack(">H", port))
    await w.drain()
    rep = await r.readexactly(2)
    # 0 - соединился; 5 - отказ (порт закрыт); 3, 4, 6 - не дошли (файрвол или нет маршрута)
    return {0: "open", 5: "closed", 3: "filtered", 4: "filtered", 6: "filtered"}.get(rep[1], "unknown")


async def _http_connect(r, w, u, ip: str, port: int) -> str:
    target = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if u.username:
        cred = f"{unquote(u.username)}:{unquote(u.password or '')}".encode()
        head += f"Proxy-Authorization: Basic {base64.b64encode(cred).decode()}\r\n"
    w.write((head + "\r\n").encode())
    await w.drain()
    line = (await r.readline()).decode("latin-1", "replace").split()
    code = int(line[1]) if len(line) > 1 and line[1].isdigit() else 0
    if code == 200:
        return "open"
    # 502, 503, 504: прокси не достучался; 403, 405, 407: прокси такие порты не пускает
    return "closed" if code in (502, 503) else "filtered" if code == 504 else "unknown"


async def probe(ip: str, port: int, proxy: str | None) -> str:
    """open | closed | filtered | unknown. С прокси - только через него: прямой откат показал бы
    открытым порт, открытый лишь для панели (вайтлист под мониторинг или нода самой панели)."""
    if proxy:
        return await _via_proxy(proxy, ip, port)
    return await _direct(ip, port)


def port_location(locs) -> Location | None:
    """Через какую локацию смотреть порты нод снаружи: включенная локация-прокси, сначала
    заведенная только для проверки портов (monitors выключено), потом socks5 (HTTP-прокси часто
    пускают CONNECT только на 443). None - такой нет, порты смотрит сама панель."""
    found: list[tuple[int, int, int, Location]] = []
    for loc in locs:
        scheme = urlsplit((loc.url or "").strip()).scheme
        if loc.enabled and scheme in ("socks5", "socks5h", "http", "https"):
            found.append((0 if loc.monitors is False else 1, 0 if scheme.startswith("socks") else 1, loc.id, loc))
    return min(found, key=lambda x: x[:3])[3] if found else None


async def pick_proxy(session: AsyncSession) -> tuple[str | None, str]:
    """Прокси для проверки портов (port_location): (url, имя); без него - (None, "панель")."""
    loc = port_location(await session.scalars(select(Location).order_by(Location.id)))
    if loc is None:
        return None, "панель"
    return loc.url.strip(), loc.name


# ---- баннер SSH снаружи: та ли нода отвечает по внешнему адресу ----

async def _tunnel(ip: str, port: int, proxy: str | None):
    """(reader, writer) соединения с ip:port напрямую или через прокси, ответ прокси прочитан
    целиком: дальше в потоке только данные самого сервиса. None - не вышло."""
    if not proxy:
        return await asyncio.wait_for(asyncio.open_connection(ip, port), _TIMEOUT)
    u = urlsplit(proxy)
    host, pport = u.hostname or "", u.port or (1080 if u.scheme.startswith("socks") else 3128)
    r, w = await asyncio.wait_for(asyncio.open_connection(host, pport), _TIMEOUT)
    if u.scheme.startswith("socks5"):
        auth = bool(u.username)
        w.write(b"\x05\x01" + (b"\x02" if auth else b"\x00"))
        await w.drain()
        _ver, method = await r.readexactly(2)
        if method == 0x02 and auth:
            user, pw = unquote(u.username or "").encode(), unquote(u.password or "").encode()
            w.write(b"\x01" + bytes([len(user)]) + user + bytes([len(pw)]) + pw)
            await w.drain()
            if (await r.readexactly(2))[1] != 0:
                w.close()
                return None
        a = ipaddress.ip_address(ip)
        w.write(b"\x05\x01\x00" + ((b"\x01" + a.packed) if a.version == 4 else (b"\x04" + a.packed))
                + struct.pack(">H", port))
        await w.drain()
        head = await r.readexactly(4)
        if head[1] != 0:
            w.close()
            return None
        n = {1: 4, 4: 16}.get(head[3])
        n = n if n is not None else (await r.readexactly(1))[0]
        await r.readexactly(n + 2)
        return r, w
    target = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if u.username:
        cred = f"{unquote(u.username)}:{unquote(u.password or '')}".encode()
        req += f"Proxy-Authorization: Basic {base64.b64encode(cred).decode()}\r\n"
    w.write((req + "\r\n").encode())
    await w.drain()
    status = (await r.readline()).decode("latin-1", "replace").split()
    if len(status) < 2 or status[1] != "200":
        w.close()
        return None
    while (await r.readline()) not in (b"\r\n", b"\n", b""):
        pass
    return r, w


async def ssh_banner(ip: str, port: int, proxy: str | None) -> str:
    """Первая строка, которую отдает SSH по адресу: "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.19"."""
    try:
        t = await _tunnel(ip, port, proxy)
        if t is None:
            return ""
        r, w = t
        try:
            line = await asyncio.wait_for(r.readline(), _TIMEOUT)
        finally:
            w.close()
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError):
        return ""
    text = line.decode("latin-1", "replace").strip()
    return text[:120] if text.startswith("SSH-") else ""


def _ssh_block(srv: Server) -> dict:
    ch = (((srv.last_report or {}).get("extras") or {}).get("changes") or {})
    ssh = ch.get("ssh") if isinstance(ch, dict) else None
    return ssh if isinstance(ssh, dict) else {}


def _ssh_ver(text: str) -> str:
    m = re.search(r"OpenSSH_[0-9][0-9A-Za-z.]*", text or "")
    return m.group(0) if m else ""


def foreign(srv: Server) -> dict[str, dict]:
    """Внешние адреса, где отвечает ДРУГОЙ сервер: версия OpenSSH в баннере снаружи не та, что у
    sshd ноды (helper changes-setup 0.4). На одной ноде внешний адрес в карточке вел на соседний
    CentOS 7, и проверка портов судила о нем: "SSH открыт в интернет" было про чужой хост.

    nat - это адрес, с которого приходят отчеты агента (external_ip), а на интерфейсах ноды его
    нет: нода ходит наружу через NAT, и отвечает там сам шлюз. Входящие при этом идут другими
    адресами (DNAT), поправить тут нечего: external_ip не настройка, панель берет его из запроса."""
    node = _ssh_ver(str(_ssh_block(srv).get("banner") or ""))
    if not node:
        return {}
    ext = str(getattr(srv, "external_ip", "") or "")
    own = {str(ip) for ip in (srv.last_report or {}).get("ips") or []}
    out = {}
    for key, banner in (_ex(srv).get("banners") or {}).items():
        ip = str(key).rsplit(":", 1)[0]
        outside = _ssh_ver(str(banner or ""))
        if outside and outside != node:
            out[ip] = {"ip": ip, "outside": str(banner), "node": node, "nat": ip == ext and ip not in own}
    return out


# ---- сайты ноды через Cloudflare ----

def is_cloudflare(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a.version == n.version and a in n for n in _CF_NETS)


def site_names(srv: Server) -> list[str]:
    """Домены сайтов ноды из веб-серверов, что прислал агент (nginx, Caddy, Envoy в кластере)."""
    out: set[str] = set()
    for w in (srv.last_report or {}).get("web_services") or []:
        for d in (w.get("sites") or []) if isinstance(w, dict) else []:
            d = str(d or "").strip().lower().rstrip(".")
            if "." in d and "*" not in d and " " not in d and not _is_ip(d):
                out.add(d)
    return sorted(out)


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


async def _resolve(name: str) -> list[str]:
    try:
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(name, 443, type=socket.SOCK_STREAM),
                                       _DNS_TIMEOUT)
    except (OSError, asyncio.TimeoutError, UnicodeError):
        return []
    return sorted({i[4][0] for i in infos})


async def cf_sites(srv: Server) -> dict:
    """Какие сайты ноды идут через Cloudflare: все адреса домена - его. Смотрим не больше
    _CF_LOOK доменов: на ноде с сотнями сайтов важно, есть ли такие вообще."""
    names = site_names(srv)
    look = names[:_CF_LOOK]
    sem = asyncio.Semaphore(8)

    async def one(name: str) -> list[str]:
        async with sem:
            return await _resolve(name)

    found = await asyncio.gather(*(one(n) for n in look))
    cf = [n for n, addrs in zip(look, found) if addrs and all(is_cloudflare(a) for a in addrs)]
    return {"sites": cf[:10], "n": len(cf), "checked": len(look), "total": len(names)}


async def proxy_ips(proxy: str | None) -> list[str]:
    """С каких адресов идет проверка: адреса хоста прокси (с них же он и выходит наружу). Без
    прокси - пусто: свой внешний адрес панель не знает, а по проверке с нее и так нет алертов."""
    host = urlsplit(proxy or "").hostname
    if not host:
        return []
    return [host] if _is_ip(host) else await _resolve(host)


async def scan_server(srv: Server, proxy: str | None, via: str, now: datetime) -> dict:
    """Проверить порты ноды снаружи; результат - для servers.exposure. У открытых портов SSH
    заодно читаем баннер: по нему видно, та ли нода отвечает по этому адресу."""
    ips, ports = targets(srv)
    prev = _ex(srv)
    via_ips = await proxy_ips(proxy)
    sem = asyncio.Semaphore(_PARALLEL)

    async def one(ip: str, port: int) -> dict:
        async with sem:
            st = await probe(ip, port, proxy)
        return {"ip": ip, "port": port, "state": st, "what": ports[port], **label(port, ports[port])}

    rows = list(await asyncio.gather(*(one(ip, p) for ip in ips for p in ports))) if ips and ports else []
    ssh_ports = {int(p) for p in _ssh_block(srv).get("ports") or [] if str(p).isdigit()} or {22}
    banners = {}
    for x in rows:
        if x["state"] == "open" and x["port"] in ssh_ports:
            b = await ssh_banner(x["ip"], x["port"], proxy)
            if b:
                banners[f"{x['ip']}:{x['port']}"] = b
    cf = await cf_sites(srv) if any(p in CF_PORTS for p in ports) else None
    mark_allow(srv, rows, via_ips)
    return merge(prev, rows, ips, via, now, own=proxy is None, banners=banners, cf=cf, via_ips=via_ips)


def merge(prev: dict, rows: list[dict], ips: list[str], via: str, now: datetime, own: bool = False,
          banners: dict | None = None, cf: dict | None = None, via_ips: list[str] | None = None) -> dict:
    """Итог проверки с учетом прошлых. Новый порт - тот, что открыт сейчас и не был открыт ни
    в одной проверке за месяц (seen): иначе прокси, упавший на одну проверку, на следующей
    выдал бы все порты ноды за новые. Первая проверка и первая после смены правил (RULES)
    задают точку отсчета. Открытый только белому списку с адресом проверки (allow) в seen не
    идет и из него уходит: когда его откроют всем, он придет новым портом."""
    seen = dict(prev.get("seen") or {}) if isinstance(prev.get("seen"), dict) else None
    if seen is not None and int(prev.get("rules") or 1) < RULES:
        seen = None
    opened = [x for x in rows if x["state"] == "open" and not x.get("allow")]
    new = [x for x in opened if f"{x['ip']}|{x['port']}" not in seen] if seen is not None else []
    seen = {k: v for k, v in (prev.get("seen") or {}).items() if _age_ok(v, now)} \
        if isinstance(prev.get("seen"), dict) else {}
    for x in rows:
        if x["state"] == "open" and x.get("allow"):
            seen.pop(f"{x['ip']}|{x['port']}", None)
    for x in opened:
        seen[f"{x['ip']}|{x['port']}"] = now.isoformat()
    out = {"ts": now.isoformat(), "via": via, "own": own, "ips": ips, "ports": rows, "seen": seen,
           "banners": banners or {}, "rules": RULES, "via_ips": list(via_ips or [])}
    if cf is not None:
        out["cf"] = cf
    if new:
        out["new"], out["new_ts"] = new, now.isoformat()
    elif prev.get("new") and prev.get("new_ts"):
        out["new"], out["new_ts"] = prev["new"], prev["new_ts"]  # держим до следующего прироста
    return out


def _age_ok(ts: str, now: datetime) -> bool:
    try:
        return now - datetime.fromisoformat(ts) <= _SEEN_KEEP
    except (TypeError, ValueError):
        return False


def _ex(srv: Server) -> dict:
    ex = getattr(srv, "exposure", None)
    return ex if isinstance(ex, dict) else {}


def from_panel(srv: Server) -> bool:
    """Проверка шла с адреса самой панели (локации-прокси нет): если файрвол ноды пускает
    панель, открытое тут закрыто для остальных. До 1.4.135 флага не было - смотрим на via."""
    ex = _ex(srv)
    return bool(ex.get("own")) if "own" in ex else ex.get("via") == "панель"


def scanned_at(srv: Server) -> datetime | None:
    ex = _ex(srv)
    try:
        return datetime.fromisoformat(str(ex.get("ts") or ""))
    except ValueError:
        return None


def _relabel(srv: Server, rows: list) -> list[dict]:
    """Строки проверки с подписями по свежему отчету. Подпись запоминается в момент проверки, а
    helper мог прислать имена процессов позже: без этого таблица до 6 часов показывала бы
    "процесс пользователя root", хотя имя уже известно. Опасность и служебность тоже считаем
    заново: проверки до 1.4.137 служебных портов не отмечали."""
    _ips, cur = targets(srv)
    out = []
    for x in rows:
        if not isinstance(x, dict) or not isinstance(x.get("port"), int):
            continue
        what = cur.get(x["port"]) or str(x.get("what") or "")
        out.append(dict(x, what=what, **label(x["port"], what)))
    return out


# ---- белый список с адресом проверки (helper changes-setup 0.7) ----

def _fw(srv: Server) -> dict:
    fw = (((srv.last_report or {}).get("extras") or {}).get("changes") or {}).get("fw")
    return fw if isinstance(fw, dict) else {}


def _covers(spec: str, port: int) -> bool:
    """Покрывает ли список портов правила ("*", "80,443", "1000:2000", "8000:") порт."""
    for part in str(spec or "").split(","):
        if part == "*":
            return True
        lo, sep, hi = part.partition(":")
        try:
            a = int(lo) if lo else 0
            b = (int(hi) if hi else 65535) if sep else a
        except ValueError:
            continue
        if a <= port <= b:
            return True
    return False


def _fw_rules(items, fam: int) -> list[tuple]:
    """Правила файрвола семейства fam: (сеть источника или None - "кто угодно", порты). Сеть шире
    /8 (/16 у IPv6) - тоже "кто угодно": это не белый список."""
    out = []
    for it in items if isinstance(items, list) else []:
        parts = str(it).split()
        if len(parts) != 3 or str(fam) not in parts[0]:
            continue
        if parts[1] == "*":
            out.append((None, parts[2]))
            continue
        try:
            net = ipaddress.ip_network(parts[1], strict=False)
        except ValueError:
            continue
        out.append((None if net.prefixlen < (8 if net.version == 4 else 16) else net, parts[2]))
    return out


def _port_path(srv: Server, port: int) -> tuple[str, int] | None:
    """Каким путем снаружи идут на порт: ("in", порт) - на саму ноду, ("fwd", порт в контейнере) -
    опубликованный порт Docker (DNAT, мимо INPUT, правила смотрят порт контейнера), None - порт
    Kubernetes: kube-proxy заворачивает его своими правилами, судить не беремся."""
    rep = srv.last_report or {}
    for c in ((rep.get("docker") or {}).get("containers") or []):
        for p in (c.get("ports") or []) if isinstance(c, dict) else []:
            if isinstance(p, dict) and p.get("pub") == port and (p.get("proto") or "tcp") == "tcp":
                return ("fwd", int(p.get("priv") or 0)) if p.get("priv") else None
    for n in ((rep.get("kube") or {}).get("nodeports") or []):
        if isinstance(n, dict) and port in (n.get("node_port"), n.get("port") if n.get("type") == "LoadBalancer" else None):
            return None
    return "in", port


def only_allowlist(srv: Server, ip: str, port: int, via_ips: list[str]) -> bool:
    """Порт виден проверке только потому, что ее адрес в белом списке файрвола ноды: остальным
    по умолчанию отказ (политика DROP или "отбросить остальное"), правила "всем" на этот порт
    нет, а правило для сети с адресом проверки есть. Чего не знаем (helper старше 0.7, nftables
    для контейнеров, порты Kubernetes) - считаем открытым: лучше лишний алерт, чем пропущенная дыра."""
    fw = _fw(srv)
    if not fw or not via_ips:
        return False
    try:
        fam = ipaddress.ip_address(ip).version
    except ValueError:
        return False
    vias = []
    for v in via_ips:
        try:
            a = ipaddress.ip_address(v)
        except ValueError:
            continue
        if a.version == fam:
            vias.append(a)
    path = _port_path(srv, port)
    if not vias or path is None:
        return False
    kind, p = path
    if kind == "in":
        if str(fw.get(f"p{fam}") or "").upper() not in ("DROP", "REJECT") and str(fam) not in str(fw.get("idrop") or ""):
            return False
        rules = _fw_rules(fw.get("in"), fam)
    else:
        if str(fam) not in str(fw.get("fdrop") or ""):
            return False
        rules = _fw_rules(fw.get("fwd"), fam)
    hit = [net for net, spec in rules if _covers(spec, p)]
    return None not in hit and any(v in net for net in hit for v in vias)


def mark_allow(srv: Server, rows: list[dict], via_ips: list[str] | None) -> list[dict]:
    """Отметить открытые проверке только по белому списку (allow) по свежему отчету helper'а."""
    for x in rows:
        x.pop("allow", None)
        if x.get("state") == "open" and only_allowlist(srv, str(x.get("ip") or ""), int(x.get("port") or 0), via_ips or []):
            x["allow"] = True
    return rows


def marked_rows(srv: Server, rows: list) -> list[dict]:
    """Строки проверки с подписями и отметкой "только белому списку" по свежему отчету."""
    return mark_allow(srv, _relabel(srv, rows), _ex(srv).get("via_ips"))


def view(srv: Server) -> dict | None:
    """Итог проверки для карточки: без seen (служебная история) и с отметкой намеренных портов."""
    ex = _ex(srv)
    if not ex:
        return None
    out = {k: v for k, v in ex.items() if k != "seen"}
    out["ports"] = marked_rows(srv, ex.get("ports") or [])
    if ex.get("new"):
        out["new"] = [x for x in marked_rows(srv, ex["new"]) if not x.get("allow")]
    out["own"] = from_panel(srv)
    _mark_cf(out)
    out["foreign"] = list(foreign(srv).values())
    out["muted"] = sorted(muted_ports(srv), key=lambda p: int(p) if p.isdigit() else 0)
    return out


def _mark_cf(out: dict) -> None:
    """Сайты ноды идут через Cloudflare: 80 и 443, закрытые напрямую, открыты только для него
    (cf: only), открытые - и в обход него (cf: also)."""
    if not (out.get("cf") or {}).get("n"):
        return
    for x in out["ports"]:
        if x["port"] in CF_PORTS and x.get("state") in ("open", "closed", "filtered"):
            x["cf"] = "also" if x["state"] == "open" else "only"


def cf_open(srv: Server, now: datetime) -> list[int]:
    """80 и 443, открытые в обход Cloudflare по свежей проверке (cf: also): сайты ноды идут через
    него, а порт открыт и напрямую. Проверка с самой панели, порт только белому списку с адресом
    проверки и адрес другого сервера не в счет. Пускает ли файрвол туда только Cloudflare, проверка
    и видит: такой порт для нее закрыт."""
    ex = _ex(srv)
    ts = scanned_at(srv)
    if ts is None or now - ts > FRESH or from_panel(srv) or not (ex.get("cf") or {}).get("n"):
        return []
    alien = foreign(srv)
    return sorted({x["port"] for x in marked_rows(srv, ex.get("ports") or [])
                   if x["port"] in CF_PORTS and x.get("state") == "open" and not x.get("allow")
                   and x.get("ip") not in alien})


def exposed(srv: Server, now: datetime) -> list[dict]:
    """Опасные и служебные порты, открытые в интернет по свежей проверке, без тех, что человек
    отметил как намеренные (alert_mutes "exposed:<порт>"). Проверка с самой панели не в счет."""
    ex = _ex(srv)
    ts = scanned_at(srv)
    if ts is None or now - ts > FRESH or from_panel(srv):
        return []
    muted = muted_ports(srv)
    alien = foreign(srv)  # адрес ведет на другой сервер: его порты не про эту ноду
    return [x for x in marked_rows(srv, ex.get("ports") or [])
            if x.get("state") == "open" and not x.get("allow") and (x.get("risk") or x.get("svc"))
            and str(x.get("port")) not in muted and x.get("ip") not in alien]


def exposed_level(rows: list[dict]) -> int:
    """2 - открыто опасное, 1 - только служебное, 0 - ничего"""
    return 2 if any(x.get("risk") for x in rows) else 1 if rows else 0


def exposure_text(rows: list[dict], limit: int = 4) -> str:
    """'открыто в интернет: etcd 2380 на ...; служебное открыто в интернет: kube-proxy 10249 на ...'"""
    bad = [x for x in rows if x.get("risk")]
    svc = [x for x in rows if not x.get("risk")]
    parts = []
    if bad:
        parts.append("открыто в интернет: " + exposed_text(bad, limit))
    if svc:
        parts.append("служебное открыто в интернет: " + exposed_text(svc, limit))
    return "; ".join(parts)


def exposed_text(rows: list[dict], limit: int = 4) -> str:
    """'Redis 6379 (контейнер redis (redis:7)) на 203.0.113.5, PostgreSQL 5432 на ...'"""
    by: dict[tuple, list[str]] = {}
    for x in rows:
        by.setdefault((x.get("name") or "", x["port"], x.get("what") or ""), []).append(x["ip"])
    items = [f"{name + ' ' if name else 'порт '}{port}{f' ({what})' if what else ''} на {', '.join(ips)}"
             for (name, port, what), ips in list(by.items())[:limit]]
    if len(by) > limit:
        items.append(f"и еще {len(by) - limit}")
    return "; ".join(items)


def muted_ports(srv: Server) -> set[str]:
    """Порты, которые человек отметил как открытые намеренно."""
    return {m[len(MUTE_PREFIX):] for m in (getattr(srv, "alert_mutes", None) or []) if m.startswith(MUTE_PREFIX)}


def new_ports(srv: Server, now: datetime, hours: int = 24) -> list[dict]:
    """Порты, впервые открывшиеся снаружи за последние сутки (кроме отмеченных намеренными).
    Проверка с самой панели не в счет."""
    ex = _ex(srv)
    if from_panel(srv):
        return []
    try:
        ts = datetime.fromisoformat(str(ex.get("new_ts") or ""))
    except ValueError:
        return []
    if now - ts > timedelta(hours=hours):
        return []
    muted = muted_ports(srv)
    alien = foreign(srv)
    return [x for x in marked_rows(srv, ex.get("new") or []) if str(x.get("port")) not in muted
            and x.get("ip") not in alien and not x.get("allow")]


def new_since(srv: Server) -> str | None:
    """Когда проверка последний раз нашла новые порты (отметка события "новый порт")."""
    return _ex(srv).get("new_ts") or None


def new_text(rows: list[dict]) -> str:
    """'8080 (контейнер app (app:1)) на 203.0.113.5; Redis 6379 на ...' - опасные первыми."""
    rows = sorted(rows, key=lambda x: (not x.get("risk"), x.get("port") or 0))
    return exposed_text(rows)


# ---- короткий текст алерта в Telegram (app/tg_fmt.py) ----

# длинные подписи сервисов -> как их назовет человек в чате
_SHORT_NAMES = {"Erlang epmd": "epmd", "Zabbix agent": "Zabbix", "ClickHouse HTTP": "ClickHouse",
                "проверка здоровья балансировщика (kube-proxy)": "kube-proxy"}
_WHO_RE = re.compile(r"^(?:процесс (?!пользователя )([^,\s]+)|контейнер (\S+)|сервис Kubernetes \S*?/(\S+))")


def _who(what: str) -> str:
    """Кто держит порт, одним словом: процесс, контейнер или сервис Kubernetes."""
    m = _WHO_RE.match(str(what or ""))
    return next((g for g in m.groups() if g), "") if m else ""


def _short_name(x: dict) -> str:
    """Название сервиса для чата: epmd, Zabbix, kube-proxy. "экспортер метрик" и подписи "или"
    (9000 - ClickHouse, MinIO или php-fpm) - по тому, кто держит порт."""
    name = str(x.get("name") or "")
    if name in _SHORT_NAMES:
        return _SHORT_NAMES[name]
    if name == "экспортер метрик" or " или " in name:
        return _who(str(x.get("what") or "")) or name.split(",")[0].split(" ")[0]
    return re.split(r",\s|\s\(", name)[0]


def _by_port(rows: list[dict]) -> list[dict]:
    """Один порт на нескольких адресах - одна строка, порядок сохраняется."""
    out: dict[int, dict] = {}
    for x in rows:
        out.setdefault(x.get("port"), x)
    return list(out.values())


def tg_short_exposed(rows: list[dict]) -> tuple[str, list[str]] | None:
    """Алерт exposed (вход - exposed()): "Открыто в интернет", "RabbitMQ 5672, epmd 4369, Zabbix 10050
    и еще 2". Опасное первым, и сначала разные сервисы: второй порт того же RabbitMQ уходит в "и еще"."""
    if not rows:
        return None
    rows = sorted(_by_port(rows), key=lambda x: not x.get("risk"))
    seen: set[str] = set()
    first, dup = [], []
    for x in rows:
        nm = _short_name(x) or _who(str(x.get("what") or ""))
        item = f"{nm} {x.get('port')}" if nm else f"порт {x.get('port')}"
        (dup if nm and nm in seen else first).append(item)
        seen.add(nm)
    return "Открыто в интернет", [tg_fmt.names(first + dup)]


def tg_short_new(rows: list[dict]) -> tuple[str, list[str]] | None:
    """Алерт port_new (вход - new_ports()): "Открылись новые порты", "64100 rest-server, 64101
    kervax-rest-tls". Порт первым, за ним сервис или кто его держит; опасные первыми."""
    if not rows:
        return None
    rows = _by_port(sorted(rows, key=lambda x: (not x.get("risk"), x.get("port") or 0)))
    items = []
    for x in rows:
        nm = _short_name(x) or _who(str(x.get("what") or ""))
        items.append(f"{x.get('port')} {nm}" if nm else str(x.get("port")))
    what = "Открылся новый порт" if len(rows) == 1 else "Открылись новые порты"
    return what, [tg_fmt.names(items)]


_running: asyncio.Task | None = None
_last_start: datetime | None = None
# Кому пора, смотрим раз в 5 минут, а не каждый тик: выборка тянет отчеты всех нод
_LOOK_EVERY = timedelta(minutes=5)


async def _scan_due(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> int:
    async with session_factory() as s:
        proxy, via = await pick_proxy(s)
        servers = list(await s.scalars(select(Server).where(Server.enabled.is_(True))))
        due = []
        for srv in servers:
            if srv.last_seen is None or not (srv.last_report or {}).get("listen"):
                continue  # агент старше 2.26: судить не по чему
            seen = srv.last_seen if srv.last_seen.tzinfo else srv.last_seen.replace(tzinfo=timezone.utc)
            if now - seen > timedelta(minutes=10):
                continue
            last = scanned_at(srv)
            # правила выбора портов поменялись или проверка не знает своих адресов (до 1.4.263) -
            # проверяем сразу, не дожидаясь 6 часов
            ex = _ex(srv)
            if last is None or now - last >= SCAN_EVERY or int(ex.get("rules") or 1) < RULES or "via_ips" not in ex:
                due.append((last or datetime.min.replace(tzinfo=timezone.utc), srv.id))
        due.sort()
        n = 0
        for _last, sid in due[:8]:
            srv = await s.get(Server, sid)
            if srv is None:
                continue
            srv.exposure = await scan_server(srv, proxy, via, datetime.now(timezone.utc))
            await s.commit()
            n += 1
        return n


async def start_scan(session_factory: async_sessionmaker[AsyncSession], now: datetime) -> None:
    """Запустить проверку тех, кому пора, фоном: тик планировщика её не ждет (порт, закрытый
    файрволом, отвечает только таймаутом)."""
    global _running, _last_start
    if _running is not None and not _running.done():
        return
    if _last_start is not None and now - _last_start < _LOOK_EVERY:
        return
    _last_start = now

    async def run() -> None:
        try:
            n = await _scan_due(session_factory, now)
            if n:
                log.info("проверено снаружи нод: %d", n)
        except Exception:  # noqa: BLE001 - фоновая задача не должна ронять планировщик
            log.exception("внешняя проверка портов упала")

    _running = asyncio.create_task(run())

