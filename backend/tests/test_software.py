"""Версии без поддержки (app/software.py): что стоит в контейнерах и на хосте и когда у этого
кончается поддержка по снимку endoflife.date."""
import re
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
    "nodejs": {"name": "Node.js", "cycles": [["24", "2028-04-30"], ["18", "2025-04-30"]]},
    "eclipse-temurin": {"name": "Java", "cycles": [["8", "2030-12-31"], ["17", "2027-10-31"]]},
    "debian": {"name": "Debian", "cycles": [["10", "2024-06-30"]]},
    "clickhouse": {"name": "ClickHouse", "cycles": [["26.8", "2027-08-27"], ["25.8", "2026-08-28"]]},
    "grafana": {"name": "Grafana", "cycles": [["13.2", "2027-05-18"], ["13.1", "2027-03-20"], ["12.4", "2027-05-24"],
                                              ["11.6", "2026-05-01"]]},
    "alpine-linux": {"name": "Alpine", "cycles": [["3.24", "2028-06-01"], ["3.20", "2026-04-01"]]},
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
    # метка от базового образа (Ubuntu 22.04) старше всех циклов, но это не версия продукта: верим тегу
    assert cv(_c("ch", "clickhouse/clickhouse-server:25.8.4.13", img_ver="22.04"), {}) == (
        {"clickhouse": ("25.8.4.13", "tag")}, None)
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
        {"p": "mariadb", "v": "10.6.16", "pkg": "mariadb-server", "full": "1:10.6.16+maria~ubu2204"},
        {"p": "nginx", "v": "1.27.4", "pkg": "nginx", "full": "1.27.4-1~jammy"},
        {"p": "nodejs", "v": "18.20.4", "pkg": "nodejs", "full": "18.20.4-1nodesource1"},
        {"p": "php", "v": "7.4.33", "pkg": "php7.4-fpm, php7.4-cli", "full": "7.4.33-1+ubuntu20.04.1+deb.sury.org+1"},
        {"p": "postgresql", "v": "12.22", "pkg": "postgresql-12", "full": "12.22-1.pgdg20.04+1"},
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
    assert got == {1: [{"p": "php", "v": "7.4.33", "pkg": "php7.4-fpm", "full": "7.4.33-1+deb.sury.org+1"}], 2: None}
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
    # у пункта - как исправить и ключ решения; решение убирает его из Требует действий, в карточке он остается
    assert [(g["mkey"], g["muted"], g["fix"][0]["mode"]) for g in sw["items"]] == [
        ("swver:eol:nginx-1.27", False, "pull"), ("swver:eol:postgresql-12", False, "db")]
    r = await client.patch(f"/api/servers/{sid}", json={"alert_mutes": ["swver:eol:nginx-1.27"]}, headers=auth_headers)
    assert [a["key"] for a in r.json()["sw_advice"]] == ["postgresql-eol"]
    sw = (await client.get(f"/api/servers/{sid}/software", headers=auth_headers)).json()
    assert [(g["key"], g["muted"]) for g in sw["items"]] == [("nginx-1.27", True), ("postgresql-12", False)]


def test_decisions(monkeypatch):
    _tbl(monkeypatch)
    rep = _rep(_c("mon", "grafana/grafana:13.1.0"), _c("db", "postgres:12"))
    mutes = ["swver:soon:grafana-13.1"]
    inv = software.inventory(rep, None, NOW, mutes)
    assert [(g["key"], g["mkey"], g["muted"]) for g in inv["items"]] == [
        ("postgresql-12", "swver:eol:postgresql-12", False), ("grafana-13.1", "swver:soon:grafana-13.1", True)]
    assert [a["key"] for a in software.advice(rep, None, NOW, mutes)] == ["postgresql-eol"]
    # отложено до конца поддержки: когда она кончилась, пункт снова виден, решение нужно новое
    later = datetime(2027, 4, 1, tzinfo=timezone.utc)
    assert [a["key"] for a in software.advice(rep, None, later, mutes)] == ["grafana-eol", "postgresql-eol"]


