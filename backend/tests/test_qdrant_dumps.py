"""Qdrant uses the managed dump flow for Docker and standalone Kubernetes targets."""
import time

import pytest

from app.api.servers import _backup_coverage
from app.models import Server


CONTAINER = {"name": "vector-db", "image": "qdrant/qdrant:v1.15.0", "state": "running"}
POD = {"ns": "default", "name": "vector-db-0", "owner": "StatefulSet",
       "image": CONTAINER["image"], "phase": "Running", "ready": True}
WORKLOAD = "k8s.default.sts.vector-db"


@pytest.mark.parametrize("agent,helper,containers,can_dump", [
    ("2.65", "0.51", [CONTAINER], False),
    ("2.66", "0.50", [CONTAINER], False),
    ("2.66", "0.51", [], False),
    ("2.66", "0.51", [CONTAINER], True),
])
def test_qdrant_dump_coverage(agent, helper, containers, can_dump):
    rep = {"setup_versions": {"backup-setup": helper}, "db_engines": ["Qdrant"],
           "backup": {"present": True, "manageable": True, "configured": True},
           "docker": {"containers": containers}}
    server = Server(name="vector-node", token_hash="x", agent_version=agent, last_report=rep)
    row = next(a for a in _backup_coverage(server) if a.subject == "Qdrant")
    assert row.dump_engine == "qdrant"
    assert row.can_dump is can_dump
    if not can_dump:
        return
    dump = {"engine": "qdrant", "container": "vector-db", "files": 1, "last_ts": time.time()}
    rep["backup"]["dumps"] = [dump]
    assert next(a for a in _backup_coverage(server) if a.subject == "Qdrant").kind == "db_ok"
    dump["error_ts"] = time.time()
    assert next(a for a in _backup_coverage(server) if a.subject == "Qdrant").kind == "db"


@pytest.mark.parametrize("container,expected", [
    ("", 409), ("other-db", 409), ("k8s.default.sts.vector-db", 409), ("vector-db", 200),
])
async def test_qdrant_dump_command_requires_docker_target(client, auth_headers, container, expected):
    r = await client.post("/api/servers", json={"name": "vector-node"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    agent_auth = {"Authorization": f"Bearer {token}"}
    report = {"agent_version": "2.66", "mem_used": 1, "mem_total": 2,
              "setup_versions": {"backup-setup": "0.51"}, "docker": {"containers": [CONTAINER]}}
    assert (await client.post("/api/agent/report", json=report, headers=agent_auth)).status_code == 200
    r = await client.post(f"/api/servers/{sid}/backup/command", headers=auth_headers,
                          json={"action": "dump_setup", "engine": "qdrant", "container": container})
    assert r.status_code == expected, r.text
    commands = (await client.post("/api/agent/report", json=report, headers=agent_auth)).json()["backup_commands"]
    assert bool(commands) == (expected == 200)
    if commands:
        assert commands[0]["engine"] == "qdrant" and commands[0]["container"] == container


@pytest.mark.parametrize("helper,access,pods,can_dump", [
    ("0.51", {"exec": True, "portforward": True}, [POD], False),
    ("0.52", {"exec": True}, [POD], False),
    ("0.52", {"portforward": True}, [POD], False),
    ("0.52", {"exec": True, "portforward": True}, [dict(POD, ready=False)], False),
    ("0.52", {"exec": True, "portforward": True}, [POD, dict(POD, name="vector-db-1")], False),
    ("0.52", {"exec": True, "portforward": True}, [POD], True),
])
async def test_qdrant_kube_coverage_matches_dump_command(client, auth_headers, helper, access, pods, can_dump):
    report = {"agent_version": "2.66", "mem_used": 1, "mem_total": 2,
              "setup_versions": {"backup-setup": helper}, "extras": {"kube-dumps": access},
              "backup": {"present": True, "manageable": True, "configured": True},
              "kube": {"pods": pods}}
    server = Server(name="vector-node", token_hash="x", agent_version="2.66", last_report=report)
    row = next(a for a in _backup_coverage(server) if a.subject == "Qdrant")
    assert row.container == WORKLOAD
    assert row.can_dump is can_dump

    r = await client.post("/api/servers", json={"name": "vector-node"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    agent_auth = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/agent/report", json=report, headers=agent_auth)).status_code == 200
    r = await client.post(f"/api/servers/{sid}/backup/command", headers=auth_headers,
                          json={"action": "dump_setup", "engine": "qdrant", "container": WORKLOAD})
    assert r.status_code == (200 if can_dump else 409), r.text
    commands = (await client.post("/api/agent/report", json=report, headers=agent_auth)).json()["backup_commands"]
    assert bool(commands) == can_dump
    if commands:
        assert commands[0]["engine"] == "qdrant" and commands[0]["container"] == WORKLOAD
        report["backup"]["dumps"] = [
            {"engine": "qdrant", "container": WORKLOAD, "files": 1, "last_ts": time.time()},
        ]
        assert next(a for a in _backup_coverage(server) if a.subject == "Qdrant").kind == "db_ok"
