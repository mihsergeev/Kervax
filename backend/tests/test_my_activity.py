"""Окно "Моя учетка": свои входы и смена пароля, без чужого журнала."""
import os

import httpx

from app import audit
from app.db import create_engine_and_factory


async def test_my_activity_shows_only_own_logins(client: httpx.AsyncClient, auth_headers):
    _, factory = create_engine_and_factory(os.environ["KERVAX_DB_URL"])
    me = (await client.get("/api/auth/me", headers=auth_headers)).json()["username"]
    async with factory() as s:
        await audit.record(s, me, "login_fail", "203.0.113.7")
        await audit.record(s, me, "login_ok", "203.0.113.8")
        await audit.record(s, "other", "login_ok", "203.0.113.9")
        await audit.record(s, me, "monitor_create", "shop")  # не вход
        await audit.record(s, "root", "user_reset_pw", me)  # сброс админом - тоже смена пароля

    r = await client.get("/api/auth/me/activity", headers=auth_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    ips = [x["ip"] for x in d["logins"]]
    assert "203.0.113.9" not in ips  # чужие входы не видны
    assert ips[:2] == ["203.0.113.8", "203.0.113.7"]  # свежие первыми
    assert {x["action"] for x in d["logins"]} <= {"login_ok", "login_fail", "login_2fa_fail",
                                                   "login_blocked", "login_lockout"}
    assert d["fails_24h"] >= 1 and d["password_changed"]