def test_target_and_tag(monkeypatch):
    _tbl(monkeypatch)
    d = NOW.date()
    # своя основная версия, если в ней есть поддерживаемая (Grafana 13.1 -> 13.2, не 12.4)
    assert software.target("grafana", "13.1", d) == ("13.2", [])
    # иначе самая новая поддерживаемая; кончающаяся меньше чем через полгода (13.1, PostgreSQL 14) не в счет
    assert software.target("grafana", "11.6", d) == ("13.2", ["12.4"])
    assert software.target("postgresql", "12", d) == ("18", [])
    assert software.target("nginx", "1.27", d) == ("1.30", [])
    assert software.target("traefik", "2.11", d) == (None, [])
    monkeypatch.setattr(software, "table", lambda: {"products": {"x": {"cycles": [["2", "2027-01-01"], ["1", True]]}}})
    assert software.target("x", "1", d) == ("2", [])  # поддерживаемой дольше полугода нет - хотя бы живая
    nt = software.new_tag
    assert nt("grafana", "13.1.0", "13.2") == "13.2" and nt("postgresql", "16-alpine", "18") == "18-alpine"
    assert nt("traefik", "v2.11.57", "3.7") == "v3.7" and nt("debian", "bullseye-slim", "12") == "bookworm-slim"
    assert nt("ubuntu", "focal", "26.04") == "26.04"
    assert nt("nginx", "latest", "1.30") is None and nt("nginx", "", "1.30") is None
    assert software._retag("docker.io/library/nginx:1.27-alpine@sha256:ab", "1.30-alpine") == "docker.io/library/nginx:1.30-alpine"
    assert software._retag("registry.example.com:5000/web", "2") == "registry.example.com:5000/web:2"


def _item(rep, key, host=None):
    return next(g for g in software.inventory(rep, host, NOW)["items"] if g["key"] == key)


def _ids(b):
    return [s["id"] for s in b["steps"]]


def _cmds(blocks) -> str:
    return "\n".join(s.get("cmd", "") for b in blocks for s in b["steps"])


def _sed(cmd: str, text: str) -> str:
    """sed -E из команды на Python: что именно поменяется в compose-файле."""
    pat, rep = re.search(r'sed -i -E "s#(.*?)#(.*?)#" ', cmd).groups()
    pat, rep = (x.replace('\\"', '"').replace("\\$", "$").replace("[[:space:]]", "[ \t]") for x in (pat, rep))
    return re.sub(pat, rep, text, flags=re.M)


def test_fix_image(monkeypatch):
    _tbl(monkeypatch)
    rep = _rep(_c("mon-grafana", "grafana/grafana:13.1.0", binds=["/srv/mon/data"]))
    [b] = software.fix(_item(rep, "grafana-13.1"), rep, NOW)
    assert (b["name"], b["kind"], b["mode"], b["others"]) == ("mon-grafana", "container", "image", [])
    assert {k: b["args"][k] for k in ("name", "from", "to", "c", "new")} == {
        "name": "Grafana", "from": "13.1", "to": "13.2", "c": "mon-grafana", "new": "grafana/grafana:13.2"}
    assert _ids(b) == ["hint_grafana", "c_prep", "img_pull", "c_backup", "img_apply", "c_check", "c_rollback"]
    cmd = {s["id"]: s.get("cmd", "") for s in b["steps"]}
    # compose-проект и тома находит первая команда по меткам контейнера, агент их не присылает
    assert cmd["c_prep"].startswith("C=mon-grafana\n")
    assert '{{index .Config.Labels "com.docker.compose.project.config_files"}}' in cmd["c_prep"]
    assert "{{range .Mounts}}{{if .RW}}" in cmd["c_prep"] and "B=/var/backups/kervax-upgrade/$C" in cmd["c_prep"]
    # новый образ - заранее; копия при остановленном контейнере, старый образ под своим тегом
    assert cmd["img_pull"] == "sudo docker pull grafana/grafana:13.2"
    assert 'sudo docker stop -t 60 "$C" && sudo tar -czpf "$B/$T.tgz"' in cmd["c_backup"]
    assert '"$D/.env"' in cmd["c_backup"] and '"kervax-old:$C"' in cmd["c_backup"]
    # только строка image со старым образом, up - этого сервиса и только если замена нашлась
    yml = ("services:\n  grafana:\n    image: grafana/grafana:13.1.0\n  render:\n"
           "    image: \"grafana/grafana:13.1.0-ubuntu\"\n    labels: [grafana/grafana:13.1.0]\n")
    assert _sed(cmd["img_apply"], yml) == yml.replace("image: grafana/grafana:13.1.0\n", "image: grafana/grafana:13.2\n")
    assert _sed(cmd["img_apply"], "    image: 'grafana/grafana:13.1.0'\n") == "    image: 'grafana/grafana:13.2'\n"
    assert cmd["img_apply"].endswith(
        "sudo grep -nF grafana/grafana:13.2 $(echo \"$F\" | tr , ' ') && sudo docker compose -p \"$P\" $A up -d --no-deps \"$S\"")
    # откат: данные новой версии в сторону, архив на место, старый compose-файл с ним
    assert '"$m.kervax-new"' in cmd["c_rollback"] and 'tar -C / -xzpf "$X"' in cmd["c_rollback"]
    assert "kervax-old" not in cmd["c_rollback"]
    # смена основной версии - предупреждение
    rep = _rep(_c("edge", "traefik:v2.11.57"))
    monkeypatch.setattr(software, "table", lambda: TBL | {"products": TBL["products"] | {
        "traefik": {"name": "Traefik", "cycles": [["3.7", False], ["2.11", "2026-09-07"]]}}})
    [b] = software.fix(_item(rep, "traefik-2.11"), rep, NOW)
    assert _ids(b)[:3] == ["major", "hint_traefik", "c_prep"] and b["args"]["new"] == "traefik:v3.7"


