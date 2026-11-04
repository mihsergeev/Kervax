import asyncio
import base64

import pytest
from pydantic import ValidationError

from app.schemas import BackupServerDeployIn


@pytest.mark.parametrize("mount", ["backup", "/backup/../other", "/backup//data", "/backup\nport=5", "/backup:$HOME"])
def test_deploy_rejects_unsafe_mount_syntax(mount):
    with pytest.raises(ValidationError):
        BackupServerDeployIn(storage_mount=mount)


async def _node(client, auth_headers, name):
    created = (await client.post("/api/servers", json={"name": name}, headers=auth_headers)).json()
    return created["server"]["id"], {"Authorization": f"Bearer {created['token']}"}


async def _report(client, headers, version, helper, **fields):
    report = {"agent_version": version, "mem_used": 1, "mem_total": 2,
              "setup_versions": {"backupserver-setup": helper}, **fields}
    result = await client.post("/api/agent/report", json=report, headers=headers)
    assert result.status_code == 200, result.text


async def _wait_for_command(client, headers):
    for _ in range(30):
        result = await client.get("/api/agent/commands", headers=headers)
        assert result.status_code == 200, result.text
        if result.json()["backup_commands"]:
            return result.json()["backup_commands"][0]
        await asyncio.sleep(0.05)
    pytest.fail("agent did not receive the backup command")


async def test_deploy_sends_mount_and_preserves_legacy_default(client, auth_headers):
    sid, agent = await _node(client, auth_headers, "backup-node")
    disk = {"mount": "/backup", "total": 2**41, "used": 0, "avail": 2**41}
    path = f"/api/servers/{sid}/backup-server/deploy"
    payload = {"port": 64200, "tls": False, "storage_mount": "/backup"}

    await _report(client, agent, "2.62", "0.39", disks=[disk])
    assert (await client.post(path, json=payload, headers=auth_headers)).status_code == 409
    await _report(client, agent, "2.63", "0.38", disks=[disk])
    assert (await client.post(path, json=payload, headers=auth_headers)).status_code == 409
    await _report(client, agent, "2.63", "0.39", disks=[disk])
    assert (await client.post(path, json={**payload, "storage_mount": "/missing"}, headers=auth_headers)).status_code == 400
    assert (await client.post(path, json={**payload, "port": 64101, "tls": True}, headers=auth_headers)).status_code == 400

    for fields in (payload, {"port": 64200, "tls": False}):
        if "storage_mount" not in fields:
            await _report(client, agent, "2.62", "0.38", disks=[disk])
        response = await client.post(path, json=fields, headers=auth_headers)
        assert response.status_code == 200, response.text
        job = response.json()
        command = await _wait_for_command(client, agent)
        assert command["action"] == "deploy_server" and command["port"] == 64200
        assert command.get("storage_mount") == fields.get("storage_mount")
        result = await client.post("/api/agent/backup-result", headers=agent,
                                   json={"id": command["id"], "ok": True, "output": "HTTP 401"})
        assert result.status_code == 200, result.text
        for _ in range(40):
            response = await client.get(f"/api/servers/{sid}/backup/setup/{job['id']}", headers=auth_headers)
            assert response.status_code == 200, response.text
            job = response.json()
            if job["status"] != "running":
                break
            await asyncio.sleep(0.06)
        assert job["status"] == "done" and job["steps"] == [{"step": "deploy-server", "ok": True, "detail": "HTTP 401"}]


async def test_restore_credentials_use_selected_storage_path(client, auth_headers, monkeypatch):
    from app.api import servers

    _, backup_agent = await _node(client, auth_headers, "backup-node")
    await _report(client, backup_agent, "2.63", "0.39", hostname="backup-node.example.com",
                  backup_server={"present": True, "running": True, "data_dir": "/backup/kervax-rest-server/data"})
    sid, _ = await _node(client, auth_headers, "source-node")
    url = "rest:http://source:transport-password@backup-node.example.com:64100/source/"

    async def creds(_factory, _cid, _timeout):
        return True, base64.b64encode(f"repo_url={url}\nrepopass=repository-password\n".encode()).decode()

    monkeypatch.setattr(servers, "_bcmd_wait", creds)
    response = await client.post(f"/api/servers/{sid}/backup/credentials", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["repo_local"] == "/backup/kervax-rest-server/data/source"
