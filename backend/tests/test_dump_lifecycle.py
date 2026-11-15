"""A deliberate stack stop suspends freshness checks without deleting backups."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app import collector
from app.api.servers import _backup_coverage
from app.dump_lifecycle import dump_paused
from app.models import Server
from app.config import Settings
from app.db import Base, create_engine_and_factory

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)
TS = int(NOW.timestamp())


def report(**container_changes):
    c = dict(name="shop-vector", image="qdrant/qdrant:v1.15.0", state="exited",
             exit=143, policy="unless-stopped", oom=False, err="")
    c.update(container_changes)
    return {"docker": {"present": True, "containers": [c]}, "backup": {
        "manageable": True, "configured": False, "dumps": [{
            "engine": "qdrant", "container": c["name"], "files": 2,
            "last_ts": TS - 4 * 86400, "enabled_ts": TS - 8 * 86400,
            "error_ts": TS - 86400, "dir": "/backup/qdrant/shop-vector"}]}}


def server(rep):
    return Server(name="shop-node", token_hash="x", enabled=True,
                  offline_after_seconds=120,
                  last_seen=NOW, created_at=NOW - timedelta(days=30),
                  backup_not_required=True, last_report=rep,
                  alert_state={"backup_dump": 1, "backup_dump_from": NOW.isoformat(),
                               "backup_dump_since": (NOW - timedelta(hours=2)).isoformat()})


@pytest.mark.parametrize("code", [0, 143])
def test_stopped_stack_keeps_dumps_and_closes_the_condition(code):
    rep = report(exit=code)
    before = deepcopy(rep)
    s = server(rep)
    assert dump_paused(rep, rep["backup"]["dumps"][0])
    assert collector.dump_issues(rep, NOW) == ([], "", [], 0)
    assert collector._server_conditions(s, NOW)["backup_dump"][0] == 0
    assert not any(p["kind"] == "backup_dump" for p in collector._server_problems(s, NOW))
    rows = _backup_coverage(s)
    assert len(rows) == 1 and rows[0].kind == "db_paused"
    assert rows[0].instance == "shop-vector" and not rows[0].can_dump
    assert "после запуска" in rows[0].note
    detail = collector._recovery_short("backup_dump", s.alert_state, {}, s)
    assert "приостановлен" in detail and "свежие" not in detail
    assert rep == before


@pytest.mark.parametrize("change", [
    {"state": "running"}, {"state": "dead"}, {"state": "created"},
    {"state": "restarting"}, {"exit": 1}, {"exit": 137}, {"exit": None},
    {"exit": "0"}, {"exit": False}, {"oom": True}, {"err": "failure"},
    {"policy": "always"}, {"policy": "on-failure"}, {"policy": "no"},
])
def test_unknown_or_crashed_target_remains_a_problem(change):
    rep = report(**change)
    assert not dump_paused(rep, rep["backup"]["dumps"][0])
    assert collector.dump_issues(rep, NOW)[0] == ["qdrant@shop-vector"]
    assert collector._server_conditions(server(rep), NOW)["backup_dump"][0] == 1
    assert _backup_coverage(server(rep))[0].kind == "db"


@pytest.mark.parametrize("mode", ["missing", "no_inventory", "duplicate", "native", "kube"])
def test_incomplete_inventory_is_not_a_deliberate_stop(mode):
    rep = report()
    if mode == "missing":
        rep["docker"]["containers"] = []
    elif mode == "no_inventory":
        rep["docker"].pop("present")
    elif mode == "duplicate":
        rep["docker"]["containers"] *= 2
    else:
        rep["backup"]["dumps"][0]["container"] = "" if mode == "native" else "k8s.app.sts.vector"
    assert collector.dump_issues(rep, NOW)[0]


def test_restart_restores_monitoring_and_fresh_dump_recovers_normally():
    rep = report()
    s = server(rep)
    assert collector.dump_issues(rep, NOW)[0] == []
    rep["docker"]["containers"][0]["state"] = "running"
    assert collector.dump_issues(rep, NOW)[0] == ["qdrant@shop-vector"]
    assert _backup_coverage(s)[0].kind == "db"
    rep["backup"]["dumps"][0].update(last_ts=TS, error_ts=None)
    assert collector.dump_issues(rep, NOW)[0] == []
    assert _backup_coverage(s)[0].kind == "db_ok"
    assert collector._recovery_short("backup_dump", {}, {}, s) == "дампы снова свежие"


def test_paused_dump_does_not_hide_another_instances_failure_or_space_issue():
    rep = report()
    other = deepcopy(rep["backup"]["dumps"][0])
    other["container"] = "shop-vector-2"
    rep["backup"]["dumps"].append(other)
    assert collector.dump_issues(rep, NOW)[0] == ["qdrant@shop-vector-2"]
    rows = _backup_coverage(server(rep))
    assert {(r.instance, r.kind) for r in rows} == {
        ("shop-vector", "db_paused"), ("shop-vector-2", "db")}
    other.update(error_ts=None, skipped=True, skip_free_pct=3)
    rep["backup"]["dumps"][0].update(skipped=True, skip_free_pct=2)
    assert collector.dump_issues(rep, NOW)[2:] == (["qdrant@shop-vector-2"], 3)


def test_file_backup_and_multiple_engines_use_the_same_pause_rule():
    rep = report()
    pg = deepcopy(rep["backup"]["dumps"][0])
    pg.update(engine="pg", container="shop-sql")
    rep["backup"]["dumps"].append(pg)
    rep["docker"]["containers"].append({**rep["docker"]["containers"][0],
                                        "name": "shop-sql", "image": "postgres:17", "exit": 0})
    rep["backup"].update(present=True, configured=True, metric_present=True,
                         success=1, last_backup_ts=TS, restic_version="0.19.1")
    issues = collector.dump_issues(rep, NOW)
    assert not issues[0] and issues[2:] == ([], 0)
    assert {r.kind for r in _backup_coverage(server(rep))} == {"db_paused"}


def test_running_and_legacy_targets_are_not_duplicated():
    rep = report(state="running")
    rep["backup"]["dumps"][0].update(container="", error_ts=None, last_ts=TS)
    rep["db_engines"] = ["Qdrant"]
    rows = _backup_coverage(server(rep))
    assert len(rows) == 1 and rows[0].kind == "db_ok"


async def test_collector_clears_existing_alert_and_does_not_repeat_stop_recovery(tmp_path, monkeypatch):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'stop.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sent = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(str(text))
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    try:
        async with factory() as session:
            s = server(report())
            s.alert_state = {**s.alert_state, "backup_dump_told_lvl": 1}
            session.add(s)
            await session.commit()
            sid = s.id
        cfg = Settings(alert_webhook="http://hook")
        await collector.evaluate_servers(factory, cfg, NOW)
        async with factory() as session:
            s = await session.get(Server, sid)
            assert not s.alert_state.get("backup_dump")
            assert s.last_report["backup"]["dumps"][0]["files"] == 2
            assert _backup_coverage(s)[0].kind == "db_paused"
        assert len(sent) == 1 and "приостановлен" in sent[0]
        assert "свежие" not in sent[0]
        await collector.evaluate_servers(factory, cfg, NOW + timedelta(seconds=10))
        assert len(sent) == 1
    finally:
        await engine.dispose()
