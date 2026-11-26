"""Контейнер с ключами от хоста: privileged, опасные capabilities, docker-сокет, корень хоста на запись."""
from datetime import datetime, timedelta, timezone

from app import collector, docker_risk
from app.api import servers
from app.config import Settings
from app.models import Server
from tests.test_api_access import _report, _server

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _c(name, image, **kw):
    return {"name": name, "image": image, "state": "running", **kw}


def _rep(*cs, agent="2.27"):
    return {"agent_version": agent, "mem_total": 8 * 2**30, "mem_used": 2**30,
            "docker": {"access": True, "containers": list(cs)}}


def _srv(rep, mutes=None):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state={}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def _names(rep, mutes=None):
    return {x["name"]: x["flags"] for x in docker_risk.view(_srv(rep, mutes))}


def _sample(server, rep, at, clock=None, *, interval=15):
    report = dict(rep, clock_unix=int((clock or at).timestamp()))
    docker_risk.observe(server, report, at, interval=interval)
    server.last_seen = at
    server.last_report = report


def _confirmed(server):
    rep = server.last_report
    for minutes in range(10, -1, -1):
        _sample(server, rep, NOW - timedelta(minutes=minutes))
    return server


def test_tools_get_only_what_they_need():
    rep = _rep(
        _c("cadvisor", "gcr.io/cadvisor/cadvisor:v0.52.1", priv=True, binds=["/", "/var/run", "/sys"], ro=["/", "/sys"]),
        _c("node-exporter", "prom/node-exporter:v1.9.1", pid_host=True, binds=["/"], ro=["/"]),
        _c("promtail", "grafana/promtail:3.4.2", binds=["/var/run/docker.sock", "/var/log"], ro=["/var/log"]),
        _c("kervax-docker-proxy", "wollomatic/socket-proxy:1.13.1", binds=["/var/run/docker.sock"]),
        _c("sentinel", "ghcr.io/coollabsio/sentinel:1.0.2", binds=["/var/run/docker.sock"]),
        _c("amnezia-awg2", "amnezia-awg2", priv=True, caps=["SYS_MODULE"]),
        _c("ci-dind", "docker:27-dind", priv=True),
    )
    assert _names(rep) == {}
    # тот же promtail, но privileged - уже сверх положенного
    rep["docker"]["containers"][2]["priv"] = True
    assert _names(rep) == {"promtail": ["priv"]}


def test_app_containers_with_host_keys():
    rep = _rep(
        _c("app", "registry.example.com:5000/shop/app:1.2", binds=["/var/run/docker.sock", "/srv/app"]),
        _c("worker", "shop/worker:1", priv=True, caps=["SYS_ADMIN"]),  # caps при privileged не дублируем
        _c("tool", "shop/tool:1", caps=["SYS_PTRACE", "SYS_ADMIN"]),
        _c("backup", "shop/backup:1", binds=["/", "/etc"], ro=["/"]),
        _c("stopped", "shop/old:1", priv=True, state="exited"),
    )
    v = {x["name"]: x for x in docker_risk.view(_srv(rep))}
    assert {k: x["flags"] for k, x in v.items()} == {
        "app": ["sock"], "worker": ["priv"], "tool": ["caps"], "backup": ["host"]}
    assert v["tool"]["text"] == "cap SYS_PTRACE, SYS_ADMIN" and v["tool"]["caps"] == ["SYS_PTRACE", "SYS_ADMIN"]
    assert v["backup"]["text"] == "корень хоста на запись" and v["app"]["text"] == "docker-сокет"


def test_old_agent_judges_only_the_socket():
    rep = _rep(_c("backup", "shop/backup:1", binds=["/"]),
               _c("app", "shop/app:1", binds=["/run/docker.sock"]), agent="2.26")
    assert _names(rep) == {"app": ["sock"]}  # без списка ro любой bind выглядел бы записью


