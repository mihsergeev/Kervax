"""Версии без поддержки (app/software.py): что стоит в контейнерах и на хосте и когда у этого
кончается поддержка по снимку endoflife.date."""
from datetime import date, datetime, timezone

from sqlalchemy import select

from app import software
from app.db import Base, create_engine_and_factory
from app.models import Server, ServerPackages

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
TBL = {"built": "2026-10-18", "source": "endoflife.date", "products": {
    "postgresql": {"name": "PostgreSQL", "cycles": [["18", "2030-11-14"], ["14", "2026-11-12"], ["12", "2024-11-21"],
                                                     ["9.6", "2021-11-11"]]},
    "php": {"name": "PHP", "cycles": [["8.4", "2028-12-31"], ["7.4", "2022-11-28"]]},
    "nginx": {"name": "nginx", "cycles": [["1.30", False], ["1.27", "2025-06-24"]]},
    "redis": {"name": "Redis", "cycles": [["8.10", False], ["6.0", True]]},
    "valkey": {"name": "Valkey", "cycles": [["8.1", "2030-01-01"]]},
    "mariadb": {"name": "MariaDB", "cycles": [["10.6", "2026-07-06"], ["11.4", "2029-05-29"]]},
    "rabbitmq": {"name": "RabbitMQ", "cycles": [["3.8", "2022-07-31"]]},
    "traefik": {"name": "Traefik", "cycles": [["2.11", "2026-09-07"]]},
    "nodejs": {"name": "Node.js", "cycles": [["18", "2025-04-30"]]},
    "eclipse-temurin": {"name": "Java", "cycles": [["8", "2030-12-31"], ["17", "2027-10-31"]]},
    "debian": {"name": "Debian", "cycles": [["10", "2024-06-30"]]},
}}


def _tbl(monkeypatch):
    monkeypatch.setattr(software, "table", lambda: TBL)


def test_real_table_is_complete():
    t = software.table()
    assert t["source"] == "endoflife.date" and date.fromisoformat(t["built"])
    for code in ("php", "nodejs", "postgresql", "mysql", "mariadb", "redis", "nginx", "traefik", "rabbitmq"):
        cy = t["products"][code]["cycles"]
        assert cy and all(isinstance(e, bool) or date.fromisoformat(e) for _c, e in cy), code


def test_parsing(monkeypatch):
    _tbl(monkeypatch)
    assert software.split_image("docker.io/library/nginx:1.25-alpine@sha256:ab") == ("", "nginx", "1.25-alpine")
    assert software.split_image("registry.example.com:5000/shop/nginx:prod") == ("registry.example.com:5000", "shop/nginx", "prod")
    assert software.split_image("localhost/app") == ("localhost", "app", "")
    assert software.norm("1:10.6.16+maria~ubu2204") == "10.6.16" and software.norm("jdk8u412-b08") == "8"
    assert software.norm("jdk-17.0.11+9") == "17.0.11" and software.norm("v2.11.57") == "2.11.57"
    assert software.norm("latest") == ""
    # самый длинный цикл: 9.6.24 - это 9.6, у 18 цикла 18.x нет
    assert software.cycle_of("postgresql", "9.6.24") == ("9.6", "2021-11-11")
    assert software.cycle_of("postgresql", "18.6") == ("18", "2030-11-14")
    assert software.cycle_of("postgresql", "13.1") is None and software.cycle_of("nope", "1") is None
    # старше самого старого цикла таблицы - без поддержки; новее таблицы - не знаем
    assert software.cycle_of("postgresql", "8.4.22") == ("8.4", True)
    assert software.cycle_of("postgresql", "19.1") is None
    assert software.cycle_of("nginx", "1.18.0") == ("1.18", True)


def _c(name, image, **kw):
    return {"name": name, "image": image, "state": "running", **kw}


