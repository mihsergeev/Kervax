import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Update, select

from app import backup_command_result, collector
from app.api import servers
from app.models import BackupCommand, DockerCommand, KubeCommand, PanelError

from test_api_access import _report, _server, _user


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


@pytest.mark.parametrize(("kind", "body"), [
    ("docker", {"container": "demo", "action": "logs"}),
    ("kube", {"ns": "default", "kind": "pod", "name": "demo", "action": "logs"}),
    ("backup", {"action": "set_schedule", "schedule": "12:00"}),
])
async def test_agent_result_requires_claim_and_is_final(client, auth_headers, kind, body):
    server_id, agent = await _server(client, auth_headers, "node-a")
    _, other_agent = await _server(client, auth_headers, "node-b")
    path = f"/api/servers/{server_id}/{kind}/command"
    created = await client.post(path, headers=auth_headers, json=body)
    assert created.status_code == 200, created.text
    cid = created.json()["id"]

    async def result(headers, output, ok):
        return await client.post(f"/api/agent/{kind}-result", headers=headers,
                                 json={"id": cid, "ok": ok, "output": output})

    assert (await result(other_agent, "from another node", True)).status_code == 404
    assert (await result(agent, "before delivery", True)).status_code == 200
    state = await client.get(f"{path}/{cid}", headers=auth_headers)
    assert state.json()["status"] == "pending"
    poll = await client.get("/api/agent/commands", headers=agent)
    assert [command["id"] for command in poll.json()[f"{kind}_commands"]] == [cid]

    assert (await result(agent, "actual result", True)).status_code == 200
    assert (await result(agent, "duplicate", False)).status_code == 200
    state = (await client.get(f"{path}/{cid}", headers=auth_headers)).json()
    assert (state["status"], state["ok"], state["result"]) == ("done", True, "actual result")