def test_edge_proxy_socket_is_left_to_docker_exposure():
    rep = _rep(_c("caddy", "lucaslorentz/caddy-docker-proxy:alpine", binds=["/var/run/docker.sock"], priv=True))
    assert _names(rep) == {"caddy": ["priv"]}


def test_mutes_and_alert():
    rep = _rep(_c("app", "shop/app:1", priv=True), _c("vpn", "custom/vpn:1", caps=["SYS_MODULE"]))
    s = _confirmed(_srv(rep))
    lvl, ctx = collector._server_conditions(s, NOW)["docker_risk"]
    assert lvl == 1 and ctx["sig"] == ["app:priv", "vpn:caps"]
    assert ctx["detail"] == ("у контейнеров ключи от хоста: app (privileged), vpn (cap SYS_MODULE) - "
                             "взлом приложения в них даст root на сервере")
    # vpn так задуман: в пункт и алерт не идет, в разделе виден как заглушенный
    s = _confirmed(_srv(rep, mutes=["dockrisk:vpn"]))
    assert [x["name"] for x in docker_risk.advice(s)] == ["app"]
    assert [x["muted"] for x in docker_risk.view(s)] == [False, True]
    assert collector._server_conditions(s, NOW)["docker_risk"][1]["sig"] == ["app:priv"]
    assert docker_risk.advice(_confirmed(_srv(rep, mutes=["docker_risk"]))) == []
    # без доступа к докеру молчание ничего не значит
    rep["docker"]["access"] = False
    assert "docker_risk" not in collector._server_conditions(_srv(rep), NOW)


def test_image_keys():
    assert docker_risk._image_keys("docker.io/grafana/promtail:3.4@sha256:ab") == ("promtail", "grafana/promtail")
    assert docker_risk._image_keys("registry:5000/agent:1") == ("agent", "registry:5000/agent")
    assert docker_risk.allowed("x", "datadog/agent:7") == {"sock", "caps"}
    assert docker_risk.allowed("x", "shop/agent:7") == frozenset()


def test_caps_only_for_startup_are_a_note():
    """cap_add DAC_READ_SEARCH (+CHOWN, SETUID, SETGID при cap_drop ALL): entrypoint от root делает
    chown и setpriv --reuid, главный процесс работает без прав. Тревога - только если права остались."""
    dropped = {"uid": 10001, "nnp": True}
    rep = _rep(
        _c("backend", "shop/backend:1", caps=["DAC_READ_SEARCH"], proc=dropped),
        _c("root", "shop/root:1", caps=["DAC_READ_SEARCH"], proc={"uid": 0, "nnp": True}),
        _c("eff", "shop/eff:1", caps=["DAC_READ_SEARCH"], proc={"uid": 10001, "caps": ["DAC_READ_SEARCH"], "nnp": True}),
        _c("nonnp", "shop/nonnp:1", caps=["DAC_READ_SEARCH"], proc={"uid": 10001, "nnp": False}),
        _c("old", "shop/old:1", caps=["DAC_READ_SEARCH"]),  # агент до 2.57: proc нет - как раньше
        _c("bad", "shop/bad:1", caps=["DAC_READ_SEARCH"], proc={"uid": True, "nnp": True}),
        _c("sock", "shop/sock:1", caps=["SYS_PTRACE"], proc=dropped, binds=["/var/run/docker.sock"]),
        _c("exporter", "prom/node-exporter:v1.9.1", caps=["SYS_PTRACE"], proc=dropped),  # caps по назначению
        _c("down", "shop/backend:1", caps=["DAC_READ_SEARCH"], proc=dropped, state="exited"),
    )
    s = _srv(rep)
    assert _names(rep) == {"root": ["caps"], "eff": ["caps"], "nonnp": ["caps"], "old": ["caps"], "bad": ["caps"],
                           "sock": ["sock"]}
    assert docker_risk.start_caps(s) == [
        {"name": "backend", "image": "shop/backend:1", "caps": ["DAC_READ_SEARCH"], "uid": 10001},
        {"name": "sock", "image": "shop/sock:1", "caps": ["SYS_PTRACE"], "uid": 10001}]
    # только такой контейнер - ни пункта, ни алерта
    only = _rep(_c("backend", "shop/backend:1", caps=["DAC_READ_SEARCH"], proc=dropped))
    s = _confirmed(_srv(only))
    assert docker_risk.advice(s) == [] and collector._server_conditions(s, NOW)["docker_risk"][0] == 0
    rep["docker"]["access"] = False
    assert docker_risk.start_caps(_srv(rep)) == []


