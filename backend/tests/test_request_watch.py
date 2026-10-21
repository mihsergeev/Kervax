"""Левые запросы к панели: счетчик отказов и всплесков по адресам, алерт с тем, кто это и на что
похоже, отбой после тишины, проблема у сервера и раздел в окне Пользователи."""

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update

from app import request_watch as rw
from app.alerts import full_text
from app.config import Settings
from app.models import RequestStat, Server


def _from(ip: str) -> dict:
    return {"X-Forwarded-For": f"{ip}, 172.18.0.5"}


def _fake_dispatch(monkeypatch) -> list:
    sent: list = []

    async def fake(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake)
    return sent


def test_paths_fold_ids_and_tokens():
    assert rw.norm_path("/api/servers/12/notes") == "/api/servers/{id}/notes"
    assert rw.norm_path("/api/agent/push/9f2c41d0a7b84e6f9c2d") == "/api/agent/push/{token}"
    assert rw.norm_path("/api/agent/commands") == "/api/agent/commands"


async def test_middleware_counts_rejections_by_address(client: httpx.AsyncClient):
    rw.reset()
    for _ in range(60):
        r = await client.get("/api/agent/commands", headers={**_from("203.0.113.80"), "Authorization": "Bearer nope"})
        assert r.status_code == 401
    # вход не считается: у неудачных входов своя защита
    for _ in range(3):
        await client.post("/api/auth/login", json={"username": "admin", "password": "bad"}, headers=_from("203.0.113.81"))
    f = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    await rw.flush(f)
    async with f() as s:
        rows = list(await s.scalars(select(RequestStat)))
    by = {r.key: r for r in rows}
    assert "203.0.113.81" not in by
    r = by["203.0.113.80"]
    assert (r.total, r.bad, r.codes, r.paths) == (60, 60, {"401": 60}, {"401 /api/agent/commands": 60})


async def test_minute_without_rejections_is_not_written():
    rw.reset()
    for _ in range(20):
        rw.count("203.0.113.90", "/api/agent/commands", 200, minute=100)
    snap = rw.count("203.0.113.90", "/api/agent/commands", 200, minute=101)
    assert snap is not None and rw._rows(*snap) == []
    for _ in range(rw.WRITE_FLOOR):
        rw.count("203.0.113.91", "/api/agent/report", 200, minute=101)
    rows = rw._rows(*rw.count("x", "/api/x", 200, minute=102))
    assert [(r.key, r.total, r.bad, r.paths) for r in rows] == [
        ("203.0.113.91", rw.WRITE_FLOOR, 0, {"/api/agent/report": rw.WRITE_FLOOR})]
    rw.reset()


async def test_panel_clicks_are_not_a_burst():
    """На проде открытая панель дала "всплеск" 62 запроса в минуту с адреса админа. Всплеск - только
    запросы агентов, push и ansible, а отказы считаются на любые адреса."""
    rw.reset()
    for _ in range(rw.FLOOD_PER_SERVER):
        rw.count("203.0.113.92", "/api/servers", 200, minute=200)
    for _ in range(3):
        rw.count("203.0.113.93", "/api/servers/7", 403, minute=200)
    rows = rw._rows(*rw.count("x", "/api/x", 200, minute=201))
    assert [(r.key, r.total, r.bad, r.paths) for r in rows] == [
        ("203.0.113.93", 0, 3, {"403 /api/servers/{id}": 3})]
    rw.reset()


def test_hints_name_the_source():
    def agg(bad=0, total=0, paths=None, codes=None):
        from collections import Counter
        return {"bad": bad, "total": total, "paths": Counter(paths or {}), "codes": Counter(codes or {})}

    gone = agg(bad=rw.BAD_LIMIT, paths={"404 /api/agent/push/{token}": rw.BAD_LIMIT}, codes={404: rw.BAD_LIMIT})
    assert rw.hint(gone, 1).startswith("push-монитор удален")
    push = agg(total=rw.FLOOD_PER_SERVER, paths={"/api/agent/push/{token}": rw.FLOOD_PER_SERVER})
    assert rw.hint(push, 1).startswith("скрипт push-монитора")
    agent = agg(total=rw.FLOOD_PER_SERVER, paths={"/api/agent/report": rw.FLOOD_PER_SERVER})
    assert rw.hint(agent, 1).startswith("агент шлет")
    # самый частый адрес и так виден списком рядом
    other = agg(total=rw.FLOOD_PER_SERVER, paths={"/api/agent/x": 1, "/api/other": rw.FLOOD_PER_SERVER})
    assert rw.hint(other, 1) == ""


async def _server(f, name: str, ip: str) -> int:
    async with f() as s:
        srv = Server(name=name, token_hash=f"hash-{name}", external_ip=ip)
        s.add(srv)
        await s.commit()
        return srv.id


async def _rows(f, now, key: str, n: int, bad: int, codes: dict, paths: dict) -> None:
    async with f() as s:
        for i in range(n):
            s.add(RequestStat(minute=now - timedelta(minutes=i), key=key, total=bad + 5, bad=bad, codes=codes, paths=paths))
        await s.commit()


