"""Демо-стенд панели: наполняет пустую базу вымышленными нодами и мониторами.

Нужен для скриншотов README (docs/make-shots.py) и чтобы посмотреть панель, не
поднимая ни одной настоящей ноды. Всё в кадре вымышлено: домены из RFC 2606
(example.com/.net/.org), адреса — из документационных диапазонов RFC 5737
(203.0.113.x, 198.51.100.x). Флага страны у таких адресов не будет: таблица
IP→страна их не знает, и дорисовывать его в демо было бы обманом.

    python docs/demo-seed.py <путь-к-базе.db> [ru|en]

Язык нужен потому, что имена мониторов и тексты ошибок — это ДАННЫЕ: панель их
не переводит, и в английском кадре они остались бы русскими.

Работает по SQLAlchemy-моделям напрямую, минуя API: агента, который прислал бы
эти отчёты, не существует.
"""
import asyncio
import hashlib
import math
import os
import random
import secrets
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

DB = sys.argv[1] if len(sys.argv) > 1 else "demo.db"
LANG = sys.argv[2] if len(sys.argv) > 2 else "ru"
os.environ.setdefault("KERVAX_DB_URL", f"sqlite+aiosqlite:///{DB}")
# Одноразовая база на время съёмки: секреты нужны лишь для того, чтобы панель
# согласилась стартовать, поэтому берём случайные, а не константы в исходниках.
os.environ.setdefault("KERVAX_JWT_SECRET", secrets.token_hex(32))
os.environ.setdefault("KERVAX_ADMIN_PASSWORD", secrets.token_urlsafe(18))

from app.bootstrap import ensure_admin  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, create_engine_and_factory  # noqa: E402
from app.models import (  # noqa: E402
    Check, CheckIncident, CheckSample, Location, LocationResult, LocationSample,
    Server, ServerMetric,
)

rnd = random.Random(20260823)  # фиксированное зерно: скрины воспроизводимы
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
GB = 1024 ** 3


def wave(t_min: float, base: float, amp: float, period: float = 1440.0) -> float:
    """Суточная синусоида + шум: ровная линия на графике выглядит как заглушка."""
    v = base + amp * math.sin(2 * math.pi * (t_min % period) / period) + rnd.uniform(-amp / 4, amp / 4)
    return max(0.3, v)


# ── ноды ──────────────────────────────────────────────────────────────────────
# (имя, группа, ОС, ядер, ОЗУ ГБ, база CPU%, диски, роль)
NODES = [
    ("web-01",      "Production", "Ubuntu 24.04.1 LTS", 8,  16, 34, [("/", 42, 100), ("/var/log", 61, 20)], "web"),
    ("web-02",      "Production", "Ubuntu 24.04.1 LTS", 8,  16, 29, [("/", 38, 100), ("/var/log", 55, 20)], "web"),
    ("db-01",       "Production", "Debian 12 (bookworm)", 16, 64, 46, [("/", 31, 60), ("/var/lib/postgresql", 74, 500)], "db"),
    ("cache-01",    "Production", "Debian 12 (bookworm)", 4,  32, 18, [("/", 24, 40)], "cache"),
    ("app-01",      "Production", "Rocky Linux 9.4",     8,  32, 52, [("/", 47, 120)], "app"),
    ("k8s-node-1",  "Staging",    "Ubuntu 22.04.5 LTS",  16, 32, 61, [("/", 66, 200)], "kube"),
    ("backup-01",   "Infra",      "Debian 12 (bookworm)", 4, 8,  12, [("/", 22, 40), ("/srv/restic", 88, 4000)], "backup"),
    ("ci-runner-1", "Dev",        "Ubuntu 24.04.1 LTS",  8,  16, 71, [("/", 58, 200)], "ci"),
]

TOP_CPU = {
    "web": [("nginx", 14.2, 268), ("php-fpm", 9.7, 512), ("node", 6.1, 331)],
    "db": [("postgres", 38.4, 8140), ("postgres", 12.9, 3320), ("pgbouncer", 2.2, 96)],
    "cache": [("redis-server", 11.8, 2210), ("keydb", 3.1, 410), ("sshd", 0.4, 12)],
    "app": [("java", 41.6, 6120), ("python3", 8.3, 740), ("supervisord", 0.7, 28)],
    "kube": [("kubelet", 17.4, 812), ("containerd", 12.1, 640), ("coredns", 3.3, 74)],
    "backup": [("restic", 22.9, 1480), ("rest-server", 1.8, 64), ("sshd", 0.3, 11)],
    "ci": [("buildkitd", 46.2, 3980), ("dockerd", 14.7, 720), ("git", 5.4, 210)],
}

