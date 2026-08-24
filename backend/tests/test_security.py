"""Тесты фиксов безопасности аутентификации (перенос из панели-референса)."""

import time

import httpx
import pytest

from app import totp


async def test_password_change_invalidates_old_tokens(client: httpx.AsyncClient):
    r = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "testpass-2026"}
    )
    old = {"Authorization": f"Bearer {r.json()['access_token']}"}
    assert (await client.get("/api/auth/me", headers=old)).status_code == 200

    r = await client.post(
        "/api/auth/password",
        json={"current_password": "testpass-2026", "new_password": "newstrongpass1"},
        headers=old,
    )
    assert r.status_code == 200
    new = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # старый токен больше не действителен, новый — работает
    assert (await client.get("/api/auth/me", headers=old)).status_code == 401
    assert (await client.get("/api/auth/me", headers=new)).status_code == 200
    # логин по новому паролю проходит
    r2 = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "newstrongpass1"}
    )
    assert r2.status_code == 200


async def test_password_change_wrong_current(client: httpx.AsyncClient, auth_headers):
    r = await client.post(
        "/api/auth/password",
        json={"current_password": "WRONG", "new_password": "whatever-1234"},
        headers=auth_headers,
    )
    assert r.status_code == 400


async def test_totp_code_not_replayable(client: httpx.AsyncClient, auth_headers):
    setup = (await client.post("/api/auth/2fa/setup", headers=auth_headers)).json()
    secret = setup["secret"]
    code = totp._hotp(secret, int(time.time() // 30))
    assert (
        await client.post(
            "/api/auth/2fa/enable", json={"otp": code}, headers=auth_headers
        )
    ).status_code == 200

    # первый вход с кодом — успех
    r1 = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "testpass-2026", "otp": code},
    )
    assert r1.status_code == 200
    # тот же код повторно — отклонён (replay)
    r2 = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "testpass-2026", "otp": code},
    )
    assert r2.status_code == 401
    assert r2.json()["detail"] == "2fa_invalid"


async def test_login_rate_limited_after_failures(client: httpx.AsyncClient):
    from app import ratelimit

    for _ in range(ratelimit.MAX_FAILURES):
        r = await client.post(
            "/api/auth/login", json={"username": "admin", "password": "bad"}
        )
        assert r.status_code == 401
    # следующая попытка блокируется независимо от правильности пароля
    r = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "testpass-2026"}
    )
    assert r.status_code == 429


async def test_login_events_audited(client: httpx.AsyncClient, auth_headers):
    # auth_headers уже сделал успешный вход (login_ok); добавим неудачный
    await client.post(
        "/api/auth/login", json={"username": "admin", "password": "bad"}
    )
    r = await client.get("/api/audit?limit=50", headers=auth_headers)
    actions = {e["action"] for e in r.json()}
    assert "login_ok" in actions
    assert "login_fail" in actions


async def test_break_glass_reset(tmp_path):
    from sqlalchemy import select

    from app.bootstrap import ensure_admin
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import User
    from app.security import verify_password

    db = (tmp_path / "bg.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    strong = {"jwt_secret": "a" * 40}
    await ensure_admin(
        factory, Settings(admin_user="admin", admin_password="oldpass-12345", **strong)
    )
    async with factory() as s:
        u = await s.scalar(select(User).where(User.username == "admin"))
        u.totp_enabled = True
        u.totp_secret = "SECRET"
        u.token_version = 3
        await s.commit()

    # без флага — пароль не трогается
    await ensure_admin(
        factory, Settings(admin_user="admin", admin_password="other-9999999", **strong)
    )
    async with factory() as s:
        u = await s.scalar(select(User).where(User.username == "admin"))
        assert verify_password("oldpass-12345", u.password_hash)  # не сброшен

    # с флагом — сброс пароля + отключение 2FA + инвалидация токенов
    await ensure_admin(
        factory,
        Settings(
            admin_user="admin", admin_password="newpass-45678",
            admin_password_reset=True, **strong,
        ),
    )
    async with factory() as s:
        u = await s.scalar(select(User).where(User.username == "admin"))
        assert verify_password("newpass-45678", u.password_hash)
        assert u.totp_enabled is False
        assert u.token_version == 4  # инкремент
    await engine.dispose()


def test_enforce_secrets_rejects_weak():
    from app.config import Settings
    from app.main import _enforce_secrets

    with pytest.raises(RuntimeError):
        _enforce_secrets(Settings(jwt_secret="changeme", admin_password="strongpass"))
    with pytest.raises(RuntimeError):
        _enforce_secrets(Settings(jwt_secret="x" * 40, admin_password="admin"))
    # debug — разрешаем слабые (локальная разработка)
    _enforce_secrets(Settings(debug=True, jwt_secret="changeme", admin_password="admin"))
    # сильные — ок
    _enforce_secrets(
        Settings(jwt_secret="a" * 40, admin_password="strong-enough-pass")
    )


async def test_login_limiter_is_shared_between_processes(client: httpx.AsyncClient) -> None:
    """Счётчик неудач общий, а не свой у каждого процесса.

    В scale-режиме uvicorn поднимает несколько воркеров. Пока счётчик жил в
    памяти процесса, порог умножался на их число — на живой панели с тремя
    воркерами из 45 попыток подбора 22 дошли до проверки пароля. Проверяем, что
    попытки видны через общее хранилище: свежая сессия к той же базе застаёт
    ключ уже заблокированным.
    """
    from app import ratelimit

    app = client._transport.app  # type: ignore[attr-defined]
    async with app.state.session_factory() as writer:
        for _ in range(ratelimit.MAX_FAILURES):
            await ratelimit.record_failure(writer, "203.0.113.77")

    async with app.state.session_factory() as reader:  # «другой воркер»
        assert await ratelimit.is_locked(reader, "203.0.113.77")
        assert not await ratelimit.is_locked(reader, "198.51.100.4")

    async with app.state.session_factory() as reader:
        await ratelimit.clear(reader, "203.0.113.77")
        assert not await ratelimit.is_locked(reader, "203.0.113.77")
