"""Границы групп, разделов и срока жизни входа под чужой учеткой."""

from datetime import datetime, timedelta, timezone

import pytest

from app import checks, collector, inside_probe
from app.models import AgentProbe, BackupSetupJob, Check, ProbeRequest, Server
from tests.test_manual_run import REPORT, _local_setup


PW = "fakepass-example"


async def _user(client, headers, name, role="editor", **scope):
    r = await client.post("/api/users", headers=headers, json={
        "username": name, "password": PW, "role": role, **scope,
    })
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = await client.post("/api/auth/login", json={"username": name, "password": PW})
    assert r.status_code == 200, r.text
    return uid, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _objects(client):
    async with client._transport.app.state.session_factory() as s:
        a = Server(name="web-a", token_hash="fake-token-a", group_name="web")
        b = Server(name="db-b", token_hash="fake-token-b", group_name="infra")
        c = Check(name="site-a", type="http", target="https://example.com", group_name="web")
        s.add_all([a, b, c])
        await s.commit()
        job = BackupSetupJob(server_id=b.id, backup_server_id=b.id, status="error",
                             steps=[{"step": "prepare", "ok": False, "detail": "private job output"}],
                             message="private job output")
        s.add(job)
        await s.commit()
        return a.id, b.id, c.id, job.id


async def test_backup_setup_status_enforces_server_group(client, auth_headers):
    a, b, _, jid = await _objects(client)
    _, editor = await _user(client, auth_headers, "web-editor", server_groups=["web"])
    _, viewer = await _user(client, auth_headers, "web-viewer", role="viewer", server_groups=["web"])
    for headers in (editor, viewer):
        r = await client.get(f"/api/servers/{b}/backup/setup/{jid}", headers=headers)
        assert r.status_code == 404, r.text
        assert "private job output" not in r.text
    assert (await client.get(f"/api/servers/{a}/backup/setup/{jid}", headers=editor)).status_code == 404
    r = await client.get(f"/api/servers/{b}/backup/setup/{jid}", headers=auth_headers)
    assert r.status_code == 200 and r.json()["message"] == "private job output"


async def test_backup_setup_output_obeys_log_permission(client, auth_headers):
    _, b, _, jid = await _objects(client)
    _, viewer = await _user(client, auth_headers, "infra-viewer", role="viewer", server_groups=["infra"])
    _, reader = await _user(client, auth_headers, "infra-reader", role="viewer", server_groups=["infra"],
                            can_read_logs=True)
    _, alien = await _user(client, auth_headers, "web-reader", role="viewer", server_groups=["web"],
                           can_read_logs=True)
    _, editor = await _user(client, auth_headers, "infra-editor", server_groups=["infra"])
    path = f"/api/servers/{b}/backup/setup/{jid}"
    response = await client.get(path, headers=viewer)
    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["steps"] == [{"step": "prepare", "ok": False, "detail": ""}]
    assert response.json()["message"] == ""
    for headers in (reader, editor, auth_headers):
        response = await client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.json()["steps"][0]["detail"] == "private job output"
        assert response.json()["message"] == "private job output"
    assert (await client.get(path, headers=alien)).status_code == 404


def _window(**objects):
    now = datetime.now(timezone.utc)
    return {"title": "maintenance", "starts_at": now.isoformat(),
            "ends_at": (now + timedelta(hours=1)).isoformat(), **objects}


@pytest.mark.parametrize("section,hidden_kind", [("sites", "server_ids"), ("servers", "check_ids")])
async def test_maintenance_cannot_change_objects_from_closed_section(client, auth_headers, section, hidden_kind):
    a, _, cid, _ = await _objects(client)
    _, editor = await _user(client, auth_headers, "scoped-editor", sections=[section])
    hidden = [a] if hidden_kind == "server_ids" else [cid]
    body = _window(**{hidden_kind: hidden})
    r = await client.post("/api/maintenance", headers=auth_headers, json=body)
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    assert (await client.get("/api/maintenance", headers=editor)).json() == []
    assert (await client.post("/api/maintenance", headers=editor, json=body)).status_code == 400
    assert (await client.put(f"/api/maintenance/{wid}", headers=editor, json=body)).status_code == 404
    assert (await client.delete(f"/api/maintenance/{wid}", headers=editor)).status_code == 404
    # Свой раздел остается доступным.
    visible = {"check_ids": [cid]} if section == "sites" else {"server_ids": [a]}
    assert (await client.post("/api/maintenance", headers=editor, json=_window(**visible))).status_code == 201


