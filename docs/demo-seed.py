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
from app import collector, disk_wear  # noqa: E402
from app.models import (  # noqa: E402
    AlertEvent, Check, CheckIncident, CheckSample, Location, LocationResult, LocationSample,
    Server, ServerMetric, ServerPackages, ServerUsage, VulnFeed, WebErrorSample,
)
from app.setup_scripts import current_setup_versions  # noqa: E402

rnd = random.Random(20260823)  # фиксированное зерно: скрины воспроизводимы
NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
T = int(NOW.timestamp())
DAY = 86400
GB = 1024 ** 3
# версия агента из agent/main.go: ниже нее панель прячет кнопки и часть проверок
AGENT = "2.38"
# Версии helper'ов берем из раздаваемых скриптов (make-shots.py задает KERVAX_AGENT_DIST_DIR),
# иначе после каждого бампа демо-ноды снова стали бы "устаревшими" на всех кадрах
HELPERS = current_setup_versions() or {
    "backup-setup": "0.37", "kube-setup": "0.14", "webserver-setup": "0.26", "dbstat-setup": "0.13",
    "agent-watchdog": "1.0", "timesync-setup": "0.2", "backupserver-setup": "0.32",
    "diskhealth-setup": "0.2", "diskusage-setup": "0.6", "units-setup": "0.4", "gpu-setup": "0.4"}


def iso(ago: timedelta) -> str:
    return (NOW - ago).isoformat()


def wave(t_min: float, base: float, amp: float, period: float = 1440.0) -> float:
    """Суточная синусоида + шум: ровная линия на графике выглядит как заглушка."""
    v = base + amp * math.sin(2 * math.pi * (t_min % period) / period) + rnd.uniform(-amp / 4, amp / 4)
    return max(0.3, v)


# ── ноды ──────────────────────────────────────────────────────────────────────
# (имя, группа, ОС, ядер, ОЗУ ГБ, база CPU%, диски, роль)
NODES = [
    ("web-01",      "Production", "Ubuntu 24.04.1 LTS", 8,  16, 34, [("/", 42, 100), ("/var/log", 61, 20)], "web"),
    ("web-02",      "Production", "Ubuntu 24.04.1 LTS", 8,  16, 29, [("/", 38, 100), ("/var/log", 55, 20)], "web"),
    ("db-01",       "Production", "Debian 12 (bookworm)", 16, 64, 46, [("/", 86, 1800)], "db"),
    ("cache-01",    "Production", "Debian 12 (bookworm)", 4,  32, 18, [("/", 24, 40)], "cache"),
    ("app-01",      "Production", "Rocky Linux 9.4",     8,  32, 52, [("/", 47, 120)], "app"),
    ("k8s-node-1",  "Staging",    "Ubuntu 22.04.5 LTS",  16, 32, 61, [("/", 66, 200)], "kube"),
    ("backup-01",   "Infra",      "Debian 12 (bookworm)", 4, 8,  12, [("/", 22, 40), ("/srv/restic", 88, 4000)], "backup"),
    ("ci-runner-1", "Dev",        "Ubuntu 24.04.1 LTS",  8,  16, 71, [("/", 58, 200)], "ci"),
    ("gpu-01",      "Production", "Ubuntu 24.04.1 LTS", 64, 512, 38, [("/", 36, 200), ("/models", 71, 3500)], "gpu"),
]

TOP_CPU = {
    "web": [("nginx", 14.2, 268), ("php-fpm", 9.7, 512), ("node", 6.1, 331)],
    "db": [("postgres", 38.4, 8140), ("postgres", 12.9, 3320), ("pgbouncer", 2.2, 96)],
    "cache": [("redis-server", 11.8, 2210), ("keydb", 3.1, 410), ("sshd", 0.4, 12)],
    "app": [("java", 41.6, 6120), ("python3", 8.3, 740), ("supervisord", 0.7, 28)],
    "kube": [("kubelet", 17.4, 812), ("containerd", 12.1, 640), ("coredns", 3.3, 74)],
    "backup": [("restic", 22.9, 1480), ("rest-server", 1.8, 64), ("sshd", 0.3, 11)],
    "ci": [("buildkitd", 46.2, 3980), ("dockerd", 14.7, 720), ("git", 5.4, 210)],
    "gpu": [("python3", 96.4, 18240), ("text-embeddings", 22.1, 4100), ("litellm", 4.2, 610)],
}

WEB_SITES = {
    "web-01": ["example.com", "www.example.com", "shop.example.com", "api.example.com",
               "blog.example.com", "status.example.com"],
    "web-02": ["example.net", "cdn.example.net", "media.example.net"],
}

# инвентарь dbstat-setup: engine pg/mysql/clickhouse/redis, у redis в size число ключей
DB_STATS = {
    "db-01": [{"engine": "pg", "container": "", "version": "16.4", "port": 5432,
               "dbs": [{"name": "shop", "size": 912_000_000_000},
                       {"name": "billing", "size": 214_000_000_000},
                       {"name": "analytics", "size": 286_000_000_000}],
               "users": ["shop_app", "billing_app", "report_ro"], "conn_used": 142, "conn_max": 300}],
    "cache-01": [{"engine": "redis", "container": "", "version": "7.2.5", "port": 6379,
                  "dbs": [{"name": "db0", "size": 1_840_000}, {"name": "db1", "size": 212_000}],
                  "users": [], "conn_used": 310, "conn_max": 10000}],
}
DB_ENGINES = {"db": ["PostgreSQL"], "cache": ["Redis"]}


