import httpx
import pytest

pytestmark = pytest.mark.pg


async def test_login_ok(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "testpass-2026"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"]
    assert body["token_type"] == "bearer"


async def test_login_wrong_password(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": "admin", "password": "wrong"}
    )
    assert response.status_code == 401


async def test_login_unknown_user(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/login", json={"username": "nobody", "password": "testpass-2026"}
    )
    assert response.status_code == 401


async def test_me_requires_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/auth/me")
    assert response.status_code == 401


async def test_me_with_token(client: httpx.AsyncClient, auth_headers) -> None:
    response = await client.get("/api/auth/me", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["username"] == "admin"


async def test_me_with_garbage_token(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert response.status_code == 401


def _totp_at(monkeypatch, step: int) -> None:
    # часы TOTP без системных: шаг 30 с задается тестом, а не временем прогона
    from types import SimpleNamespace

    from app import totp

    monkeypatch.setattr(totp, "time", SimpleNamespace(time=lambda: step * 30 + 1))


async def _enable_2fa(client, headers, monkeypatch, step: int) -> tuple[str, dict[str, str]]:
    from app import totp

    secret = (await client.post("/api/auth/2fa/setup", headers=headers)).json()["secret"]
    _totp_at(monkeypatch, step)
    body = {"otp": totp._hotp(secret, step), "current_password": "testpass-2026"}
    r = await client.post("/api/auth/2fa/enable", json=body, headers=headers)
    assert r.status_code == 200 and r.json()["enabled"] is True
    return secret, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_2fa_codes_are_single_use_for_enable_and_disable(client, auth_headers, monkeypatch) -> None:
    from app import totp

    step = 60_000_000
    secret, headers = await _enable_2fa(client, auth_headers, monkeypatch, step)
    login = {"username": "admin", "password": "testpass-2026"}
    # кодом, которым подтвердили включение, войти нельзя
    r = await client.post("/api/auth/login", json={**login, "otp": totp._hotp(secret, step)})
    assert r.status_code == 401 and r.json()["detail"] == "2fa_invalid"
    used = totp._hotp(secret, step + 1)
    assert (await client.post("/api/auth/login", json={**login, "otp": used})).status_code == 200
    # уже принятый код не отключает 2FA
    r = await client.post("/api/auth/2fa/disable", json={"otp": used}, headers=headers)
    assert r.status_code == 400
    assert (await client.get("/api/auth/2fa", headers=headers)).json()["enabled"] is True

    _totp_at(monkeypatch, step + 2)
    fresh = totp._hotp(secret, step + 2)
    r = await client.post("/api/auth/2fa/disable", json={"otp": fresh}, headers=headers)
    assert r.status_code == 200 and r.json()["enabled"] is False
    # отключение сеансы не отзывает
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 200


async def test_enabling_2fa_revokes_older_sessions_but_keeps_the_caller(client, auth_headers, monkeypatch) -> None:
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "testpass-2026"})
    other_device = {"Authorization": f"Bearer {r.json()['access_token']}"}
    _, headers = await _enable_2fa(client, auth_headers, monkeypatch, 60_000_000)
    assert (await client.get("/api/auth/me", headers=auth_headers)).status_code == 401
    assert (await client.get("/api/auth/me", headers=other_device)).status_code == 401
    r = await client.get("/api/auth/me", headers=headers)
    assert r.status_code == 200 and r.json()["username"] == "admin"
    assert (await client.get("/api/auth/2fa", headers=headers)).json()["enabled"] is True


async def test_enabling_2fa_needs_the_current_password(client, auth_headers, monkeypatch) -> None:
    from sqlalchemy import func, select

    from app import totp
    from app.models import AuditLog

    step = 60_000_000
    secret = (await client.post("/api/auth/2fa/setup", headers=auth_headers)).json()["secret"]
    _totp_at(monkeypatch, step)
    code = totp._hotp(secret, step)
    # одного токена мало: с чужим паролем 2FA не включается, код не расходуется
    r = await client.post("/api/auth/2fa/enable", json={"otp": code, "current_password": "not-the-password"},
                          headers=auth_headers)
    assert r.status_code == 400 and r.json()["detail"] == "Неверный текущий пароль"
    assert (await client.get("/api/auth/2fa", headers=auth_headers)).json()["enabled"] is False
    async with client._transport.app.state.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "2fa_enable_fail")) == 1
    r = await client.post("/api/auth/2fa/enable", json={"otp": code, "current_password": "testpass-2026"},
                          headers=auth_headers)
    assert r.status_code == 200 and r.json()["enabled"] is True


async def test_2fa_enable_password_guesses_are_limited(client, auth_headers) -> None:
    await client.post("/api/auth/2fa/setup", headers=auth_headers)
    codes = [(await client.post("/api/auth/2fa/enable", headers=auth_headers, json={
        "otp": "000000", "current_password": f"wrong-{i}-example"})).status_code for i in range(12)]
    assert codes[0] == 400 and codes[-1] == 429


async def test_enable_from_an_old_tab_without_password_is_not_counted_as_guessing(client, auth_headers) -> None:
    from sqlalchemy import func, select

    from app.models import AuditLog, LoginFailure

    await client.post("/api/auth/2fa/setup", headers=auth_headers)
    for body in ({"otp": "000000"}, {"otp": "000000", "current_password": ""}) * 3:
        r = await client.post("/api/auth/2fa/enable", json=body, headers=auth_headers)
        assert r.status_code == 400 and r.json()["detail"] == "Обновите страницу: нужен текущий пароль"
    async with client._transport.app.state.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.action == "2fa_enable_fail")) == 0
        assert await session.scalar(select(func.count()).select_from(LoginFailure)) == 0
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "testpass-2026"})
    assert r.status_code == 200


async def test_new_setup_starts_a_new_code_counter(client, auth_headers, monkeypatch) -> None:
    from app import totp

    step = 60_000_000
    old, headers = await _enable_2fa(client, auth_headers, monkeypatch, step)
    _totp_at(monkeypatch, step + 1)
    r = await client.post("/api/auth/2fa/disable", json={"otp": totp._hotp(old, step + 1)}, headers=headers)
    assert r.status_code == 200
    # сразу включаем заново с новым секретом в том же 30-секундном шаге
    new, headers = await _enable_2fa(client, headers, monkeypatch, step + 1)
    login = {"username": "admin", "password": "testpass-2026"}
    _totp_at(monkeypatch, step + 2)
    r = await client.post("/api/auth/login", json={**login, "otp": totp._hotp(old, step + 2)})
    assert r.status_code == 401 and r.json()["detail"] == "2fa_invalid"
    r = await client.post("/api/auth/login", json={**login, "otp": totp._hotp(new, step + 2)})
    assert r.status_code == 200


async def test_setup_does_not_replace_a_secret_enabled_in_parallel(client, auth_headers, monkeypatch) -> None:
    from fastapi import HTTPException
    from sqlalchemy import select

    from app import totp
    from app.api import auth as auth_api
    from app.models import User

    async with client._transport.app.state.session_factory() as session:
        # учетка прочитана до включения 2FA, setup выполняется уже после него
        stale = await session.scalar(select(User).where(User.username == "admin"))
        secret, _ = await _enable_2fa(client, auth_headers, monkeypatch, 60_000_000)
        with pytest.raises(HTTPException) as refused:
            await auth_api.twofa_setup(stale, session)
        assert refused.value.status_code == 400
    _totp_at(monkeypatch, 60_000_001)
    r = await client.post("/api/auth/login", json={
        "username": "admin", "password": "testpass-2026", "otp": totp._hotp(secret, 60_000_001)})
    assert r.status_code == 200
