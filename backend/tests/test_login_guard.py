"""Защита входа: блокировка адреса на срок, повторная на сутки, белый список, один алерт на
волну подбора и раздел "Защита входа" в окне Пользователи."""

import time

import httpx

from app import ratelimit

GOOD = {"username": "admin", "password": "testpass-2026"}
BAD = {"username": "admin", "password": "bad"}


def _from(ip: str) -> dict:
    # адрес клиента дописывает свой прокси (caddy), соединение - от nginx фронтенда
    return {"X-Forwarded-For": f"{ip}, 172.18.0.5"}


async def _alerts(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def fake(session, settings, text):
        sent.append(text)

    monkeypatch.setattr("app.alerts.security_alert", fake)
    return sent


async def test_ban_holds_after_the_window_and_repeat_is_a_day(client: httpx.AsyncClient):
    """Раньше вход открывался, как только старые неудачи выходили из окна (минут через пять).
    Теперь блокировка держится свой срок, а повторная за сутки - сутки."""
    app = client._transport.app  # type: ignore[attr-defined]
    t0 = time.time()
    g = ratelimit.Guard(max_failures=5, ban_minutes=60)
    async with app.state.session_factory() as s:
        bans = [await ratelimit.record_failure(s, "203.0.113.5", g, "admin", now=t0 + i) for i in range(5)]
        assert bans[:4] == [None] * 4 and bans[4] is not None
        assert not bans[4].repeat and bans[4].logins == "admin"
        assert await ratelimit.active_ban(s, "203.0.113.5", now=t0 + 30 * 60)
        assert not await ratelimit.active_ban(s, "198.51.100.4", now=t0 + 30 * 60)
        assert not await ratelimit.active_ban(s, "203.0.113.5", now=t0 + 61 * 60)
        # через 10 минут после конца блокировки снова подбор - теперь на сутки
        t1 = t0 + 70 * 60
        again = [await ratelimit.record_failure(s, "203.0.113.5", g, "root", now=t1 + i) for i in range(5)]
        assert again[4] is not None and again[4].repeat
        assert await ratelimit.active_ban(s, "203.0.113.5", now=t1 + 23 * 3600)
        assert await ratelimit.unban(s, "203.0.113.5")
        assert not await ratelimit.active_ban(s, "203.0.113.5", now=t1 + 60)


async def test_blocked_login_says_when_and_is_not_logged_per_attempt(client: httpx.AsyncClient, auth_headers):
    ip = _from("203.0.113.50")
    for _ in range(ratelimit.MAX_FAILURES):
        assert (await client.post("/api/auth/login", json=BAD, headers=ip)).status_code == 401
    for _ in range(3):
        r = await client.post("/api/auth/login", json=GOOD, headers=ip)
        assert r.status_code == 429
        assert r.json()["detail"] == "locked:60"
    log = (await client.get("/api/audit?limit=100", headers=auth_headers)).json()
    assert [e["action"] for e in log].count("login_lockout") == 1
    assert "login_blocked" not in {e["action"] for e in log}
    g = (await client.get("/api/login-guard", headers=auth_headers)).json()
    assert [(b["key"], b["failures"], b["blocked"], b["logins"]) for b in g["bans"]] == [("203.0.113.50", 5, 3, "admin")]
    assert g["recent"][0]["key"] == "203.0.113.50" and g["recent"][0]["banned_until"]


async def test_allowlisted_address_is_never_blocked(client: httpx.AsyncClient, auth_headers):
    r = await client.put("/api/login-guard", headers=auth_headers, json={
        "max_failures": 5, "ban_minutes": 60, "allow": [{"net": "203.0.113.0/28", "note": "офис"}]})
    assert r.status_code == 200 and r.json()["allow"] == [{"net": "203.0.113.0/28", "note": "офис"}]
    office = _from("203.0.113.9")
    for _ in range(12):
        assert (await client.post("/api/auth/login", json=BAD, headers=office)).status_code == 401
    assert (await client.post("/api/auth/login", json=GOOD, headers=office)).status_code == 200
    # соседний адрес вне подсети блокируется как обычно
    other = _from("203.0.113.20")
    for _ in range(5):
        await client.post("/api/auth/login", json=BAD, headers=other)
    assert (await client.post("/api/auth/login", json=GOOD, headers=other)).status_code == 429
    g = (await client.get("/api/login-guard", headers=auth_headers)).json()
    rec = {r["key"]: r for r in g["recent"]}
    assert rec["203.0.113.9"]["fails"] == 12 and rec["203.0.113.9"]["allowed"] == "офис"
    assert rec["203.0.113.9"]["banned_until"] is None
    assert [b["key"] for b in g["bans"]] == ["203.0.113.20"]


async def test_one_alert_per_wave_and_none_for_repeat(client: httpx.AsyncClient, monkeypatch):
    sent = await _alerts(monkeypatch)
    for ip in ("203.0.113.61", "203.0.113.62", "203.0.113.63"):
        for _ in range(5):
            await client.post("/api/auth/login", json=BAD, headers=_from(ip))
    # три адреса за минуту - одно сообщение про первый, остальные видны в окне
    assert len(sent) == 1
    # коротко: что происходит, какой адрес и на сколько заблокирован, какие логины пробовали
    assert sent[0].startswith("🚨🔐 Kervax\nПодбирают пароль к панели\n")
    assert "203.0.113.61 заблокирован на час" in sent[0] and "Логины: admin" in sent[0]
    assert "—" not in sent[0] and "неудачных входов" not in sent[0]


async def test_guard_validation_and_unban(client: httpx.AsyncClient, auth_headers):
    bad_net = {"max_failures": 5, "ban_minutes": 60, "allow": [{"net": "example.com"}]}
    assert (await client.put("/api/login-guard", headers=auth_headers, json=bad_net)).status_code == 422
    bad_rule = {"max_failures": 4, "ban_minutes": 60, "allow": []}
    assert (await client.put("/api/login-guard", headers=auth_headers, json=bad_rule)).status_code == 422
    # одиночный адрес без /32, повтор схлопывается
    r = await client.put("/api/login-guard", headers=auth_headers, json={
        "max_failures": 3, "ban_minutes": 15,
        "allow": [{"net": "198.51.100.7/32"}, {"net": "198.51.100.7"}, {"net": "2001:db8::/48"}]})
    d = r.json()
    assert [a["net"] for a in d["allow"]] == ["198.51.100.7", "2001:db8::/48"]
    assert d["max_failures"] == 3 and d["ban_minutes"] == 15
    ip = _from("203.0.113.70")
    for _ in range(3):
        await client.post("/api/auth/login", json=BAD, headers=ip)
    r = await client.post("/api/auth/login", json=GOOD, headers=ip)
    assert r.status_code == 429 and r.json()["detail"] == "locked:15"
    d = (await client.post("/api/login-guard/unban", headers=auth_headers, json={"key": "203.0.113.70"})).json()
    assert d["bans"] == []
    assert (await client.post("/api/auth/login", json=GOOD, headers=ip)).status_code == 200
    log = (await client.get("/api/audit?limit=100", headers=auth_headers)).json()
    acts = {e["action"] for e in log}
    assert {"login_guard", "login_unban"} <= acts


async def test_guard_is_admin_only(client: httpx.AsyncClient, auth_headers):
    await client.post("/api/users", headers=auth_headers, json={"username": "viewer1", "password": "viewer-pass-2026", "role": "viewer"})
    tok = (await client.post("/api/auth/login", json={"username": "viewer1", "password": "viewer-pass-2026"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await client.get("/api/login-guard", headers=h)).status_code == 403
    assert (await client.put("/api/login-guard", headers=h, json={"max_failures": 5, "ban_minutes": 60, "allow": []})).status_code == 403
