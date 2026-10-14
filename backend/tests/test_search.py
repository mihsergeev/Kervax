"""Поиск по парку (app/search.py): что находится, как сортируется и что видно учетке."""
from datetime import datetime, timezone

from app import search as search_mod
from app.models import Check, Server

REPORT_A = {
    "hostname": "web-a.internal",
    "os": "Ubuntu 22.04.4 LTS",
    "ips": ["203.0.113.10", "10.0.0.5"],
    "docker": {"version": "27.3.1", "compose": "2.29.7", "containers": [
        {"name": "shop-nginx-1", "image": "nginx:1.18", "state": "running", "binds": ["/app/shop/nginx.conf"]},
        {"name": "shop-db-1", "image": "postgres:16", "state": "running", "binds": []},
    ]},
    "listen": [
        {"p": "tcp", "u": "root", "ip": "0.0.0.0", "port": 80},
        {"p": "tcp", "u": "root", "ip": "::", "port": 80},
        {"p": "tcp", "u": "postgres", "ip": "127.0.0.1", "port": 5432},
        {"p": "tcp", "u": "root", "ip": "0.0.0.0", "port": 8080},
    ],
    "web_services": [{"kind": "nginx", "sites": ["shop.example.com", "~regex", "blog.example.com"]}],
    "db_stats": [{"engine": "PostgreSQL", "version": "16.4", "container": "shop-db-1",
                  "dbs": [{"name": "shop", "size": 1}, {"name": "postgres", "size": 1}]}],
    "db_engines": ["PostgreSQL", "Redis"],
    "services": [{"kind": "rabbitmq", "source": "контейнер shop-mq-1",
                  "queues": [{"name": "orders", "ready": 0, "vhost": "/"}]}],
    "kube": None,
    "os_upd": {"kernel": "6.8.0-45-generic"},
    "extras": {"changes": {
        "listen": {"80": "docker-proxy", "5432": "postgres", "8080": "java"},
        "users": [{"n": "deploy", "uid": 1001, "sh": "/bin/bash"}],
        "keys": [{"c": "ivan@laptop", "fp": "SHA256:abc", "u": "deploy", "t": "ED25519"},
                 {"c": "ivan@laptop", "fp": "SHA256:abc", "u": "root", "t": "ED25519"}],
        "units": ["docker.service", "shop-worker.service"],
        "cron": {"/etc/cron.d/shop-cleanup": "aa"},
        "ssh": {"banner": "OpenSSH_9.6p1"},
    }},
}
REPORT_B = {
    "hostname": "k8s-b",
    "ips": ["203.0.113.20"],
    "kube": {"pods": [
        {"name": "api-7d9f-x1", "ns": "prod", "ctrl": "deployment/api", "phase": "Running", "ready": True},
        {"name": "api-7d9f-x2", "ns": "prod", "ctrl": "deployment/api", "phase": "Pending", "ready": False},
    ]},
}


async def _fill(client) -> dict:
    search_mod._cache.clear()
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        a = Server(name="web-a", token_hash="t-a", group_name="shop", last_seen=now, last_report=REPORT_A,
                   external_ip="203.0.113.10", agent_version="2.27", note="диск скоро менять")
        b = Server(name="k8s-b", token_hash="t-b", group_name="k8s", last_seen=now, last_report=REPORT_B)
        s.add_all([a, b])
        c = Check(name="shop main", type="http", target="https://user:secret@shop.example.com/", group_name="shop")
        s.add(c)
        await s.commit()
        return {"a": a.id, "b": b.id, "c": c.id}


def _groups(j) -> dict:
    return {g["kind"]: g for g in j["groups"]}


