"""Удаленная учетная запись не возвращается вместе с повторно использованным SQLite id."""

from app import auth_generations, backup, config
from app.bootstrap import ensure_admin
from app.models import User
from app.security import create_access_token


PW = "fakepass-example"


async def _create(client, admin, name="reused-viewer", role="viewer", password=PW):
    r = await client.post("/api/users", headers=admin, json={
        "username": name, "password": password, "role": role,
    })
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
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
