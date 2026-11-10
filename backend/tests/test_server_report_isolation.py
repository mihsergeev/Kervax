from datetime import datetime, timedelta, timezone

import pytest

from app import collector
from app.config import Settings
from app.models import Server

from test_api_access import _report, _server


@pytest.mark.parametrize("bad_data", [
    {"kube": {"pods": 42}},
    {"dns": {"servers": 42}},
    {"disks": [{"mount": ["invalid"], "used": 20, "total": 100}]},
])
async def test_bad_inventory_does_not_stop_other_server_alerts(client, auth_headers, monkeypatch, bad_data):
    broken_id, agent = await _server(client, auth_headers, "broken-agent")
    offline_id, offline_agent = await _server(client, auth_headers, "offline-node")
    response = await client.post("/api/agent/report", headers=offline_agent, json=_report())
    assert response.status_code == 200, response.text
    ts = int(datetime.now(timezone.utc).timestamp())
    if "dns" in bad_data:
        bad_data = {"dns": {**bad_data["dns"], "ts": ts}}
    response = await client.post("/api/agent/report", headers=agent, json=_report(clock_unix=ts, **bad_data))
    assert response.status_code == 200, response.text
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    async with factory() as session:
        offline = await session.get(Server, offline_id)
        offline.last_seen = now - timedelta(hours=1)
        await session.commit()

    messages = []

    async def send(config, text, parse_mode=None):
        messages.append(text)
        return []

    monkeypatch.setattr(collector.alerts, "send_alert", send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="https://hook.example.test"), now)
    assert any("offline-node" in text for text in messages), messages
    response = await client.get("/api/servers", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert {item["name"] for item in response.json()} == {"broken-agent", "offline-node"}
    if "disks" in bad_data:
        broken = next(item for item in response.json() if item["id"] == broken_id)
        assert broken["last_report"]["disks"] == []
        # A malformed disk received before the upgrade remains safe to read.
        async with factory() as session:
            saved = await session.get(Server, broken_id)
            saved.last_report = {**saved.last_report, **bad_data}
            await session.commit()
        await collector.evaluate_servers(factory, Settings(alert_webhook="https://hook.example.test"), now)


async def test_linux_agent_without_disk_and_load_inventory_keeps_reporting(client, auth_headers):
    server_id, agent = await _server(client, auth_headers, "node-a")
    response = await client.post("/api/agent/report", headers=agent, json=_report(disks=None, load=None))
    assert response.status_code == 200, response.text
    node = await client.get(f"/api/servers/{server_id}", headers=auth_headers)
    assert node.status_code == 200, node.text
    state = node.json()
    assert state["online"] and state["last_report"]["cpu_percent"] == 1
    assert state["last_report"]["disks"] == state["last_report"]["load"] == []