def test_docker_risk_needs_distinct_reports_and_resets_after_missing_inventory():
    bad = _c("app", "example/app:1", id="0123456789ab", priv=True)
    s = _srv({})
    _sample(s, _rep(bad), NOW)
    for minutes in range(1, 11):
        _sample(s, _rep(bad), NOW + timedelta(minutes=minutes), clock=NOW)
    assert not docker_risk.observed(s.last_report)
    assert "docker_risk" not in collector._server_conditions(s, s.last_seen)
    assert [x["name"] for x in docker_risk.split(s)[1]] == ["app"]
    for minutes in range(11, 26):
        # Even though socket version is accessible, a broken container listing proves nothing.
        rep = {"docker": {"access": True, "containers_complete": False}} if minutes == 14 else _rep(bad)
        _sample(s, rep, NOW + timedelta(minutes=minutes))
        assert bool(docker_risk.advice(s)) == (minutes == 25)


def test_docker_risk_legacy_without_container_id():
    s = _srv({})
    for minutes in range(12):
        # This name and image stay equal, but Docker sees new, briefly running instances.
        c = _c("app", "example/app:1", priv=True, status="Up About a minute")
        _sample(s, _rep(c, agent="2.40"), NOW + timedelta(minutes=minutes))
        assert not docker_risk.advice(s)
    stable = _c("app", "example/app:1", priv=True, status="Up an hour")
    for minutes in range(12, 23):
        _sample(s, _rep(stable, agent="2.40"), NOW + timedelta(minutes=minutes))
    assert docker_risk.advice(s)
    _sample(s, _rep({**stable, "status": "Up About a minute"}, agent="2.40"), NOW + timedelta(minutes=23))
    assert not docker_risk.advice(s)
    # Old report producers may lack Status altogether. Use uninterrupted observations.
    s = _srv({})
    for minutes in range(17):
        rep = _rep(_c("app", "example/app:1", priv=True), agent="2.40") if minutes != 5 else _rep()
        _sample(s, rep, NOW + timedelta(minutes=minutes))
        assert bool(docker_risk.advice(s)) == (minutes == 16)


def test_docker_risk_carries_only_previously_announced_instances_on_upgrade():
    old = _c("old", "example/app:1", id="0123456789ab", priv=True)
    fresh = _c("new", "example/app:1", id="0123456789cd", priv=True)
    s = _srv(_rep(old, fresh))
    s.alert_state = {"docker_risk": 1, "docker_risk_sig": ["old:priv"], "docker_risk_told": ["old:priv"]}
    _sample(s, _rep(old, fresh), NOW + timedelta(minutes=1))
    confirmed, pending = docker_risk.split(s)
    assert [x["name"] for x in confirmed] == ["old"]
    assert [x["name"] for x in pending] == ["new"]
    _sample(s, _rep({**old, "id": "0123456789ef"}), NOW + timedelta(minutes=2))
    assert not docker_risk.advice(s)


