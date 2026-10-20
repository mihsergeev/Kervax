"""Вход админа под чужой учеткой (app/impersonate.py): права ровно как у пользователя, только
просмотр, свой пароль админа перед входом, срок, "Вернуться к себе" и журнал действий."""

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app import audit, impersonate
from app.models import AuditLog, Check, Impersonation, PanelError, Server

ADMIN_PW = "testpass-2026"


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _factory(client: httpx.AsyncClient):
    return client._transport.app.state.session_factory  # noqa: SLF001


async def _fill(client) -> dict:
    now = datetime.now(timezone.utc)
    async with _factory(client)() as s:
        a = Server(name="web-a", token_hash="t-a", group_name="shop", last_seen=now)
        b = Server(name="db-b", token_hash="t-b", group_name="infra", last_seen=now)
        c = Check(name="shop main", type="http", target="https://shop.example.com/", group_name="shop")
        s.add_all([a, b, c])
        await s.commit()
        return {"a": a.id, "b": b.id, "c": c.id}


async def _mk(client, auth_headers, username, role, **scope) -> int:
    r = await client.post("/api/users", headers=auth_headers, json={
        "username": username, "password": f"{username}-pass-2026", "role": role, **scope})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _start(client, auth_headers, uid, password=ADMIN_PW) -> httpx.Response:
    return await client.post(f"/api/users/{uid}/impersonate", headers=auth_headers, json={"password": password})


async def _audit(client, action: str) -> list[AuditLog]:
    async with _factory(client)() as s:
        return list(await s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)))


async def test_rights_are_exactly_the_users(client, auth_headers):
    ids = await _fill(client)
    uid = await _mk(client, auth_headers, "infraview", "viewer", server_groups=["infra"], sections=["servers"])
    r = await _start(client, auth_headers, uid)
    assert r.status_code == 200, r.text
    imp = _h(r.json()["access_token"])
    assert r.json()["username"] == "infraview"

    me = (await client.get("/api/auth/me", headers=imp)).json()
    assert me["username"] == "infraview" and me["role"] == "viewer"
    assert me["impersonator"] == "admin" and me["imp_id"] == r.json()["imp_id"] and me["imp_expires"]
    assert me["server_groups"] == ["infra"]

    # серверы только своей группы, чужой по id не открыть
    names = [x["name"] for x in (await client.get("/api/servers", headers=imp)).json()]
    assert names == ["db-b"]
    assert (await client.get(f"/api/servers/{ids['a']}", headers=imp)).status_code == 404
    # закрытый раздел и все админское закрыто, хотя за токеном стоит админ
    assert (await client.get("/api/checks", headers=imp)).status_code == 403
    for path in ("/api/users", "/api/users/overview", "/api/audit", "/api/panel-errors"):
        assert (await client.get(path, headers=imp)).status_code == 403, path

    # а сам админ своим токеном видит все, как и раньше
    me_admin = (await client.get("/api/auth/me", headers=auth_headers)).json()
    assert me_admin["username"] == "admin" and me_admin["impersonator"] is None
    assert len((await client.get("/api/servers", headers=auth_headers)).json()) == 2


async def test_only_viewing_nothing_can_be_changed(client, auth_headers):
    ids = await _fill(client)
    uid = await _mk(client, auth_headers, "shopedit", "editor", server_groups=["shop"], site_groups=["shop"])
    imp = _h((await _start(client, auth_headers, uid)).json()["access_token"])

    # правка, хоть она у пользователя и есть, и все чувствительное по своей учетке
    attempts = [
        ("DELETE", f"/api/servers/{ids['a']}", None),
        ("POST", f"/api/checks/{ids['c']}/run", None),
        ("POST", "/api/auth/password", {"current_password": "shopedit-pass-2026", "new_password": "x" * 14}),
        ("POST", "/api/auth/2fa/setup", None),
        ("POST", "/api/auth/telegram/link", None),
        ("DELETE", "/api/auth/telegram", None),
        ("POST", "/api/users", {"username": "zz", "password": "zzzzzzzz-zzzz", "role": "viewer"}),
        ("PATCH", f"/api/users/{uid}", {"role": "admin"}),
        ("POST", f"/api/users/{uid}/impersonate", {"password": ADMIN_PW}),
        ("POST", "/api/settings/ansible", None),
    ]
    for method, path, body in attempts:
        r = await client.request(method, path, headers=imp, json=body)
        assert r.status_code == 403, (method, path, r.text)
        assert r.json()["detail"].startswith("Вы смотрите панель глазами shopedit"), (method, path)

    async with _factory(client)() as s:
        assert await s.get(Server, ids["a"]) is not None
        # ожидаемый отказ - не ошибка панели
        from app import panel_errors as pe

        await pe.drain()
        assert not list(await s.scalars(select(PanelError).where(PanelError.kind == "api")))

    # ошибка интерфейса пишется, но на админа, а не на пользователя
    r = await client.post("/api/panel-errors/front", headers=imp, json={"message": "boom", "page": "/"})
    assert r.status_code == 200, r.text
    rows = (await client.get("/api/panel-errors", headers=auth_headers, params={"kind": "front"})).json()
    assert [x["username"] for x in rows["items"]] == ["admin как shopedit"]


