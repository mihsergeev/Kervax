"""Контейнер с ключами от хоста: privileged, опасные capabilities, docker-сокет, корень хоста на запись."""
from datetime import datetime, timezone

from app import collector, docker_risk
from app.models import Server

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


def test_tools_get_only_what_they_need():
    rep = _rep(
        _c("cadvisor", "gcr.io/cadvisor/cadvisor:v0.52.1", priv=True, binds=["/", "/var/run", "/sys"], ro=["/", "/sys"]),
        _c("node-exporter", "prom/node-exporter:v1.9.1", pid_host=True, binds=["/"], ro=["/"]),
        _c("promtail", "grafana/promtail:3.4.2", binds=["/var/run/docker.sock", "/var/log"], ro=["/var/log"]),
        _c("kervax-docker-proxy", "sha256:4cada1e3f8e2", binds=["/var/run/docker.sock"]),
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
    s = _srv(rep)
    lvl, ctx = collector._server_conditions(s, NOW)["docker_risk"]
    assert lvl == 1 and ctx["sig"] == ["app:priv", "vpn:caps"]
    assert ctx["detail"] == ("у контейнеров ключи от хоста: app (privileged), vpn (cap SYS_MODULE) - "
                             "взлом приложения в них даст root на сервере")
    # vpn так задуман: в пункт и алерт не идет, в разделе виден как заглушенный
    s = _srv(rep, mutes=["dockrisk:vpn"])
    assert [x["name"] for x in docker_risk.advice(s)] == ["app"]
    assert [x["muted"] for x in docker_risk.view(s)] == [False, True]
    assert collector._server_conditions(s, NOW)["docker_risk"][1]["sig"] == ["app:priv"]
    assert docker_risk.advice(_srv(rep, mutes=["docker_risk"])) == []
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
    assert docker_risk.advice(_srv(only)) == [] and collector._server_conditions(_srv(only), NOW)["docker_risk"][0] == 0
    rep["docker"]["access"] = False
    assert docker_risk.start_caps(_srv(rep)) == []