async def test_alert_names_the_server_and_explains_then_recovers(client: httpx.AsyncClient, auth_headers, monkeypatch):
    sent = _fake_dispatch(monkeypatch)
    f = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    sid = await _server(f, "web-a", "203.0.113.5")
    now = datetime.now(timezone.utc)
    await _rows(f, now, "203.0.113.5", 5, 15, {"401": 15}, {"401 /api/agent/commands": 15})
    settings = Settings(alert_webhook="http://hook")
    await rw.check(f, settings, now)
    assert len(sent) == 1 and sent[0].kind == "bogus_requests" and not sent[0].recovery
    # в чат - кто шлет и куда чаще всего, сколько и что делать - в полном тексте (история, панель)
    text = str(sent[0])
    assert "<b>Левые запросы в панель</b>" in text and "с сервера web-a (203.0.113.5)" in text
    assert "/api/agent/commands" in text and "75 отказов" not in text and "—" not in text
    full = full_text(sent[0])
    assert "с сервера web-a (203.0.113.5)" in full and "75 отказов за 10 минут (401 - 75)" in full
    assert "агент шлет неверный токен" in full and "—" not in full
    # второй тик - без повтора
    await rw.check(f, settings, now + timedelta(minutes=1))
    assert len(sent) == 1
    # проблема у сервера: в списке и в карточке
    srv = next(x for x in (await client.get("/api/servers", headers=auth_headers)).json() if x["id"] == sid)
    assert [p["kind"] for p in srv["problems"]] == ["bogus_requests"]
    assert "203.0.113.5" in srv["problems"][0]["text"]
    one = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    assert any(p["kind"] == "bogus_requests" for p in one["problems"])
    # раздел в окне Пользователи
    d = (await client.get("/api/panel-requests", headers=auth_headers)).json()
    assert d["active"][0]["key"] == "203.0.113.5" and d["active"][0]["servers"] == [{"id": sid, "name": "web-a"}]
    assert d["sources"][0]["bad"] == 75 and d["sources"][0]["active"]
    # затихло: через 20 минут еще держим, через 40 - отбой
    async with f() as s:
        await s.execute(update(RequestStat).values(minute=now - timedelta(hours=1)))
        await s.commit()
    await rw.check(f, settings, now + timedelta(minutes=20))
    assert len(sent) == 1
    await rw.check(f, settings, now + timedelta(minutes=40))
    assert len(sent) == 2 and sent[1].recovery and "web-a" in str(sent[1])
    async with f() as s:
        assert await rw.server_issues(s) == {}


async def test_flood_from_unknown_address_and_nat_threshold(client: httpx.AsyncClient, monkeypatch):
    sent = _fake_dispatch(monkeypatch)
    f = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    now = datetime.now(timezone.utc)
    async with f() as s:
        s.add(RequestStat(minute=now, key="198.51.100.20", total=700, bad=0, codes={}, paths={"/api/agent/report": 700}))
        # за одним адресом пять серверов: 2000 запросов для них нормально (порог 3000)
        s.add(RequestStat(minute=now, key="198.51.100.30", total=2000, bad=0, codes={}, paths={"/api/agent/commands": 2000}))
        await s.commit()
    for i in range(5):
        await _server(f, f"nat-{i}", "198.51.100.30")
    await rw.check(f, Settings(alert_webhook="http://hook"), now)
    assert len(sent) == 1
    text = str(sent[0])
    assert "<b>Левые запросы в панель</b>" in text and "с адреса 198.51.100.20 (не сервер панели)" in text
    assert "обычно около" not in text
    full = full_text(sent[0])
    assert "с адреса 198.51.100.20 (не сервер панели)" in full
    assert "700 запросов за 10 минут, обычно около 150" in full and "несколько его копий" in full


async def test_muted_server_gets_no_alert(client: httpx.AsyncClient, monkeypatch):
    sent = _fake_dispatch(monkeypatch)
    f = client._transport.app.state.session_factory  # type: ignore[attr-defined]
    sid = await _server(f, "web-b", "203.0.113.6")
    async with f() as s:
        await s.execute(update(Server).where(Server.id == sid).values(alert_mutes=["bogus_requests"]))
        await s.commit()
    now = datetime.now(timezone.utc)
    await _rows(f, now, "203.0.113.6", 5, 15, {"404": 15}, {"404 /api/agent/push/{token}": 15})
    await rw.check(f, Settings(alert_webhook="http://hook"), now)
    assert sent == []


async def test_panel_requests_is_admin_only(client: httpx.AsyncClient, auth_headers):
    await client.post("/api/users", headers=auth_headers, json={"username": "viewer2", "password": "viewer-pass-2026", "role": "viewer"})
    tok = (await client.post("/api/auth/login", json={"username": "viewer2", "password": "viewer-pass-2026"})).json()["access_token"]
    assert (await client.get("/api/panel-requests", headers={"Authorization": f"Bearer {tok}"})).status_code == 403