def test_fix_db_without_upgrade_commands(monkeypatch):
    _tbl(monkeypatch)
    rep = _rep(_c("db-1", "postgres:12"), _c("db-2", "postgres:12"), _c("my", "mariadb:10.6"),
               _c("q", "rabbitmq:3.8.5-management"), db=[("pg", "db-1", "12.22")])
    [b] = software.fix(_item(rep, "postgresql-12"), rep, NOW)
    assert (b["mode"], b["name"], b["others"], b["args"]["to"]) == ("db", "db-1", ["db-2"], "18")
    assert _ids(b) == ["c_prep", "db_dump_pg", "db_copy", "path_postgresql"]
    assert "pg_dumpall -U \"${POSTGRES_USER:-postgres}\"" in _cmds([b]) and 'docker stop -t 120 "$C"' in _cmds([b])
    for key in ("postgresql-12", "mariadb-10.6", "rabbitmq-3.8"):
        blocks = software.fix(_item(rep, key), rep, NOW)
        cmds = _cmds(blocks)
        # базу командой не обновляем: ни нового образа, ни правки compose, ни пересоздания
        assert not re.search(r"docker pull|sed -i|up -d|compose -p|build", cmds), key
    [b] = software.fix(_item(rep, "mariadb-10.6"), rep, NOW)
    assert _ids(b) == ["c_prep", "db_copy", "path_mariadb", "db_inplace"]


def test_fix_app_images(monkeypatch):
    _tbl(monkeypatch)
    v = {"NGINX_VERSION": "1.27.4"}
    rep = _rep(_c("web", "nginx:latest", ver=v), _c("web-2", "nginx:latest", ver=v), _c("app-a", "app-a-web", ver=v),
               _c("api", "registry.example.com/shop/api:prod", ver=v), _c("stale", "sha256:0123abcd", ver=v),
               _c("gw", "nginx:1.27-alpine"))
    blocks = software.fix(_item(rep, "nginx-1.27"), rep, NOW)
    # контейнеры с одним образом - один способ, способов не больше FIX_MAX
    assert [(b["name"], b["mode"], b["others"]) for b in blocks] == [
        ("web", "pull", ["web-2"]), ("app-a", "rebuild", []), ("api", "ci", []), ("stale", "dangling", [])]
    pull, rebuild, ci, dangling = blocks
    assert _ids(pull) == ["c_prep", "c_backup", "pull_apply", "c_check", "c_rollback"]
    # тег без версии: откат - прежний образ под старым именем
    assert 'sudo docker tag "kervax-old:$C" nginx:latest && sudo docker compose' in _cmds([pull])
    assert _ids(rebuild) == ["c_prep", "rb_from", "c_backup", "rb_build", "c_check", "c_rollback"]
    assert 'build --pull "$S"' in _cmds([rebuild]) and "'^FROM'" in _cmds([rebuild])
    # собран не здесь - только слова, откуда версия
    assert _ids(ci) == ["c_ci"] and ci["args"]["env"] == "NGINX_VERSION=1.27.4" and _cmds([ci]).strip() == ""
    assert _ids(dangling) == ["c_dangling"]
    rep = _rep(_c("gw", "nginx:1.27-alpine"))
    [b] = software.fix(_item(rep, "nginx-1.27"), rep, NOW)
    assert b["mode"] == "image" and b["args"]["new"] == "nginx:1.30-alpine"


