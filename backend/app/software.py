"""Версии без поддержки: среды выполнения, СУБД и прокси, у которых кончилась или скоро кончится
поддержка, - что обновить на сервере, а не что сломалось.

Откуда версии:
- контейнеры: версия СУБД из инвентаря dbstat-setup (самая точная), переменные окружения образа
  с версией (PHP_VERSION, NGINX_VERSION, у bitnami BITNAMI_APP_NAME и APP_VERSION - агент 2.35
  отдает только их), их наследуют и образы приложений, собранные поверх; у известных образов -
  метка org.opencontainers.image.version и тег (postgres:12, php:7.4-fpm);
- хост: пакеты из сторонних репозиториев (pgdg, sury, nodesource, nginx.org, MariaDB, MySQL,
  Redis, MongoDB, Elastic, ClickHouse). Пакеты дистрибутива дистрибутив и чинит, пока жива ОС,
  их срок - поддержка ОС (os_support), а не версии: у PostgreSQL 14 в Ubuntu 22.04 он позже.
Сроки - офлайн-снимок endoflife.date (ops/build-eol.py -> app/data/eol.json).

Не алерты: пункты в "Требует действий" и блок "Версии ПО" в карточке. Образы без версии в теге
(latest) и образы, которые больше года не пересобирались, - только в карточке.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

WARN_DAYS = 180  # как у ОС: за полгода пора планировать переезд
OLD_IMAGE_DAYS = 365
WHERE_MAX = 4
_DATA = Path(__file__).with_name("data") / "eol.json"

# известные образы (без реестра или из реестра самого вендора) -> продукт endoflife.date
_IMAGES = {
    "postgres": "postgresql", "postgis/postgis": "postgresql", "bitnami/postgresql": "postgresql",
    "timescale/timescaledb": "postgresql", "timescale/timescaledb-ha": "postgresql",
    "mysql": "mysql", "bitnami/mysql": "mysql", "percona/percona-server": "mysql", "percona": "mysql",
    "mariadb": "mariadb", "bitnami/mariadb": "mariadb",
    "redis": "redis", "bitnami/redis": "redis", "redis/redis-stack-server": "redis", "redis/redis-stack": "redis",
    "valkey/valkey": "valkey", "bitnami/valkey": "valkey",
    "mongo": "mongodb", "bitnami/mongodb": "mongodb",
    "elasticsearch": "elasticsearch", "elasticsearch/elasticsearch": "elasticsearch", "bitnami/elasticsearch": "elasticsearch",
    "clickhouse/clickhouse-server": "clickhouse", "yandex/clickhouse-server": "clickhouse", "bitnami/clickhouse": "clickhouse",
    "rabbitmq": "rabbitmq", "bitnami/rabbitmq": "rabbitmq",
    "zookeeper": "zookeeper", "bitnami/zookeeper": "zookeeper",
    "nginx": "nginx", "nginxinc/nginx-unprivileged": "nginx", "bitnami/nginx": "nginx",
    "traefik": "traefik", "haproxy": "haproxy", "haproxytech/haproxy-alpine": "haproxy",
    "haproxytech/haproxy-debian": "haproxy", "caddy": "caddy",
    "php": "php", "bitnami/php-fpm": "php", "yiisoftware/yii2-php": "php",
    "node": "nodejs", "bitnami/node": "nodejs", "python": "python", "bitnami/python": "python", "ruby": "ruby",
    "eclipse-temurin": "eclipse-temurin", "openjdk": "eclipse-temurin", "amazoncorretto": "eclipse-temurin",
    "grafana/grafana": "grafana", "grafana/grafana-oss": "grafana", "grafana/grafana-enterprise": "grafana",
    "alpine": "alpine-linux", "ubuntu": "ubuntu", "debian": "debian", "centos": "centos",
}
_REGISTRIES = {"", "docker.io", "index.docker.io", "registry-1.docker.io", "docker.elastic.co"}
# переменная окружения образа -> продукт
_ENV = {
    "PHP_VERSION": "php", "NODE_VERSION": "nodejs", "PYTHON_VERSION": "python", "RUBY_VERSION": "ruby",
    "JAVA_VERSION": "eclipse-temurin", "PG_VERSION": "postgresql", "PG_MAJOR": "postgresql",
    "MYSQL_VERSION": "mysql", "MYSQL_MAJOR": "mysql", "MARIADB_VERSION": "mariadb", "REDIS_VERSION": "redis",
    "VALKEY_VERSION": "valkey", "MONGO_VERSION": "mongodb", "RABBITMQ_VERSION": "rabbitmq",
    "NGINX_VERSION": "nginx", "HAPROXY_VERSION": "haproxy", "CADDY_VERSION": "caddy",
}
_BITNAMI = {
    "postgresql": "postgresql", "mysql": "mysql", "mariadb": "mariadb", "redis": "redis", "valkey": "valkey",
    "mongodb": "mongodb", "elasticsearch": "elasticsearch", "clickhouse": "clickhouse", "rabbitmq": "rabbitmq",
    "zookeeper": "zookeeper", "nginx": "nginx", "php-fpm": "php", "node": "nodejs", "python": "python",
    "ruby": "ruby", "grafana": "grafana", "haproxy": "haproxy",
}
# движок из инвентаря dbstat-setup -> продукт (redis уточняется по образу: valkey пишет engine redis)
_DB = {"pg": "postgresql", "mysql": "mysql", "clickhouse": "clickhouse", "redis": "redis"}
_CODENAMES = {
    "debian": {"jessie": "8", "stretch": "9", "buster": "10", "bullseye": "11", "bookworm": "12", "trixie": "13"},
    "ubuntu": {"trusty": "14.04", "xenial": "16.04", "bionic": "18.04", "focal": "20.04", "jammy": "22.04",
               "noble": "24.04"},
}
# пакеты сторонних репозиториев: имя, признак репозитория в версии (пусто - пакет бывает только
# у вендора), продукт. У дистрибутива такого признака нет, его пакеты не трогаем
_PKG_RULES = [
    (re.compile(r"^php\d+\.\d+-(fpm|cli)$"), re.compile(r"sury|remi"), "php"),
    (re.compile(r"^php-fpm$"), re.compile(r"remi"), "php"),
    (re.compile(r"^nodejs$"), re.compile(r"nodesource"), "nodejs"),
    (re.compile(r"^postgresql-\d+$|^postgresql\d+-server$"), re.compile(r"pgdg", re.I), "postgresql"),
    (re.compile(r"^mariadb-server(-core)?(-\d+\.\d+)?$|^MariaDB-server$"), re.compile(r"\+maria|\.el\d"), "mariadb"),
    (re.compile(r"^mysql-community-server$"), None, "mysql"),
    (re.compile(r"^redis-server$|^redis$"), re.compile(r"rl\d"), "redis"),
    (re.compile(r"^nginx$"), re.compile(r"^\d+\.\d+\.\d+-\d+~[a-z]+$|\.ngx$"), "nginx"),
    (re.compile(r"^haproxy$"), re.compile(r"ppa|~bpo"), "haproxy"),
    (re.compile(r"^mongodb-org-server$"), None, "mongodb"),
    (re.compile(r"^elasticsearch$"), None, "elasticsearch"),
    (re.compile(r"^clickhouse-server$"), None, "clickhouse"),
    (re.compile(r"^rabbitmq-server$"), re.compile(r"^\d+\.\d+\.\d+-1(\.el\d+)?$"), "rabbitmq"),
]


@lru_cache(maxsize=1)
def table() -> dict:
    return json.loads(_DATA.read_text(encoding="utf-8"))


def product_name(p: str) -> str:
    return str(((table().get("products") or {}).get(p) or {}).get("name") or p)


def norm(v) -> str:
    """Версия из строки образа или пакета: эпоха dpkg и jdk- долой, только цифры с точками
    (1:10.6.16+maria~ubu2204 -> 10.6.16, jdk8u412-b08 -> 8, v2.11.57 -> 2.11.57)."""
    s = re.sub(r"^\d+:", "", str(v or "").strip().lower())
    s = re.sub(r"^(jdk-?|v)", "", s)
    m = re.match(r"\d+(?:\.\d+){0,3}", s)
    return m.group(0) if m else ""


def _num(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def cycle_of(p: str, ver: str) -> tuple[str, str | bool] | None:
    """Цикл версии и его срок: самый длинный цикл, с которого начинается версия
    (PostgreSQL 9.6.24 -> 9.6, 16.4 -> 16). Версия старше самого старого цикла в таблице - без
    поддержки, срок без даты (у ClickHouse в таблице только последние два года выпусков).
    None - продукта нет или версия новее таблицы."""
    if not ver:
        return None
    cycles = ((table().get("products") or {}).get(p) or {}).get("cycles") or []
    best = None
    for c, eol in cycles:
        if (ver == c or ver.startswith(c + ".")) and (best is None or len(c) > len(best[0])):
            best = (c, eol)
    if best is None and cycles and p != "eclipse-temurin":
        oldest = min((_num(c) for c, _e in cycles if _num(c)), default=())
        if oldest and _num(ver) < oldest:
            # цикл такой же длины, как у таблицы: 24.8.4 при циклах вида 26.5 -> 24.8
            return ".".join(ver.split(".")[:len(oldest)]), True
    return best


def split_image(image: str) -> tuple[str, str, str]:
    """(реестр, путь, тег): docker.io/library/nginx:1.25-alpine@sha256:... -> ("", "nginx", "1.25-alpine")"""
    s = str(image or "").strip().lower().split("@", 1)[0]
    parts = s.split("/")
    reg = ""
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        reg, parts = parts[0], parts[1:]
    tag = ""
    if ":" in parts[-1]:
        parts[-1], tag = parts[-1].split(":", 1)
    path = "/".join(parts)
    if path.startswith("library/"):
        path = path[8:]
    return ("" if reg in _REGISTRIES else reg), path, tag


def image_product(image: str) -> str | None:
    reg, path, _tag = split_image(image)
    return _IMAGES.get(path) if not reg else None


def _tag_version(p: str, tag: str) -> str:
    if p == "postgresql":
        m = re.search(r"pg(\d+)", tag)  # timescaledb: 2.14.2-pg16
        if m:
            return m.group(1)
    if p in _CODENAMES:
        for name, ver in _CODENAMES[p].items():
            if tag.startswith(name):
                return ver
    return norm(tag)


def container_versions(c: dict, dbs: dict[str, tuple[str, str]]) -> tuple[dict[str, tuple[str, str]], str | None]:
    """Версии в контейнере: ({продукт: (версия, откуда)}, продукт известного образа без версии).
    Откуда: db - инвентарь СУБД, env - окружение образа, label - метка, tag - тег."""
    image = str(c.get("image") or "")
    ip = image_product(image)
    out: dict[str, tuple[str, str]] = {}
    db = dbs.get(str(c.get("name") or ""))
    if db:
        p = _DB.get(db[0])
        if p == "redis" and ip == "valkey":
            p = "valkey"
        if p == "mysql" and ("mariadb" in db[1].lower() or ip == "mariadb"):
            p = "mariadb"
        v = norm(db[1])
        if p and v:
            out[p] = (v, "db")
    env = c.get("ver") if isinstance(c.get("ver"), dict) else {}
    for k, p in _ENV.items():
        v = norm(env.get(k))
        if v and p not in out:
            # PG_MAJOR без PG_VERSION, MYSQL_MAJOR без MYSQL_VERSION - тоже версия
            out[p] = (v, "env")
    bp = _BITNAMI.get(str(env.get("BITNAMI_APP_NAME") or "").lower())
    if bp and bp not in out and norm(env.get("APP_VERSION")):
        out[bp] = (norm(env.get("APP_VERSION")), "env")
    missing = None
    if ip and ip not in out:
        lv = norm(c.get("img_ver"))
        tv = _tag_version(ip, split_image(image)[2])
        if lv and cycle_of(ip, lv):
            out[ip] = (lv, "label")
        elif tv:
            out[ip] = (tv, "tag")
        else:
            missing = ip
    return out, missing


def host_versions(items) -> list[dict]:
    """Пакеты сторонних репозиториев: [{"p", "v", "pkg"}], для servers.sw_host."""
    seen: dict[tuple[str, str], dict] = {}
    for it in items or []:
        if not (isinstance(it, (list, tuple)) and len(it) >= 2):
            continue
        name, ver = str(it[0]), str(it[1])
        for nrx, mark, p in _PKG_RULES:
            if not nrx.search(name) or (mark is not None and not mark.search(ver)):
                continue
            v = norm(ver)
            if v:
                x = seen.setdefault((p, v), {"p": p, "v": v, "pkg": name})
                if name not in x["pkg"].split(", "):
                    x["pkg"] = f"{x['pkg']}, {name}"
            break
    return sorted(seen.values(), key=lambda x: (x["p"], x["v"]))


def _days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        w = "день"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        w = "дня"
    else:
        w = "дней"
    return f"{n} {w}"


def _state(eol, today: date) -> tuple[str, int | None]:
    """eol - кончилась, soon - кончится за WARN_DAYS, ok; и сколько дней до срока."""
    if eol is True:
        return "eol", None
    if not isinstance(eol, str):
        return "ok", None
    left = (date.fromisoformat(eol) - today).days
    return ("eol" if left < 0 else "soon" if left <= WARN_DAYS else "ok"), left


def inventory(rep: dict, sw_host, now: datetime) -> dict:
    """Все версии сервера по продукту и циклу: {"items": [...], "unknown": [...], "old": [...]}.
    items: {"key", "p", "name", "cycle", "ver", "eol", "state", "left", "where": [{"kind", "name", "src"}]}."""
    today = now.astimezone(timezone.utc).date()
    dbs = {str(d.get("container") or ""): (str(d.get("engine") or ""), str(d.get("version") or ""))
           for d in rep.get("db_stats") or [] if isinstance(d, dict) and d.get("container")}
    groups: dict[tuple[str, str], dict] = {}
    unknown, old = [], []

    def add(p: str, ver: str, where: dict) -> None:
        cy = cycle_of(p, ver)
        if cy is None:
            return
        cycle, eol = cy
        g = groups.get((p, cycle))
        if g is None:
            st, left = _state(eol, today)
            g = groups[(p, cycle)] = {"key": f"{p}-{cycle}", "p": p, "name": product_name(p), "cycle": cycle,
                                      "ver": ver, "eol": eol, "state": st, "left": left, "where": []}
        g["where"].append(where)

    dk = rep.get("docker") if isinstance(rep.get("docker"), dict) else {}
    for c in dk.get("containers") or []:
        if not isinstance(c, dict) or c.get("state") != "running":
            continue
        vers, missing = container_versions(c, dbs)
        for p, (v, src) in vers.items():
            add(p, v, {"kind": "container", "name": str(c.get("name") or ""), "src": src, "ver": v})
        if missing:
            unknown.append({"name": str(c.get("name") or ""), "image": str(c.get("image") or ""),
                            "p": missing, "pname": product_name(missing)})
        try:
            built = int(c.get("img_created") or 0)
        except (TypeError, ValueError):
            built = 0
        if built > 0:
            age = int((now.timestamp() - built) // 86400)
            if age >= OLD_IMAGE_DAYS:
                old.append({"name": str(c.get("name") or ""), "image": str(c.get("image") or ""),
                            "built": built, "age_days": age})
    for h in sw_host or []:
        if isinstance(h, dict) and h.get("p") and h.get("v"):
            add(str(h["p"]), str(h["v"]), {"kind": "host", "name": str(h.get("pkg") or ""), "src": "pkg",
                                           "ver": str(h["v"])})
    order = {"eol": 0, "soon": 1, "ok": 2}
    items = sorted(groups.values(), key=lambda g: (order[g["state"]], g["name"].lower(), g["cycle"]))
    old.sort(key=lambda x: -x["age_days"])
    return {"items": items, "unknown": unknown, "old": old}


def _where_text(where: list[dict]) -> str:
    cont = sorted({w["name"] for w in where if w["kind"] == "container"})
    host = sorted({w["name"] for w in where if w["kind"] == "host"})
    parts = []
    if cont:
        shown = ", ".join(cont[:WHERE_MAX]) + (f" и еще {len(cont) - WHERE_MAX}" if len(cont) > WHERE_MAX else "")
        parts.append(("контейнер " if len(cont) == 1 else "контейнеры ") + shown)
    if host:
        pk = sorted({x for h in host for x in h.split(", ")})
        shown = ", ".join(pk[:WHERE_MAX]) + (f" и еще {len(pk) - WHERE_MAX}" if len(pk) > WHERE_MAX else "")
        parts.append(f"на хосте ({shown})")
    return ", ".join(parts)


def item_text(g: dict) -> str:
    what = f"{g['name']} {g['cycle']}"
    where = _where_text(g["where"])
    if g["state"] == "eol":
        when = f" с {date.fromisoformat(g['eol']).strftime('%d.%m.%Y')}" if isinstance(g["eol"], str) else ""
        return f"{what} без поддержки{when}: {where}"
    when = date.fromisoformat(g["eol"]).strftime("%d.%m.%Y")
    return f"поддержка {what} кончится {when}, через {_days(int(g['left']))}: {where}"


def _cycle_key(c: str) -> list[int]:
    return [int(x) for x in c.split(".") if x.isdigit()]


def advice(rep: dict, sw_host, now: datetime) -> list[dict]:
    """Что обновить: [{"key", "level", "name", "what", "text"}], строка на продукт: 2 - без поддержки,
    1 - скоро; what - продукт и циклы без мест ("PHP 7.3 и 7.4") для свернутой строки на главной.
    На одном хосте бывает PHP 5.6, 7.0, 7.1 и 7.3 сразу - это одна строка, циклы по отдельности
    в карточке."""
    by: dict[tuple[str, str], list[dict]] = {}
    for g in inventory(rep or {}, sw_host, now)["items"]:
        if g["state"] in ("eol", "soon"):
            by.setdefault((g["p"], g["state"]), []).append(g)
    out = []
    for (p, st), gs in by.items():
        if len(gs) == 1:
            text = item_text(gs[0])
            what = f"{gs[0]['name']} {gs[0]['cycle']}"
        else:
            gs.sort(key=lambda g: _cycle_key(g["cycle"]))
            cs = [g["cycle"] for g in gs]
            what = f"{gs[0]['name']} {', '.join(cs[:-1])} и {cs[-1]}"
            where = _where_text([w for g in gs for w in g["where"]])
            if st == "eol":
                text = f"{what} без поддержки: {where}"
            else:
                first = min(gs, key=lambda g: g["left"])
                when = date.fromisoformat(first["eol"]).strftime("%d.%m.%Y")
                text = f"поддержка {what} кончается, ближайший срок {when}, через {_days(int(first['left']))}: {where}"
        out.append({"key": f"{p}-{st}", "level": 2 if st == "eol" else 1, "name": gs[0]["name"], "what": what,
                    "text": text})
    return out


_last_run: datetime | None = None


async def refresh(session_factory, now: datetime) -> None:
    """Раз в час: пакеты сторонних репозиториев из списка пакетов сервера - в servers.sw_host,
    чтобы карточка и главная не читали тысячи пакетов на каждый запрос."""
    global _last_run
    from app.models import Server, ServerPackages

    if _last_run is not None and (now - _last_run).total_seconds() < 3600:
        return
    _last_run = now
    async with session_factory() as s:
        pk = {r.server_id: r.items for r in await s.scalars(select(ServerPackages))}
        for srv in await s.scalars(select(Server).where(Server.enabled.is_(True))):
            if srv.id not in pk:
                continue  # список пакетов не приходил: не повод забывать прошлое
            new = host_versions(pk[srv.id])
            if new != (srv.sw_host or []):
                srv.sw_host = new
        await s.commit()
