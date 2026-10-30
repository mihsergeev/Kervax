"""privileged ради docker внутри (dind, агент 2.60): живой docker внутри - заметка, а не совет убрать
privileged; dockerd без дела и privileged без docker - совет как раньше."""
from app import docker_risk, home_mute
from tests.test_docker_risk import _c, _rep, _srv
from tests.test_docker_risk_fix import AGENT, _blocks, _cm, _ids

DAY = 86400
# CI собирает образы: в нем контейнер шага прямо сейчас и dockerd съел минуты CPU
BUSY = {"dockerd": True, "up": 9 * DAY, "cpu": 1500, "run": 1, "ran": True}
# CI-агент с поднятым, но пустым docker: неделю без контейнеров внутри, CPU пара секунд
IDLE = {"dockerd": True, "up": 7 * DAY, "cpu": 2}


def _ci(name, dind, svc=None, **kw):
    return _c(name, AGENT, priv=True, compose=_cm(svc or name), dind=dind, **kw)


def test_live_dind_is_a_note_not_advice():
    s = _srv(_rep(_ci("ci-agent-1", BUSY), _ci("ci-agent-2", IDLE)))
    v = {x["name"]: x for x in docker_risk.view(s, fix=True)}
    busy, idle = v["ci-agent-1"], v["ci-agent-2"]
    assert busy["note"] and busy["flags"] == ["priv"] and "fix" not in busy
    assert busy["dind"] == {"state": "live", "run": 1, "up": 9 * DAY, "cpu": 1500, "ran": True, "ci": True}
    assert "note" not in idle and idle["dind"]["state"] == "idle" and idle["fix"]["names"] == ["ci-agent-2"]
    # в алерт и "Требует действий" - только пустой
    assert [x["name"] for x in docker_risk.advice(s)] == ["ci-agent-2"]


def test_live_dind_signals():
    def state(d):
        return docker_risk.dind({"dind": d})["state"]

    assert state({"dockerd": True, "up": 30 * DAY, "cpu": 3, "ran": True}) == "live"  # сборки были
    assert state({"dockerd": True, "up": 30 * DAY, "cpu": 400}) == "live"  # cgroup v1: по CPU
    assert state({"dockerd": True, "up": 3600, "cpu": 1}) == "live"  # только запустился: судить рано
    assert state(IDLE) == "idle"
    assert state({"dockerd": False}) == state({}) == "none"
    assert docker_risk.dind({}) is None  # старый агент или hidepid


def test_note_alone_clears_alert_and_home():
    s = _srv(_rep(_ci("ci-agent-1", BUSY)))
    assert docker_risk.advice(s) == []
    assert home_mute._dockrisk(s, None, {}) == ""
    assert home_mute._dockrisk(_srv(_rep(_ci("ci-agent-1", IDLE))), None, {}) == "ci-agent-1:priv:"


def test_live_dind_with_other_keys_keeps_only_them():
    s = _srv(_rep(_ci("ci-agent-1", BUSY, binds=["/var/run/docker.sock"])))
    [x] = docker_risk.view(s)
    assert x["flags"] == ["sock"] and "note" not in x and x["text"] == "docker-сокет"


def test_idle_dind_says_how_long_and_keeps_check():
    [b] = _blocks(_rep(_ci("ci-agent-2", IDLE, svc="agent-2")))
    assert _ids(b) == ["ci_idle", "bak", "priv_off", "up", "undo", "alt_ci"]
    assert b["args"]["idle_days"] == "7" and b["args"]["idle_cpu"] == "2"
    assert b["steps"][0]["cmd"] == "sudo docker exec ci-agent-2 docker images -q | wc -l"


def test_no_docker_inside_skips_the_check():
    [b] = _blocks(_rep(_ci("ci-agent-3", {"dockerd": False}, svc="agent-3")))
    assert _ids(b) == ["bak", "priv_off", "up", "undo"]
    assert b["args"]["dind_env"] == "DOCKER_IN_DOCKER=start"


def test_old_agent_as_before():
    [b] = _blocks(_rep(_c("ci-agent-1", AGENT, priv=True, compose=_cm("agent-1"))))
    assert _ids(b)[0] == "ci_check" and _ids(b)[-1] == "alt_ci"


def test_muted_note_stays_muted():
    s = _srv(_rep(_ci("ci-agent-1", BUSY)), ["dockrisk:ci-agent-1"])
    [x] = docker_risk.view(s)
    assert x["note"] and x["muted"]
