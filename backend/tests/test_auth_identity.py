"""Удаленная учетная запись не возвращается вместе с повторно использованным SQLite id."""

import asyncio

import pytest

from app import auth_generations, backup, config
from app.bootstrap import ensure_admin
from app.models import User
from app.security import create_access_token


PW = "fakepass-example"


async def _create_user(client, admin, name, role, password):
    r = await client.post("/api/users", headers=admin, json={
        "username": name, "password": password, "role": role,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create(client, admin, name="reused-viewer", role="viewer", password=PW):
    uid = await _create_user(client, admin, name, role, password)
    r = await client.post("/api/auth/login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    return uid, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _factory(client):
    return client._transport.app.state.session_factory


def _assert_sqlite_reuses_id(client, new_id, old_id):
    if client._transport.app.state.engine.dialect.name == "sqlite":
        assert new_id == old_id


async def test_deleted_last_account_token_cannot_authenticate_recreated_admin(client, auth_headers):
    body = {"username": "reused-viewer", "password": "fakepass-example", "role": "viewer"}
    response = await client.post("/api/users", headers=auth_headers, json=body)
    assert response.status_code == 201, response.text
    old_id = response.json()["id"]
    response = await client.post("/api/auth/login", json={"username": body["username"], "password": body["password"]})
    assert response.status_code == 200
    stale = {"Authorization": f"Bearer {response.json()['access_token']}"}
    assert (await client.get("/api/auth/me", headers=stale)).json()["role"] == "viewer"
    assert (await client.delete(f"/api/users/{old_id}", headers=auth_headers)).status_code == 204
    assert (await client.get("/api/auth/me", headers=stale)).status_code == 401
    # Без filler SQLite присваивает последней удаленной учетке тот же id.
    response = await client.post("/api/users", headers=auth_headers, json={**body, "role": "admin"})
    assert response.status_code == 201, response.text
    _assert_sqlite_reuses_id(client, response.json()["id"], old_id)
    response = await client.get("/api/auth/me", headers=stale)
    assert response.status_code == 401, response.text


async def test_revocation_survives_repeated_recreation_with_same_password(client, auth_headers):
    stale = []
    last_id = None
    for role in ("viewer", "editor", "admin", "viewer"):
        uid, current = await _create(client, auth_headers, role=role)
        if last_id is not None:
            _assert_sqlite_reuses_id(client, uid, last_id)  # без filler-учеток
        for headers in stale:
            assert (await client.get("/api/auth/me", headers=headers)).status_code == 401
        assert (await client.get("/api/auth/me", headers=current)).json()["role"] == role
        stale.append(current)
        assert (await client.delete(f"/api/users/{uid}", headers=auth_headers)).status_code == 204
        last_id = uid


async def test_delete_seed_includes_password_change_and_logout_versions(client, auth_headers):
    uid, before_password = await _create(client, auth_headers)
    changed = await client.post("/api/auth/password", headers=before_password, json={
        "current_password": PW, "new_password": "fakepass-another",
    })
    assert changed.status_code == 200, changed.text
    after_password = {"Authorization": f"Bearer {changed.json()['access_token']}"}
    assert (await client.post("/api/auth/logout", headers=after_password)).status_code == 204
    login = await client.post("/api/auth/login", json={"username": "reused-viewer", "password": "fakepass-another"})
    after_logout = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.delete(f"/api/users/{uid}", headers=auth_headers)).status_code == 204
    new_id, current = await _create(client, auth_headers, role="admin")
    _assert_sqlite_reuses_id(client, new_id, uid)
    for old in (before_password, after_password, after_logout):
        assert (await client.get("/api/auth/me", headers=old)).status_code == 401
    assert (await client.get("/api/auth/me", headers=current)).json()["role"] == "admin"


async def test_existing_legacy_login_is_kept_but_bootstrap_recreation_uses_seed(client, auth_headers):
    settings = config.get_settings()
    # Токен старого формата без uid остается действительным для той же живой учетки.
    legacy = {"Authorization": "Bearer " + create_access_token("admin", settings.jwt_secret, 60)}
    admin = (await client.get("/api/auth/me", headers=legacy)).json()
    assert admin["role"] == "admin"
    _, other_admin = await _create(client, auth_headers, name="spare-admin", role="admin")
    assert (await client.delete(f"/api/users/{admin['id']}", headers=other_admin)).status_code == 204
    await ensure_admin(_factory(client), settings)
    assert (await client.get("/api/auth/me", headers=legacy)).status_code == 401
    response = await client.post("/api/auth/login", json={"username": "admin", "password": settings.admin_password})
    assert response.status_code == 200
    current = {"Authorization": f"Bearer {response.json()['access_token']}"}
    assert (await client.get("/api/auth/me", headers=current)).json()["role"] == "admin"


async def test_old_config_restore_cannot_roll_back_revocation_or_restore_users(client, auth_headers):
    factory = _factory(client)
    settings = config.get_settings()
    async with factory() as session:
        before_delete = await backup.export_data(session)
    assert "users" not in before_delete["tables"]
    uid, first = await _create(client, auth_headers)
    assert (await client.delete(f"/api/users/{uid}", headers=auth_headers)).status_code == 204
    async with factory() as session:
        await backup.import_data(session, before_delete, settings)
        assert await auth_generations.initial_version(session, "reused-viewer") == 1
        old_seed = await backup.export_data(session)
    second_id, second = await _create(client, auth_headers, role="admin")
    _assert_sqlite_reuses_id(client, second_id, uid)
    assert (await client.get("/api/auth/me", headers=first)).status_code == 401
    # Конфигурационный backup не восстанавливает учетные записи, даже если ключ есть во входе.
    old_seed["tables"]["users"] = [{"id": uid, "username": "reused-viewer", "role": "viewer", "token_version": 0}]
    async with factory() as session:
        await backup.import_data(session, old_seed, settings)
    assert (await client.get("/api/auth/me", headers=second)).json()["role"] == "admin"
    assert (await client.delete(f"/api/users/{second_id}", headers=auth_headers)).status_code == 204
    async with factory() as session:
        await backup.import_data(session, old_seed, settings)
        assert await auth_generations.initial_version(session, "reused-viewer") == 2
    _, third = await _create(client, auth_headers, role="admin")
    assert (await client.get("/api/auth/me", headers=first)).status_code == 401
    assert (await client.get("/api/auth/me", headers=second)).status_code == 401
    assert (await client.get("/api/auth/me", headers=third)).status_code == 200


async def test_delete_reads_current_database_version_not_stale_orm_snapshot(client, auth_headers):
    uid, first = await _create(client, auth_headers)
    factory = _factory(client)
    async with factory() as deleting:
        stale = await deleting.get(User, uid)
        assert stale.token_version == 0
        # Между чтением DELETE-запроса и его записью успел завершиться password/logout.
        async with factory() as changed:
            user = await changed.get(User, uid)
            user.token_version = 4
            await changed.commit()
        login = await client.post("/api/auth/login", json={"username": "reused-viewer", "password": PW})
        latest = {"Authorization": f"Bearer {login.json()['access_token']}"}
        await auth_generations.retire(deleting, stale)
        await deleting.delete(stale)
        await deleting.commit()
    async with factory() as session:
        assert await auth_generations.initial_version(session, "reused-viewer") == 5
    _, current = await _create(client, auth_headers, role="admin")
    for old in (first, latest):
        assert (await client.get("/api/auth/me", headers=old)).status_code == 401
    assert (await client.get("/api/auth/me", headers=current)).status_code == 200


async def test_revocation_merge_never_decreases_known_version(client):
    async with _factory(client)() as session:
        key = auth_generations._key("reused-viewer")
        await auth_generations.merge_revocation(session, key, "5")
        await auth_generations.merge_revocation(session, key, "9")
        await auth_generations.merge_revocation(session, key, "3")
        await session.commit()
        assert await auth_generations.initial_version(session, "reused-viewer") == 9


async def _overlapping_delete_recreate(client, auth_headers, monkeypatch):
    old_id, _ = await _create(client, auth_headers, name="old-viewer")
    entered = asyncio.Event()
    proceed = asyncio.Event()
    original = auth_generations.retire
    paused = False

    async def delayed(session, user):
        nonlocal paused
        if user.username == "old-viewer" and not paused:
            paused = True
            entered.set()
            await proceed.wait()
        return await original(session, user)

    monkeypatch.setattr(auth_generations, "retire", delayed)
    pending = asyncio.create_task(client.delete(f"/api/users/{old_id}", headers=auth_headers))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert (await client.delete(f"/api/users/{old_id}", headers=auth_headers)).status_code == 204
        new_id, new_headers = await _create(client, auth_headers, name="new-viewer")
        _assert_sqlite_reuses_id(client, new_id, old_id)
        proceed.set()
        response = await asyncio.wait_for(pending, 10)
        return response, new_id, new_headers
    finally:
        proceed.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_delayed_delete_cannot_remove_different_account_with_reused_id(client, auth_headers, monkeypatch):
    response, _, headers = await _overlapping_delete_recreate(client, auth_headers, monkeypatch)
    assert response.status_code == 404, response.text
    current = await client.get("/api/auth/me", headers=headers)
    assert current.status_code == 200 and current.json()["username"] == "new-viewer"


async def test_overlapping_delete_cannot_lose_other_accounts_revocation(client, auth_headers, monkeypatch):
    _, new_id, stale = await _overlapping_delete_recreate(client, auth_headers, monkeypatch)
    # Если новый пользователь сохранился, удаляем его штатно. Старый баг удалял его
    # от имени old-viewer и не оставлял seed для new-viewer.
    current = await client.get("/api/auth/me", headers=stale)
    if current.status_code == 200:
        assert (await client.delete(f"/api/users/{new_id}", headers=auth_headers)).status_code == 204
    await _create(client, auth_headers, name="new-viewer", role="admin")
    response = await client.get("/api/auth/me", headers=stale)
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("replacement_name", ["reused-viewer", "replacement-admin"])
async def test_password_change_cannot_take_over_recreated_account(client, auth_headers, monkeypatch, replacement_name):
    from app.api import auth

    uid, stale = await _create(client, auth_headers)
    original = auth._guarded
    replaced = False

    async def replace_after_check(session, request, username, action, ok, error):
        nonlocal replaced
        await original(session, request, username, action, ok, error)
        if action == "password_fail" and not replaced:
            replaced = True
            assert (await client.delete(f"/api/users/{uid}", headers=auth_headers)).status_code == 204
            # Verify the replacement login after the in-flight password check releases its source.
            new_id = await _create_user(client, auth_headers, replacement_name, "admin", "fakepass-owner")
            _assert_sqlite_reuses_id(client, new_id, uid)

    monkeypatch.setattr(auth, "_guarded", replace_after_check)
    response = await client.post("/api/auth/password", headers=stale, json={
        "current_password": PW, "new_password": "fakepass-attacker",
    })
    stolen = await client.post("/api/auth/login", json={
        "username": replacement_name, "password": "fakepass-attacker",
    })
    assert stolen.status_code == 401, f"stale change={response.status_code}, replacement login={stolen.status_code}"
    assert response.status_code == 401, response.text
    owner = await client.post("/api/auth/login", json={"username": replacement_name, "password": "fakepass-owner"})
    assert owner.status_code == 200


async def test_password_change_cannot_reissue_token_revoked_by_concurrent_logout(client, auth_headers, monkeypatch):
    from app.api import auth

    _, stale = await _create(client, auth_headers)
    original = auth._guarded

    async def logout_after_check(session, request, username, action, ok, error):
        await original(session, request, username, action, ok, error)
        if action == "password_fail":
            assert (await client.post("/api/auth/logout", headers=stale)).status_code == 204

    monkeypatch.setattr(auth, "_guarded", logout_after_check)
    response = await client.post("/api/auth/password", headers=stale, json={
        "current_password": PW, "new_password": "fakepass-attacker",
    })
    assert response.status_code == 401
    assert (await client.get("/api/auth/me", headers=stale)).status_code == 401
    owner = await client.post("/api/auth/login", json={"username": "reused-viewer", "password": PW})
    assert owner.status_code == 200