def test_fix_host(monkeypatch):
    _tbl(monkeypatch)
    host = [{"p": "php", "v": "7.4.33", "pkg": "php7.4-fpm, php7.4-cli", "full": "7.4.33-1+deb.sury.org+1"},
            {"p": "nodejs", "v": "18.20.4", "pkg": "nodejs", "full": "18.20.4-1nodesource1"},
            {"p": "nginx", "v": "1.27.4", "pkg": "nginx", "full": "1.27.4-1~jammy"},
            {"p": "postgresql", "v": "12.22", "pkg": "postgresql-12", "full": "12.22-1.pgdg22.04+1"},
            {"p": "mariadb", "v": "10.6.16", "pkg": "mariadb-server", "full": "1:10.6.16+maria~ubu2204"}]
    rep = {"os": "Ubuntu 22.04.5 LTS"}

    def one(key):
        [b] = software.fix(_item(rep, key, host), rep, NOW)
        assert b["kind"] == "host"
        return b, {s["id"]: s.get("cmd", "") for s in b["steps"]}

    b, cmd = one("php-7.4")
    assert _ids(b) == ["h_php_install", "h_php_switch", "h_php_check", "h_php_remove"] and b["args"]["to"] == "8.4"
    # рядом со старой, с теми же расширениями (каких нет для новой - пропускает)
    assert "'php7.4-*'" in cmd["h_php_install"] and "sed 's/^php7\\.4-/php8.4-/'" in cmd["h_php_install"]
    assert cmd["h_php_install"].endswith("xargs -r sudo apt install -y")
    assert "sudo php-fpm8.4 -t" in cmd["h_php_check"] and cmd["h_php_remove"].startswith("sudo apt-get remove $(dpkg-query")
    b, cmd = one("nodejs-18")
    assert "s#node_[0-9]*\\.x#node_24.x#g" in cmd["h_node_repo"]
    assert cmd["h_node_rollback"].endswith("--allow-downgrades nodejs=18.20.4-1nodesource1")
    b, cmd = one("nginx-1.27")
    assert _ids(b) == ["h_etc_backup", "h_nginx_upgrade", "h_nginx_check", "h_apt_rollback"]
    assert cmd["h_apt_rollback"] == "sudo apt install -y --allow-downgrades nginx='1.27.4-1~jammy'"
    # базы на хосте: дамп своего кластера и путь словами, без команд переезда
    b, cmd = one("postgresql-12")
    assert _ids(b) == ["h_pg_dump", "h_pg_path"] and "pg_dumpall --cluster 12/main" in cmd["h_pg_dump"]
    b, cmd = one("mariadb-10.6")
    assert _ids(b) == ["h_my_dump", "path_mariadb", "h_repo"]
    assert not re.search(r"pg_upgradecluster|pg_dropcluster|apt install -y (postgresql|mariadb)", _cmds(
        software.fix(_item(rep, "postgresql-12", host), rep, NOW) + software.fix(_item(rep, "mariadb-10.6", host), rep, NOW)))
    # не apt - только слова
    rep = {"os": "AlmaLinux 9.4 (Seafoam Ocelot)"}
    assert [(_ids(b), _cmds([b]).strip()) for b in software.fix(_item(rep, "postgresql-12", host), rep, NOW)] == [
        (["h_rpm", "path_postgresql"], "")]


def test_fix_commands_are_neutral(monkeypatch):
    """Команды одни для обоих языков панели: без кириллицы."""
    _tbl(monkeypatch)
    host = [{"p": "php", "v": "7.4.33", "pkg": "php7.4-fpm", "full": "7.4.33-1+deb.sury.org+1"},
            {"p": "postgresql", "v": "12.22", "pkg": "postgresql-12", "full": ""}]
    rep = _rep(_c("mon", "grafana/grafana:13.1.0"), _c("db", "postgres:12"), _c("web", "nginx:latest", ver={"NGINX_VERSION": "1.27.4"}))
    rep["os"] = "Debian GNU/Linux 12 (bookworm)"
    for g in software.inventory(rep, host, NOW)["items"]:
        if g["state"] != "ok":
            assert not re.search("[а-яА-Я]", _cmds(software.fix(g, rep, NOW))), g["key"]