WEB_SITES = {
    "web-01": ["example.com", "www.example.com", "shop.example.com", "api.example.com",
               "blog.example.com", "status.example.com"],
    "web-02": ["example.net", "cdn.example.net", "media.example.net"],
}

DB_STATS = {
    "db-01": [{"engine": "postgres", "version": "16.4", "port": 5432, "source": "host",
               "databases": [{"name": "shop", "size": 41_100_000_000},
                             {"name": "billing", "size": 12_400_000_000},
                             {"name": "analytics", "size": 6_900_000_000}],
               "logins": ["shop_app", "billing_app", "readonly"]}],
    "cache-01": [{"engine": "redis", "version": "7.2.5", "port": 6379, "source": "host",
                  "databases": [{"name": "db0", "size": 2_100_000_000}], "logins": []}],
}


def report(node, t_min: float) -> dict:
    name, group, osname, cores, ram_gb, cpu_base, disks, role = node
    cpu = round(wave(t_min, cpu_base, 14), 1)
    mem_total = ram_gb * GB
    mem_used = int(mem_total * wave(t_min, 0.54, 0.08, 720))
    rep = {
        "hostname": name, "os": osname, "agent_version": "1.97",
        "cpu_model": "AMD EPYC 9354P" if cores >= 16 else "Intel Xeon E-2388G",
        "is_vm": role != "db", "virt": "kvm" if role != "db" else "",
        "uptime_seconds": int(86400 * (3 + NODES.index(node) * 11) + t_min * 60),
        "cpu_percent": cpu, "cpu_cores": cores,
        "cpu_user": round(cpu * 0.62, 1), "cpu_system": round(cpu * 0.24, 1),
        "cpu_iowait": round(cpu * 0.09, 1), "cpu_irq": round(cpu * 0.05, 1),
        "cpu_cores_pct": [round(max(0.4, cpu + rnd.uniform(-18, 18)), 1) for _ in range(cores)],
        "cpu_freq": round(2400 + rnd.uniform(-120, 900)), "cpu_temp": round(38 + cpu * 0.34, 1),
        "cpu_throttle": 0, "oom_kill": 0,
        "mem_used": mem_used, "mem_total": mem_total,
        "mem_cached": int(mem_total * 0.21), "mem_free": mem_total - mem_used - int(mem_total * 0.21),
        "mem_slab": int(mem_total * 0.03), "mem_dirty": 18 * 1024 ** 2, "mem_writeback": 0,
        "swap_used": int(0.04 * 4 * GB) if role in ("app", "ci") else 0,
        "swap_total": 4 * GB, "swap_in": 0.0, "swap_out": 0.0,
        "load": [round(cores * cpu / 100 * 1.1, 2), round(cores * cpu / 100, 2),
                 round(cores * cpu / 100 * 0.92, 2)],
        "disks": [{"mount": m, "used": int(total * GB * pct / 100), "total": total * GB}
                  for m, pct, total in disks],
        "net_rx": round(wave(t_min, 12_400_000, 6_000_000)),
        "net_tx": round(wave(t_min, 8_100_000, 4_400_000)),
        "net_ifaces": [{"if": "eth0", "rx": round(wave(t_min, 12_400_000, 6_000_000)),
                        "tx": round(wave(t_min, 8_100_000, 4_400_000))},
                       {"if": "docker0", "rx": round(wave(t_min, 900_000, 400_000)),
                        "tx": round(wave(t_min, 1_200_000, 500_000))}],
        "disk_read": round(wave(t_min, 4_200_000, 2_600_000)),
        "disk_write": round(wave(t_min, 6_800_000, 3_100_000)),
        "disk_read_iops": round(wave(t_min, 210, 140)), "disk_write_iops": round(wave(t_min, 380, 190)),
        "disk_devs": [{"dev": "nvme0n1", "util": round(wave(t_min, 22, 15), 1),
                       "await": round(wave(t_min, 1.4, 0.9), 2), "temp": round(34 + rnd.uniform(0, 6))}],
        "conntrack_count": round(wave(t_min, 18_400, 9_000)), "conntrack_max": 262_144,
        "sock_used": round(wave(t_min, 1240, 400)), "sock_tcp": round(wave(t_min, 890, 300)),
        "sock_tcp_tw": round(wave(t_min, 310, 180)), "sock_udp": 24,
        "top_cpu": [{"pid": 1000 + i, "comm": c, "cpu": v, "rss": r * 1024 ** 2}
                    for i, (c, v, r) in enumerate(TOP_CPU[role])],
        "top_mem": [{"pid": 1000 + i, "comm": c, "cpu": v, "rss": r * 1024 ** 2}
                    for i, (c, v, r) in enumerate(sorted(TOP_CPU[role], key=lambda x: -x[2]))],
        "caps": {"kmsg": True},
        "clock": {"synced": True, "ntp": True, "service": "systemd-timesyncd"},
        "clock_unix": int((NOW - timedelta(minutes=t_min)).timestamp()),
        # Версии helper-скриптов на ноде — панель сверяет их с теми, что раздаёт
        # сама. У ci-runner-1 backup-setup намеренно старый: так в кадр попадает
        # «Требует действий», а это одна из главных обязанностей панели —
        # показывать невыкаченное, и на идеально ровном демо её было бы не видно.
        "setup_versions": {"backup-setup": "0.19" if role == "ci" else "0.23",
                           "kube-setup": "0.14", "webserver-setup": "0.4",
                           "dbstat-setup": "0.2", "agent-watchdog": "1.0",
                           "timesync-setup": "0.2", "backupserver-setup": "0.19"},
        "checks": [{"key": "ssh", "type": "port", "status": "ok", "message": "22/tcp"},
                   {"key": "ntp", "type": "clock", "status": "ok", "message": "synced"}],
        "db_engines": ["postgres"] if role == "db" else (["redis"] if role == "cache" else []),
        "db_stats": DB_STATS.get(name, []),
        "services": ([{"kind": "rabbitmq", "source": "host", "vhost": "/",
                       "queues": [{"name": "orders", "depth": 12}, {"name": "mail", "depth": 0}]}]
                     if role == "app" else []),
    }
    if name in WEB_SITES:
        rep["web_services"] = [{"kind": "nginx", "version": "1.26.2", "source": "host",
                                "sites": WEB_SITES[name]}]
    if role in ("web", "app", "ci"):
        rep["docker"] = {"present": True, "access": True, "version": "27.3.1", "compose": True,
                         "containers": [
                             {"name": f"{name}-app-1", "image": "app:2.14.0", "state": "running",
                              "status": "Up 6 days", "restart": "always", "cpu": 12.4,
                              "mem": 640 * 1024 ** 2},
                             {"name": f"{name}-worker-1", "image": "app:2.14.0", "state": "running",
                              "status": "Up 6 days", "restart": "always", "cpu": 4.1,
                              "mem": 310 * 1024 ** 2},
                             # без СУБД в контейнерах: панель справедливо спросила бы
                             # про инвентарь и дампы, а в демо отвечать на это нечем
                             {"name": f"{name}-nginx-1", "image": "nginx:1.27-alpine",
                              "state": "running", "status": "Up 21 days", "restart": "unless-stopped",
                              "cpu": 0.8, "mem": 96 * 1024 ** 2}]}
    if role == "kube":
        rep["kube"] = {"present": True, "access": True, "flavor": "k0s", "version": "v1.31.2",
                       "nodes": [{"name": "k8s-node-1", "ready": True, "roles": "control-plane",
                                  "version": "v1.31.2"},
                                 {"name": "k8s-node-2", "ready": True, "roles": "worker",
                                  "version": "v1.31.2"}],
                       "pods": [{"ns": "shop", "name": "web-7d9c8b6f5-2xkqz", "phase": "Running",
                                 "ready": "1/1", "restarts": 0},
                                {"ns": "shop", "name": "worker-5f8b9c7d6-mn4pl", "phase": "Running",
                                 "ready": "1/1", "restarts": 2},
                                {"ns": "kube-system", "name": "coredns-6c8b5d9f7-qw2rt",
                                 "phase": "Running", "ready": "1/1", "restarts": 0}],
                       "ingress": [{"ns": "shop", "hosts": ["shop.example.org"]}]}
    if role == "backup":
        rep["backup_server"] = {"present": True, "running": True, "version": "0.13.0",
                                "repos": [{"name": "web-01", "valid": True, "size": 214_000_000_000},
                                          {"name": "db-01", "valid": True, "size": 812_000_000_000}]}
    # Ночное окно: бэкап, закончившийся после 8 утра, панель штатно помечает как
    # выбившийся из окна — в демо это была бы плашка на весь экран.
    done = NOW.replace(hour=3, minute=40 - NODES.index(node), second=0, microsecond=0)
    if done > NOW:
        done -= timedelta(days=1)
    rep["backup"] = {"present": True, "configured": 1, "metric_present": 1,
                     "success": 1, "skipped": False,
                     "timer_enabled": 1, "timer_active": 1,
                     "last_backup_ts": int(done.timestamp()),
                     "started_ts": int((done - timedelta(minutes=12)).timestamp()),
                     "repo": "rest:https://backup.example.net/" + name,
                     "paths": ["/etc", "/srv", "/var/lib"], "schedule": "03:20",
                     "snapshots": rnd.randint(38, 96), "size": rnd.randint(40, 400) * 1_000_000_000,
                     "duration": rnd.randint(120, 900), "restic_version": "0.19.1"}
    return rep


