import asyncio
import base64

import pytest
from pydantic import ValidationError

from app.schemas import BackupServerDeployIn, BackupServerStorageIn

from test_api_access import _user


@pytest.mark.parametrize("mount", ["backup", "/backup/../other", "/backup//data", "/backup\nport=5", "/backup:$HOME"])
def test_deploy_rejects_unsafe_mount_syntax(mount):
    with pytest.raises(ValidationError):
        BackupServerDeployIn(storage_mount=mount)
    with pytest.raises(ValidationError):
        BackupServerStorageIn(storage_mount=mount)


async def _node(client, auth_headers, name, **fields):
    created = (await client.post("/api/servers", json={"name": name, **fields}, headers=auth_headers)).json()
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

    omitted = await client.post(path, json={"port": 64200, "tls": False}, headers=auth_headers)
    assert omitted.status_code == 409 and "выберите раздел" in omitted.json()["detail"]

    for fields in (payload, {**payload, "storage_mount": "/"}):
        if fields["storage_mount"] == "/":
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


async def test_move_empty_backup_storage_without_serialized_repos(client, auth_headers):
    sid, agent = await _node(client, auth_headers, "backup-node")
    disk = {"mount": "/backup", "total": 2**41, "used": 0, "avail": 2**41}
    path = f"/api/servers/{sid}/backup-server/storage"
    bs = {"present": True, "running": True, "helper_version": 1, "port": 64201,
          "data_dir": "/app/rest-server/data"}

    async def report(helper="0.40", **changes):
        await _report(client, agent, "2.63", helper, disks=[disk], backup_server={**bs, **changes})

    async def move(mount="/backup"):
        return await client.post(path, json={"storage_mount": mount}, headers=auth_headers)

    assert (await move()).status_code == 409
    assert (await move("/")).status_code == 422
    await report("0.39")
    assert (await move()).status_code == 409
    await report(helper_version=0)
    assert (await move()).status_code == 409
    await report(data_dir="/external")
    assert (await move()).status_code == 409
    await report(repos=[{"name": "node-a"}])
    assert (await move()).status_code == 409
    await report()
    assert (await move("/missing")).status_code == 400

    for data_dir in ("/app/rest-server/data", "/backup/kervax-rest-server/data"):
        await report(data_dir=data_dir)
        response = await move()
        assert response.status_code == 200, response.text
        job = response.json()
        command = await _wait_for_command(client, agent)
        assert command["action"] == "deploy_server" and command["port"] == 64201
        assert command["storage_mount"] == "/backup"
        result = await client.post("/api/agent/backup-result", headers=agent,
                                   json={"id": command["id"], "ok": True, "output": "moved"})
        assert result.status_code == 200, result.text
        for _ in range(40):
            response = await client.get(f"/api/servers/{sid}/backup/setup/{job['id']}", headers=auth_headers)
            assert response.status_code == 200, response.text
            job = response.json()
            if job["status"] != "running":
                break
            await asyncio.sleep(0.06)
        assert job["status"] == "done" and job["steps"] == [{"step": "move-storage", "ok": True, "detail": "moved"}]


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


@pytest.mark.parametrize("role", ["editor", "admin"])
async def test_scoped_setup_cannot_attach_existing_repository(client, auth_headers, role):
    operator = await _user(client, auth_headers, "scoped-operator", role=role, server_groups=["team-a"])
    source_id, source_agent = await _node(client, auth_headers, "source", group_name="team-a", agent_ip="192.0.2.10")
    backup_id, backup_agent = await _node(client, auth_headers, "backup", group_name="team-a", agent_ip="192.0.2.11")
    await _report(client, source_agent, "2.69", "0.43", hostname="existing-repo")
    backup_server = {"present": True, "running": True, "repos": [{"name": "existing-repo"}]}
    path = f"/api/servers/{source_id}/backup/setup"
    body = {"backup_server_id": backup_id, "reinstall": True, "tls": False}
    await _report(client, backup_agent, "2.68", "0.43", backup_server=backup_server)
    assert (await client.post(path, headers=operator, json=body)).status_code == 409
    await _report(client, backup_agent, "2.69", "0.42", backup_server=backup_server)
    assert (await client.post(path, headers=operator, json=body)).status_code == 409
    assert (await client.get("/api/agent/commands", headers=backup_agent)).json()["backup_commands"] == []

    await _report(client, backup_agent, "2.69", "0.43", backup_server=backup_server)
    created = await client.post(path, headers=operator, json=body)
    assert created.status_code == 200, created.text
    command = await _wait_for_command(client, backup_agent)
    assert command["action"] == "provision_new" and command["name"] == "existing-repo"
    # Even an unexpected response resembling legacy provisioning cannot send this password to the client.
    secret = "synthetic-existing-password"
    encoded = base64.b64encode(secret.encode()).decode()
    result = await client.post("/api/agent/backup-result", headers=backup_agent,
                               json={"id": command["id"], "ok": True, "output": f"REPOPASS_B64={encoded}"})
    assert result.status_code == 200, result.text
    for _ in range(40):
        response = await client.get(f"{path}/{created.json()['id']}", headers=operator)
        assert response.status_code == 200, response.text
        if response.json()["status"] != "running":
            break
        await asyncio.sleep(0.06)
    assert response.json()["status"] == "error"
    assert encoded not in response.text and secret not in response.text
    assert (await client.get("/api/agent/commands", headers=source_agent)).json()["backup_commands"] == []
