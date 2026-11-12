"""Явное отключение приложения: сервер предлагает соседей, человек выбирает ID."""
from datetime import datetime, timezone

from sqlalchemy import select

from app import checks as checks_exec, collector
from app.models import AgentPathProbe, AgentProbe, Check, CheckIncident, CheckSample, Server


async def _user(client, admin, role, groups):
    name = f"retire-{role}"
    response = await client.post("/api/users", headers=admin, json={
        "username": name, "password": "fixture-password-001", "role": role,
        "site_groups": groups,
    })
    assert response.status_code == 201
    response = await client.post("/api/auth/login", json={
        "username": name, "password": "fixture-password-001",
    })
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


async def test_retire_exact_selection_closes_incidents_and_keeps_history(client, auth_headers):
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        server = Server(name="shared-node", token_hash="fixture-node", enabled=True)
        session.add(server)
        await session.flush()
        ids = []
        for name, group, parent in (
            ("app-a", "visible", server.id),
            ("app-b", "visible", server.id),
            ("other-app", "visible", server.id),
            ("foreign", "hidden", server.id),
            ("other-node", "visible", None),
        ):
            check = Check(name=name, type="http", target=f"https://{name}.example.test",
                          group_name=group, enabled=True,
                          parent_mode="server" if parent else "off", parent_server_id=parent)
            session.add(check)
            await session.flush()
            ids.append(check.id)
            session.add(CheckIncident(check_id=check.id, status="down",
                                      started_at=datetime.now(timezone.utc), notified=True))
            session.add(CheckSample(check_id=check.id, status="down", ts=datetime.now(timezone.utc)))
        await session.commit()

    editor = await _user(client, auth_headers, "editor", ["visible"])
    preview = await client.get(f"/api/checks/{ids[0]}/retirement-candidates", headers=editor)
    assert preview.status_code == 200
    assert {c["id"] for c in preview.json()["checks"]} == set(ids[:3])
    assert (await client.post(f"/api/checks/{ids[0]}/retire", headers=editor,
                              json={"ids": [ids[0], ids[3]]})).status_code == 404
    assert (await client.post(f"/api/checks/{ids[0]}/retire", headers=editor,
                              json={"ids": [ids[0], ids[4]]})).status_code == 409
    result = await client.post(f"/api/checks/{ids[0]}/retire", headers=editor,
                               json={"ids": ids[:2]})
    assert result.status_code == 200 and result.json() == {"updated": 2}
    repeat = await client.post(f"/api/checks/{ids[0]}/retire", headers=editor,
                               json={"ids": ids[:2]})
    assert repeat.status_code == 200 and repeat.json() == {"updated": 0}
    # A scheduler request started before retirement may finish afterwards.
    async with factory() as session:
        row = await session.get(Check, ids[0])
        pending = []
        await collector.record_outcome(session, row, checks_exec.CheckOutcome("down", message="late failure"),
                                       datetime.now(timezone.utc), pending)
        assert not pending
        await session.commit()
    async with factory() as session:
        checks = {c.id: c for c in await session.scalars(select(Check))}
        assert [checks[i].enabled for i in ids] == [False, False, True, True, True]
        incidents = {i.check_id: i for i in await session.scalars(select(CheckIncident))}
        assert all(incidents[i].ended_at is not None for i in ids[:2])
        assert all(incidents[i].ended_at is None for i in ids[2:])
        assert len(list(await session.scalars(select(CheckSample)))) == len(ids) + 1


async def test_retire_viewer_forbidden_and_changed_relation_rejected(client, auth_headers):
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        server = Server(name="node", token_hash="fixture-node", enabled=True)
        session.add(server)
        await session.flush()
        ids = []
        for name in ("first", "second"):
            check = Check(name=name, type="http", target=f"https://{name}.example.test",
                          enabled=True, group_name="visible", parent_mode="server",
                          parent_server_id=server.id)
            session.add(check)
            await session.flush()
            ids.append(check.id)
        await session.commit()
    viewer = await _user(client, auth_headers, "viewer", ["visible"])
    assert (await client.post(f"/api/checks/{ids[0]}/retire", headers=viewer,
                              json={"ids": ids})).status_code == 403
    editor = await _user(client, auth_headers, "editor", ["visible"])
    assert len((await client.get(f"/api/checks/{ids[0]}/retirement-candidates",
                                 headers=editor)).json()["checks"]) == 2
    async with factory() as session:
        second = await session.get(Check, ids[1])
        second.parent_mode = "off"
        second.parent_server_id = None
        await session.commit()
    response = await client.post(f"/api/checks/{ids[0]}/retire", headers=editor,
                                 json={"ids": ids})
    assert response.status_code == 409
    async with factory() as session:
        assert all([(await session.get(Check, cid)).enabled for cid in ids])


async def test_retarget_clears_agent_cache_before_rebind(client, auth_headers):
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    async with factory() as session:
        server = Server(name="node-a", token_hash="fixture-node", enabled=True,
                        last_report={"web_services": [{"sites": ["a.example.test"]}]})
        session.add(server)
        await session.flush()
        check = Check(name="app", type="http", target="https://a.example.test",
                      enabled=True, probe_local=True, probe_server_id=server.id,
                      probe_bound_at=now, extra_paths=["/health"])
        session.add(check)
        await session.flush()
        cid = check.id
        session.add(AgentProbe(check_id=cid, server_id=server.id, ts=now, code=200))
        session.add(AgentPathProbe(check_id=cid, path="/health", server_id=server.id,
                                   ts=now, code=200))
        await session.commit()
    response = await client.patch(f"/api/checks/{cid}", headers=auth_headers,
                                  json={"target": "https://b.example.test"})
    assert response.status_code == 200, response.text
    await collector.rebind_local_probes(factory)
    await collector.rebind_local_probes(factory)
    async with factory() as session:
        assert (await session.get(Check, cid)).probe_server_id is None
        assert await session.get(AgentProbe, cid) is None
        assert await session.get(AgentPathProbe, (cid, "/health")) is None


async def test_bulk_leaving_local_probe_clears_agent_cache(client, auth_headers):
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    async with factory() as session:
        check = Check(name="app", type="http", target="https://a.example.test",
                      enabled=True, probe_local=True)
        session.add(check)
        await session.flush()
        cid = check.id
        session.add(AgentProbe(check_id=cid, server_id=1, ts=now, code=200))
        session.add(AgentPathProbe(check_id=cid, path="/health", server_id=1,
                                   ts=now, code=200))
        await session.commit()
    response = await client.patch("/api/checks/bulk", headers=auth_headers,
                                  json={"ids": [cid], "probe_local": False})
    assert response.status_code == 200, response.text
    async with factory() as session:
        assert (await session.get(Check, cid)).probe_local is False
        assert await session.get(AgentProbe, cid) is None
        assert await session.get(AgentPathProbe, (cid, "/health")) is None