def report(node, t_min: float) -> dict:
    name, group, osname, cores, ram_gb, cpu_base, disks, role = node
    cpu = round(wave(t_min, cpu_base, 14), 1)
    mem_total = ram_gb * GB
    mem_used = int(mem_total * wave(t_min, 0.54, 0.08, 720))
    rep = {
        "hostname": name, "os": osname, "agent_version": AGENT,
        "cpu_model": "AMD EPYC 9354P" if cores >= 16 else "Intel Xeon E-2388G",
        "is_vm": role not in ("db", "gpu"), "virt": "kvm" if role not in ("db", "gpu") else "",
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
        "disks": [{"mount": m, "used": int(total * GB * pct / 100), "total": total * GB,
                   "avail": int(total * GB * (100 - pct) / 100),
                   "inodes": total * 65536, "inodes_used": int(total * 65536 * pct / 400)}
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
        "setup_versions": {**HELPERS, **({"backup-setup": "0.19"} if role == "ci" else {})},
        "checks": [{"key": "ssh", "type": "port", "status": "ok", "message": "22/tcp"},
                   {"key": "ntp", "type": "clock", "status": "ok", "message": "synced"}],
        "db_engines": DB_ENGINES.get(role, []),
        "db_stats": DB_STATS.get(name, []),
        "services": [],
    }
    if name in WEB_SITES:
        rep["web_services"] = [{"kind": "nginx", "version": "1.30.0", "source": "host",
                                "sites": WEB_SITES[name]}]
    if role in ("web", "app", "ci"):
        rep["docker"] = {"present": True, "access": True, "version": "28.5.1", "compose": "2.40.3",
                         # Поля ровно те, что шлет агент: name/image/state/status/policy/
                         # restarts/binds, cpu (% одного ядра, агент 2.21) и mem (байты, 2.14)
                         "containers": [
                             {"name": f"{name}-app-1", "image": "app:2.14.0", "state": "running",
                              "status": "Up 6 days", "policy": "always", "restarts": 0,
                              "binds": ["/srv/app/data"], "cpu": round(wave(t_min, 24, 9), 1),
                              "mem": 1_340_000_000},
                             {"name": f"{name}-worker-1", "image": "app:2.14.0", "state": "running",
                              "status": "Up 6 days", "policy": "always", "restarts": 2,
                              "binds": [], "cpu": round(wave(t_min, 11, 5), 1), "mem": 610_000_000},
                             # без СУБД в контейнерах: панель справедливо спросила бы
                             # про инвентарь и дампы, а в демо отвечать на это нечем
                             {"name": f"{name}-nginx-1", "image": "nginx:1.30-alpine",
                              "state": "running", "status": "Up 21 days",
                              "policy": "unless-stopped", "restarts": 0, "binds": [],
                              "cpu": round(wave(t_min, 3, 1.5), 1), "mem": 48_000_000}]}
    if role == "gpu":
        rep["docker"] = {"present": True, "access": True, "version": "28.5.1", "compose": "2.40.3",
                         "containers": [
                             {"name": "llm-vllm-1", "image": "vllm/vllm-openai:v0.11.0",
                              "state": "running", "status": "Up 3 days", "policy": "always",
                              "restarts": 0, "binds": ["/models"], "cpu": 96.4, "mem": 19_100_000_000},
                             {"name": "llm-embed-1",
                              "image": "ghcr.io/huggingface/text-embeddings-inference:1.8",
                              "state": "running", "status": "Up 3 days", "policy": "always",
                              "restarts": 0, "binds": ["/models"], "cpu": 22.1, "mem": 4_300_000_000},
                             {"name": "llm-gateway-1", "image": "ghcr.io/berriai/litellm:v1.77.5",
                              "state": "running", "status": "Up 12 days", "policy": "always",
                              "restarts": 0, "binds": [], "cpu": 4.2, "mem": 640_000_000}]}
    if role == "kube":
        rep["kube"] = kube_block()
        rep["kube_expiry"] = [
            {"kind": "flux-token", "where": "flux-system/flux-system", "expires": T + 5 * DAY, "note": "password"},
            {"kind": "cluster-cert", "where": "/var/lib/k0s/pki/apiserver.crt", "expires": T + 38 * DAY,
             "note": "apiserver"},
            {"kind": "kubeconfig", "where": "/var/lib/k0s/pki/admin.conf", "expires": T + 38 * DAY},
            {"kind": "secret-cert", "where": "shop/shop-tls", "expires": T + 61 * DAY, "note": "tls.crt"},
            {"kind": "kubelet-cert", "where": "/var/lib/k0s/kubelet/pki/kubelet-client-current.pem",
             "expires": T + 290 * DAY}]
        rep["flux"] = [
            {"kind": "GitRepository", "where": "flux-system/flux-system", "ready": True},
            {"kind": "Kustomization", "where": "flux-system/apps", "ready": True},
            {"kind": "HelmRelease", "where": "shop/shop-api", "ready": False, "reason": "UpgradeFailed",
             "message": "Helm upgrade failed: context deadline exceeded"}]
    if role == "backup":
        rep["backup_server"] = backup_server_block()
    # Ночное окно: бэкап, закончившийся после 8 утра, панель штатно помечает как
    # выбившийся из окна — в демо это была бы плашка на весь экран.
    done = NOW.replace(hour=3, minute=40 - NODES.index(node), second=0, microsecond=0)
    if done > NOW:
        done -= timedelta(days=1)
    dur = rnd.randint(120, 900)
    rep["backup"] = {"present": True, "restic_found": True, "configured": True, "metric_present": True,
                     "success": 1, "timer_enabled": True, "timer_active": True,
                     "service_result": "success",
                     "last_backup_ts": int(done.timestamp()),
                     "started_ts": int((done - timedelta(seconds=dur + 60)).timestamp()),
                     "duration_sec": dur, "full_duration_sec": dur + 60,
                     "restic_version": "restic 0.19.1 compiled with go1.24.4 on linux/amd64",
                     "manageable": True, "mode": "include", "schedule": "03:20",
                     "includes": ["/etc", "/srv", "/var/lib"], "excludes": ["/var/lib/docker"],
                     "repo_dest": f"rest:https://203.0.113.16:64101/{name}", "helper_version": 1}
    if role == "backup":
        rep["backup"]["includes"] = ["/etc"]
    enrich(rep, name, role)
    return rep


def kube_block() -> dict:
    gi, mi = GB, 1024 ** 2

    def pod(ns, name, node="k8s-node-1", ctrl="", ready=True, restarts=0, cpu=120, mem=300, **kw):
        return {"ns": ns, "name": name, "phase": kw.pop("phase", "Running"), "ready": ready,
                "restarts": restarts, "node": node, "owner": kw.pop("owner", "ReplicaSet"), "ctrl": ctrl,
                "use": {"cpu_m": cpu, "mem": mem * mi}, **kw}
    return {
        "present": True, "access": True, "flavor": "k0s", "version": "v1.33.4", "namespaces": 7,
        "nodes": [{"name": "k8s-node-1", "ready": True, "roles": "control-plane", "version": "v1.33.4",
                   "ip": "10.0.5.10", "use": {"cpu_m": 5200, "mem": 21 * gi}, "cap": {"cpu_m": 16000, "mem": 32 * gi}},
                  {"name": "k8s-node-2", "ready": True, "roles": "worker", "version": "v1.33.4",
                   "ip": "10.0.5.11", "use": {"cpu_m": 3100, "mem": 11 * gi}, "cap": {"cpu_m": 8000, "mem": 16 * gi}}],
        "workloads": [{"ns": "shop", "kind": "Deployment", "name": "web", "ready": 3, "desired": 3},
                      {"ns": "shop", "kind": "Deployment", "name": "api", "ready": 2, "desired": 2},
                      {"ns": "shop", "kind": "Deployment", "name": "worker", "ready": 1, "desired": 2},
                      {"ns": "shop", "kind": "StatefulSet", "name": "redis", "ready": 1, "desired": 1},
                      {"ns": "monitoring", "kind": "DaemonSet", "name": "promtail", "ready": 2, "desired": 2}],
        "pods": [pod("shop", "web-7d9c8b6f5-2xkqz", ctrl="deployment/web", cpu=240, mem=410),
                 pod("shop", "web-7d9c8b6f5-8hjwd", node="k8s-node-2", ctrl="deployment/web", cpu=225, mem=398),
                 pod("shop", "web-7d9c8b6f5-p4lzn", ctrl="deployment/web", cpu=231, mem=405),
                 pod("shop", "api-6f4d8c7b9-tq5rx", ctrl="deployment/api", cpu=180, mem=520),
                 pod("shop", "api-6f4d8c7b9-zm2kv", node="k8s-node-2", ctrl="deployment/api", cpu=171, mem=508),
                 pod("shop", "worker-5f8b9c7d6-mn4pl", node="k8s-node-2", ctrl="deployment/worker", ready=False,
                     restarts=9, cpu=0, mem=0, reason="CrashLoopBackOff", nolim=True),
                 pod("shop", "worker-5f8b9c7d6-r7c2b", ctrl="deployment/worker", cpu=310, mem=640),
                 pod("shop", "redis-0", ctrl="statefulset/redis", owner="StatefulSet", cpu=40, mem=890),
                 pod("monitoring", "promtail-x8k2d", ctrl="daemonset/promtail", owner="DaemonSet", cpu=30, mem=120),
                 pod("monitoring", "promtail-b5n7q", node="k8s-node-2", ctrl="daemonset/promtail",
                     owner="DaemonSet", cpu=28, mem=115),
                 pod("kube-system", "coredns-6c8b5d9f7-qw2rt", ctrl="deployment/coredns", cpu=6, mem=34)],
        "cronjobs": [{"ns": "shop", "name": "sitemap", "schedule": "0 * * * *", "image": "shop-app:2.14",
                      "last_schedule": T - 1200, "last_success": T - 1200}],
        "ingress": [{"ns": "shop", "hosts": ["shop.example.org"]}],
    }


def backup_server_block() -> dict:
    def repo(name, size_gb, snaps, ago_h, **kw):
        return {"name": name, "valid": True, "size_bytes": size_gb * 10 ** 9, "snapshots": snaps,
                "last_activity": T - int(ago_h * 3600), "oldest_snapshot": T - 180 * DAY,
                "keep_daily": 7, "keep_weekly": 4, "keep_monthly": 6, "rotation_ts": T - 5 * 3600,
                "rotation_ok": 1, "rotation_removed": 1, **kw}
    return {"present": True, "running": True, "version": "0.14.0", "helper_version": 1, "tls_front": True,
            "tls_port": 64101, "port": 64100, "data_dir": "/srv/restic", "disk_total": 4000 * GB,
            "disk_used": 3520 * GB, "disk_free": 480 * GB,
            "repos": [repo("web-01", 214, 17, 6), repo("web-02", 96, 17, 6.1), repo("db-01", 812, 17, 5),
                      repo("cache-01", 18, 17, 6.3), repo("app-01", 143, 17, 6.2), repo("k8s-node-1", 61, 17, 6.4),
                      repo("ci-runner-1", 12, 17, 6.5), repo("gpu-01", 9, 17, 6.6),
                      repo("backup-01", 1, 17, 6.7)]}


# ── безопасность, диски, GPU ─────────────────────────────────────────────────
# Ради этого кадры и снимаются: web-02 собран "как бывает" (Redis наружу, вход по паролю,
# перебор паролей без fail2ban, забытая учетка со слабым ключом), db-01 - железный сервер с
# изношенными SSD, gpu-01 - инференс. Остальные ноды настроены прилично, чтобы было видно,
# как выглядит норма. Форматы - как у агента и helper'ов, см. backend/tests/test_*.py

EXT_IP = {n[0]: f"203.0.113.{10 + i}" for i, n in enumerate(NODES)}

# Только вымышленные CVE (CVE-2026-91xxx): ссылки на ubuntu.com по ним ведут в 404, зато
# никто не примет кадр за отчет о настоящей уязвимости
CVE = {
    "ossl": ["CVE-2026-91012", "critical", 9.8], "ossl2": ["CVE-2026-91047", "medium", 6.5],
    "git": ["CVE-2026-91233", "critical", 9.1], "sudo": ["CVE-2026-91356", "high", 7.8],
    "k1": ["CVE-2026-91402", "critical", 9.4], "k2": ["CVE-2026-91418", "high", 7.8],
}
VULN_FEED = {
    "gen": "2026-10-18T06:12:40",
    "kev": [CVE["ossl2"][0], CVE["sudo"][0], CVE["k2"][0]],
    "pkgs": {
        "openssl": [["openssl", "3.0.13-0ubuntu3.10", [CVE["ossl"], CVE["ossl2"]]]],
        "libssl3t64": [["openssl", "3.0.13-0ubuntu3.10", [CVE["ossl"], CVE["ossl2"]]]],
        # +esm: исправление только в Ubuntu Pro, панель так и пишет
        "git": [["git", "1:2.43.0-1ubuntu7.3+esm1", [CVE["git"]]]],
        "sudo": [["sudo", "1.9.15p5-3ubuntu5.24.04.2", [CVE["sudo"]]]],
    },
    "kernel": [["linux", r"6\.8\.0\-\d+(\-generic|\-generic\-64k)", "6.8.0-147", [CVE["k1"], CVE["k2"]]]],
}
OLD = {"openssl": "3.0.13-0ubuntu3.4", "libssl3t64": "3.0.13-0ubuntu3.4",
       "git": "1:2.43.0-1ubuntu7.2", "sudo": "1.9.15p5-3ubuntu5.24.04.1"}
NEW = {"openssl": "3.0.13-0ubuntu3.10", "libssl3t64": "3.0.13-0ubuntu3.10",
       "git": "1:2.43.0-1ubuntu7.3+esm1", "sudo": "1.9.15p5-3ubuntu5.24.04.2"}
# что helper sockfix-setup меняет в compose прокси (так он пишет разницу в отчет)
SOCKFIX_DIFF = r"""--- docker-compose.yml
+++ docker-compose.yml (fixed)
@@ -8,10 +8,32 @@
     environment:
       - CADDY_INGRESS_NETWORKS=caddy
+      - DOCKER_HOST=tcp://docker-api:2375
     networks:
       - caddy
+      - docker-api
     volumes:
-      - /var/run/docker.sock:/var/run/docker.sock
       - ./data:/data
+  docker-api:
+    # read-only docker API for caddy: the proxy facing the internet no longer holds the socket
+    image: wollomatic/socket-proxy:1.13.1
+    restart: unless-stopped
+    user: "65534:989"   # gid of /var/run/docker.sock
+    read_only: true
+    mem_limit: 64M
+    cap_drop: [ALL]
+    security_opt: ["no-new-privileges:true"]
+    volumes:
+      - /var/run/docker.sock:/var/run/docker.sock:ro
+    networks: [docker-api]
+    command:
+      - -listenip=0.0.0.0
+      - -allowfrom=caddy
+      - -watchdoginterval=600
+      - -stoponwatchdog
+      - -allowHEAD=/_ping
+      - -allowGET=/(v[0-9]+\.[0-9]+/)?(_ping|version|info|events|containers/json|networks(/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?)
 networks:
   caddy:
     external: true
+  docker-api:
+    internal: true"""
# какие пакеты на какой ноде не обновлены, и ядро
VULN_NODES = {
    "web-01": ({"openssl", "libssl3t64"}, "6.8.0-149-generic"),
    "web-02": ({"openssl", "libssl3t64", "git", "sudo"}, "6.8.0-45-generic"),
    "ci-runner-1": ({"git"}, "6.8.0-149-generic"),
    "gpu-01": (set(), "6.8.0-45-generic"),
    "k8s-node-1": (set(), "5.15.0-126-generic"),
}

CHANGES_TEXT = {
    "ru": {"key": "добавлен SSH-ключ пользователю deploy: ED25519 SHA256:pX2c7tQm9Lr4WdV0aK8sYh3NbE6uJf1oZiC5gR7nT0k (deploy@laptop)",
           "login": "вход по паролю: alice с 198.51.100.44, раньше с этого адреса не входил",
           "sudo": "изменен /etc/sudoers.d/90-deploy",
           "cron": "добавлен крон /etc/cron.d/cleanup",
           "unit": "включен certbot.timer"},
    "en": {"key": "SSH key added for deploy: ED25519 SHA256:pX2c7tQm9Lr4WdV0aK8sYh3NbE6uJf1oZiC5gR7nT0k (deploy@laptop)",
           "login": "password login: alice from 198.51.100.44, never logged in from this address before",
           "sudo": "/etc/sudoers.d/90-deploy changed",
           "cron": "cron added /etc/cron.d/cleanup",
           "unit": "certbot.timer enabled"},
}


def _access(insecure: bool) -> dict:
    """extras.changes от changes-setup: SSH, учетки, ключи, sudo, порты"""
    users = [{"n": "root", "uid": 0, "sh": "/bin/bash", "pw": "L", "last": T - 9 * DAY},
             {"n": "deploy", "uid": 1000, "sh": "/bin/bash", "pw": "L", "last": T - 5 * 3600}]
    keys = [{"u": "root", "t": "ED25519", "b": 256, "fp": "SHA256:Hq8vN2kD5sLw0cR7yT4mB1xF9eJ3uA6gZpK2oV8iC4n",
             "c": "ops@workstation", "f": "/root/.ssh/authorized_keys"},
            {"u": "deploy", "t": "ED25519", "b": 256, "fp": "SHA256:Wm3rT8bY1nQ6vK0cX5fJ2hL9dS4pA7gE1uZ3oI6kN8q",
             "c": "deploy@ci", "f": "/home/deploy/.ssh/authorized_keys"}]
    sudo = {"groups": {"sudo": ["deploy"]}, "files": {"/etc/sudoers": "a1"}, "nopasswd": []}
    ssh = {"pass": "no", "kbd": "no", "pam": "yes", "methods": "publickey", "root": "prohibit-password",
           "rootpass": "no", "rootkbd": "no", "ports": [22], "fail": 3, "accp": 14, "guard": "fail2ban",
           "fail_top": []}
    if insecure:
        users += [{"n": "alice", "uid": 1001, "sh": "/bin/bash", "pw": "P", "last": T - 400 * DAY},
                  {"n": "ci", "uid": 1002, "sh": "/bin/bash", "pw": "L", "last": 0}]
        keys += [{"u": "deploy", "t": "ED25519", "b": 256, "fp": "SHA256:pX2c7tQm9Lr4WdV0aK8sYh3NbE6uJf1oZiC5gR7nT0k",
                  "c": "deploy@laptop", "f": "/home/deploy/.ssh/authorized_keys"},
                 {"u": "alice", "t": "RSA", "b": 1024, "fp": "SHA256:b7Kd2Nq0vT5yL8wR3xC6mF1hJ4sP9gA2eU7oZ5iB3nV",
                  "c": "old laptop", "f": "/home/alice/.ssh/authorized_keys"},
                 {"u": "ci", "t": "ED25519", "b": 256, "fp": "SHA256:Ys5fL1cQ8nW3vR6kT0bM9xD2hJ7pA4gE5uZ1oN8iK3r",
                  "c": "", "f": "/home/ci/.ssh/authorized_keys"}]
        sudo = {"groups": {"sudo": ["alice", "deploy"]},
                "files": {"/etc/sudoers": "a1", "/etc/sudoers.d/90-deploy": "c7"}, "nopasswd": ["%sudo"]}
        ssh = {"pass": "yes", "kbd": "no", "pam": "yes", "methods": "any", "root": "prohibit-password",
               "rootpass": "no", "rootkbd": "no", "ports": [22], "fail": 2140, "accp": 9, "guard": "",
               "fail_top": [{"ip": "198.51.100.77", "n": 1310}, {"ip": "203.0.113.200", "n": 512},
                            {"ip": "198.51.100.143", "n": 318}]}
    return {"v": 1, "ts": T - 120, "seen_since": T - 400 * DAY, "users": users, "keys": keys,
            "sudo": sudo, "ssh": ssh}


# что слушает каждая роль наружу: (порт, процесс, пользователь)
LISTEN = {
    "web": [(22, "sshd", "root"), (80, "nginx", "root"), (443, "nginx", "root"),
            (9100, "node_exporter", "nobody")],
    "db": [(22, "sshd", "root"), (9100, "node_exporter", "nobody")],
    "gpu": [(22, "sshd", "root"), (443, "caddy", "root")],
}


def enrich(rep: dict, name: str, role: str) -> None:
    ip = EXT_IP[name]
    insecure = name == "web-02"
    ch = _access(insecure)
    listen = LISTEN.get(role, [(22, "sshd", "root")])
    if insecure:
        listen = listen + [(6379, "redis-server", "redis")]
    rep["ips"] = [ip]
    rep["listen"] = [{"p": "tcp", "ip": "0.0.0.0", "port": p, "u": u} for p, _, u in listen]
    ch["listen"] = {str(p): proc for p, proc, _ in listen}
    # node_exporter слушает 0.0.0.0, но фаервол пускает к нему только сервер мониторинга:
    # снаружи порт закрыт, и панель пишет "только по белому списку"
    ch["fw"] = {"src": "nftables", "p4": "DROP", "p6": "DROP", "idrop": "", "fdrop": "",
                "in": ["4 198.51.100.60/32 9100"], "fwd": []}
    ex = {"units": {"v": 1, "ts": T - 30, "fix": True, "units": [], "restarting": [], "noboot": []},
          "changes": ch}
    vm = rep["is_vm"]
    ex["disk-health"] = {"v": 1, "ts": T - 60, "virt": "kvm" if vm else "none", "container": False,
                         "smart": not vm, "raid": [], "disks": [], "missing": [],
                         "io": {"count": 0, "last": []}}
    root = rep["disks"][0]
    ex["disk-usage"] = {"v": 1, "ts": T - 300, "fix": {"v": 1, "allow": ["journal", "rotated-logs", "apt-cache"]},
                        "fs": [{"mount": root["mount"], "size": root["total"], "used": root["used"],
                                "avail": root["avail"], "pct": round(100 * root["used"] / root["total"]),
                                "inodes": root["inodes"], "top_state": "ok", "top_ts": T - 300,
                                "top": [{"path": "/var", "bytes": int(root["used"] * 0.6)},
                                        {"path": "/usr", "bytes": int(root["used"] * 0.12)}]}],
                        "items": []}
    if role == "db":
        db_disks(rep, ex)
    if role == "gpu":
        ex["gpu"] = gpu_block()
    rep["extras"] = ex
    services(rep, name, role)
    if name in VULN_NODES:
        stale, kernel = VULN_NODES[name]
        rep["pkgs"] = {"mgr": "dpkg", "n": 612 + len(name), "h": f"demo-{name}"}
        rep["os_upd"] = {"updates": 23 if insecure else 4, "security": 7 if insecure else 0,
                         "kernel": kernel, "reboot": insecure, "reboot_ts": T - 12 * DAY if insecure else 0,
                         "reboot_pkgs": ["linux-image-6.8.0-149-generic", "libc6"] if insecure else [],
                         "unatt": "off" if insecure else "on", "unatt_ts": 0 if insecure else T - 5 * 3600,
                         "apt_ts": T - (20 if insecure else 0) * DAY - 3600, "ts": T - 600}


GI, MI = 1 << 30, 1 << 20

# web-01: shop отдает 502, потому что php-fpm в web-01-app-1 перестал принимать соединения,
# а часть запросов к api упирается в таймаут бэкенда на другом хосте
WEB_UP = [{"u": "172.18.0.5:9000", "k": "refused", "n": 38, "c": "web-01-app-1"},
          {"u": "10.0.4.10:8080", "k": "timeout", "n": 9}]
WEB_LOGS = [
    {"log": "docker:web-01-nginx-1", "name": "web-01-nginx-1", "sites": ["shop.example.com", "api.example.com"],
     "rpm": 11200, "e5": 47, "e4": 140, "un": 0, "c5": {"502": 38, "504": 9},
     "p5": [{"p": "/checkout", "n": 29}, {"p": "/api/v1/cart", "n": 12}, {"p": "/api/v1/orders", "n": 6}],
     "l5": ['203.0.113.40 - - "POST /checkout HTTP/1.1" 502 157 "-" "Mozilla/5.0"'],
     "hosts": [{"h": "shop.example.com", "rpm": 9800, "e5": 38}, {"h": "api.example.com", "rpm": 1400, "e5": 9}],
     "up": WEB_UP},
    {"log": "/var/log/nginx/blog.access.log", "sites": ["blog.example.com"], "rpm": 2100, "e5": 0},
    {"log": "/var/log/nginx/access.log", "sites": ["example.com", "www.example.com", "status.example.com"],
     "rpm": 1200, "e5": 0}]


def _job(cmd, sched, where="host", ages=(), cpu=0, runs=1440, mx=0, done=0, last=60, src="/etc/cron.d/app",
         n=None, **kw):
    run = [{"pid": 4000 + i, "age": a, "cpu": cpu, "procs": 3} for i, a in enumerate(sorted(ages, reverse=True))]
    return {"where": where, "kind": "cron", "user": kw.pop("user", "root"), "sched": sched, "src": src, "cmd": cmd,
            "last": T - last if last else 0, "runs": runs, "max": mx, "done": done, "n": n or len(run),
            "run": run[:5], **kw}


def _prog(name, state="RUNNING", **kw):
    return {"name": name, "state": state, "since": kw.pop("since", T - 6 * DAY),
            "up": 6 * DAY if state in ("RUNNING", "online") else 0, "pid": 41, "exit": kw.pop("exit", 0),
            "r15": kw.pop("r15", 0), "r60": kw.pop("r60", 0), "p60": kw.pop("p60", 0), **kw}


def _lsrc(where, h1, d1, last, span=DAY):
    return {"where": where, "container": "", "h1": h1, "d1": d1, "lines1": h1 * 4, "span": span,
            "last_ts": T - 70, "last": last}


def _pool(where, pool="www", mx=5, ch=3, **kw):
    d = {"where": where, "pool": pool, "listen": "9000", "pm": "dynamic", "max": mx, "children": ch, "conns": 0,
         "queue": 0, "sat": 0, "span": DAY, "reached": 0, "reached_d": 0, "slow": 0, "slow_d": 0, "tout": 0,
         "tout_d": 0, "slowlog": 0, "terminate": 0, "status": 0}
    d.update(kw)
    return d


def services(rep: dict, name: str, role: str) -> None:
    """Сервисы, которые находят агент и helper'ы: веб, базы, очереди, воркеры, кроны, LLM"""
    ex = rep["extras"]
    ex["backup-verify"] = {"v": 1, "ts": T - 20 * 3600, "secs": 14, "snap": "5e288fd8", "snap_ts": T - 22 * 3600,
                           "files": {"ok": 8, "skipped": 1, "bad": []}, "dumps": [], "err": ""}
    if role == "db":
        ex["backup-verify"]["dumps"] = [{"engine": "pg", "file": "/backup/pg/all.sql.gz", "ok": True}]
    ex["backup-runs"] = {"v": 1, "ts": T, "unit": "kervax-backup.service", "runs": [
        {"ts": rep["backup"]["last_backup_ts"] - (14 - i) * DAY, "add": int(2.3 * GI * (1 + (i % 4) / 6)),
         "st": 20 * GI, "proc": 700 * GI, "files": 300000, "fnew": 100 + i * 7, "fchg": 10 + i, "dur": 900}
        for i in range(15)]}
    if name == "web-01":
        ex["web-rate"] = {"ts": T - 30, "rpm": sum(x["rpm"] for x in WEB_LOGS),
                          "e5": sum(x["e5"] for x in WEB_LOGS), "logs": WEB_LOGS}
        ex["web-certs"] = {"v": 1, "ts": T - 600, "renew": "timer", "certs": [
            {"where": "host", "file": "/etc/letsencrypt/live/blog.example.com/fullchain.pem", "end": T + 5 * DAY,
             "cn": "blog.example.com", "names": "blog.example.com"},
            {"where": "host", "file": "/etc/letsencrypt/live/example.com/fullchain.pem", "end": T + 68 * DAY,
             "cn": "example.com", "names": "example.com www.example.com status.example.com"},
            {"where": "web-01-nginx-1", "file": "/etc/nginx/ssl/shop.example.com.pem", "end": T + 48 * DAY,
             "cn": "shop.example.com", "names": "shop.example.com api.example.com"}]}
        ex["web-config"] = {"v": 1, "ts": T - 600, "items": [
            {"server": "nginx", "where": "host", "ok": True, "err": ""},
            {"server": "nginx", "where": "web-01-nginx-1", "ok": True, "err": ""}]}
        ex["fpm"] = {"v": 1, "ts": T - 40, "skipped": [], "pools": [
            _pool("container web-01-app-1", mx=20, ch=12, conns=9, reached=0, reached_d=3, tout=0,
                  tout_d=2, terminate=60, tout_ts=T - 9 * 3600,
                  tout_last="script '/app/web/index.php' (request: \"POST /index.php?r=order/pay\") "
                            "execution timed out (60.12 sec), terminating"),
            _pool("host", pool="blog", mx=20, ch=4, listen="/run/php/php8.3-fpm-blog.sock")]}
    if role == "db":
        ex["db-health"] = {"v": 1, "ts": T - 120, "items": [{
            "engine": "pg", "container": "", "role": "primary", "replicas": 1, "lag": 0, "receiver": "",
            "xact": 4700, "xact_db": "analytics", "xact_app": "report-builder",
            "idle_xact": 0, "idle_xmin": 0, "lock_waits": 0, "lock_wait": 0, "xid": 310_000_000, "xid_db": "shop",
            "dead": [{"db": "shop", "table": "order_events", "dead": 540_000, "live": 9_000_000, "vac": 3600}],
            "load": {"win": 3600, "pss": "on", "qtext": 180_000_000, "cut": False, "temp_b": 14 * GI, "temp_f": 220,
                     "xact": 5_400_000, "rd": 96 * GI, "wal_rec": 41_000_000, "fpi": 2_300_000, "wal_b": 9 * GI,
                     "wbf": 0, "ck_t": 12, "ck_r": 0, "ms_all": 5_200_000, "top": [
                         {"id": "1", "db": "shop", "calls": 182000, "ms": 1_900_000, "temp": 0, "rd": 12 * GI,
                          "q": "SELECT o.* FROM orders o JOIN order_items i ON i.order_id = o.id "
                               "WHERE o.created_at > $1"},
                         {"id": "2", "db": "analytics", "calls": 240, "ms": 1_100_000, "temp": 11 * GI, "rd": 40 * GI,
                          "q": "SELECT date_trunc($1, ts), count(*) FROM events WHERE ts BETWEEN $2 AND $3 "
                               "GROUP BY 1"},
                         {"id": "3", "db": "billing", "calls": 96000, "ms": 410_000, "temp": 0, "rd": 0,
                          "q": "UPDATE invoices SET status = $1 WHERE id = $2"},
                         {"id": "4", "db": "shop", "calls": 1_240_000, "ms": 380_000, "temp": 0, "rd": 2 * GI,
                          "q": "SELECT * FROM products WHERE id = ANY($1)"}]},
            "cfg": {"v": 160004, "sb": 2 * GI, "wb": 16 * MI, "mws": 4 * GI, "wm": 4 * MI, "mwm": 256 * MI,
                    "ecs": 4 * GI, "ckt": 300, "cct": 0.9, "mc": 300, "walc": "pglz", "hp": "try", "hps": "off",
                    "hpn": 0, "rpc": 4, "mem": 64 * GI, "mem_lim": False, "hpt": 0, "hpsz": 2 * MI, "pt": 0,
                    "stop_to": -1, "stop_sig": "SIGINT"}}]}
    if role == "cache":
        ex["db-health"] = {"v": 1, "ts": T - 120, "items": [{
            "engine": "redis", "container": "", "role": "primary", "replicas": 0, "lag": 0, "link": "", "down": 0,
            "mem": int(0.92 * 8 * GI), "maxmem": 8 * GI, "policy": "noeviction", "rdb": "ok", "rdb_changes": 2,
            "rdb_age": 600, "aof": 0, "aof_status": "ok", "stop_writes": "yes", "win": 900, "evicted": 0,
            "rejected": 0, "cmds": [{"c": "get", "n": 912000, "us": 1_300_000}, {"c": "hgetall", "n": 88000,
                                    "us": 2_900_000}, {"c": "zrangebyscore", "n": 4100, "us": 1_100_000}]}]}
        ex["queues"] = {"v": 1, "ts": T - 60, "skipped": [], "brokers": [
            {"system": "sidekiq", "where": "host", "db": 0, "ns": "", "total": 2,
             "queues": [{"name": "default", "ready": 14}, {"name": "mailers", "ready": 4}]},
            {"system": "celery", "where": "host", "db": 1, "ns": "", "total": 2,
             "queues": [{"name": "document_indexing", "ready": 35}, {"name": "celery", "ready": 0}]}]}
    if role == "app":
        rep["docker"]["containers"].append(
            {"name": "app-01-rabbitmq-1", "image": "rabbitmq:4.3-management", "state": "running",
             "status": "Up 21 days", "policy": "unless-stopped", "restarts": 0, "binds": ["/srv/rabbitmq"],
             "cpu": 6.2, "mem": 410_000_000})
        rep["services"] = [{"kind": "rabbitmq", "source": "контейнер app-01-rabbitmq-1", "total": 4, "queues": [
            {"name": "orders", "vhost": "/", "ready": 2140, "unacked": 60, "consumers": 0},
            {"name": "mail", "vhost": "/", "ready": 3, "unacked": 1, "consumers": 2},
            {"name": "orders.dlq", "vhost": "/", "ready": 412, "consumers": 0},
            {"name": "search-index", "vhost": "/", "ready": 180, "unacked": 20, "consumers": 4}]}]
        ex["workers"] = {"v": 1, "ts": T - 40, "skipped": [], "daemons": [
            {"kind": "supervisor", "where": "container app-01-worker-1", "progs": [
                _prog("queue-listen:queue-listen_00", p60=6), _prog("queue-listen:queue-listen_01", p60=5),
                _prog("queue-listen:queue-listen_02", p60=6),
                _prog("mailer", "FATAL", since=T - 1500, exit=1,
                      log="SMTPAuthenticationError: 535 5.7.8 authentication failed"),
                _prog("report-export", "STOPPED", since=T - 3 * DAY)]},
            {"kind": "pm2", "where": "host", "progs": [_prog("api:0", "online"), _prog("api:1", "online"),
                                                        _prog("ws-gateway", "online")]}]}
        ex["cron"] = {"v": 1, "ts": T - 40, "journal": True, "disc24": 0,
                      "daemons": [{"where": "host", "kind": "cron", "mta": False, "jobs": 4}],
                      "jobs": [
                          _job("/srv/app/bin/console orders:sync >> /var/log/orders-sync.log 2>&1", "*/5 * * * *",
                               ages=[3900, 3600, 3300, 3000, 2700], n=13, runs=288, mx=40, done=12, order=1),
                          _job("/srv/app/bin/console reports:daily", "30 2 * * *", runs=1, last=6 * 3600, mx=820,
                               done=6),
                          _job("certbot renew -q", "17 3 * * *", runs=1, last=5 * 3600, mx=14, done=3),
                          _job("/srv/app/bin/console cache:warm", "0 * * * *", runs=24, last=1700, mx=35, done=24)]}
        ex["logerr"] = {"v": 1, "ts": T - 40, "logs": 48, "read": 7_400_000, "skipped": 0, "sources": [
            _lsrc("container app-01-app-1", 31, 31 * 24,
                  "PHP Fatal error:  Uncaught PDOException: SQLSTATE[HY000] [2002] Connection refused in "
                  "/app/vendor/doctrine/dbal/src/Driver/PDO/Connection.php:33"),
            _lsrc("container app-01-worker-1", 40, 40 * 24, "WARNING: queue orders: consumer cancelled by broker")]}
    if role == "ci":
        # внешний прокси держит docker-сокет, helper sockfix-setup уже собрал compose без него
        rep["docker"]["containers"].append(
            {"name": "caddy-caddy-1", "image": "lucaslorentz/caddy-docker-proxy:2.12.0", "state": "running",
             "status": "Up 40 days", "policy": "always", "restarts": 0,
             "binds": ["/var/run/docker.sock", "/srv/caddy/data"], "cpu": 0.4, "mem": 52_000_000,
             "img_ver": "2.12.0", "img_created": T - 200 * DAY})
        ex["sockfix"] = {"v": 1, "ts": T - 120, "items": [{
            "container": "caddy-caddy-1", "kind": "caddy", "project": "caddy", "service": "caddy",
            "dir": "/srv/caddy", "files": ["/srv/caddy/docker-compose.yml"], "nets": ["caddy"],
            "sock": "/var/run/docker.sock", "compose": "2.40.3", "ok": True, "gid": 989,
            "file": "/srv/caddy/docker-compose.yml", "fixed": "/var/lib/kervax/sockfix/caddy.yml",
            "diff": SOCKFIX_DIFF}]}
    if role == "gpu":
        rep["llm"] = [
            {"engine": "vllm", "kind": "container", "name": "llm-vllm-1", "model": "Qwen/Qwen3-32B", "port": 8000,
             "up": 3 * DAY, "ok": True, "win": 300, "running": 14, "waiting": 2, "kv": 71.0, "req_s": 3.4,
             "prompt_tps": 5200, "gen_tps": 890, "errors": 0, "ttft": [0.24, 0.71, 1.3],
             "tpot": [0.029, 0.043, 0.07], "e2e": [3.1, 8.4, 14.2]},
            {"engine": "tei", "kind": "container", "name": "llm-embed-1", "model": "BAAI/bge-m3", "port": 8080,
             "up": 3 * DAY, "ok": True, "win": 300, "running": 2, "waiting": 0, "req_s": 41.0, "errors": 0,
             "e2e": [0.012, 0.031, 0.06]},
            {"engine": "litellm", "kind": "container", "name": "llm-gateway-1", "model": "", "port": 4000,
             "up": 12 * DAY, "ok": True, "win": 300, "req_s": 3.9, "errors": 0}]
    if role == "backup":
        bs = rep["backup_server"]
        ex["backup-server"] = {"v": 1, "ts": T - 60, "restic": "0.19.1", "restic_target": "0.19.1",
                               "legacy": None, "repos": {
                                   r["name"]: {"cleaner": "script", "removed_ts": T - 5 * 3600,
                                               "seen_since": T - 60 * DAY, "check_ts": T - 2 * DAY, "check_ok": 1,
                                               "check_part": 3, "check_parts": 12,
                                               "recent": [r["last_activity"] - i * DAY for i in range(6)]}
                                   for r in bs["repos"]}}


def db_disks(rep: dict, ex: dict) -> None:
    """db-01: два NVMe в RAID1 под базой, износ к 90% через полтора месяца, архивный HDD
    начал переназначать сектора, диск заполнится за четыре дня, место съели WAL и забытый дамп"""
    nvme = {"type": "nvme", "model": "Samsung SSD 990 PRO 2TB", "size": 2000398934016, "rota": 0,
            "ok": True, "err": "", "health": "PASSED", "failing": False, "spare": 100, "spare_thr": 10,
            "media": 0, "crit": "0x00", "poh": 21400, "realloc": None, "pending": None, "uncorr": None,
            "crc": None, "grow": {}}
    ex["disk-health"] = {
        "v": 1, "ts": T - 60, "virt": "none", "container": False, "smart": True,
        "raid": [{"dev": "md1", "state": "active", "level": "raid1", "total": 2, "active": 2, "map": "[UU]",
                  "members": "nvme0n1p2 nvme1n1p2", "failed": "", "spare": "", "sync": ""}],
        "disks": [{**nvme, "dev": "nvme0n1", "serial": "S7DEMO0000001", "temp": 47, "wear": 85},
                  {**nvme, "dev": "nvme1n1", "serial": "S7DEMO0000002", "temp": 49, "wear": 82},
                  {"dev": "sda", "model": "WDC WD40EFRX-68N32N0", "serial": "WD-DEMO00000003", "type": "sata",
                   "size": 4000787030016, "rota": 1, "ok": True, "err": "", "health": "PASSED", "failing": False,
                   "temp": 38, "wear": None, "spare": None, "spare_thr": None, "media": None, "crit": "",
                   "poh": 38100, "realloc": 24, "pending": 2, "uncorr": 0, "crc": 0, "grow": {"realloc": 16}}],
        "missing": [], "io": {"count": 0, "last": []}}
    rep["disk_devs"] = [
        {"dev": "nvme0n1", "util": 41.0, "await": 0.9, "temp": 47},
        {"dev": "nvme1n1", "util": 39.5, "await": 0.9, "temp": 49},
        {"dev": "sda", "util": 3.1, "await": 7.4, "temp": 38}]
    used = rep["disks"][0]["used"]
    ex["disk-usage"] = {
        "v": 1, "ts": T - 300,
        "fix": {"v": 1, "allow": ["journal", "rotated-logs", "apt-cache", "container-log"]},
        "fs": [{"mount": "/", "size": rep["disks"][0]["total"], "used": used, "avail": rep["disks"][0]["avail"],
                "pct": 86, "inodes": rep["disks"][0]["inodes"], "top_state": "ok", "top_ts": T - 300,
                "top": [{"path": "/var", "bytes": int(used * 0.97)},
                        {"path": "/var/lib", "bytes": int(used * 0.85)},
                        {"path": "/var/lib/postgresql", "bytes": int(used * 0.84)},
                        {"path": "/var/lib/postgresql/16/main/base", "bytes": int(used * 0.73)},
                        {"path": "/var/lib/postgresql/16/main/pg_wal", "bytes": int(used * 0.105)},
                        {"path": "/var/tmp", "bytes": 151_000_000_000},
                        {"path": "/var/log", "bytes": 27_400_000_000}]}],
        "items": [
            {"id": "vartmp-dir", "bytes": 148_300_000_000, "free": 148_300_000_000, "mount": "/",
             "path": "/var/tmp/shop-before-migration", "level": "manual",
             "fix": "rm -rf '/var/tmp/shop-before-migration'", "count": 3, "kind": "dump", "fresh": False},
            {"id": "rotated-logs", "bytes": 14_900_000_000, "free": 14_900_000_000, "mount": "/",
             "path": "/var/log", "level": "safe", "count": 212,
             "fix": r'find /var/log -xdev -type f \( -name "*.gz" -o -name "*.xz" -o -name "*.zst" \) -mtime +7 -delete'},
            {"id": "big-log", "bytes": 9_700_000_000, "free": 9_700_000_000, "mount": "/",
             "path": "/var/log/postgresql/postgresql-16-main.log", "level": "careful",
             "fix": "truncate -s 0 '/var/log/postgresql/postgresql-16-main.log'"},
            {"id": "journal", "bytes": 4_100_000_000, "free": 3_890_000_000, "mount": "/",
             "path": "/var/log/journal", "level": "safe", "fix": "journalctl --vacuum-size=200M"},
            {"id": "apt-cache", "bytes": 1_200_000_000, "free": 1_200_000_000, "mount": "/",
             "path": "/var/cache/apt/archives", "level": "safe", "fix": "apt-get clean"}]}
    rep["disk_writers"] = [
        {"kind": "unit", "name": "postgresql@16-main.service", "w": 58_000_000_000, "r": 9_400_000_000, "s": 3600},
        {"kind": "unit", "name": "wal-g-push.service", "w": 3_100_000_000, "r": 12_800_000_000, "s": 3600},
        {"kind": "unit", "name": "systemd-journald.service", "w": 820_000_000, "r": 0, "s": 3600},
        {"kind": "unit", "name": "pgbouncer.service", "w": 40_000_000, "r": 0, "s": 3600}]


def wear_history(block: dict) -> dict:
    """servers.disk_wear: полтора месяца замеров, по ним панель считает скорость износа"""
    prev = {}
    for d in block["disks"]:
        if d.get("wear") is None:
            continue
        w = d["wear"]
        t = [[(NOW.date() - timedelta(days=8 * k)).isoformat(), w - k] for k in range(6, 0, -1)]
        prev[d["serial"]] = {"t": t, "seen": NOW.date().isoformat()}
    return disk_wear.update(prev, block, NOW)


def gpu_block() -> dict:
    card = {"name": "NVIDIA H100 80GB HBM3", "memt": 81559, "pwl": 700.0, "clkm": 1980, "ps": "P0", "fan": None,
            "pm": "Enabled", "ecc_u": 0, "ecc_c": 0, "slow": 87, "shut": 92, "maxop": 85,
            "thr": {"hw": False, "sw": False, "brake": False, "pcap": False},
            "retire": False, "remap": False, "remap_fail": False}
    return {
        "v": 1, "ts": T - 30, "smi": True, "ok": True, "err": "", "driver": "570.172.08", "cuda": "12.8",
        "kernel": "6.8.0-45-generic", "mod": True, "mod_built": True, "nvrm": "",
        "pci": [{"bus": "0000:19:00.0", "dev": "2330", "drv": "nvidia"},
                {"bus": "0000:3b:00.0", "dev": "2330", "drv": "nvidia"}],
        "gpus": [{**card, "i": 0, "bus": "00000000:19:00.0", "uuid": "GPU-0d3c5a1e-7b2f-4c9d-8e61-5f0a9b2c7d14",
                  "util": 91, "mutil": 58, "mem": 74210, "temp": 71, "pw": 612.0, "clk": 1755},
                 {**card, "i": 1, "bus": "00000000:3B:00.0", "uuid": "GPU-8a6e2f90-1c4d-4b7a-9f35-2e8d0c6b1a73",
                  "util": 34, "mutil": 12, "mem": 23880, "temp": 52, "pw": 248.0, "clk": 1410}],
        "procs": [{"gpu": "GPU-0d3c5a1e-7b2f-4c9d-8e61-5f0a9b2c7d14", "pid": 7101, "mem": 73650,
                   "name": "python3", "kind": "container", "owner": "llm-vllm-1"},
                  {"gpu": "GPU-8a6e2f90-1c4d-4b7a-9f35-2e8d0c6b1a73", "pid": 7340, "mem": 23310,
                   "name": "text-embeddings-router", "kind": "container", "owner": "llm-embed-1"}],
        "xid": []}


def server_columns(srv: Server, rep: dict, name: str, role: str) -> None:
    """Колонки сервера, которые в живой панели заполняет планировщик: скан портов снаружи,
    лента изменений, прогнозы. Планировщик на съемке выключен, так что пишем их сами"""
    ip = EXT_IP[name]
    ch = rep["extras"]["changes"]

    def row(port: int, state: str) -> dict:
        return {"ip": ip, "port": port, "state": state, "what": "", "name": "", "risk": False}

    closed = {9100} | ({22} if role != "web" else set())   # SSH на не-веб нодах закрыт фаерволом
    ports = [row(p, "filtered" if p in closed else "open") for p in sorted(int(x) for x in ch["listen"])]
    srv.exposure = {"ts": iso(timedelta(hours=2)), "via": LOCATIONS[LANG][0][0], "own": False,
                    "ips": [ip], "via_ips": ["198.51.100.21"], "rules": 1, "banners": {},
                    "ports": ports, "new": [row(6379, "open")] if name == "web-02" else [],
                    # новый порт нашелся вчера: метка в карточке есть, а вторая строка про тот же Redis
                    # на главной не нужна
                    "new_ts": iso(timedelta(hours=30)), "seen": {},
                    "cf": {"sites": [], "n": 0, "checked": 3, "total": 3}}
    if name == "web-02":
        tx = CHANGES_TEXT[LANG]
        srv.changes = {"snap": ch, "alert_ts": iso(timedelta(hours=3)), "ssh_seen": {}, "log": [
            {"kind": "key", "act": "add", "alert": True, "ts": iso(timedelta(hours=3)), "text": tx["key"]},
            {"kind": "login", "act": "add", "alert": True, "ts": iso(timedelta(hours=5)), "text": tx["login"]},
            {"kind": "sudo", "act": "mod", "alert": True, "ts": iso(timedelta(days=2)), "text": tx["sudo"]},
            {"kind": "cron", "act": "add", "alert": False, "ts": iso(timedelta(days=4)), "text": tx["cron"]},
            {"kind": "unit", "act": "add", "alert": False, "ts": iso(timedelta(days=6)), "text": tx["unit"]}]}
        # PHP 8.1 на хосте: поддержка кончилась 31.12.2025
        srv.sw_host = [{"p": "php", "v": "8.1.2", "pkg": "php8.1-fpm, php8.1-cli"}]
    else:
        srv.changes = {"snap": ch, "alert_ts": "", "ssh_seen": {}, "log": []}
    if role == "db":
        srv.disk_wear = wear_history(rep["extras"]["disk-health"])
        srv.disk_forecast = {"ts": T, "items": [
            {"mount": "/", "kind": "space", "pct": 86.0, "rate": 3.5, "eta_h": 96.0}]}
    if role == "backup":
        srv.disk_forecast = {"ts": T, "items": [
            {"mount": "/srv/restic", "kind": "space", "pct": 88.0, "rate": 0.4, "eta_h": 720.0}]}


def alert_state(name: str) -> dict:
    """Память алертов, которую в живой панели копит планировщик: история очереди (по ней
    "растет и никто не разбирает") и с какого момента лежит Flux"""
    if name == "app-01":
        return {"qwatch": {"контейнер app-01-rabbitmq-1|//orders": {
            "t0": T - 2700, "d0": 300, "p": 2200, "nd": True, "last": 2200, "seen": T}}}
    if name == "k8s-node-1":
        return {"flux_down_from": iso(timedelta(hours=2))}
    return {}


def _e5(t: int) -> int:
    """5xx в минуту на web-01: сейчас идет волна (php-fpm не принимает соединения), была
    короткая утром"""
    if t <= 16:
        return 38 + (t * 7) % 17
    if 540 <= t <= 556:
        return 14 + (t * 5) % 11
    return 0


def minute_extra(name: str, role: str, t: int) -> dict:
    """Поля минутной строки метрик, которые есть не у всех: веб, GPU, LLM"""
    if name == "web-01":
        rpm = int(wave(t, 14500, 4000))
        e5 = _e5(t)
        label = collector.web_log_label(WEB_LOGS[0])
        return {"web_rpm": rpm, "web_5xx": e5,
                "web_top": [{"k": label, "r": int(rpm * 0.77), "e": e5},
                            {"k": "blog.example.com", "r": int(rpm * 0.14), "e": 0}],
                "web_codes": [{"c": "502", "n": e5 - e5 // 5}, {"c": "504", "n": e5 // 5}] if e5 else []}
    if role == "gpu":
        u0, u1 = wave(t, 84, 9, 360), wave(t, 30, 12, 240)
        return {"gpu": [{"g": "0", "u": round(min(u0, 100), 1), "m": 91.0, "t": round(60 + u0 * 0.13, 1),
                         "p": round(min(u0, 100) * 0.85, 1)},
                        {"g": "1", "u": round(min(u1, 100), 1), "m": 29.3, "t": round(44 + u1 * 0.2, 1),
                         "p": round(u1 * 0.4, 1)}],
                "llm": [{"k": "vLLM llm-vllm-1", "run": int(wave(t, 13, 4, 180)), "wait": int(wave(t, 1.5, 1.5, 90)),
                         "kv": round(wave(t, 68, 9, 180), 1), "gen": int(wave(t, 880, 160, 180)),
                         "req": round(wave(t, 3.3, 0.8, 180), 2), "ttft": round(wave(t, 0.7, 0.18, 120), 3),
                         "tpot": round(wave(t, 0.043, 0.006, 120), 4), "e2e": round(wave(t, 8.2, 1.7, 120), 2)}]}
    return {}


def extra_rows(srv: Server, rep: dict, name: str, role: str) -> list:
    out: list = []
    if name in VULN_NODES:
        stale, _ = VULN_NODES[name]
        items = [[p, OLD[p] if p in stale else NEW[p]] for p in sorted(NEW)]
        items += [["bash", "5.2.21-2ubuntu4"], ["curl", "8.5.0-2ubuntu10.6"], ["nginx", "1.30.0-1~noble"]]
        out.append(ServerPackages(server_id=srv.id, mgr="dpkg", hash=rep["pkgs"]["h"], n=rep["pkgs"]["n"],
                                  items=items, ts=NOW, ev_ts=0))
    if name == "web-01":
        label = collector.web_log_label(WEB_LOGS[0])
        for t in range(1440, 0, -1):
            e5 = _e5(t)
            if not e5:
                continue
            ts = NOW - timedelta(minutes=t)
            r = e5 / 47
            out.append(WebErrorSample(
                server_id=srv.id, ts=ts, src_ts=int(ts.timestamp()), log=WEB_LOGS[0]["log"], label=label,
                e5=e5, rpm=int(wave(t, 11200, 3000)),
                codes={"502": e5 - e5 // 5, "504": e5 // 5},
                paths=[{"p": "/checkout", "n": round(29 * r)}, {"p": "/api/v1/cart", "n": round(12 * r)},
                       {"p": "/api/v1/orders", "n": round(6 * r)}],
                hosts=[{"h": "shop.example.com", "n": e5 - e5 // 5}, {"h": "api.example.com", "n": e5 // 5}],
                lines=WEB_LOGS[0]["l5"],
                ups=[dict(u, n=max(1, round(u["n"] * r))) for u in WEB_UP]))
    if role == "db":
        # неделя по часам: кто пишет на диск (a - записано, b - прочитано)
        h0 = T // 3600
        for h in range(168):
            k = 1 + 0.35 * math.sin(2 * math.pi * (h % 24) / 24)
            for w in rep["disk_writers"]:
                out.append(ServerUsage(server_id=srv.id, metric="disk", hour=h0 - h, name=w["name"], kind=w["kind"],
                                       a=int(w["w"] * k), b=int(w["r"] * k)))
    return out


EVENTS = {
    "ru": {
        "disk": "⚠️ backup-01 — диск /srv/restic 88% ≥ 85%",
        "disk_ok": "✅ backup-01 — диск /srv/restic 81%, снова в норме",
        "cpu": "⚠️ ci-runner-1 — CPU 96% ≥ 90% - больше всего CPU у buildkitd 1180%, dockerd 140%",
        "queue": "🧱 app-01 — очередь orders (контейнер app-01-rabbitmq-1) растет 45 мин и нет ни одного "
                 "потребителя: 300 -> 2 200 сообщений",
        "kube": "🔄☸️ k8s-node-1 — под shop/worker-5f8b9c7d6-mn4pl перезапускался 6 раз за сутки",
        "web": "🔥 web-01 — 0.42% запросов за 15 минут закончились ошибкой 5xx (61 из 14500). Больше всего: "
               "shop.example.com - 52. Коды: 502 - 52, 504 - 9. Почему: web-01-app-1:9000 не принимает "
               "соединения - 52. Чаще всего падает: /checkout - 40",
        "port": "🚪 web-02 — новый порт открыт в интернет: Redis 6379 на 203.0.113.11",
        "wear": "💾 db-01 — диск nvme0n1: износ 85%, 90% примерно к {d90} - пора заказывать замену",
        "units": "⚠️⚙️ ci-runner-1 — упал certbot.service: код 1",
        "down": "🔴 Тестовый стенд — HTTP 502 — плохой ответ от вышестоящего сервера (Bad Gateway)",
        "up": "✅ Тестовый стенд — снова доступен",
    },
    "en": {
        "disk": "⚠️ backup-01 — disk /srv/restic 88% ≥ 85%",
        "disk_ok": "✅ backup-01 — disk /srv/restic 81%, back to normal",
        "cpu": "⚠️ ci-runner-1 — CPU 96% ≥ 90% - top CPU: buildkitd 1180%, dockerd 140%",
        "queue": "🧱 app-01 — queue orders (container app-01-rabbitmq-1) has been growing for 45 min with no "
                 "consumers: 300 -> 2 200 messages",
        "kube": "🔄☸️ k8s-node-1 — pod shop/worker-5f8b9c7d6-mn4pl restarted 6 times in 24 h",
        "web": "🔥 web-01 — 0.42% of requests in 15 minutes ended with 5xx (61 of 14500). Most: "
               "shop.example.com - 52. Codes: 502 - 52, 504 - 9. Why: web-01-app-1:9000 refuses "
               "connections - 52. Most failing: /checkout - 40",
        "port": "🚪 web-02 — new port open to the internet: Redis 6379 on 203.0.113.11",
        "wear": "💾 db-01 — disk nvme0n1: wear 85%, 90% around {d90} - time to order a replacement",
        "units": "⚠️⚙️ ci-runner-1 — certbot.service failed: code 1",
        "down": "🔴 Staging — HTTP 502 — bad response from the upstream server (Bad Gateway)",
        "up": "✅ Staging — back up",
    },
}


def alert_events() -> list[AlertEvent]:
    """Неделя доставленных алертов для истории: повторяющиеся (их панель покажет как шум) и разовые"""
    tx = EVENTS[LANG]
    staging = NAME["staging"]
    d90 = (NOW.date() + timedelta(days=40)).strftime("%d.%m")

    def ev(d, h, kind, target, sec, grp, key, rec=False, **fmt):
        return AlertEvent(ts=NOW - timedelta(days=d, hours=h), kind=kind, target=target, section=sec, grp=grp,
                          recovery=rec, text=tx[key].format(**fmt))
    rows = []
    for d in range(6):
        rows += [ev(d, 4, "disk", "backup-01", "servers", "Infra", "disk"),
                 ev(d, 2, "disk", "backup-01", "servers", "Infra", "disk_ok", True)]
    for d in (0, 1, 3, 4, 5):
        rows.append(ev(d, 6, "cpu", "ci-runner-1", "servers", "Dev", "cpu"))
    for d, h in ((0, 0.7), (0, 9), (2, 3), (4, 3)):
        rows.append(ev(d, h, "queue_stuck", "app-01", "services", "Production", "queue"))
    for d in (0, 1, 2):
        rows.append(ev(d, 8, "kube_restarts", "k8s-node-1", "kuber", "Staging", "kube"))
    rows += [ev(0, 0.2, "web_5xx", "web-01", "servers", "Production", "web"),
             ev(1, 6, "port_new", "web-02", "servers", "Production", "port"),
             ev(1, 7, "disk_health", "db-01", "servers", "Production", "wear", d90=d90),
             ev(2, 5, "units", "ci-runner-1", "servers", "Dev", "units"),
             ev(0, 0.9, "down", staging, "sites", "Staging", "down"),
             ev(3, 4, "down", staging, "sites", "Staging", "down"),
             ev(3, 3.5, "down", staging, "sites", "Staging", "up", True)]
    return rows


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
        srvs: dict[str, Server] = {}
        for node in NODES:
            name, group, osname, cores, ram_gb, cpu_base, disks, role = node
            rep = report(node, 0)
            srv = Server(
                name=name, group_name=group, enabled=True, hostname=name,
                os=osname, agent_version=AGENT,
                token_hash=hashlib.sha256(f"demo-{name}".encode()).hexdigest(),
                local_ip=f"10.0.{NODES.index(node)}.10",
                external_ip=EXT_IP[name],
                last_report=rep, last_seen=NOW - timedelta(seconds=rnd.randint(3, 40)),
                metric_written_at=NOW, first_report_at=NOW - timedelta(days=200),
                # съемка и просмотр длятся дольше двух минут: со штатным порогом ноды
                # успели бы стать "недоступны" и карточки опустели
                offline_after_seconds=86400,
                # «дампы СУБД настроены отдельно» — иначе панель справедливо
                # напоминает про них на каждой ноде с базой или redis-контейнером
                db_dumps_ok=True,
            )
            server_columns(srv, rep, name, role)
            srv.alert_state = alert_state(name)
            s.add(srv)
            await s.flush()
            srvs[name] = srv
            rows = []
            for i in range(1440):  # сутки, шаг 1 минута
                t = 1440 - i
                ts = NOW - timedelta(minutes=t)
                cpu = round(wave(t, cpu_base, 14), 1)
                worst = max(pct for _, pct, _ in disks)
                if role == "db":    # база растет: за сутки +3.5 пункта, отсюда и прогноз
                    disks = [("/", round(86 - 3.5 * t / 1440, 2), 1800)]
                    worst = disks[0][1]
                rows.append(ServerMetric(
                    **minute_extra(name, role, t),
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
            s.add_all(extra_rows(srv, rep, name, role))
            await s.flush()
        s.add(VulnFeed(release="noble", gen=VULN_FEED["gen"], ts=NOW, err="", data=VULN_FEED))
        # 22.04 (k8s-node-1): база есть, важного для его пакетов в ней нет
        s.add(VulnFeed(release="jammy", gen=VULN_FEED["gen"], ts=NOW, err="",
                       data={"gen": VULN_FEED["gen"], "kev": [], "pkgs": {}, "kernel": []}))
        s.add_all(alert_events())

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