# ── мониторы ─────────────────────────────────────────────────────────────────
# (ключ имени, тип, цель, статус, мс, ssl дней, домен дней, группа)
CHECKS = [
    ("shop",    "http", "https://shop.example.com",        "up",       142, 68,  213, "Production"),
    ("site",    "http", "https://example.com",             "up",        96, 68,  213, "Production"),
    ("api",     "http", "https://api.example.com/health",  "up",        54, 68,  213, "Production"),
    ("account", "http", "https://my.example.com",          "degraded", 2840, 12, 213, "Production"),
    ("blog",    "http", "https://blog.example.com",        "up",       318, 68,  213, "Production"),
    ("partner", "http", "https://example.net",             "up",       205, 41,   6, "Production"),
    ("cdn",     "http", "https://cdn.example.net",         "up",        38, 41,   6, "Production"),
    ("staging", "http", "https://staging.example.org",     "down",    None, 24,  128, "Staging"),
    ("mail",    "tcp_port", "mail.example.com",            "up",        18, None, None, "Infra"),
    ("pg",      "tcp_port", "db.example.net",              "up",        11, None, None, "Infra"),
    ("hook",    "cert", "hooks.example.com",               "up",      None,  9,  None, "Production"),
    ("ci",      "http", "https://ci.example.org",          "up",       412, 55,  340, "Dev"),
]