async def test_who_can_start_and_under_whom(client, auth_headers):
    uid = await _mk(client, auth_headers, "plainview", "viewer")
    other_admin = await _mk(client, auth_headers, "admin2", "admin")
    editor = await _mk(client, auth_headers, "someeditor", "editor")
    me_id = (await client.get("/api/auth/me", headers=auth_headers)).json()["id"]

    # свой пароль админа обязателен (step-up), неверный пишется в журнал как неудачный вход
    r = await _start(client, auth_headers, uid, password="wrong-password-1")
    assert r.status_code == 400
    fails = await _audit(client, "impersonate_pw_fail")
    assert [(x.username, x.action) for x in fails] == [("admin", "impersonate_pw_fail")]
    assert not await _audit(client, "impersonate_start")

    # ни под собой, ни под другим админом
    assert (await _start(client, auth_headers, me_id)).status_code == 400
    r = await _start(client, auth_headers, other_admin)
    assert r.status_code == 400 and "администратор" in r.json()["detail"]
    assert (await _start(client, auth_headers, 9999)).status_code == 404

    # не-админ начать не может
    login = await client.post("/api/auth/login", json={"username": "someeditor", "password": "someeditor-pass-2026"})
    eh = _h(login.json()["access_token"])
    r = await client.post(f"/api/users/{uid}/impersonate", headers=eh, json={"password": "someeditor-pass-2026"})
    assert r.status_code == 403
    assert editor


async def test_return_to_self_restores_admin_and_kills_token(client, auth_headers):
    uid = await _mk(client, auth_headers, "plainview", "viewer")
    r = await _start(client, auth_headers, uid)
    imp, imp_id = _h(r.json()["access_token"]), r.json()["imp_id"]
    assert (await client.get("/api/auth/me", headers=imp)).status_code == 200

    # токен админа живет все время входа: вернуться можно без повторного входа
    assert (await client.get("/api/users", headers=auth_headers)).status_code == 200
    # закрыть чужой вход может только тот, кто его начал, и только своим токеном
    assert (await client.post(f"/api/users/impersonation/{imp_id}/stop", headers=imp)).status_code == 403
    r = await client.post(f"/api/users/impersonation/{imp_id}/stop", headers=auth_headers)
    assert r.status_code == 204
    # токен входа отозван сразу, не дожидаясь срока
    assert (await client.get("/api/auth/me", headers=imp)).status_code == 401
    assert (await client.get("/api/auth/me", headers=auth_headers)).json()["username"] == "admin"
    # повторное закрытие - тихо
    assert (await client.post(f"/api/users/impersonation/{imp_id}/stop", headers=auth_headers)).status_code == 204

    start = await _audit(client, "impersonate_start")
    stop = await _audit(client, "impersonate_stop")
    assert [(x.username, x.target) for x in start] == [("admin", "plainview")]
    assert "только просмотр" in start[0].detail
    assert [(x.username, x.target, x.detail) for x in stop] == [("admin", "plainview", "вернулся к себе")]