@pytest.mark.parametrize("deleted", ["target", "admin"])
async def test_impersonation_not_reused_after_account_recreation(client, auth_headers, deleted):
    target_id, _ = await _user(client, auth_headers, "target-viewer", role="viewer")
    admin_id, admin = await _user(client, auth_headers, "second-admin", role="admin")
    r = await client.post(f"/api/users/{target_id}/impersonate", headers=admin)
    assert r.status_code == 200, r.text
    imp = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.get("/api/auth/me", headers=imp)).status_code == 200
    # SQLite повторяет последний удаленный id, в PostgreSQL sequence идет вперед.
    await _user(client, auth_headers, "filler", role="viewer")
    uid, name, role = ((target_id, "target-viewer", "viewer") if deleted == "target"
                       else (admin_id, "second-admin", "admin"))
    assert (await client.delete(f"/api/users/{uid}", headers=auth_headers)).status_code == 204
    assert (await client.get("/api/auth/me", headers=imp)).status_code == 401
    new_id, _ = await _user(client, auth_headers, name, role=role)
    assert new_id != uid
    assert (await client.get("/api/auth/me", headers=imp)).status_code == 401


async def test_viewer_cannot_read_or_search_monitor_url_secrets(client, auth_headers):
    r = await client.post("/api/checks", headers=auth_headers, json={
        "name": "private-site", "type": "http",
        "target": "https://user:fakepass@example.com/?token=fake-token-example&to%6ben=fake-encoded-token",
    })
    assert r.status_code == 201, r.text
    _, viewer = await _user(client, auth_headers, "site-viewer", role="viewer")
    for path in ("/api/depmap", "/api/search?q=private-site", f"/api/checks/{r.json()['id']}"):
        response = await client.get(path, headers=viewer)
        assert response.status_code == 200, response.text
        assert "fakepass" not in response.text, path
        assert "fake-token-example" not in response.text, path
        assert "fake-encoded-token" not in response.text, path
    # Даже поиск точного значения не должен подтверждать наличие секрета.
    for secret in ("fake-token-example", "fake-encoded-token"):
        response = await client.get("/api/search", params={"q": secret}, headers=viewer)
        assert response.json()["groups"] == []
    # Адрес в настройках не переписывается маскированным значением.
    assert "fake-token-example" in (await client.get(f"/api/checks/{r.json()['id']}", headers=auth_headers)).text


async def test_editor_cannot_get_location_credentials_from_agent_install(client, auth_headers):
    a, b, _, _ = await _objects(client)
    _, editor = await _user(client, auth_headers, "proxy-editor", server_groups=["web"])
    _, viewer = await _user(client, auth_headers, "proxy-viewer", role="viewer", server_groups=["web"])
    for n, proxy in enumerate(("http://probe:fake-proxy-password@proxy.example:3128", "http://proxy.example:3128")):
        loc = await client.post("/api/locations", headers=auth_headers, json={"name": f"location-{n}", "url": proxy})
        assert loc.status_code == 201, loc.text
        body = {"mode": "location", "location_id": loc.json()["id"]}
        path = f"/api/servers/{a}/install-via"
        assert (await client.post(path, headers=auth_headers, json=body)).json()["proxy"] == proxy
        assert (await client.post(path, headers=viewer, json=body)).status_code == 403
        assert (await client.post(f"/api/servers/{b}/install-via", headers=editor, json=body)).status_code == 404
        response = await client.post(path, headers=editor, json=body)
        if "@" in proxy:
            assert response.status_code == 403
            assert "fake-proxy-password" not in response.text
        else:
            assert response.status_code == 200 and response.json()["proxy"] == proxy


async def test_old_manual_probe_does_not_send_new_monitor_settings(client, auth_headers, monkeypatch):
    cid, _, agent = await _local_setup(client, auth_headers, monkeypatch, version="2.7")
    response = await client.post(f"/api/checks/{cid}/run", headers=auth_headers)
    assert response.json()["run_pending"]
    response = await client.patch(f"/api/checks/{cid}", headers=auth_headers, json={
        "target": "https://new.example", "probe_local": False,
        "auth_method": "basic", "auth_user": "probe", "auth_pass": "fake-new-password",
    })
    assert response.status_code == 200, response.text
    for response in (await client.get("/api/agent/commands", headers=agent),
                     await client.post("/api/agent/report", headers=agent, json=REPORT)):
        assert response.status_code == 200, response.text
        assert response.json()["site_probes"] == []


async def test_manual_probe_requires_access_to_the_assigned_server(client, auth_headers, monkeypatch):
    cid, sid, _ = await _local_setup(client, auth_headers, monkeypatch)
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        (await session.get(Server, sid)).group_name = "infra"
        (await session.get(Check, cid)).group_name = "web"
        await session.commit()
    _, editor = await _user(client, auth_headers, "sites-editor", site_groups=["web"], server_groups=["web"])
    path = f"/api/checks/{cid}/run"
    assert (await client.post(path, headers=editor)).status_code == 404
    response = await client.post(path, headers=auth_headers)
    assert response.status_code == 200 and response.json()["run_pending"], response.text
    poll = f"{path}/{response.json()['run_pending']}"
    assert (await client.get(poll, headers=editor)).status_code == 404
    assert (await client.get(poll, headers=auth_headers)).status_code == 200


