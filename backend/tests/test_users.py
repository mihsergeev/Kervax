"""Учётка «наблюдатель» (viewer): вход есть, любые изменения запрещены."""

import asyncio

import httpx
import pytest


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


@pytest.mark.parametrize("second_action", ["demote", "delete"])
async def test_concurrent_user_changes_keep_an_admin(client, auth_headers, monkeypatch, second_action):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models import User

    users = await client.get("/api/users", headers=auth_headers)
    first_id = next(u["id"] for u in users.json() if u["username"] == "admin")
    second_user = {"username": "second-admin", "password": "fakepass-example"}
    created = await client.post("/api/users", headers=auth_headers, json={**second_user, "role": "admin"})
    assert created.status_code == 201, created.text
    second_id = created.json()["id"]
    login = await client.post("/api/auth/login", json=second_user)
    assert login.status_code == 200, login.text
    second_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    commit = AsyncSession.commit
    ready = asyncio.Event()
    resume = asyncio.Event()

    async def pause_demotion(session):
        if any(isinstance(u, User) and u.id == first_id and u.role == "editor" for u in session.dirty):
            ready.set()
            await asyncio.wait_for(resume.wait(), 5)
        await commit(session)

    monkeypatch.setattr(AsyncSession, "commit", pause_demotion)
    first = asyncio.create_task(client.patch(f"/api/users/{first_id}", headers=auth_headers, json={"role": "editor"}))
    second = None
    try:
        await asyncio.wait_for(ready.wait(), 5)
        url = f"/api/users/{second_id}"
        second = asyncio.create_task(
            client.patch(url, headers=second_headers, json={"role": "editor"}) if second_action == "demote"
            else client.delete(url, headers=auth_headers)
        )
        # The first change is staged. The second can only commit first if it bypasses the lock.
        await asyncio.wait({second}, timeout=0.5)
    finally:
        resume.set()
        first_result = await first
        if second is not None:
            second_result = await second
    assert first_result.status_code == 200, first_result.text
    assert second_result.status_code == (400 if second_action == "demote" else 403), second_result.text
    async with client._transport.app.state.session_factory() as session:
        assert list(await session.scalars(select(User.id).where(User.role == "admin"))) == [second_id]


async def test_concurrent_promotion_prevents_stale_account_deletion(client, auth_headers, monkeypatch):
    from app import auth_generations

    await _mk_viewer(client, auth_headers)
    users = await client.get("/api/users", headers=auth_headers)
    viewer_id = next(u["id"] for u in users.json() if u["role"] == "viewer")
    url = f"/api/users/{viewer_id}"
    retire = auth_generations.retire

    async def promote_first(session, user):
        result = await client.patch(url, headers=auth_headers, json={"role": "admin"})
        assert result.status_code == 200, result.text
        return await retire(session, user)

    monkeypatch.setattr(auth_generations, "retire", promote_first)
    result = await client.delete(url, headers=auth_headers)
    assert result.status_code == 404, result.text
    users = await client.get("/api/users", headers=auth_headers)
    assert any(u["id"] == viewer_id and u["role"] == "admin" for u in users.json())


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


async def test_users_window_sees_logins_failures_and_the_action_log(client, auth_headers):
    vh = await _mk_viewer(client, auth_headers)
    await client.post("/api/auth/login", json={"username": "watcher", "password": "wrong-pass-0001"})
    await client.post("/api/auth/login", json={"username": "nobody", "password": "wrong-pass-0001"})
    # просмотр не видит окно
    assert (await client.get("/api/users/overview", headers=vh)).status_code == 403

    r = await client.get("/api/users/overview", headers=auth_headers)
    assert r.status_code == 200, r.text
    ov = r.json()
    users = {u["username"]: u for u in ov["users"]}
    w = users["watcher"]
    assert w["role"] == "viewer" and w["last_login"] and w["last_ip"]
    assert w["fails_24h"] == 1 and w["tg_linked"] is False
    # создание учетки - действие админа, входы в счет действий не идут
    assert users["admin"]["actions_30d"] == 1 and w["actions_30d"] == 0
    # неудачные входы всего - и под несуществующим логином
    assert ov["fails_24h"] == 2 and ov["fail_ips_24h"] == 1
    acts = [a["action"] for a in ov["audit"]]
    assert acts[0] == "login_fail" and "user_create" in acts
    # время с поясом: на SQLite без него окно сдвигало вход на разницу с UTC
    assert w["last_login"].endswith(("Z", "+00:00")) and ov["audit"][0]["ts"].endswith(("Z", "+00:00"))