def test_container_versions(monkeypatch):
    _tbl(monkeypatch)
    cv = software.container_versions
    # тег известного образа, latest - версии нет
    assert cv(_c("db", "postgres:12"), {}) == ({"postgresql": ("12", "tag")}, None)
    assert cv(_c("web", "nginx:latest"), {}) == ({}, "nginx")
    assert cv(_c("web", "nginx:1.27-alpine"), {}) == ({"nginx": ("1.27", "tag")}, None)
    # окружение образа точнее тега, и оно есть у собранных поверх образов приложений
    assert cv(_c("web", "nginx:latest", ver={"NGINX_VERSION": "1.27.4"}), {}) == ({"nginx": ("1.27.4", "env")}, None)
    assert cv(_c("app", "registry.example.com/shop-app:prod", ver={"PHP_VERSION": "7.4.33"}), {}) == (
        {"php": ("7.4.33", "env")}, None)
    # чужой реестр - тег не версия продукта (shop/nginx:1.0.3 - версия приложения)
    assert cv(_c("x", "registry.example.com/shop/nginx:1.0.3"), {}) == ({}, None)
    # инвентарь СУБД точнее всего; valkey и MariaDB инвентарь зовет redis и mysql
    assert cv(_c("db", "postgres:latest", ver={"PG_MAJOR": "12"}), {"db": ("pg", "12.22")}) == (
        {"postgresql": ("12.22", "db")}, None)
    assert cv(_c("kv", "valkey/valkey:8"), {"kv": ("redis", "8.1.1")}) == ({"valkey": ("8.1.1", "db")}, None)
    assert cv(_c("my", "mariadb:lts"), {"my": ("mysql", "10.6.21-MariaDB-ubu2004")}) == (
        {"mariadb": ("10.6.21", "db")}, None)
    # bitnami: имя приложения и APP_VERSION
    assert cv(_c("r", "bitnami/redis:7.4", ver={"BITNAMI_APP_NAME": "redis", "APP_VERSION": "6.0.20"}), {}) == (
        {"redis": ("6.0.20", "env")}, None)
    # метка версии - только если это цикл продукта, у traefik тега версии часто нет
    assert cv(_c("edge", "traefik:latest", img_ver="v2.11.20"), {}) == ({"traefik": ("2.11.20", "label")}, None)
    assert cv(_c("edge", "traefik:latest", img_ver="build-77"), {}) == ({}, "traefik")
    # кодовое имя дистрибутива и Java
    assert cv(_c("old", "debian:buster-slim"), {}) == ({"debian": ("10", "tag")}, None)
    assert cv(_c("j", "eclipse-temurin:17-jre", ver={"JAVA_VERSION": "jdk-17.0.11+9"}), {}) == (
        {"eclipse-temurin": ("17.0.11", "env")}, None)


def test_host_packages(monkeypatch):
    items = [["php7.4-fpm", "7.4.33-1+ubuntu20.04.1+deb.sury.org+1"], ["php7.4-cli", "7.4.33-1+ubuntu20.04.1+deb.sury.org+1"],
             ["php8.1-fpm", "8.1.2-1ubuntu2.19"],  # дистрибутив: срок вместе с ОС
             ["postgresql-12", "12.22-1.pgdg20.04+1"], ["postgresql-14", "14.19-0ubuntu0.22.04.1"],
             ["nginx", "1.27.4-1~jammy"], ["nodejs", "18.20.4-1nodesource1"],
             ["mariadb-server", "1:10.6.16+maria~ubu2204"], ["redis-server", "5:6.0.16-1ubuntu1"], ["openssl", "3.0.2"]]
    assert software.host_versions(items) == [
        {"p": "mariadb", "v": "10.6.16", "pkg": "mariadb-server"},
        {"p": "nginx", "v": "1.27.4", "pkg": "nginx"},
        {"p": "nodejs", "v": "18.20.4", "pkg": "nodejs"},
        {"p": "php", "v": "7.4.33", "pkg": "php7.4-fpm, php7.4-cli"},
        {"p": "postgresql", "v": "12.22", "pkg": "postgresql-12"},
    ]
    assert software.host_versions(None) == [] and software.host_versions([["x"], "y"]) == []


def _rep(*cs, db=()):
    return {"docker": {"containers": list(cs)}, "db_stats": [{"engine": e, "container": c, "version": v} for e, c, v in db]}