async def test_viewer_does_not_get_probe_url_from_agent_error(client, auth_headers, monkeypatch):
    cid, sid, agent = await _local_setup(client, auth_headers, monkeypatch, version="2.7")
    factory = client._transport.app.state.session_factory
    target = "https://closed.example/health?token=fake-agent-error-token"
    async with factory() as session:
        check = await session.get(Check, cid)
        check.target = target
        check.parent_mode = "server"
        check.parent_server_id = sid
        await session.commit()
    _, viewer = await _user(client, auth_headers, "probe-viewer", role="viewer")
    path = f"/api/checks/{cid}"
    response = await client.post(f"{path}/run", headers=auth_headers)
    rid = response.json()["run_pending"]
    assert rid
    response = await client.post("/api/agent/site-probe-result", headers=agent, json={"site_probes": [
        {"id": -rid, "error": f'Get "{target}": EOF'},
    ]})
    assert response.status_code == 200, response.text
    async with factory() as session:
        request = await session.get(ProbeRequest, rid)
        assert "fake-agent-error-token" not in request.error
        # Results stored before the fix must be masked on read as well.
        request.message = f'Get "{target}": EOF'
        (await session.get(Check, cid)).last_message = request.message
        await session.commit()
    for url, field in ((path, "last_message"), (f"{path}/run/{rid}", "run_message")):
        response = await client.get(url, headers=viewer)
        assert response.status_code == 200, response.text
        assert "EOF" in response.json()[field]
        assert "fake-agent-error-token" not in response.text


@pytest.mark.parametrize("probe_local", [True, False])
async def test_agent_inventory_cannot_claim_authenticated_monitor(client, auth_headers, monkeypatch, probe_local):
    fleet = []
    for name in ("trusted-node", "claiming-node"):
        response = await client.post("/api/servers", headers=auth_headers, json={"name": name, "group_name": name})
        node = response.json()
        fleet.append((node["server"]["id"], {"Authorization": f"Bearer {node['token']}"}))
    (trusted_id, trusted), (claiming_id, claiming) = fleet
    report = dict(REPORT, agent_version="2.7", web_services=[{"kind": "nginx", "sites": ["private.example"]}])
    assert (await client.post("/api/agent/report", headers=claiming, json=report)).status_code == 200

    async def panel_probe(check):
        return checks.CheckOutcome("up", message="HTTP 200")

    monkeypatch.setattr(checks, "run_check", panel_probe)
    response = await client.post("/api/checks", headers=auth_headers, json={
        "name": "private-monitor", "type": "http", "target": "https://private.example",
        "probe_local": probe_local, "http_headers": 'Authorization: Bearer fake-monitor-token',
        "auth_method": "basic", "auth_user": "probe", "auth_pass": "fake-monitor-password",
    })
    assert response.status_code == 201, response.text
    cid = response.json()["id"]
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        check = await session.get(Check, cid)
        if probe_local:
            # A binding from an older version must be denied before the scheduler has run.
            check.probe_server_id = claiming_id
        else:
            await inside_probe.request(session, check, datetime.now(timezone.utc))
        await session.commit()
    response = await client.post("/api/agent/report", headers=claiming, json={
        **report, "site_probes": [{"id": cid, "code": 200, "latency_ms": 2}],
    })
    assert response.status_code == 200 and response.json()["site_probes"] == [], response.text
    async with factory() as session:
        assert await session.get(AgentProbe, cid) is None
    await collector.rebind_local_probes(factory)
    for response in (await client.get("/api/agent/commands", headers=claiming),
                     await client.post("/api/agent/report", headers=claiming, json=report)):
        assert response.status_code == 200, response.text
        assert response.json()["site_probes"] == []
        assert "fake-monitor-password" not in response.text
        assert "fake-monitor-token" not in response.text

    if probe_local:
        response = await client.post(f"/api/checks/{cid}/run", headers=auth_headers)
        assert response.status_code == 200 and response.json()["run_source"] == "panel", response.text
        assert response.json()["run_status"] == "up"
        # Existing explicit assignment is the trust boundary, even when another node also claims the host.
        response = await client.patch(f"/api/checks/{cid}", headers=auth_headers,
                                      json={"parent_mode": "server", "parent_server_id": trusted_id})
        assert response.status_code == 200, response.text
        assert (await client.post("/api/agent/report", headers=trusted, json=report)).status_code == 200
        await collector.rebind_local_probes(factory)
        response = await client.post("/api/agent/report", headers=trusted, json=report)
        assert [task["id"] for task in response.json()["site_probes"]] == [cid]
        assert response.json()["site_probes"][0]["auth_pass"] == "fake-monitor-password"
