"""Учётка «наблюдатель» (viewer): вход есть, любые изменения запрещены."""

import httpx


async def _mk_viewer(
    client: httpx.AsyncClient, auth_headers, username="watcher", password="viewerpass-001"
) -> dict[str, str]:
    r = await client.post(
        "/api/users",
        json={"username": username, "password": password, "role": "viewer"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "viewer"
    r = await client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_viewer_can_read(client, auth_headers):
    vh = await _mk_viewer(client, auth_headers)
    # просмотр разрешён
    assert (await client.get("/api/checks/overview", headers=vh)).status_code == 200
    assert (await client.get("/api/servers", headers=vh)).status_code == 200
    me = await client.get("/api/auth/me", headers=vh)
    assert me.status_code == 200 and me.json()["role"] == "viewer"


async def test_viewer_cannot_write(client, auth_headers):
    vh = await _mk_viewer(client, auth_headers)
    # создание монитора — запрещено
    r = await client.post(
        "/api/checks",
        json={"name": "x", "type": "http", "target": "https://example.com"},
        headers=vh,
    )
    assert r.status_code == 403
    # создание другой учётки — запрещено
    r = await client.post(
        "/api/users",
        json={"username": "z", "password": "zzzzzzzz-zzzz", "role": "viewer"},
        headers=vh,
    )
    assert r.status_code == 403
    # список пользователей — только админ
    assert (await client.get("/api/users", headers=vh)).status_code == 403


async def test_viewer_can_change_own_password(client, auth_headers):
    vh = await _mk_viewer(client, auth_headers)
    # self-service своей учётки разрешён даже viewer'у
    r = await client.post(
        "/api/auth/password",
        json={"current_password": "viewerpass-001", "new_password": "viewerpass-002"},
        headers=vh,
    )
    assert r.status_code == 200


async def test_admin_still_writes(client, auth_headers):
    # у админа всё как было
    r = await client.post(
        "/api/checks",
        json={"name": "ok", "type": "http", "target": "https://example.com"},
        headers=auth_headers,
    )
    assert r.status_code == 201


async def test_users_crud(client, auth_headers):
    vh = await _mk_viewer(client, auth_headers)
    # список видит админ
    lst = await client.get("/api/users", headers=auth_headers)
    assert lst.status_code == 200
    users = lst.json()
    assert any(u["role"] == "viewer" for u in users)
    vid = next(u["id"] for u in users if u["role"] == "viewer")
    aid = next(u["id"] for u in users if u["role"] == "admin")

    # дубль логина → 409
    dup = await client.post(
        "/api/users",
        json={"username": "watcher", "password": "otherpass-001", "role": "viewer"},
        headers=auth_headers,
    )
    assert dup.status_code == 409

    # сброс пароля наблюдателя админом → старый токен наблюдателя недействителен
    r = await client.post(
        f"/api/users/{vid}/password",
        json={"new_password": "resetpass-001"},
        headers=auth_headers,
    )
    assert r.status_code == 204
    assert (await client.get("/api/auth/me", headers=vh)).status_code == 401

    # нельзя удалить последнего админа
    assert (
        await client.delete(f"/api/users/{aid}", headers=auth_headers)
    ).status_code == 400

    # удаление наблюдателя — ок
    assert (
        await client.delete(f"/api/users/{vid}", headers=auth_headers)
    ).status_code == 204


async def test_short_password_rejected(client: httpx.AsyncClient, auth_headers):
    """Пароль короче минимума не проходит НИ на одном маршруте.

    Правило живёт в трёх схемах сразу (создание учётки, сброс админом, смена
    своего пароля), и раньше оно было записано числом в каждой из них — стоит
    поправить одну и забыть про остальные, как «минимум» перестаёт быть общим.
    """
    from app.security import MIN_PASSWORD_LEN

    short = "a" * (MIN_PASSWORD_LEN - 1)
    ok = "b" * MIN_PASSWORD_LEN

    r = await client.post(
        "/api/users",
        json={"username": "shorty", "password": short, "role": "viewer"},
        headers=auth_headers,
    )
    assert r.status_code == 422

    r = await client.post(
        "/api/users",
        json={"username": "shorty", "password": ok, "role": "viewer"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]

    assert (
        await client.post(
            f"/api/users/{uid}/password", json={"new_password": short},
            headers=auth_headers,
        )
    ).status_code == 422

    assert (
        await client.post(
            "/api/auth/password",
            json={"current_password": "testpass-2026", "new_password": short},
            headers=auth_headers,
        )
    ).status_code == 422


async def test_first_admin_needs_a_long_password(tmp_path):
    """Стартовый пароль из .env проверить больше негде: через API этот
    аккаунт уже не заводится, а на первом старте он становится настоящим."""
    import pytest

    from app.bootstrap import ensure_admin
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.security import MIN_PASSWORD_LEN

    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{(tmp_path / 'first.db').as_posix()}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    strong = {"jwt_secret": "a" * 40}

    with pytest.raises(RuntimeError, match="KERVAX_ADMIN_PASSWORD"):
        await ensure_admin(
            factory,
            Settings(admin_user="admin", admin_password="a" * (MIN_PASSWORD_LEN - 1),
                     **strong),
        )
    # длинный проходит
    await ensure_admin(
        factory,
        Settings(admin_user="admin", admin_password="a" * MIN_PASSWORD_LEN, **strong),
    )
    await engine.dispose()