async def test_status_poll_does_not_replace_result_saved_during_timeout(client, auth_headers, monkeypatch):
    server_id, agent = await _server(client, auth_headers, "node-a")
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        cmd = DockerCommand(server_id=server_id, action="logs", container="demo", status="running",
                            created_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        session.add(cmd)
        await session.commit()

    transition = servers._transition_running_command

    async def transition_with_reply(session, command, **values):
        if values.get("result") == "агент не ответил (таймаут)":
            reply = await client.post("/api/agent/docker-result", headers=agent,
                                      json={"id": cmd.id, "ok": True, "output": "actual result"})
            assert reply.status_code == 200
        return await transition(session, command, **values)

    monkeypatch.setattr(servers, "_transition_running_command", transition_with_reply)
    path = f"/api/servers/{server_id}/docker/command/{cmd.id}"
    state = (await client.get(path, headers=auth_headers)).json()
    assert (state["status"], state["ok"], state["result"]) == ("done", True, "actual result")


async def test_status_poll_cannot_apply_manual_timeout_to_backup_job_command(client, auth_headers):
    server_id, agent = await _server(client, auth_headers, "node-a")
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        cmd = BackupCommand(server_id=server_id, action="deploy_server", status="running",
                            created_at=datetime.now(timezone.utc) - timedelta(minutes=2))
        session.add(cmd)
        await session.commit()

    path = f"/api/servers/{server_id}/backup/command/{cmd.id}"
    assert (await client.get(path, headers=auth_headers)).json()["status"] == "running"
    result = await client.post("/api/agent/backup-result", headers=agent,
                               json={"id": cmd.id, "ok": True, "output": "deployed"})
    assert result.status_code == 200
    assert (await client.get(path, headers=auth_headers)).json()["status"] == "done"


@pytest.mark.parametrize("endpoint", ["report", "commands"])
async def test_expired_container_commands_never_reach_reconnected_agent(client, auth_headers, endpoint, monkeypatch):
    # Each parametrized fixture uses a new database with the same server IDs.
    monkeypatch.setattr(collector.panel_errors, "_recent", {})
    server_id, agent = await _server(client, auth_headers, "node-a")
    factory = client._transport.app.state.session_factory
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    async with factory() as session:
        commands = {
            "docker": (
                DockerCommand(server_id=server_id, action="restart", container="stale", created_at=old,
                              created_by="admin"),
                DockerCommand(server_id=server_id, action="logs", container="fresh"),
            ),
            "kube": (
                KubeCommand(server_id=server_id, action="delete_pod", ns="default", kind="pod", name="stale",
                            created_at=old, created_by="admin"),
                KubeCommand(server_id=server_id, action="logs", ns="default", kind="pod", name="fresh"),
            ),
        }
        session.add_all([command for pair in commands.values() for command in pair])
        await session.commit()

    if endpoint == "report":
        reply = await client.post("/api/agent/report", headers=agent, json=_report())
    else:
        reply = await client.get("/api/agent/commands", headers=agent)
    assert reply.status_code == 200, reply.text
    for kind, (_, fresh) in commands.items():
        assert [item["id"] for item in reply.json()[f"{kind}_commands"]] == [fresh.id]

    await collector._expire_commands(factory)
    async with factory() as session:
        for stale, fresh in commands.values():
            assert (await session.get(type(stale), stale.id)).status == "error"
            assert (await session.get(type(fresh), fresh.id)).status == "running"
        errors = await session.scalars(select(PanelError).where(PanelError.kind == "command"))
        assert {error.action for error in errors} == {"docker restart", "kube delete_pod"}


async def test_cleanup_preserves_deploy_wait_and_expires_abandoned_backups(client, auth_headers):
    server_id, agent = await _server(client, auth_headers, "node-a")
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    async with factory() as session:
        deploy = BackupCommand(server_id=server_id, action="deploy_server", status="running",
                               created_at=now - timedelta(minutes=10, seconds=30))
        stuck = BackupCommand(server_id=server_id, action="provision", status="running",
                              created_at=now - timedelta(minutes=10, seconds=30))
        abandoned = BackupCommand(server_id=server_id, action="deploy_server", status="running",
                                   created_at=now - timedelta(minutes=15))
        session.add_all([deploy, stuck, abandoned])
        await session.commit()

    await collector._expire_commands(factory)
    async with factory() as session:
        assert (await session.get(BackupCommand, deploy.id)).status == "running"
        assert (await session.get(BackupCommand, stuck.id)).status == "error"
        assert (await session.get(BackupCommand, abandoned.id)).status == "error"
    response = await client.post("/api/agent/backup-result", headers=agent,
                                 json={"id": deploy.id, "ok": True, "output": "deployed"})
    assert response.status_code == 200, response.text
    async with factory() as session:
        assert (await session.get(BackupCommand, deploy.id)).status == "done"


@pytest.mark.parametrize("action", ["get_creds", "get_client_creds", "provision_client", "provision_new"])
async def test_backup_result_with_password_is_private(client, auth_headers, action):
    server_id, agent_headers = await _server(client, auth_headers, "node-a")
    editor = await _user(client, auth_headers, "backup-editor")
    factory = client._transport.app.state.session_factory
    cmd_id = await servers._bcmd_create(factory, server_id, action, {})
    response = await client.get("/api/agent/commands", headers=agent_headers)
    assert [item["id"] for item in response.json()["backup_commands"]] == [cmd_id]
    encoded = base64.b64encode(b"repopass=synthetic-repository-password\n").decode()
    output = f"OK existing demo\nREPOPASS_B64={encoded}" if action.startswith("provision_") else encoded
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


@pytest.mark.parametrize("backup_group", ["team-a", "team-b"])
async def test_scoped_admin_cannot_recover_repository_selected_by_agent_report(
    client, auth_headers, monkeypatch, backup_group,
):
    scoped_admin = await _user(client, auth_headers, "scoped-admin", role="admin", server_groups=["team-a"])
    server_id, agent = await _server(client, auth_headers, "source-node", "team-a")
    backup_id, backup_agent = await _server(client, auth_headers, "backup-node", backup_group)
    url = "rest:https://backup-node.example:64101/other-repository"
    assert (await client.post("/api/agent/report", headers=agent,
                              json=_report(backup={"present": True, "repo_dest": url}))).status_code == 200
    assert (await client.post("/api/agent/report", headers=backup_agent,
                              json=_report(hostname="backup-node.example", backup_server={"present": True}))).status_code == 200
    queued = []

    async def queue(_factory, sid, action, payload):
        queued.append((sid, action, payload))
        return len(queued)

    async def wait(_factory, cid, _timeout):
        action = queued[cid - 1][1]
        if action == "get_client_creds":
            return True, base64.b64encode(b"repopass=synthetic-repository-password\n").decode()
        if action == "get_cert":
            return True, base64.b64encode(b"synthetic-certificate").decode()
        return False, ""

    monkeypatch.setattr(servers, "_bcmd_create", queue)
    monkeypatch.setattr(servers, "_bcmd_wait", wait)
    path = f"/api/servers/{server_id}/backup/credentials"
    response = await client.post(path, headers=scoped_admin)
    assert response.status_code == 403
    assert queued == [(server_id, "get_creds", {})]

    queued.clear()
    response = await client.post(path, headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["repopass"] == "synthetic-repository-password"
    assert response.json()["source"] == "backup-server"
    assert queued == [(server_id, "get_creds", {}), (backup_id, "get_client_creds", {"name": "other-repository"}),
                      (backup_id, "get_cert", {})]


async def test_scoped_admin_can_get_client_credentials_without_querying_hidden_backup_server(
    client, auth_headers, monkeypatch,
):
    scoped_admin = await _user(client, auth_headers, "scoped-admin", role="admin", server_groups=["team-a"])
    server_id, _ = await _server(client, auth_headers, "source-node", "team-a")
    _, backup_agent = await _server(client, auth_headers, "backup-node", "team-b")
    assert (await client.post("/api/agent/report", headers=backup_agent, json=_report(
        hostname="backup-node.example", backup_server={"present": True, "data_dir": "/private/backups"},
    ))).status_code == 200
    url = "rest:https://backup-node.example:64101/source-node"
    queued = []

    async def queue(_factory, sid, action, payload):
        queued.append((sid, action, payload))
        return 1

    async def wait(_factory, _cid, _timeout):
        return True, base64.b64encode(f"repo_url={url}\nrepopass=synthetic-client-password\n".encode()).decode()

    monkeypatch.setattr(servers, "_bcmd_create", queue)
    monkeypatch.setattr(servers, "_bcmd_wait", wait)
    response = await client.post(f"/api/servers/{server_id}/backup/credentials", headers=scoped_admin)
    assert response.status_code == 200, response.text
    assert response.json()["repopass"] == "synthetic-client-password"
    assert response.json()["repo_local"] == response.json()["cacert_pem"] == ""
    assert queued == [(server_id, "get_creds", {})]


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


async def test_timeout_logs_only_commands_it_closed(client, auth_headers):
    server_id, agent_headers = await _server(client, auth_headers, "node-a")
    factory = client._transport.app.state.session_factory
    old = datetime.now(timezone.utc) - timedelta(minutes=15)
    async with factory() as session:
        replied = BackupCommand(server_id=server_id, action="set_schedule", status="running",
                                created_at=old, created_by="admin")
        stuck = BackupCommand(server_id=server_id, action="set_schedule", status="running",
                               created_at=old, created_by="admin")
        session.add_all([replied, stuck])
        await session.commit()

    @asynccontextmanager
    async def scheduler_session():
        async with factory() as session:
            execute = session.execute

            async def execute_with_reply(statement, *args, **kwargs):
                if isinstance(statement, Update) and statement.table.name == BackupCommand.__tablename__:
                    session.execute = execute
                    response = await client.post("/api/agent/backup-result", headers=agent_headers,
                                                 json={"id": replied.id, "ok": True, "output": "done"})
                    assert response.status_code == 200
                return await execute(statement, *args, **kwargs)

            session.execute = execute_with_reply
            yield session

    await collector._expire_commands(scheduler_session)

    async with factory() as session:
        assert (await session.get(BackupCommand, replied.id)).status == "done"
        assert (await session.get(BackupCommand, stuck.id)).status == "error"
        errors = await session.scalars(select(PanelError).where(PanelError.kind == "command"))
        assert [error.extra["cmd_id"] for error in errors] == [stuck.id]