async def test_search_finds_everything(client, auth_headers):
    ids = await _fill(client)

    async def q(text):
        r = await client.get("/api/search", params={"q": text}, headers=auth_headers)
        assert r.status_code == 200, r.text
        return _groups(r.json())

    g = await q("shop.example.com")
    assert g["site"]["items"][0] == {"title": "shop.example.com", "sub": ["web-a", "nginx"], "id": ids["a"],
                                     "section": "servers", "sec": "web", "tag": "", "hint_k": "", "hint_v": ""}
    # логин и пароль из адреса монитора в ответ не попадают
    chk = g["check"]["items"][0]
    assert chk["id"] == ids["c"] and chk["section"] == "sites" and "secret" not in str(chk)
    assert "~regex" not in str(await q("regex"))

    # по IP находится сервер, с подсказкой, что совпал адрес
    srv = (await q("203.0.113.10"))["server"]["items"][0]
    assert srv["id"] == ids["a"] and srv["hint_k"] == "ip" and srv["hint_v"] == "203.0.113.10"
    assert (await q("скоро менять"))["server"]["items"][0]["hint_k"] == "note"

    # образ и путь у контейнера, раздел - докер
    g = await q("nginx:1.18")
    assert g["container"]["items"][0]["title"] == "shop-nginx-1" and g["container"]["items"][0]["section"] == "docker"
    assert (await q("/app/shop"))["container"]["items"][0]["hint_k"] == "path"

    # номер порта - только точно: 80 не находит 8080; адреса одного порта сливаются
    g = await q("80")
    assert [x["title"] for x in g["port"]["items"]] == [":80"]
    assert g["port"]["items"][0]["sub"] == ["web-a", "tcp", "docker-proxy", "0.0.0.0, ::"]
    assert (await q(":5432"))["port"]["items"][0]["sec"] == "conn"
    # по имени процесса порт находится, кроме прокси докера
    assert (await q("java"))["port"]["items"][0]["title"] == ":8080"
    assert "port" not in await q("docker-proxy")

    # базы: служебные не ищутся, движок с версией - да
    assert (await q("shop"))["db"]["items"][0]["tag"] in ("database", "engine")
    assert "db" not in await q("template1")
    assert all(x["title"] != "postgres" for x in (await q("postgres"))["db"]["items"])
    assert (await q("postgresql 16"))["db"]["items"][0]["title"] == "PostgreSQL 16.4"
    assert any(x["title"] == "Redis" for x in (await q("redis"))["db"]["items"])
    assert (await q("orders"))["queue"]["items"][0]["section"] == "services"

    # поды сгруппированы по контроллеру, неготовый помечен
    pod = (await q("api-7d9f"))["pod"]["items"][0]
    assert pod["title"] == "deployment/api" and pod["sub"] == ["k8s-b", "prod", "2"] and pod["tag"] == "bad"

    # пользователи и ключи (ключ один раз, хоть и у двух пользователей)
    assert (await q("deploy"))["user"]["items"][0]["sec"] == "proc"
    keys = (await q("ivan@"))["key"]
    assert keys["total"] == 1 and keys["items"][0]["title"] == "ivan@laptop"
    assert (await q("SHA256:abc"))["key"]["items"][0]["hint_k"] == "fp"
    assert (await q("shop-worker"))["unit"]["items"][0]["title"] == "shop-worker.service"
    assert (await q("cron.d"))["cron"]["total"] == 1

    # версии: метка тоже ищется
    v = (await q("docker 27"))["version"]["items"]
    assert [(x["title"], x["tag"]) for x in v] == [("27.3.1", "docker")]
    assert (await q("openssh_9"))["version"]["items"][0]["tag"] == "ssh"
    assert (await q("6.8.0"))["version"]["items"][0]["tag"] == "kernel"

    # слишком короткий запрос - пусто, все слова должны найтись
    assert (await q("a")) == {}
    assert "container" not in await q("nginx postgres")


async def test_search_respects_access(client, auth_headers):
    ids = await _fill(client)
    r = await client.post("/api/users", headers=auth_headers, json={
        "username": "shopviewer", "password": "shopviewer-pass-2026", "role": "viewer",
        "server_groups": ["k8s"], "site_groups": ["nope"], "sections": ["servers", "kuber"]})
    assert r.status_code in (200, 201), r.text
    login = await client.post("/api/auth/login", json={"username": "shopviewer", "password": "shopviewer-pass-2026"})
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}

    async def q(text):
        return _groups((await client.get("/api/search", params={"q": text}, headers=h)).json())

    # чужая группа серверов и чужие мониторы не находятся
    assert await q("shop") == {}
    assert await q("203.0.113.10") == {}
    # свои находятся, но только в разрешенных разделах
    assert (await q("api"))["pod"]["items"][0]["id"] == ids["b"]
    assert (await q("k8s-b"))["server"]["total"] == 1


async def test_search_cache_follows_reports(client, auth_headers):
    ids = await _fill(client)
    r = await client.get("/api/search", params={"q": "nginx:1.18"}, headers=auth_headers)
    assert _groups(r.json())["container"]["total"] == 1
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        a = await s.get(Server, ids["a"])
        rep = dict(REPORT_A)
        rep["docker"] = {**REPORT_A["docker"], "containers": [{"name": "shop-nginx-1", "image": "nginx:1.27"}]}
        a.last_report = rep
        a.last_seen = datetime.now(timezone.utc)
        await s.commit()
        await s.delete(await s.get(Server, ids["b"]))
        await s.commit()
    g = _groups((await client.get("/api/search", params={"q": "nginx:1"}, headers=auth_headers)).json())
    assert [x["sub"][1] for x in g["container"]["items"]] == ["nginx:1.27"]
    # удаленный сервер ушел и из кэша
    assert ids["b"] not in search_mod._cache