async def test_expires_after_an_hour(client, auth_headers):
    uid = await _mk(client, auth_headers, "plainview", "viewer")
    r = await _start(client, auth_headers, uid)
    imp, imp_id = _h(r.json()["access_token"]), r.json()["imp_id"]
    exp = datetime.fromisoformat(r.json()["expires_at"])
    left = exp - datetime.now(timezone.utc)
    assert timedelta(minutes=59) < left <= timedelta(minutes=impersonate.IMP_TTL_MINUTES)

    async with _factory(client)() as s:
        row = await s.get(Impersonation, imp_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await s.commit()
    assert (await client.get("/api/auth/me", headers=imp)).status_code == 401
    # браузер вернулся к админу и закрыл вход: в журнале - "истек срок"
    assert (await client.post(f"/api/users/impersonation/{imp_id}/stop", headers=auth_headers)).status_code == 204
    assert [x.detail for x in await _audit(client, "impersonate_stop")] == ["истек срок"]


async def test_new_start_closes_previous(client, auth_headers):
    a = await _mk(client, auth_headers, "firstview", "viewer")
    b = await _mk(client, auth_headers, "secondview", "viewer")
    first = _h((await _start(client, auth_headers, a)).json()["access_token"])
    second = _h((await _start(client, auth_headers, b)).json()["access_token"])
    assert (await client.get("/api/auth/me", headers=first)).status_code == 401
    assert (await client.get("/api/auth/me", headers=second)).json()["username"] == "secondview"
    assert [(x.target, x.detail) for x in await _audit(client, "impersonate_stop")] == [("firstview", "начат новый вход")]


async def test_password_or_role_change_ends_it(client, auth_headers):
    uid = await _mk(client, auth_headers, "plainview", "viewer")
    imp = _h((await _start(client, auth_headers, uid)).json()["access_token"])
    # сброс пароля пользователя гасит и вход под ним
    r = await client.post(f"/api/users/{uid}/password", headers=auth_headers, json={"new_password": "n" * 14})
    assert r.status_code == 204
    assert (await client.get("/api/auth/me", headers=imp)).status_code == 401

    imp = _h((await _start(client, auth_headers, uid)).json()["access_token"])
    # пользователь стал админом - под ним больше нельзя
    assert (await client.patch(f"/api/users/{uid}", headers=auth_headers, json={"role": "admin"})).status_code == 200
    assert (await client.get("/api/auth/me", headers=imp)).status_code == 401


async def test_audit_is_written_as_admin_on_behalf_of_user(client, auth_headers):
    uid = await _mk(client, auth_headers, "plainview", "viewer")
    imp_id = (await _start(client, auth_headers, uid)).json()["imp_id"]
    async with _factory(client)() as s:
        impersonate.set_acting(impersonate.Acting(imp_id, "admin", "plainview", datetime.now(timezone.utc)))
        try:
            await audit.record(s, "plainview", "check_create", "shop main")
            # чужое имя (системные записи, другие учетки) не трогается
            await audit.record(s, "авто", "known_set", "web-a")
        finally:
            impersonate.set_acting(None)
    rows = {x.action: x for x in (await _audit(client, "check_create")) + (await _audit(client, "known_set"))}
    assert (rows["check_create"].username, rows["check_create"].as_user) == ("admin", "plainview")
    assert (rows["known_set"].username, rows["known_set"].as_user) == ("авто", "")

    # и окно Пользователи отдает пометку
    ov = (await client.get("/api/users/overview", headers=auth_headers)).json()
    mark = next(x for x in ov["audit"] if x["action"] == "check_create")
    assert mark["username"] == "admin" and mark["as_user"] == "plainview"


async def test_marker_does_not_leak_to_next_request(client, auth_headers):
    """Запросы в тестах идут в одной задаче asyncio: пометка прошлого запроса не должна
    приписать следующий вход пользователя админу."""
    uid = await _mk(client, auth_headers, "plainview", "viewer")
    imp = _h((await _start(client, auth_headers, uid)).json()["access_token"])
    assert (await client.get("/api/auth/me", headers=imp)).status_code == 200
    r = await client.post("/api/auth/login", json={"username": "plainview", "password": "plainview-pass-2026"})
    assert r.status_code == 200
    ok = [x for x in await _audit(client, "login_ok") if x.username == "plainview"]
    assert len(ok) == 1 and ok[0].as_user == ""
