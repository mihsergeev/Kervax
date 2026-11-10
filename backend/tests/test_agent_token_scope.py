from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app import collector, manual_probe
from app.models import AgentPathProbe, AgentProbe, Check, ProbeRequest, Server


REPORT = {"agent_version": "2.9", "web_services": [{"kind": "nginx", "sites": ["site.example.com"]}]}
PROTECTED = {"type": "http", "target": "https://site.example.com", "probe_local": True,
             "http_headers": '{"X-Check": "fixture-value"}', "extra_paths": ["/health"],
             "check_ssl": False, "check_domain": False}


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def scoped_user(client, admin, role, *, sections=None):
    username = f"scoped-{role}"
    password = "fixture-password-001"
    r = await client.post("/api/users", headers=admin, json={
        "username": username, "password": password, "role": role,
        "server_groups": ["shared"], "site_groups": ["visible"], "sections": sections or [],
    })
    assert r.status_code == 201
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return bearer(r.json()["access_token"])


async def enroll(client, headers):
    r = await client.post("/api/servers", headers=headers, json={"name": "test-node", "group_name": "shared"})
    assert r.status_code == 201
    return r.json()["server"]["id"], bearer(r.json()["token"])


async def protected_site(client, headers, sid, group, **overrides):
    return await client.post("/api/checks", headers=headers, json={
        **PROTECTED, "name": f"{group} site", "group_name": group,
        "parent_mode": "server", "parent_server_id": sid, **overrides,
    })


@pytest.mark.parametrize("role,sections", [("editor", []), ("admin", []), ("admin", ["servers"])])
async def test_scoped_user_cannot_rotate_a_trusted_token(client, auth_headers, role, sections):
    user = await scoped_user(client, auth_headers, role, sections=sections)
    sid, agent = await enroll(client, auth_headers)
    # A privileged (and, on upgrade, legacy) agent may already have an authorized assignment
    # even though it has not yet reported. The scoped user must not replace its credential.
    r = await protected_site(client, auth_headers, sid, "hidden")
    assert r.status_code == 201
    assert (await client.get(f"/api/checks/{r.json()['id']}", headers=user)).status_code in (403, 404)
    path = f"/api/servers/{sid}/rotate"
    assert (await client.post(path, headers=user)).status_code == 403
    assert (await client.post("/api/agent/report", headers=agent, json=REPORT)).status_code == 200
    factory = client._transport.app.state.session_factory
    await collector.rebind_local_probes(factory)
    r = await client.post("/api/agent/report", headers=agent, json=REPORT)
    assert len(r.json()["site_probes"]) == 2
    assert (await client.post(path, headers=user)).status_code == 403
    r = await client.post(path, headers=auth_headers)
    assert r.status_code == 200
    assert (await client.post("/api/agent/report", headers=agent, json=REPORT)).status_code == 401
    r = await client.post("/api/agent/report", headers=bearer(r.json()["token"]), json=REPORT)
    assert len(r.json()["site_probes"]) == 2


@pytest.mark.parametrize("sections,permitted", [([], True), (["servers"], False)])
async def test_first_token_never_gains_hidden_site_access(client, auth_headers, sections, permitted):
    editor = await scoped_user(client, auth_headers, "editor", sections=sections)
    sid, old_agent = await enroll(client, editor)
    path = f"/api/servers/{sid}/rotate"
    r = await client.post(path, headers=editor)
    assert r.status_code == 200 and "install.sh" in r.json()["install_cmd"]
    agent = bearer(r.json()["token"])
    assert (await client.post("/api/agent/report", headers=old_agent, json=REPORT)).status_code == 401
    assert (await protected_site(client, auth_headers, sid, "hidden")).status_code == 400
    visible = await protected_site(client, auth_headers, sid, "visible")
    assert visible.status_code == (201 if permitted else 400)
    if permitted:
        r = await client.post("/api/checks/reorder", headers=auth_headers, json={
            "order": [{"id": visible.json()["id"], "group_name": "hidden"}],
        })
        assert r.status_code == 400
    assert (await client.post("/api/agent/report", headers=agent, json=REPORT)).status_code == 200
    assert (await client.post(path, headers=editor)).status_code == 403
    public = await protected_site(client, auth_headers, sid, "public", http_headers="", extra_paths=[])
    assert public.status_code == 201

    # The task delivery must enforce the boundary even with stale/restored bindings and queued jobs.
    r = await protected_site(client, auth_headers, sid, "hidden", probe_local=False, enabled=False)
    assert r.status_code == 201
    hidden_id = r.json()["id"]
    r = await client.patch("/api/checks/bulk", headers=auth_headers, json={
        "ids": [hidden_id], "probe_local": True,
    })
    assert r.status_code == 400
    r = await client.post("/api/checks/try", headers=auth_headers, json={
        "type": "http", "target": PROTECTED["target"], "part": "inside", "check_id": hidden_id,
        "http_headers": PROTECTED["http_headers"],
    })
    assert r.status_code == 200 and r.json()["inside"]["kind"] == "no_trusted_server"
    factory = client._transport.app.state.session_factory
    await collector.rebind_local_probes(factory)
    async with factory() as session:
        hidden = await session.get(Check, hidden_id)
        hidden.probe_local = hidden.enabled = True
        hidden.probe_server_id = sid
        now = datetime.now(timezone.utc)
        srv = await session.get(Server, sid)
        srv.probe_pending_at = now
        manual = ProbeRequest(check_id=hidden_id, server_id=sid, created_at=now)
        draft = ProbeRequest(check_id=0, server_id=sid, created_at=now, url=hidden.target,
                             spec={"trusted_check_id": hidden_id, "http_headers": hidden.http_headers})
        session.add_all([manual, draft])
        await session.commit()
        hidden_tasks = [hidden_id, *manual_probe.path_task_ids(hidden).values(), -manual.id, -draft.id]

    r = await client.get("/api/agent/commands", headers=agent)
    assert r.status_code == 200 and r.json()["site_probes"] == []
    r = await client.post("/api/agent/report", headers=agent, json={
        **REPORT, "site_probes": [{"id": i, "code": 200} for i in hidden_tasks],
    })
    assert r.status_code == 200
    tasks = r.json()["site_probes"]
    assert all(t["id"] not in hidden_tasks for t in tasks)
    assert any(t["id"] == public.json()["id"] for t in tasks)
    assert len(tasks) == (3 if permitted else 1)
    async with factory() as session:
        assert await session.scalar(select(AgentProbe).where(AgentProbe.check_id == hidden_id)) is None
        assert await session.scalar(select(AgentPathProbe).where(AgentPathProbe.check_id == hidden_id)) is None
        for rid in hidden_tasks[-2:]:
            assert (await session.get(ProbeRequest, -rid)).ts is None
