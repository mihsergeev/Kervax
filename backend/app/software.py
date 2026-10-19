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

У пункта в карточке готовые шаги (fix): команды под эту ноду, после которых этой версии на ноде не
остается и пункт уходит сам. Тексты шагов - во фронте по id, здесь команды. Compose-проект
контейнера (каталог, файлы, сервис) и его тома агент не присылает - их находит первая команда по
меткам контейнера на самой ноде. Базы командой не обновляем: новая версия переводит данные на
свой формат, а PostgreSQL нового major старые файлы не читает вовсе, - только копия, дамп и путь
словами.

Обновить можно не всегда: тогда решение человека (alert_mutes "swver:<состояние>:<продукт>-<цикл>"),
как у ОС. Состояние в ключе: "отложить до конца поддержки" у пункта "скоро" не прячет его, когда
поддержка кончится.
"""
from __future__ import annotations

import json
import re
import shlex
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

WARN_DAYS = 180  # как у ОС: за полгода пора планировать переезд
OLD_IMAGE_DAYS = 365
WHERE_MAX = 4
FIX_MAX = 4  # способов исправить у пункта: контейнеры с одним образом - один способ
MUTE_PREFIX = "swver:"
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


def _in_table(p: str, ver: str) -> bool:
    """Версия из цикла, который есть в таблице, а не просто старше всех ее циклов."""
    cycles = ((table().get("products") or {}).get(p) or {}).get("cycles") or []
    return any(ver == c or ver.startswith(c + ".") for c, _e in cycles)


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
        # метка - только цикл из таблицы: у clickhouse-server:25.6 в ней 22.04 базового образа Ubuntu
        if lv and _in_table(ip, lv):
            out[ip] = (lv, "label")
        elif tv:
            out[ip] = (tv, "tag")
        else:
            missing = ip
    return out, missing


def host_versions(items) -> list[dict]:
    """Пакеты сторонних репозиториев: [{"p", "v", "pkg", "full"}], для servers.sw_host; full - версия
    пакета как есть, по ней команда отката (apt install nginx=1.27.4-1~jammy)."""
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
                x = seen.setdefault((p, v), {"p": p, "v": v, "pkg": name, "full": ver})
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


def mute_key(g: dict) -> str:
    """Решение по циклу на этой ноде: swver:soon:grafana-13.1, swver:eol:postgresql-12."""
    return f"{MUTE_PREFIX}{g['state']}:{g['key']}"


def inventory(rep: dict, sw_host, now: datetime, mutes: list[str] | None = None) -> dict:
    """Все версии сервера по продукту и циклу: {"items": [...], "unknown": [...], "old": [...]}.
    items: {"key", "p", "name", "cycle", "ver", "eol", "state", "left", "where": [{"kind", "name", "src"}]},
    у кончившихся и скорых еще "mkey" и "muted" - решение человека (ключ в alert_mutes)."""
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
                                           "ver": str(h["v"]), "full": str(h.get("full") or "")})
    order = {"eol": 0, "soon": 1, "ok": 2}
    items = sorted(groups.values(), key=lambda g: (order[g["state"]], g["name"].lower(), g["cycle"]))
    got = set(mutes or [])
    for g in items:
        if g["state"] != "ok":
            g["mkey"] = mute_key(g)
            g["muted"] = g["mkey"] in got
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


def advice(rep: dict, sw_host, now: datetime, mutes: list[str] | None = None) -> list[dict]:
    """Что обновить: [{"key", "level", "name", "what", "text"}], строка на продукт: 2 - без поддержки,
    1 - скоро; what - продукт и циклы без мест ("PHP 7.3 и 7.4") для свернутой строки на главной.
    На одном хосте бывает PHP 5.6, 7.0, 7.1 и 7.3 сразу - это одна строка, циклы по отдельности
    в карточке. Принятое решением человека (mutes) сюда не идет: это не дело, а решение."""
    by: dict[tuple[str, str], list[dict]] = {}
    for g in inventory(rep or {}, sw_host, now, mutes)["items"]:
        if g["state"] in ("eol", "soon") and not g["muted"]:
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


# --- как исправить --------------------------------------------------------------------------

# Базы: новая версия при первом запуске переводит данные на свой формат (обратно только из копии),
# а PostgreSQL нового major старые файлы не читает вовсе. Тег им командой не меняем, путь - словами.
# Redis и Valkey не здесь: новая версия сама читает старые RDB и AOF
_STATEFUL = frozenset({"postgresql", "mysql", "mariadb", "mongodb", "elasticsearch", "clickhouse", "rabbitmq",
                       "zookeeper"})
_HINT = {"grafana": "hint_grafana", "traefik": "hint_traefik", "redis": "hint_redis", "valkey": "hint_valkey",
         "php": "hint_runtime", "nodejs": "hint_runtime", "python": "hint_runtime", "ruby": "hint_runtime",
         "eclipse-temurin": "hint_runtime", "alpine-linux": "hint_os", "debian": "hint_os", "ubuntu": "hint_os",
         "centos": "hint_os"}
_TAG_VER = re.compile(r"^(v?)(\d+(?:\.\d+)*)(.*)$")
_BAK = "/var/backups/kervax-upgrade"

# Первая команда у контейнера: compose-проект по меткам (агент их не присылает) и тома на запись -
# что копировать для отката. Остальные команды берут C, P, S, F, A, M, B отсюда, в том же окне
_PREP = "\n".join([
    "C={c}",
    "IFS='|' read -r P S D F < <(sudo docker inspect -f '{{{{index .Config.Labels \"com.docker.compose.project\"}}}}|"
    "{{{{index .Config.Labels \"com.docker.compose.service\"}}}}|"
    "{{{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}}}|"
    "{{{{index .Config.Labels \"com.docker.compose.project.config_files\"}}}}' \"$C\")",
    "A=$(echo \"$F\" | sed 's/^/-f /; s/,/ -f /g')",
    "M=$(sudo docker inspect -f '{{{{range .Mounts}}}}{{{{if .RW}}}}{{{{println .Source}}}}{{{{end}}}}{{{{end}}}}' \"$C\""
    " | grep -vE '^/(proc|sys|dev|run|var/run)(/|$)')",
    "B=" + _BAK + "/$C",
    "echo \"project=$P service=$S dir=$D files=$F\"; [ -n \"$M\" ] && sudo du -sh $M",
])
_UP = 'sudo docker compose -p "$P" $A up -d --no-deps "$S"'
_CHECK = "\n".join([
    "sudo docker inspect -f '{{.Config.Image}} {{.State.Status}}{{if .State.Health}} {{.State.Health.Status}}{{end}}"
    " {{.State.StartedAt}}' \"$C\"",
    'sudo docker logs --since 10m "$C" 2>&1 | tail -n 30',
])
# compose-файлы с .env и тома одним архивом при остановленном контейнере: что новая версия перепишет
# при первом запуске, старая уже может не прочитать
_COPY = ('sudo docker stop -t {t} "$C" && sudo tar -czpf "$B/$T.tgz" --numeric-owner $(echo "$F" | tr , \' \')'
         ' $(sudo test -f "$D/.env" && echo "$D/.env") $M; sudo docker start "$C"')
_PG_DUMP = "\n".join([
    'T=$(date +%F-%H%M); sudo install -d -m 700 "$B"',
    'sudo docker exec "$C" sh -c \'pg_dumpall -U "${POSTGRES_USER:-postgres}"\' | gzip | sudo tee "$B/$T.sql.gz" >/dev/null',
    'sudo ls -lh "$B"',
])
# только строка "image: <старый образ>": другие сервисы с похожим тегом не задеваются, а образ из
# переменной (.env) не найдется - grep это покажет, и up не запустится
_SED_IMG = (r'''for f in $(echo "$F" | tr , ' '); do sudo sed -i -E "s#^([[:space:]]*image:[[:space:]]*[\"']?)'''
            r'''{old}([\"']?[[:space:]]*)\$#\1{new}\2#" "$f"; done''')
_DPKG_LS = r'''dpkg-query -W -f='${{db:Status-Abbrev}}|${{Package}}\n' 'php{v}-*' 2>/dev/null | awk -F'|' '$1 ~ /^ii/ {{print $2}}' '''
_NODE_REPO = r"""grep -rl 'deb.nodesource.com/node_' /etc/apt/sources.list.d/ | xargs -r sudo sed -i 's#node_[0-9]*\.x#node_{v}.x#g'"""


def target(p: str, cycle: str, today: date) -> tuple[str | None, list[str]]:
    """Куда обновляться и что еще подойдет (три ближайших): самый новый поддерживаемый цикл той же
    основной версии (Grafana 13.1 -> 13.2, без ломки), а нет такого - самый новый поддерживаемый
    вообще (PostgreSQL 12 -> 18: переезд тот же). Поддерживаемый - без срока или срок дальше
    WARN_DAYS, иначе через месяц тот же пункт; таких нет - хотя бы не кончившийся."""
    cur = _num(cycle)
    cycles = ((table().get("products") or {}).get(p) or {}).get("cycles") or []
    newer = sorted(((c, _state(e, today)[0]) for c, e in cycles if _num(c) > cur), key=lambda x: _num(x[0]))
    ok = [c for c, st in newer if st == "ok"] or [c for c, st in newer if st == "soon"]
    if not ok:
        return None, []
    best = ([c for c in ok if _num(c)[:1] == cur[:1]] or ok)[-1]
    return best, [c for c in ok if c != best][-3:]


def new_tag(p: str, tag: str, to: str) -> str | None:
    """Тег той же формы на цикл to: 13.1.0 -> 13.2, 16-alpine -> 18-alpine, v2.11.57 -> v3.7,
    bullseye-slim -> trixie-slim. None - в теге нет версии (latest, alpine)."""
    names = _CODENAMES.get(p) or {}
    for name in names:
        if tag.startswith(name):
            return next((k for k, v in names.items() if v == to), to) + tag[len(name):]
    m = _TAG_VER.match(tag)
    return m.group(1) + to + m.group(3) if m else None


def _retag(image: str, tag: str) -> str:
    base = image.split("@", 1)[0]
    if ":" in base.rsplit("/", 1)[-1]:
        base = base.rsplit(":", 1)[0]
    return f"{base}:{tag}"


def _mode(p: str, c: dict) -> str:
    """Как уйти с версии в контейнере: db - база, копия и путь словами; image - официальный образ с
    версией в теге, сменить тег; pull - тег без версии, скачать свежий; rebuild - образ собран на ноде
    поверх старой базы, пересобрать; ci - собран не здесь (свой реестр, чужой образ с версией в теге);
    dangling - имя образа уже перевешено на другую сборку."""
    image = str(c.get("image") or "")
    if p in _STATEFUL:
        return "db"
    if image.startswith("sha256:"):
        return "dangling"
    reg, path, tag = split_image(image)
    if image_product(image) == p:
        return "image" if new_tag(p, tag, "0") else "pull"
    if not reg and "/" not in path:
        return "rebuild"
    return "pull" if not reg and not re.search(r"\d", tag) else "ci"


def _env_of(p: str, c: dict) -> str:
    """Откуда версия у образа приложения: PHP_VERSION=8.1.31."""
    env = c.get("ver") if isinstance(c.get("ver"), dict) else {}
    for k, pp in _ENV.items():
        if pp == p and env.get(k):
            return f"{k}={env[k]}"
    if _BITNAMI.get(str(env.get("BITNAMI_APP_NAME") or "").lower()) == p and env.get("APP_VERSION"):
        return f"APP_VERSION={env['APP_VERSION']}"
    return ""


def _rollback(image: str | None) -> str:
    """Откат: тома и compose-файлы из последнего архива (то, что успела записать новая версия,
    остается рядом с суффиксом .kervax-new), у тега без версии - еще и прежний образ под старым именем."""
    retag = f' && sudo docker tag "kervax-old:$C" {shlex.quote(image)}' if image else ""
    return "\n".join([
        'X=$(sudo sh -c "ls -t $B/*.tgz" | head -1); echo "backup: $X"',
        '[ -n "$X" ] && sudo docker stop -t 60 "$C" && ( set -e; for m in $M; do sudo mv -T "$m" "$m.kervax-new"; done;'
        ' sudo tar -C / -xzpf "$X" --numeric-owner )' + retag + " && " + _UP,
    ])


def _container_steps(p: str, mode: str, c: dict, a: dict) -> list[dict]:
    if not a["to"]:
        return [{"id": "no_target"}]
    image = str(c.get("image") or "")
    prep = {"id": "c_prep", "cmd": _PREP.format(c=shlex.quote(str(c.get("name") or "")))}
    stamp = 'T=$(date +%F-%H%M); sudo install -d -m 700 "$B"'
    if mode == "db":
        steps = [prep] + ([{"id": "db_dump_pg", "cmd": _PG_DUMP}] if p == "postgresql" else [])
        steps.append({"id": "db_copy", "cmd": "\n".join([stamp, _COPY.format(t=120), 'sudo ls -lh "$B"'])})
        return steps + [{"id": "path_" + p}] + ([] if p == "postgresql" else [{"id": "db_inplace"}])
    if mode == "dangling":
        return [{"id": "c_dangling"}]
    major = _num(a["to"])[:1] != _num(a["from"])[:1]
    pre = [{"id": "major"}] if major else []
    # у Traefik подсказка - про переход 2 на 3, внутри основной версии она ни к чему
    pre += [{"id": _HINT[p]}] if p in _HINT and (major or p != "traefik") else []
    if mode == "ci":
        return pre + [{"id": "c_ci"}]
    backup = {"id": "c_backup", "cmd": "\n".join([
        stamp, "sudo docker tag \"$(sudo docker inspect -f '{{.Image}}' \"$C\")\" \"kervax-old:$C\"",
        _COPY.format(t=60), 'sudo ls -lh "$B"'])}
    check = {"id": "c_check", "cmd": _CHECK}
    if mode == "image":
        old, new = image, a["new"]
        pat = re.sub(r"([.\[\]()*+?{}|^$\\])", r"\\\1", old)
        apply = "\n".join([_SED_IMG.format(old=pat, new=new),
                           f"sudo grep -nF {shlex.quote(new)} $(echo \"$F\" | tr , ' ') && {_UP}"])
        return pre + [prep] + [{"id": "img_pull", "cmd": f"sudo docker pull {shlex.quote(new)}"}, backup,
                               {"id": "img_apply", "cmd": apply}, check, {"id": "c_rollback", "cmd": _rollback(None)}]
    if mode == "pull":
        apply = f'sudo docker compose -p "$P" $A pull "$S" && {_UP}'
        return pre + [prep] + [backup, {"id": "pull_apply", "cmd": apply}, check,
                               {"id": "c_rollback", "cmd": _rollback(image)}]
    find = "sudo grep -rn --include='Dockerfile*' --include='*.Dockerfile' '^FROM' \"$D\""
    # сервис без build: в compose не собрать, его образ можно только скачать заново (pull пропускает собираемые)
    build = (f'sudo docker compose -p "$P" $A pull --ignore-buildable "$S"; '
             f'sudo docker compose -p "$P" $A build --pull "$S" && {_UP}')
    return pre + [prep] + [{"id": "rb_from", "cmd": find}, backup, {"id": "rb_build", "cmd": build}, check,
                           {"id": "c_rollback", "cmd": _rollback(image)}]


def _host_steps(p: str, w: dict, rep: dict, a: dict) -> list[dict]:
    """Пакеты стороннего репозитория на хосте. Команды - только для apt; базы, как и в контейнере,
    командой не обновляются."""
    if not a["to"]:
        return [{"id": "no_target"}]
    path = [{"id": "path_" + p}] if p in _STATEFUL else [{"id": _HINT[p]}] if p in ("redis", "valkey") else []
    if not re.match(r"(Ubuntu|Debian)\b", str(rep.get("os") or "")):
        return [{"id": "h_rpm"}] + path
    old, new, full = a["from"], a["to"], str(w.get("full") or "")
    mk = f"sudo install -d -m 700 {_BAK}"
    if p == "php":
        steps = [{"id": "h_php_install", "cmd": "\n".join([
            "sudo apt update",
            _DPKG_LS.format(v=old) + f"| sed 's/^php{re.escape(old)}-/php{new}-/' | while read -r p; do apt-cache show"
            ' "$p" >/dev/null 2>&1 && echo "$p"; done | xargs -r sudo apt install -y'])},
            {"id": "h_php_switch"}]
        chk = [f"php{new} -v | head -1"]
        if "-fpm" in str(w.get("name") or ""):
            chk.append(f"sudo php-fpm{new} -t && systemctl is-active php{new}-fpm")
        return steps + [{"id": "h_php_check", "cmd": "\n".join(chk)},
                        {"id": "h_php_remove", "cmd": "sudo apt-get remove $(" + _DPKG_LS.format(v=old).strip() + ")"}]
    if p == "nodejs":
        back = f"nodejs={shlex.quote(full)}" if full else "\"nodejs=$(apt-cache madison nodejs | awk 'NR==1{print $3}')\""
        return [{"id": "h_node_repo", "cmd": "\n".join([_NODE_REPO.format(v=new), "sudo apt update && sudo apt install -y nodejs"])},
                {"id": "h_node_check", "cmd": "node -v && npm -v"},
                {"id": "h_node_rollback", "cmd": "\n".join([
                    _NODE_REPO.format(v=old), f"sudo apt update && sudo apt install -y --allow-downgrades {back}"])}]
    if p in ("nginx", "haproxy"):
        etc = f"{mk} && sudo tar -czpf {_BAK}/{p}-etc-$(date +%F-%H%M).tgz /etc/{p}"
        if p == "haproxy":
            return [{"id": "h_etc_backup", "cmd": etc}, {"id": "h_haproxy"},
                    {"id": "h_haproxy_check", "cmd": "sudo haproxy -c -f /etc/haproxy/haproxy.cfg && systemctl is-active haproxy"}]
        steps = [{"id": "h_etc_backup", "cmd": etc},
                 {"id": "h_nginx_upgrade", "cmd": "sudo apt update && sudo apt install --only-upgrade -y nginx"},
                 {"id": "h_nginx_check", "cmd": "nginx -v && sudo nginx -t && systemctl is-active nginx"}]
        return steps + ([{"id": "h_apt_rollback", "cmd": f"sudo apt install -y --allow-downgrades nginx={shlex.quote(full)}"}]
                        if full else [])
    if p == "postgresql":
        dump = (f"{mk} && (cd / && sudo -u postgres pg_dumpall --cluster {old}/main) | gzip"
                f" | sudo tee {_BAK}/pg-{old}-$(date +%F-%H%M).sql.gz >/dev/null && sudo ls -lh {_BAK}")
        return [{"id": "h_pg_dump", "cmd": dump}, {"id": "h_pg_path", "cmd": "pg_lsclusters"}]
    if p in ("mysql", "mariadb"):
        dump = (f"{mk} && sudo sh -c '$(command -v mariadb-dump || command -v mysqldump) --all-databases"
                f" --single-transaction --routines --events' | gzip | sudo tee {_BAK}/mysql-$(date +%F-%H%M).sql.gz"
                f" >/dev/null && sudo ls -lh {_BAK}")
        return [{"id": "h_my_dump", "cmd": dump}] + path + [{"id": "h_repo"}]
    if p == "rabbitmq":
        defs = f"{mk} && sudo rabbitmqctl export_definitions {_BAK}/rabbitmq-defs-$(date +%F-%H%M).json"
        return [{"id": "h_rabbit_defs", "cmd": defs}] + path + [{"id": "h_repo"}]
    if path:
        return [{"id": "h_db_copy"}] + path + [{"id": "h_repo"}]
    return [{"id": "h_repo"}]


def fix(g: dict, rep: dict, now: datetime) -> list[dict]:
    """Как уйти с цикла g (элемент inventory) на поддерживаемый: способы [{"name", "kind", "mode",
    "others", "args", "steps"}]. Контейнеры с одним образом - один способ: команды для первого, имена
    остальных в others. args - для текстов шагов: продукт, с какой версии, на какую (to), что еще
    подойдет (more), контейнер, образ сейчас и новый, откуда версия (env), пакеты хоста. steps -
    [{"id", "cmd"?}], как у ОС."""
    today = now.astimezone(timezone.utc).date()
    to, more = target(g["p"], g["cycle"], today)
    base = {"name": g["name"], "from": g["cycle"], "to": to or "", "more": ", ".join(more)}
    dk = (rep or {}).get("docker") if isinstance((rep or {}).get("docker"), dict) else {}
    conts = {str(c.get("name") or ""): c for c in dk.get("containers") or [] if isinstance(c, dict)}
    blocks: dict[tuple, dict] = {}
    for w in g["where"]:
        if w["kind"] == "container":
            c = conts.get(w["name"]) or {"name": w["name"]}
            image = str(c.get("image") or "")
            mode = _mode(g["p"], c)
            k = ("c", mode, image)
            if k in blocks:
                blocks[k]["others"].append(w["name"])
                continue
            a = base | {"c": w["name"], "image": image, "env": _env_of(g["p"], c)}
            if mode == "image" and to:
                a["new"] = _retag(image, new_tag(g["p"], split_image(image)[2], to) or to)
            blocks[k] = {"name": w["name"], "kind": "container", "mode": mode, "others": [], "args": a,
                         "steps": _container_steps(g["p"], mode, c, a)}
        else:
            a = base | {"pkg": w["name"], "full": str(w.get("full") or "")}
            blocks[("h", w["name"])] = {"name": w["name"], "kind": "host", "mode": "host", "others": [], "args": a,
                                       "steps": _host_steps(g["p"], w, rep or {}, a)}
    return list(blocks.values())[:FIX_MAX]


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