def test_docker_risk_accepts_configured_slow_reports_and_waits_for_valid_inspect():
    s = _srv({})
    bad = _c("app", "example/app:1", id="0123456789ab", priv=True, compose={})

    def sample(minute, *containers):
        # Modern agents omit containers when a successful listing is empty.
        block = {"access": True, "containers_complete": True}
        if containers:
            block["containers"] = list(containers)
        _sample(s, {"docker": block}, NOW + timedelta(minutes=minute), interval=180)

    for minute in (0, 3, 6, 9, 12):
        sample(minute, bad)
        assert bool(docker_risk.advice(s)) == (minute == 12)
    sample(15, _c("app", "example/app:1", id=bad["id"]))
    assert docker_risk.observed(s.last_report) and not docker_risk.complete(s.last_report)
    assert not docker_risk.recovered(s.last_report, 0)
    # The next complete inspection within the same identity is still confirmed.
    sample(18, bad)
    assert docker_risk.advice(s)
    for minute in (21, 24, 27, 30, 33, 36):
        sample(minute)
        assert docker_risk.complete(s.last_report)
        assert docker_risk.recovered(s.last_report, 900) == (minute == 36)


async def test_docker_risk_ingest_panel_and_telegram(client, auth_headers, monkeypatch):
    sid, agent_headers = await _server(client, auth_headers, "node-docker-risk")
    r = await client.post(f"/api/servers/{sid}/onboard/done", headers=auth_headers)
    assert r.status_code == 200, r.text
    factory = client._transport.app.state.session_factory
    cfg = Settings(alert_telegram_token="123456:fake-token-example", alert_telegram_chat="-1000000000001")
    start = datetime.now(timezone.utc)
    current = start

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz)

    monkeypatch.setattr(servers, "datetime", Clock)
    messages = []

    async def send(cfg, text, parse_mode=None):
        if getattr(text, "kind", "") == "docker_risk":
            assert cfg["telegram_chat"] == "-1000000000001"
            messages.append(str(text))
        return []

    monkeypatch.setattr(collector.alerts, "send_alert", send)

    async def report(minute, *containers, complete=True):
        nonlocal current
        current = start + timedelta(minutes=minute)
        body = _report(clock_unix=int(current.timestamp()), uptime_seconds=86400 + minute * 60,
                       docker={"present": True, "access": True, "containers_complete": complete,
                               "containers": list(containers)})
        r = await client.post("/api/agent/report", headers=agent_headers, json=body)
        assert r.status_code == 200, r.text
        await collector.evaluate_servers(factory, cfg, current)

    async def panel():
        r = await client.get(f"/api/servers/{sid}", headers=auth_headers)
        assert r.status_code == 200, r.text
        return r.json()

    test = _c("test-job", "example/app:1", binds=["/var/run/docker.sock"], id="0123456789ab", compose={})
    await report(0, test)
    s = await panel()
    assert not s["docker_risk"]
    assert [x["name"] for x in s["docker_risk_pending"]] == ["test-job"]
    assert s["last_report"]["docker"]["containers"] == [test]
    await report(1, test)
    await report(2)
    assert not (await panel())["docker_risk_pending"]
    # Recreating the same container name and image cannot accumulate ten minutes.
    for minute in range(3, 16):
        await report(minute, {**test, "id": f"{minute // 2:012x}"})
    assert not messages

    durable = {**test, "name": "service", "id": "0123456789cd"}
    for minute in range(16, 26):
        await report(minute, durable)
    assert not messages
    await report(26, durable)
    s = await panel()
    assert [x["name"] for x in s["docker_risk"]] == ["service"]
    assert not s["docker_risk_pending"]
    assert messages == ["⚠️🐳 node-docker-risk\n<b>Контейнер с root на хосте</b>\nservice (docker-сокет)"]
    await report(27, complete=False)
    async with factory() as db:
        assert (await db.get(Server, sid)).alert_state["docker_risk"] == 1
    # A missing listing won't start the recovery hold. During the entire hold
    # there can still be rotating unconfirmed instances from the same service.
    for minute in range(28, 44):
        await report(minute, {**durable, "id": f"{minute:012x}"})
    assert messages[-1] == "✅ node-docker-risk: ранее подтвержденных контейнеров с root на хосте больше нет"
    await report(44)
    assert len(messages) == 2
