"""Что открыто в интернет: внешняя проверка портов нод.

Агент 2.26 сообщает слушающие порты (адрес привязки, владелец сокета), публичные адреса ноды,
опубликованные порты контейнеров Docker и NodePort-сервисы. Панель раз в 6 часов стучится к
этим портам снаружи и запоминает, что реально открыто, а что закрыто файрволом.

Откуда стучаться: с адреса самой панели часто разрешено больше, чем остальному интернету
(вайтлисты под мониторинг), и порт, закрытый для мира, выглядел бы открытым. Поэтому проверка
идет через первую включенную локацию-прокси (socks5 или HTTP CONNECT), а без нее - с панели,
и в результате записано, откуда смотрели.

Опасным считается сервис, которому не место в интернете: базы, очереди, Docker API, etcd,
kubelet, экспортеры метрик. Открытый SSH и веб - норма, их просто видно в списке.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import re
import struct
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    8200: ("Vault", False), 8500: ("Consul", True), 9000: ("ClickHouse, MinIO или php-fpm", True),
    9042: ("Cassandra", True), 9090: ("Prometheus", True), 9092: ("Kafka", True),
    9093: ("Alertmanager", True), 9100: ("node_exporter", True), 9200: ("Elasticsearch", True),
    9300: ("Elasticsearch", True), 10250: ("kubelet", True), 10255: ("kubelet", True),
    11211: ("Memcached", True), 15672: ("RabbitMQ UI", True), 26257: ("CockroachDB", True),
    27017: ("MongoDB", True), 27018: ("MongoDB", True), 27019: ("MongoDB", True),
}
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


# не публичные: частные сети, CGNAT и меш (100.64/10), loopback, link-local, мультикаст. Как у
# агента (IsGlobalUnicast и не IsPrivate), а не is_global: тот считает частными и сети из
# документации, которых на нодах не бывает
_NOT_PUBLIC = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "224.0.0.0/3", "fc00::/7")]
_V6_GLOBAL = ipaddress.ip_network("2000::/3")


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
            put(n.get("node_port"), f"сервис {n.get('ns', '?')}/{n.get('name', '?')} ({n.get('type') or 'NodePort'})")
    for s in rep.get("listen") or []:
        if not isinstance(s, dict) or not str(s.get("p") or "").startswith("tcp"):
            continue
        bind = str(s.get("ip") or "")
        if bind in ("0.0.0.0", "::") or _public(bind) or (behind_nat and bind == behind_nat):
            put(s.get("port"), f"процесс пользователя {s['u']}" if s.get("u") else "")
    keep = sorted(ports)[:_MAX_PORTS]
    return ips[:_MAX_IPS], {p: ports[p] for p in keep}


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
    return known or ("", False)


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


async def pick_proxy(session: AsyncSession) -> tuple[str | None, str]:
    """Включенная локация-прокси, socks5 первым (HTTP-прокси часто пускают CONNECT только на 443):
    (url, имя); без нее - (None, "панель")."""
    found: list[tuple[int, str, str]] = []
    for loc in await session.scalars(select(Location).where(Location.enabled.is_(True)).order_by(Location.id)):
        url = (loc.url or "").strip()
        scheme = urlsplit(url).scheme
        if scheme in ("socks5", "socks5h", "http", "https"):
            found.append((0 if scheme.startswith("socks") else 1, url, loc.name))
    if not found:
        return None, "панель"
    _rank, url, name = min(found, key=lambda x: x[0])
    return url, name


async def scan_server(srv: Server, proxy: str | None, via: str, now: datetime) -> dict:
    """Проверить порты ноды снаружи; результат - для servers.exposure."""
    ips, ports = targets(srv)
    prev = _ex(srv)
    sem = asyncio.Semaphore(_PARALLEL)

    async def one(ip: str, port: int) -> dict:
        async with sem:
            st = await probe(ip, port, proxy)
        name, risky = classify(port, ports[port])
        return {"ip": ip, "port": port, "state": st, "what": ports[port], "name": name, "risk": risky}

    rows = list(await asyncio.gather(*(one(ip, p) for ip in ips for p in ports))) if ips and ports else []
    return merge(prev, rows, ips, via, now)


def merge(prev: dict, rows: list[dict], ips: list[str], via: str, now: datetime) -> dict:
    """Итог проверки с учетом прошлых. Новый порт - тот, что открыт сейчас и не был открыт ни
    в одной проверке за месяц (seen): иначе прокси, упавший на одну проверку, на следующей
    выдал бы все порты ноды за новые. Первая проверка задает точку отсчета."""
    seen = dict(prev.get("seen") or {}) if isinstance(prev.get("seen"), dict) else None
    opened = [x for x in rows if x["state"] == "open"]
    new = [x for x in opened if f"{x['ip']}|{x['port']}" not in seen] if seen is not None else []
    seen = {k: v for k, v in (seen or {}).items() if _age_ok(v, now)}
    for x in opened:
        seen[f"{x['ip']}|{x['port']}"] = now.isoformat()
    out = {"ts": now.isoformat(), "via": via, "ips": ips, "ports": rows, "seen": seen}
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


def scanned_at(srv: Server) -> datetime | None:
    ex = _ex(srv)
    try:
        return datetime.fromisoformat(str(ex.get("ts") or ""))
    except ValueError:
        return None


def view(srv: Server) -> dict | None:
    """Итог проверки для карточки: без seen (служебная история) и с отметкой намеренных портов."""
    ex = _ex(srv)
    if not ex:
        return None
    out = {k: v for k, v in ex.items() if k != "seen"}
    out["muted"] = sorted(muted_ports(srv), key=lambda p: int(p) if p.isdigit() else 0)
    return out


def exposed(srv: Server, now: datetime) -> list[dict]:
    """Опасные сервисы, открытые в интернет по свежей проверке, без тех, что человек отметил как
    намеренные (alert_mutes "exposed:<порт>")."""
    ex = _ex(srv)
    ts = scanned_at(srv)
    if ts is None or now - ts > FRESH:
        return []
    muted = muted_ports(srv)
    return [x for x in ex.get("ports") or []
            if isinstance(x, dict) and x.get("state") == "open" and x.get("risk") and str(x.get("port")) not in muted]


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
    """Порты, впервые открывшиеся снаружи за последние сутки (кроме отмеченных намеренными)."""
    ex = _ex(srv)
    try:
        ts = datetime.fromisoformat(str(ex.get("new_ts") or ""))
    except ValueError:
        return []
    if now - ts > timedelta(hours=hours):
        return []
    muted = muted_ports(srv)
    return [x for x in ex.get("new") or [] if isinstance(x, dict) and str(x.get("port")) not in muted]


def new_since(srv: Server) -> str | None:
    """Когда проверка последний раз нашла новые порты (отметка события "новый порт")."""
    return _ex(srv).get("new_ts") or None


def new_text(rows: list[dict]) -> str:
    """'8080 (контейнер app (app:1)) на 203.0.113.5; Redis 6379 на ...' - опасные первыми."""
    rows = sorted(rows, key=lambda x: (not x.get("risk"), x.get("port") or 0))
    return exposed_text(rows)


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
            if last is None or now - last >= SCAN_EVERY:
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