def test_inventory_and_advice(monkeypatch):
    _tbl(monkeypatch)
    old = int(datetime(2024, 3, 1, tzinfo=timezone.utc).timestamp())
    rep = _rep(_c("db-1", "postgres:12"), _c("db-2", "postgres:12"), _c("db-3", "postgres:14", img_created=old),
               _c("web", "nginx:latest"), _c("q", "rabbitmq:3.8.5-management"),
               _c("app", "shop-app", ver={"PHP_VERSION": "8.4.3"}),
               {"name": "gone", "image": "postgres:9.6", "state": "exited"})
    inv = software.inventory(rep, [{"p": "php", "v": "7.4.33", "pkg": "php7.4-fpm"}], NOW)
    keys = [(g["key"], g["state"]) for g in inv["items"]]
    # сначала без поддержки, потом скоро, потом в порядке; остановленный контейнер не в счет
    assert keys == [("php-7.4", "eol"), ("postgresql-12", "eol"), ("rabbitmq-3.8", "eol"), ("postgresql-14", "soon"),
                    ("php-8.4", "ok")]
    pg12 = inv["items"][1]
    assert [w["name"] for w in pg12["where"]] == ["db-1", "db-2"]
    assert software.item_text(pg12) == "PostgreSQL 12 без поддержки с 21.11.2024: контейнеры db-1, db-2"
    assert software.item_text(inv["items"][0]) == "PHP 7.4 без поддержки с 28.11.2022: на хосте (php7.4-fpm)"
    assert software.item_text(inv["items"][3]) == "поддержка PostgreSQL 14 кончится 12.11.2026, через 25 дней: контейнер db-3"
    assert inv["unknown"] == [{"name": "web", "image": "nginx:latest", "p": "nginx", "pname": "nginx"}]
    assert [(o["name"], o["age_days"] >= 365) for o in inv["old"]] == [("db-3", True)]
    adv = software.advice(rep, None, NOW)
    assert [(a["key"], a["level"]) for a in adv] == [("postgresql-eol", 2), ("rabbitmq-eol", 2), ("postgresql-soon", 1)]
    # несколько циклов одного продукта - одна строка
    host = [{"p": "php", "v": "7.4.33", "pkg": "php7.4-fpm, php7.4-cli"}, {"p": "php", "v": "8.4.1", "pkg": "php8.4-fpm"},
            {"p": "postgresql", "v": "9.6.24", "pkg": "postgresql-9.6"}]
    adv = software.advice(_rep(_c("db", "postgres:12"), _c("old", "php:7.4-fpm")), host, NOW)
    assert [a["text"] for a in adv] == [
        "PHP 7.4 без поддержки с 28.11.2022: контейнер old, на хосте (php7.4-cli, php7.4-fpm)",
        "PostgreSQL 9.6 и 12 без поддержки: контейнер db, на хосте (postgresql-9.6)"]
    assert [a["what"] for a in adv] == ["PHP 7.4", "PostgreSQL 9.6 и 12"]
    # много контейнеров - список обрезается
    many = _rep(*[_c(f"db-{i}", "postgres:12") for i in range(7)])
    assert software.advice(many, None, NOW)[0]["text"].endswith("db-0, db-1, db-2, db-3 и еще 3")
    # срок без даты
    assert software.advice(_rep(_c("r", "redis:6.0")), None, NOW)[0]["text"] == "Redis 6.0 без поддержки: контейнер r"
    assert software.advice({}, None, NOW) == []


async def test_refresh_and_api(tmp_path, client, auth_headers, monkeypatch):
    _tbl(monkeypatch)
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 's.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Server(id=1, name="web", token_hash="x", enabled=True))
        s.add(Server(id=2, name="db", token_hash="y", enabled=True))
        s.add(ServerPackages(server_id=1, items=[["php7.4-fpm", "7.4.33-1+deb.sury.org+1"]], ev_ts=0))
        await s.commit()
    software._last_run = None
    await software.refresh(factory, NOW)
    async with factory() as s:
        got = {r.id: r.sw_host for r in await s.scalars(select(Server))}
    assert got == {1: [{"p": "php", "v": "7.4.33", "pkg": "php7.4-fpm"}], 2: None}
    await engine.dispose()
    software._last_run = None

    r = (await client.post("/api/servers", json={"name": "web"}, headers=auth_headers)).json()
    sid, ah = r["server"]["id"], {"Authorization": f"Bearer {r['token']}"}
    rep = {"agent_version": "2.35", "mem_used": 1, "mem_total": 2, "disks": [{"mount": "/", "used": 1, "total": 2}],
           "docker": {"present": True, "access": True, "containers": [
               _c("db-1", "postgres:12"), _c("web", "nginx:latest", ver={"NGINX_VERSION": "1.27.4"})]}}
    assert (await client.post("/api/agent/report", json=rep, headers=ah)).status_code == 200
    srv = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    assert [a["key"] for a in srv["sw_advice"]] == ["nginx-eol", "postgresql-eol"]
    sw = (await client.get(f"/api/servers/{sid}/software", headers=auth_headers)).json()
    assert sw["agent_ok"] and sw["built"] == "2026-10-18" and not sw["pkgs"]
    assert [(g["key"], g["text"].split(":")[0]) for g in sw["items"]] == [
        ("nginx-1.27", "nginx 1.27 без поддержки с 24.06.2025"), ("postgresql-12", "PostgreSQL 12 без поддержки с 21.11.2024")]
