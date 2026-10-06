"""Внешний прокси с docker-сокетом (caddy-docker-proxy, traefik): взлом прокси, который смотрит в
интернет, - root на хосте. Панель находит такие контейнеры по отчету агента, шлет алерт, отдает
ноду ansible группой kervax_docker_sock и пишет, как поправить."""
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.docker_exposure import exposed, image_kind, outdated, text

SOCK = ["/var/run/docker.sock"]


def _rep(*cs):
    return {"docker": {"present": True, "access": True, "containers": list(cs)}}


def test_image_kind():
    assert image_kind("lucaslorentz/caddy-docker-proxy:2.12.0") == "caddy"
    assert image_kind("lucaslorentz/caddy-docker-proxy:ci-alpine") == "caddy"
    assert image_kind("traefik:latest") == "traefik"
    assert image_kind("docker.io/library/traefik:v3.1@sha256:abc") == "traefik"
    assert image_kind("nginxproxy/nginx-proxy:1.6") == "nginx-proxy"
    assert image_kind("jc21/nginx-proxy-manager:latest") is None
    assert image_kind("wollomatic/socket-proxy:1") is None
    assert image_kind("gcr.io/cadvisor/cadvisor:v0.52.0") is None


def test_exposed_only_edge_proxies_with_the_socket():
    rep = _rep(
        {"name": "caddy-proxy-caddy-1", "image": "lucaslorentz/caddy-docker-proxy:2.10.0", "state": "running", "binds": SOCK},
        {"name": "traefik", "image": "traefik:latest", "state": "running", "binds": SOCK + ["/srv/traefik"]},
        # caddy за прокси сокета: сокета у него нет
        {"name": "caddy-ok", "image": "lucaslorentz/caddy-docker-proxy:2.13.1", "state": "running", "binds": []},
        # сами прокси сокета и сборщики метрик держат сокет по делу и в интернет не смотрят
        {"name": "kervax-docker-proxy", "image": "wollomatic/socket-proxy:1", "state": "running", "binds": SOCK},
        {"name": "cadvisor", "image": "gcr.io/cadvisor/cadvisor:v0.52.0", "state": "running", "binds": ["/var/run"]},
        # остановленный - не опасен, пока не запущен
        {"name": "old-caddy", "image": "lucaslorentz/caddy-docker-proxy:2.8.9", "state": "exited", "binds": SOCK},
    )
    got = exposed(rep)
    assert [g["name"] for g in got] == ["caddy-proxy-caddy-1", "traefik"]
    assert "caddy-proxy-caddy-1 (caddy), traefik (traefik) держат docker-сокет" in text(got)
    assert exposed({}) == [] and text([]) == ""


async def test_alert_fires_once_and_recovers(tmp_path, monkeypatch):
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'd.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, alert_sustain_seconds=0, last_report={}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(*containers):
        nonlocal now
        now = now + timedelta(minutes=1)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.last_report = {"clock_unix": int(now.timestamp()), "uptime_seconds": 10**6, **_rep(*containers)}
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)

    bad = {"name": "caddy-proxy-caddy-1", "image": "lucaslorentz/caddy-docker-proxy:2.10.0",
           "state": "running", "binds": SOCK}
    good = {**bad, "binds": []}
    for _ in range(3):
        await tick(bad)
    hits = [m for m in sent if "docker-сокет" in m]
    assert len(hits) == 1 and "caddy-proxy-caddy-1" in hits[0], sent
    # перевели caddy на прокси сокета - отбой
    await tick(good)
    assert any("больше не держит docker-сокет" in m for m in sent)
    await engine.dispose()


