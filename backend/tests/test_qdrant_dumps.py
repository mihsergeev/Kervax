"""Qdrant uses the managed dump flow for running Docker targets."""
import time

import pytest

from app.api.servers import _backup_coverage
from app.models import Server


CONTAINER = {"name": "vector-db", "image": "qdrant/qdrant:v1.15.0", "state": "running"}


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
