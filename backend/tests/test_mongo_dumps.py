"""MongoDB managed archives use the same API/coverage state machine as other engines."""
import time
import pytest
from app.api.servers import _backup_coverage
from app.models import Server
from app.schemas import BackupCommandIn


def server(agent="2.62", helper="0.50", dump=None, native=False):
    report = {"setup_versions": {"backup-setup": helper},
              "backup": {"present": True, "manageable": True, "configured": False},
              "db_engines": ["MongoDB"] if native else [],
              "docker": {"containers": [] if native else [
                  {"name": "document-db", "image": "mongo:7.0", "state": "running"}]}}
    if dump:
        report["backup"]["dumps"] = [dump]
    return Server(name="database-node", token_hash="x", enabled=True, agent_version=agent,
                  backup_not_required=False, last_report=report)


def row(s):
    return next(a for a in _backup_coverage(s) if a.subject == "MongoDB")


@pytest.mark.parametrize("native", [False, True])
def test_mongo_enable_gate_and_first_run(native):
    a = row(server(native=native))
    assert a.kind == "db" and a.dump_engine == "mongo" and a.can_dump
    d = {"engine": "mongo", "container": "" if native else "document-db", "files": 0,
         "enabled_ts": time.time(), "dir": "/backup/mongo", "keep": 2}
    a = row(server(native=native, dump=d))
    assert a.kind == "db_ok" and not a.gap
    d.update(files=1, last_ts=time.time())
    assert row(server(native=native, dump=d)).kind == "db_ok"
    d.update(files=0, enabled_ts=time.time()-3*86400)
    assert row(server(native=native, dump=d)).kind == "db"


@pytest.mark.parametrize("agent,helper,reason", [("2.61", "0.50", "2.62"),
    ("2.62", "0.49", "0.50"), ("2.62", "", "0.50")])
def test_mongo_old_components_are_actionable(agent, helper, reason):
    a = row(server(agent=agent, helper=helper))
    assert not a.can_dump and reason in a.note


def test_mongo_schema_accepts_only_named_engine():
    assert BackupCommandIn(action="dump_setup", engine="mongo").engine == "mongo"
    with pytest.raises(ValueError):
        BackupCommandIn(action="dump_setup", engine="mongo;id")


@pytest.mark.parametrize("agent,helper,container,expected", [
    ("2.61", "0.50", "document-db", 409), ("2.62", "0.49", "document-db", 409),
    ("2.62", "0.50", "k8s.default.sts.document-db", 409),
    ("2.62", "0.50", "document-db", 200)])
async def test_mongo_api_enforces_capability(client, auth_headers, agent, helper, container, expected):
    r = await client.post("/api/servers", json={"name": "document-node"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}
    report = {"agent_version": agent, "mem_used": 1, "mem_total": 2,
              "setup_versions": {"backup-setup": helper}}
    assert (await client.post("/api/agent/report", json=report, headers=ah)).status_code == 200
    r = await client.post(f"/api/servers/{sid}/backup/command", headers=auth_headers,
        json={"action": "dump_setup", "engine": "mongo", "container": container})
    assert r.status_code == expected, r.text
    commands = (await client.post("/api/agent/report", json=report, headers=ah)).json()["backup_commands"]
    assert bool(commands) == (expected == 200)
    if commands:
        assert commands[0]["engine"] == "mongo" and commands[0]["container"] == container


def test_explicit_error_overrides_fresh_archive_and_first_run_grace():
    from datetime import datetime, timezone
    from app.collector import dump_issues
    d={"engine":"mongo","container":"document-db","files":1,"last_ts":time.time(),
       "enabled_ts":time.time(),"error_ts":time.time()}
    s=server(dump=d)
    assert row(s).kind=="db" and "ошибкой" in row(s).note
    broken,reason,_,_=dump_issues(s.last_report,datetime.now(timezone.utc))
    assert broken==["mongo@document-db"] and "ошибкой" in reason