NAMES = {
    "ru": {"shop": "Интернет-магазин", "site": "Главный сайт", "api": "API",
           "account": "Личный кабинет", "blog": "Блог", "partner": "Витрина партнёра",
           "cdn": "CDN", "staging": "Тестовый стенд", "mail": "Почтовый шлюз",
           "pg": "Postgres (внешний)", "hook": "Сертификат вебхука", "ci": "CI"},
    "en": {"shop": "Online store", "site": "Main site", "api": "API",
           "account": "Customer portal", "blog": "Blog", "partner": "Partner storefront",
           "cdn": "CDN", "staging": "Staging", "mail": "Mail gateway",
           "pg": "Postgres (external)", "hook": "Webhook certificate", "ci": "CI"},
}

TEXTS = {
    "ru": {"down": "HTTP 502 — плохой ответ от вышестоящего сервера (Bad Gateway)",
           "slow": "медленный ответ: 2.84 с", "slow_short": "медленный ответ",
           "forbidden": "HTTP 403 — доступ запрещён (Forbidden)", "left": "до {d} дн."},
    "en": {"down": "HTTP 502 — bad response from the upstream server (Bad Gateway)",
           "slow": "slow response: 2.84 s", "slow_short": "slow response",
           "forbidden": "HTTP 403 — access denied (Forbidden)", "left": "{d} days left"},
}