async def test_ansible_group_and_server_field(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "edge"}, headers=auth_headers)
    token = r.json()["token"]
    rep = {"hostname": "edge", "os": "Ubuntu", "agent_version": "2.22", "cpu_percent": 5,
           "mem_used": 1, "mem_total": 2, **_rep(
               {"name": "caddy-proxy-caddy-1", "image": "lucaslorentz/caddy-docker-proxy:2.10.0",
                "state": "running", "binds": SOCK})}
    r = await client.post("/api/agent/report", json=rep, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    srv = [x for x in (await client.get("/api/servers", headers=auth_headers)).json() if x["name"] == "edge"][0]
    assert srv["docker_exposed"] == [{"name": "caddy-proxy-caddy-1", "kind": "caddy",
                                      "image": "lucaslorentz/caddy-docker-proxy:2.10.0"}]
    r = await client.post("/api/settings/ansible", headers=auth_headers)
    atok = r.json()["token"]
    rows = {x["name"]: x for x in (await client.get(
        "/api/ansible/servers", headers={"Authorization": f"Bearer {atok}"})).json()["servers"]}
    assert rows["edge"]["issues"] == ["docker_sock"]


def test_outdated_proxies_by_image_build_date():
    """Внешний прокси на образе старше года. Дату сборки шлет агент 2.23: по тегу возраст не понять
    (traefik:latest на одной ноде был сборкой 2021 года)."""
    now = datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp()
    old = int(datetime(2021, 12, 1, tzinfo=timezone.utc).timestamp())
    fresh = int(datetime(2026, 9, 17, tzinfo=timezone.utc).timestamp())
    rep = _rep(
        {"name": "traefik", "image": "traefik:latest", "state": "running", "img_ver": "v2.5.5", "img_created": old},
        {"name": "cdp", "image": "lucaslorentz/caddy-docker-proxy:2.13.1", "state": "running", "img_created": fresh},
        {"name": "plain", "image": "caddy:2.4.0", "state": "running", "img_created": old},
        # обычные контейнеры и остановленный прокси не трогаем
        {"name": "app", "image": "myapp:1", "state": "running", "img_created": old},
        {"name": "stopped", "image": "traefik:v1.7", "state": "exited", "img_created": old},
        # агент до 2.23 или прокси агента без списка образов: даты нет - не флагаем
        {"name": "nodate", "image": "traefik:latest", "state": "running"},
    )
    got = outdated(rep, now)
    assert [g["name"] for g in got] == ["traefik", "plain"]
    assert got[0]["kind"] == "traefik" and got[0]["version"] == "v2.5.5" and got[0]["built"] == old
    assert got[0]["age_days"] >= 365 and got[1]["kind"] == "caddy" and got[1]["version"] == ""
    assert outdated({}, now) == []


async def test_old_proxy_goes_to_server_field_and_ansible(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "aff"}, headers=auth_headers)
    token = r.json()["token"]
    built = int(time.time()) - 4 * 365 * 86400
    rep = {"hostname": "aff", "os": "Ubuntu", "agent_version": "2.23", "cpu_percent": 5,
           "mem_used": 1, "mem_total": 2, **_rep(
               {"name": "traefik", "image": "traefik:v2.5.5", "state": "running",
                "img_ver": "v2.5.5", "img_created": built})}
    r = await client.post("/api/agent/report", json=rep, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    srv = [x for x in (await client.get("/api/servers", headers=auth_headers)).json() if x["name"] == "aff"][0]
    assert [(p["name"], p["version"], p["built"]) for p in srv["proxy_outdated"]] == [("traefik", "v2.5.5", built)]
    assert srv["docker_exposed"] == []
    r = await client.post("/api/settings/ansible", headers=auth_headers)
    atok = r.json()["token"]
    rows = {x["name"]: x for x in (await client.get(
        "/api/ansible/servers", headers={"Authorization": f"Bearer {atok}"})).json()["servers"]}
    assert rows["aff"]["issues"] == ["proxy_old"]


async def test_web_errors_mark_a_log_that_is_gone(client, auth_headers):
    """Сайты разнесли по своим логам: ошибки старого общего лога за сутки видны, но помечены."""
    r = await client.post("/api/servers", json={"name": "mob"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}
    shared = {"log": "/var/log/nginx/access.log", "rpm": 50, "e5": 5, "c5": {"500": 5},
              "sites": ["anketa.shop.example", "mobile.shop.example"]}
    own = {"log": "/var/log/nginx/mobile.shop.example.access.log", "rpm": 40, "e5": 3, "c5": {"500": 3},
           "sites": ["mobile.shop.example"]}
    base = {"hostname": "mob", "os": "U", "agent_version": "2.22", "cpu_percent": 5, "mem_used": 1, "mem_total": 2}
    r = await client.post("/api/agent/report", headers=ah, json={
        **base, "extras": {"web-rate": {"ts": int(time.time()), "rpm": 50, "e5": 5, "logs": [shared]}}})
    assert r.status_code == 200
    r = await client.post("/api/agent/report", headers=ah, json={
        **base, "extras": {"web-rate": {"ts": int(time.time()) + 60, "rpm": 40, "e5": 3, "logs": [own]}}})
    assert r.status_code == 200
    rows = {x["label"]: x for x in (await client.get(
        f"/api/servers/{sid}/web-errors?hours=1", headers=auth_headers)).json()}
    assert rows["mobile.shop.example"]["gone"] is False
    assert [x["gone"] for k, x in rows.items() if k != "mobile.shop.example"] == [True]
