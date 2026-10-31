"""Границы групп, разделов и срока жизни входа под чужой учеткой."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import BackupSetupJob, Check, Server


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
