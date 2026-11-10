import asyncio
import base64
from datetime import datetime, timedelta, timezone

import pytest

from app import backup_command_result, collector
from app.api import servers
from app.models import BackupCommand, DockerCommand, KubeCommand

from test_api_access import _server, _user


@pytest.mark.parametrize(("take", "model", "fields"), [
    (servers._take_backup_commands, BackupCommand, {}),
    (servers._take_docker_commands, DockerCommand, {"container": "demo"}),
    (servers._take_kube_commands, KubeCommand, {"ns": "default", "kind": "pod", "name": "demo"}),
])
async def test_concurrent_agent_polls_claim_one_command_once(client, auth_headers, take, model, fields):
    postgres = client._transport.app.state.engine.dialect.name == "postgresql"
    server_id, _ = await _server(client, auth_headers, "node-a")
    factory = client._transport.app.state.session_factory
    async with factory() as seed:
        cmd = model(server_id=server_id, action="restart", **fields)
        seed.add(cmd)
        await seed.commit()

    # /report and /commands use this same claim, before either endpoint commits.
    async with factory() as report, factory() as fast_poll:
        assert [item["id"] for item in await take(report, server_id)] == [cmd.id]
        if postgres:
            assert await take(fast_poll, server_id) == []
            await report.commit()
        else:
            # SQLite waits for the first writer, then rechecks the pending state.
            async with asyncio.TaskGroup() as tasks:
                claim = tasks.create_task(take(fast_poll, server_id))
                try:
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(claim), timeout=0.2)
                finally:
                    await report.commit()
            assert claim.result() == []
        assert await take(fast_poll, server_id) == []


@pytest.mark.parametrize("action", ["get_creds", "get_client_creds", "provision_client"])
async def test_backup_result_with_password_is_private(client, auth_headers, action):
    server_id, agent_headers = await _server(client, auth_headers, "node-a")
    editor = await _user(client, auth_headers, "backup-editor")
    factory = client._transport.app.state.session_factory
    cmd_id = await servers._bcmd_create(factory, server_id, action, {})
    response = await client.get("/api/agent/commands", headers=agent_headers)
    assert [item["id"] for item in response.json()["backup_commands"]] == [cmd_id]
    encoded = base64.b64encode(b"repopass=synthetic-repository-password\n").decode()
    output = f"OK existing demo\nREPOPASS_B64={encoded}" if action == "provision_client" else encoded
    response = await client.post("/api/agent/backup-result", headers=agent_headers,
                                 json={"id": cmd_id, "ok": True, "output": output})
    assert response.status_code == 200, response.text
    url = f"/api/servers/{server_id}/backup/command/{cmd_id}"
    response = await client.get(url, headers=editor)
    assert response.status_code == 404
    async with factory() as session:
        saved = await session.get(BackupCommand, cmd_id)
        assert saved.result and encoded not in saved.result
    results = await asyncio.gather(servers._bcmd_wait(factory, cmd_id, 4),
                                   servers._bcmd_wait(factory, cmd_id, 4))
    assert results.count((True, output)) == 1
    assert sum(ok for ok, _ in results) == 1
    # An agent retry cannot recreate the secret after the waiter consumes it.
    response = await client.post("/api/agent/backup-result", headers=agent_headers,
                                 json={"id": cmd_id, "ok": True, "output": output})
    assert response.status_code == 200, response.text
    async with factory() as session:
        saved = await session.get(BackupCommand, cmd_id)
        assert saved.result == ""


async def test_admin_can_still_get_backup_credentials(client, auth_headers, monkeypatch):
    server_id, agent_headers = await _server(client, auth_headers, "node-a")
    queued = asyncio.Event()
    create = servers._bcmd_create

    async def signal_created(*args):
        cmd_id = await create(*args)
        queued.set()
        return cmd_id

    monkeypatch.setattr(servers, "_bcmd_create", signal_created)
    async with asyncio.TaskGroup() as tasks:
        request = tasks.create_task(client.post(f"/api/servers/{server_id}/backup/credentials", headers=auth_headers))
        await asyncio.wait_for(queued.wait(), timeout=5)
        response = await client.get("/api/agent/commands", headers=agent_headers)
        [command] = response.json()["backup_commands"]
        assert command["action"] == "get_creds"
        output = base64.b64encode(b"repopass=synthetic-repository-password\n").decode()
        response = await client.post("/api/agent/backup-result", headers=agent_headers,
                                     json={"id": command["id"], "ok": True, "output": output})
        assert response.status_code == 200

    result = request.result()
    assert result.status_code == 200, result.text
    assert result.json()["repopass"] == "synthetic-repository-password"


async def test_cleanup_scrubs_legacy_and_abandoned_command_secrets(client, auth_headers):
    server_id, _ = await _server(client, auth_headers, "node-a")
    factory = client._transport.app.state.session_factory
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    async with factory() as session:
        legacy = BackupCommand(server_id=server_id, action="get_creds", status="done", result="legacy-secret")
        expired = BackupCommand(server_id=server_id, action="get_client_creds", status="done", created_at=old)
        fresh = BackupCommand(server_id=server_id, action="provision_client", status="done")
        stuck = BackupCommand(server_id=server_id, action="provision", created_at=old,
                              payload={"repopass": "abandoned-secret"})
        session.add_all([legacy, expired, fresh, stuck])
        await session.flush()
        expired.result = backup_command_result.seal(expired.id, "expired-secret")
        fresh.result = backup_command_result.seal(fresh.id, "active-secret")
        await session.commit()

    await collector._expire_commands(factory)

    async with factory() as session:
        assert (await session.get(BackupCommand, legacy.id)).result == ""
        assert (await session.get(BackupCommand, expired.id)).result == ""
        assert (await session.get(BackupCommand, fresh.id)).result == fresh.result
        assert (await session.get(BackupCommand, stuck.id)).payload is None
