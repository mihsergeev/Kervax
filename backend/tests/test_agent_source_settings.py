"""Explicit source configuration, permissions and upgrade of existing server rows."""
import importlib.util
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.schemas import ServerCreate, ServerUpdate
from tests.test_api_access import _user


@pytest.mark.parametrize("schema", [ServerCreate, ServerUpdate])
def test_sources_are_explicit_canonical_unique_ips(schema):
    body = schema(name="node-a", agent_source_ips=[
        " 198.51.100.10 ", "::ffff:198.51.100.10", "2001:0DB8:0:0::10", "2001:db8::10",
    ])
    assert body.agent_source_ips == ["198.51.100.10", "2001:db8::10"]
    assert schema(name="node-a", agent_source_ips=[]).agent_source_ips == []
    full = [f"198.51.100.{n}" for n in range(1, 17)]
    assert schema(name="node-a", agent_source_ips=full).agent_source_ips == full


@pytest.mark.parametrize("sources", [
    None, "198.51.100.10", {"ip": "198.51.100.10"}, [123], [True], [None],
    [""], [" "], ["example.com"], ["198.51.100.0/24"], ["2001:db8::/64"],
    ["198.51.100.10,198.51.100.11"], ["198.51.100.10\n198.51.100.11"],
    ["0.0.0.0"], ["::"], ["224.0.0.1"], ["::ffff:0.0.0.0"], ["ff02::1"],
    ["fe80::1%eth0"], ["198.51.100.10", ""], ["198.51.100.10"] * 17,
])
@pytest.mark.parametrize("schema", [ServerCreate, ServerUpdate])
def test_sources_reject_non_ip_or_non_list_input(schema, sources):
    with pytest.raises(ValidationError):
        schema(name="node-a", agent_source_ips=sources)


def test_source_defaults_and_env_do_not_enable_filter(monkeypatch):
    monkeypatch.delenv("KERVAX_AGENT_SOURCE_GUARD", raising=False)
    assert Settings(_env_file=None).agent_source_guard is False
    monkeypatch.setenv("KERVAX_AGENT_SOURCE_GUARD", "1")
    assert Settings(_env_file=None).agent_source_guard is True
    assert ServerCreate(name="node-a", agent_ip="198.51.100.10").agent_source_ips == []
    assert "agent_source_ips" not in ServerUpdate(name="node-a").model_dump(exclude_unset=True)


async def test_source_settings_roundtrip_and_readonly_global_state(client, auth_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "agent_source_guard", False)
    created = await client.post("/api/servers", headers=auth_headers, json={
        "name": "node-a", "agent_ip": "198.51.100.10",
        "agent_source_ips": ["::ffff:203.0.113.10", "203.0.113.10"],
        "agent_source_guard_enabled": True,
    })
    assert created.status_code == 201, created.text
    server = created.json()["server"]
    sid = server["id"]
    assert server["agent_source_ips"] == ["203.0.113.10"]
    assert server["agent_source_guard_enabled"] is False
    changed = await client.patch(f"/api/servers/{sid}", headers=auth_headers,
                                 json={"agent_ip": "198.51.100.11", "agent_source_guard_enabled": True})
    assert changed.status_code == 200, changed.text
    assert changed.json()["agent_source_ips"] == ["203.0.113.10"]
    assert changed.json()["agent_source_guard_enabled"] is False
    monkeypatch.setattr(get_settings(), "agent_source_guard", True)
    read = await client.get(f"/api/servers/{sid}", headers=auth_headers)
    assert read.json()["agent_source_guard_enabled"] is True
    clear = await client.patch(f"/api/servers/{sid}", headers=auth_headers, json={"agent_source_ips": []})
    assert clear.status_code == 200 and clear.json()["agent_source_ips"] == []


async def test_source_settings_follow_existing_editor_and_viewer_permissions(client, auth_headers):
    editor = await _user(client, auth_headers, "source-editor", server_groups=["ops"])
    viewer = await _user(client, auth_headers, "source-viewer", role="viewer")
    mine = await client.post("/api/servers", headers=editor,
                             json={"name": "node-a", "group_name": "ops", "agent_source_ips": ["203.0.113.10"]})
    assert mine.status_code == 201, mine.text
    sid = mine.json()["server"]["id"]
    patch = {"agent_source_ips": ["2001:db8::10"]}
    changed = await client.patch(f"/api/servers/{sid}", headers=editor, json=patch)
    assert changed.status_code == 200 and changed.json()["agent_source_ips"] == patch["agent_source_ips"]
    assert (await client.patch(f"/api/servers/{sid}", headers=viewer, json=patch)).status_code == 403
    foreign = await client.post("/api/servers", headers=auth_headers, json={"name": "node-b", "group_name": "other"})
    fid = foreign.json()["server"]["id"]
    assert (await client.patch(f"/api/servers/{fid}", headers=editor, json=patch)).status_code == 404
    assert (await client.post("/api/servers", headers=viewer,
                             json={"name": "node-c", **patch})).status_code == 403


async def test_invalid_source_update_keeps_saved_list(client, auth_headers):
    created = await client.post("/api/servers", headers=auth_headers,
                                json={"name": "node-a", "agent_source_ips": ["203.0.113.10"]})
    sid = created.json()["server"]["id"]
    for value in (None, [""], ["203.0.113.0/24"], "203.0.113.11"):
        result = await client.patch(f"/api/servers/{sid}", headers=auth_headers, json={"agent_source_ips": value})
        assert result.status_code == 422, result.text
    saved = await client.get(f"/api/servers/{sid}", headers=auth_headers)
    assert saved.json()["agent_source_ips"] == ["203.0.113.10"]


def test_old_database_migration_does_not_copy_observed_or_previous_ips():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0142_agent_source_ips.py"
    spec = importlib.util.spec_from_file_location("migration_0142", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE servers (id INTEGER PRIMARY KEY, agent_ip TEXT, external_ip TEXT, last_report JSON, token_hash TEXT)"))
        conn.execute(sa.text("INSERT INTO servers VALUES (1, :a, :e, :r, :t)"), {
            "a": "198.51.100.10", "e": "203.0.113.10", "r": json.dumps({"ips": ["192.0.2.10"]}), "t": "fixture-hash",
        })
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        row = conn.execute(sa.text("SELECT * FROM servers WHERE id=1")).mappings().one()
        assert json.loads(row["agent_source_ips"]) == []
        assert row["agent_ip"] == "198.51.100.10" and row["token_hash"] == "fixture-hash"
        assert json.loads(row["last_report"]) == {"ips": ["192.0.2.10"]}
        conn.execute(sa.text("INSERT INTO servers(id) VALUES (2)"))
        assert json.loads(conn.scalar(sa.text("SELECT agent_source_ips FROM servers WHERE id=2"))) == []
        assert sa.inspect(conn).get_columns("servers")[-1]["nullable"] is False
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("INSERT INTO servers(id,agent_source_ips) VALUES (3,NULL)"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "agent_source_ips" not in {c["name"] for c in sa.inspect(conn).get_columns("servers")}
        assert conn.scalar(sa.text("SELECT token_hash FROM servers WHERE id=1")) == "fixture-hash"
    engine.dispose()