LOCATIONS = {
    "ru": [("Франкфурт", "socks5://198.51.100.21:1080"),
           ("Амстердам", "socks5://198.51.100.34:1080"),
           ("Сингапур",  "socks5://198.51.100.57:1080")],
    "en": [("Frankfurt", "socks5://198.51.100.21:1080"),
           ("Amsterdam", "socks5://198.51.100.34:1080"),
           ("Singapore", "socks5://198.51.100.57:1080")],
}

NAME = NAMES[LANG]
TXT = TEXTS[LANG]


async def main() -> None:
    settings = get_settings()
    engine, factory = create_engine_and_factory(settings.db_url)
    # Схему поднимаем из моделей, а не Alembic'ом: демо-база одноразовая, а часть
    # миграций написана под PostgreSQL и на SQLite не пройдёт.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_admin(factory, settings)
    async with factory() as s:
        # локации
        locs = []
        for name, url in LOCATIONS[LANG]:
            loc = Location(name=name, url=url, enabled=True, created_at=NOW - timedelta(days=90))
            s.add(loc)
            locs.append(loc)
        await s.flush()

        # ноды + сутки метрик
        for node in NODES:
            name, group, osname, cores, ram_gb, cpu_base, disks, role = node
            srv = Server(
                name=name, group_name=group, enabled=True, hostname=name,
                os=osname, agent_version="1.97",
                token_hash=hashlib.sha256(f"demo-{name}".encode()).hexdigest(),
                local_ip=f"10.0.{NODES.index(node)}.10",
                external_ip=f"203.0.113.{10 + NODES.index(node)}",
                last_report=report(node, 0), last_seen=NOW - timedelta(seconds=rnd.randint(3, 40)),
                metric_written_at=NOW,
                # «дампы СУБД настроены отдельно» — иначе панель справедливо
                # напоминает про них на каждой ноде с базой или redis-контейнером
                db_dumps_ok=True,
            )
            s.add(srv)
            await s.flush()
            rows = []
            for i in range(1440):  # сутки, шаг 1 минута
                t = 1440 - i
                ts = NOW - timedelta(minutes=t)
                cpu = round(wave(t, cpu_base, 14), 1)
                worst = max(pct for _, pct, _ in disks)
                rows.append(ServerMetric(
                    server_id=srv.id, ts=ts, cpu_percent=cpu,
                    mem_percent=round(100 * wave(t, 0.54, 0.08, 720), 1),
                    disk_percent=float(worst), load1=round(cores * cpu / 100 * 1.1, 2),
                    cpu_user=round(cpu * 0.62, 1), cpu_system=round(cpu * 0.24, 1),
                    cpu_iowait=round(cpu * 0.09, 1), cpu_irq=round(cpu * 0.05, 1),
                    cpu_freq=round(2400 + rnd.uniform(-120, 900)), cpu_temp=round(38 + cpu * 0.34, 1),
                    cpu_throttle=0, oom_kill=0,
                    net_rx=round(wave(t, 12_400_000, 6_000_000)),
                    net_tx=round(wave(t, 8_100_000, 4_400_000)),
                    disk_read=round(wave(t, 4_200_000, 2_600_000)),
                    disk_write=round(wave(t, 6_800_000, 3_100_000)),
                    disk_read_iops=round(wave(t, 210, 140)), disk_write_iops=round(wave(t, 380, 190)),
                    mem_cache=round(21.0, 1), mem_free=round(100 - 21 - 54.0, 1),
                    swap_in=0.0, swap_out=0.0,
                    disks=[{"mount": m, "pct": float(pct)} for m, pct, _ in disks],
                    net_ifaces=[{"if": "eth0", "rx": round(wave(t, 12_400_000, 6_000_000)),
                                 "tx": round(wave(t, 8_100_000, 4_400_000))}],
                    disk_devs=[{"dev": "nvme0n1", "util": round(wave(t, 22, 15), 1),
                                "await": round(wave(t, 1.4, 0.9), 2), "temp": 36}],
                    conntrack_count=round(wave(t, 18_400, 9_000)), conntrack_max=262_144,
                    sock_used=round(wave(t, 1240, 400)), sock_tcp=round(wave(t, 890, 300)),
                    sock_tcp_tw=round(wave(t, 310, 180)), sock_udp=24,
                    cpu_cores_pct=[round(max(0.4, cpu + rnd.uniform(-18, 18)), 1)
                                   for _ in range(min(cores, 16))],
                ))
            s.add_all(rows)
            await s.flush()

        # мониторы + неделя истории
        for order, (key, typ, target, status, ms, ssl_days, dom_days, group) in enumerate(CHECKS):
            name = NAME[key]
            chk = Check(
                name=name, type=typ, target=target, group_name=group, enabled=True,
                sort_order=order, interval_seconds=60,
                port=(25 if "mail" in target else 5432) if typ == "tcp_port" else 0,
                check_ssl=typ != "tcp_port", check_domain=typ == "http",
                ssl_days=ssl_days, domain_days=dom_days,
                ssl_message="" if ssl_days is None else TXT["left"].format(d=ssl_days),
                last_status=status, last_latency_ms=ms,
                last_message="" if status == "up" else (
                    TXT["down"] if status == "down" else TXT["slow"]),
                last_checked_at=NOW - timedelta(seconds=rnd.randint(4, 55)),
                consecutive_fails=4 if status == "down" else 0,
                expiry_checked_at=NOW - timedelta(hours=3),
            )
            s.add(chk)
            await s.flush()

            rows, cur = [], None
            for i in range(2016):  # 7 дней, шаг 5 минут
                t = (2016 - i) * 5
                ts = NOW - timedelta(minutes=t)
                st, lat = "up", ms
                if ms:
                    lat = int(wave(t, ms, ms * 0.35))
                # редкие всплески: график должен показывать жизнь, а не линейку
                if status == "down" and t < 55:
                    st, lat = "down", None
                elif status == "degraded" and t < 180:
                    st, lat = "degraded", int(wave(t, 2800, 500))
                elif rnd.random() < 0.004:
                    st, lat = "degraded", int((ms or 200) * 4.2)
                rows.append(CheckSample(check_id=chk.id, ts=ts, status=st, latency_ms=lat,
                                        message="" if st == "up" else TXT["slow_short"]))
                if st != "up" and cur is None:
                    cur = CheckIncident(
                        check_id=chk.id, status=st, started_at=ts,
                        last_message=TXT["slow_short"] if st == "degraded" else TXT["down"],
                        notified=True)
                elif st == "up" and cur is not None:
                    cur.ended_at = ts
                    s.add(cur)
                    cur = None
            if cur is not None:      # инцидент, который идёт прямо сейчас
                s.add(cur)
            s.add_all(rows)

            # Тайм-серия по локациям — без неё график на детали монитора пуст:
            # когда локация выбрана, панель рисует именно её ряд, а не общий.
            for j, loc in enumerate(locs):
                base = (ms or 120) + j * 37
                s.add_all([
                    LocationSample(
                        check_id=chk.id, location_id=loc.id,
                        ts=NOW - timedelta(minutes=(288 - i) * 5),
                        status="up", latency_ms=int(wave((288 - i) * 5, base, base * 0.3)))
                    for i in range(288)      # сутки, шаг 5 минут
                ])

            # результаты по локациям: у «Витрины партнёра» две точки видят сайт, одна — нет
            for j, loc in enumerate(locs):
                lst = chk.last_status
                msg = chk.last_message
                if key == "partner" and j == 2:
                    lst, msg = "down", TXT["forbidden"]
                s.add(LocationResult(check_id=chk.id, location_id=loc.id, status=lst,
                                     latency_ms=(ms or 120) + j * 37 if lst != "down" else None,
                                     message="" if lst == "up" else msg,
                                     consecutive_fails=0 if lst == "up" else 3,
                                     checked_at=NOW - timedelta(seconds=rnd.randint(10, 90))))
            if key == "partner":
                chk.loc_alerted = [locs[2].id]

        await s.commit()
    await engine.dispose()
    print(f"демо-данные ({LANG}): {len(NODES)} нод, {len(CHECKS)} мониторов, "
          f"{len(LOCATIONS[LANG])} локации")


if __name__ == "__main__":
    asyncio.run(main())
