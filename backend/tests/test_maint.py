"""Окна обслуживания: алерты по выбранным серверам и мониторам молчат, после окна сверка."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import collector, maint
from app.alerts import full_text
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check, MaintenanceWindow, Server

TZ = "Europe/Moscow"  # UTC+3 без перехода


def _w(start, end, repeat="", weekdays=None, **kw):
    base = dict(starts_at=start, ends_at=end, repeat=repeat, weekdays=weekdays, verify=True,
                verified_end=None, server_ids=[1], check_ids=[], title="обновление ядра")
    base.update(kw)
    return SimpleNamespace(**base)


def u(*a):
    return datetime(*a, tzinfo=timezone.utc)


# ---------------------------------------------------------------- расписание


def test_one_off():
    w = _w(u(2026, 10, 14, 20), u(2026, 10, 14, 21))
    assert maint.current(w, u(2026, 10, 14, 20, 30), TZ) == (u(2026, 10, 14, 20), u(2026, 10, 14, 21))
    assert maint.current(w, u(2026, 10, 14, 21), TZ) is None
    assert maint.next_start(w, u(2026, 10, 14, 19), TZ) == u(2026, 10, 14, 20)
    assert maint.finished(w, u(2026, 10, 14, 21, 1))


def test_daily_across_midnight_local_time():
    # 23:30-01:00 по Москве каждый день, первое - 10.10
    w = _w(u(2026, 10, 10, 20, 30), u(2026, 10, 10, 22), repeat="daily")
    # 14.10 в 00:30 по Москве = 13.10 21:30 UTC - идет вхождение, начатое 13.10
    assert maint.current(w, u(2026, 10, 13, 21, 30), TZ) == (u(2026, 10, 13, 20, 30), u(2026, 10, 13, 22))
    assert maint.current(w, u(2026, 10, 13, 12), TZ) is None
    assert maint.next_start(w, u(2026, 10, 13, 12), TZ) == u(2026, 10, 13, 20, 30)
    # до первого вхождения повторов нет
    assert maint.current(w, u(2026, 10, 9, 21), TZ) is None
    assert not maint.finished(w, u(2027, 1, 1))


def test_weekly_days():
    # пн и чт 03:00-04:00 по Москве (00:00-01:00 UTC); 12.10.2026 - понедельник
    w = _w(u(2026, 10, 12, 0), u(2026, 10, 12, 1), repeat="weekly", weekdays=[0, 3])
    assert maint.current(w, u(2026, 10, 12, 0, 30), TZ) is not None  # пн
    assert maint.current(w, u(2026, 10, 13, 0, 30), TZ) is None      # вт
    assert maint.current(w, u(2026, 10, 15, 0, 30), TZ) is not None  # чт
    assert maint.next_start(w, u(2026, 10, 13, 12), TZ) == u(2026, 10, 15, 0)


def test_active_sets():
    a = _w(u(2026, 10, 14, 10), u(2026, 10, 14, 12), server_ids=[1, 2], check_ids=[7])
    b = _w(u(2026, 10, 15, 10), u(2026, 10, 15, 12), server_ids=[3])
    assert maint.active([a, b], u(2026, 10, 14, 11), TZ) == ({1, 2}, {7})


def test_due_verify():
    w = _w(u(2026, 10, 14, 10), u(2026, 10, 14, 11))
    assert maint.due_verify(w, u(2026, 10, 14, 11, 2), TZ) is None   # рано
    assert maint.due_verify(w, u(2026, 10, 14, 11, 6), TZ) == (u(2026, 10, 14, 10), u(2026, 10, 14, 11))
    w.verified_end = u(2026, 10, 14, 11)
    assert maint.due_verify(w, u(2026, 10, 14, 11, 7), TZ) is None   # уже сверяли
    w2 = _w(u(2026, 10, 14, 10), u(2026, 10, 14, 11), verify=False)
    assert maint.due_verify(w2, u(2026, 10, 14, 11, 6), TZ) is None
    assert maint.due_verify(w, u(2026, 10, 16), TZ) is None          # давно, после простоя молчим


def test_verify_text():
    assert maint.verify_text("ядро", [], 2, 1) == 'после обслуживания "ядро" все в норме (серверов 2, мониторов 1)'
    assert maint.verify_text("ядро", ["web-a - не на связи"], 1, 0) == (
        'после обслуживания "ядро" не в норме: web-a - не на связи')


# ---------------------------------------------------------------- API


async def _server(client, name="web-a", group="") -> int:
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        srv = Server(name=name, token_hash="t-" + name, group_name=group)
        s.add(srv)
        await s.commit()
        return srv.id


def _body(**kw):
    now = datetime.now(timezone.utc)
    b = {"title": "обновление ядра", "server_ids": [], "check_ids": [],
         "starts_at": (now - timedelta(minutes=10)).isoformat(), "ends_at": (now + timedelta(hours=1)).isoformat()}
    b.update(kw)
    return b


async def test_api_create_list_update_delete(client, auth_headers):
    sid = await _server(client)
    r = await client.post("/api/maintenance", json=_body(server_ids=[sid]), headers=auth_headers)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["active"] and j["cur_end"] and j["server_ids"] == [sid] and j["created_by"]
    lst = (await client.get("/api/maintenance", headers=auth_headers)).json()
    assert [x["id"] for x in lst] == [j["id"]]
    r = await client.put(f"/api/maintenance/{j['id']}", json=_body(server_ids=[sid], title="ядро и docker"),
                         headers=auth_headers)
    assert r.status_code == 200 and r.json()["title"] == "ядро и docker"
    assert (await client.delete(f"/api/maintenance/{j['id']}", headers=auth_headers)).status_code == 204
    assert (await client.get("/api/maintenance", headers=auth_headers)).json() == []


async def test_api_validation(client, auth_headers):
    sid = await _server(client)
    now = datetime.now(timezone.utc)
    cases = [
        (_body(server_ids=[sid], ends_at=(now - timedelta(hours=1)).isoformat()), "позже начала"),
        (_body(), "хотя бы один"),
        (_body(server_ids=[sid], repeat="weekly"), "дни недели"),
        (_body(server_ids=[sid], repeat="daily", ends_at=(now + timedelta(hours=30)).isoformat()), "суток"),
        (_body(server_ids=[9999]), "недоступные"),
    ]
    for body, err in cases:
        r = await client.post("/api/maintenance", json=body, headers=auth_headers)
        assert r.status_code == 400 and err in r.json()["detail"], (body, r.text)


async def test_api_groups(client, auth_headers):
    own = await _server(client, "web-a", "web")
    other = await _server(client, "db-a", "db")
    r = await client.post("/api/users", headers=auth_headers, json={
        "username": "webeditor", "password": "webeditor-pass-2026", "role": "editor", "server_groups": ["web"]})
    assert r.status_code in (200, 201), r.text
    login = await client.post("/api/auth/login", json={"username": "webeditor", "password": "webeditor-pass-2026"})
    eh = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.post("/api/maintenance", json=_body(server_ids=[other]), headers=eh)).status_code == 400
    # окно админа на оба сервера: редактор видит только свой, правка чужой не теряет
    j = (await client.post("/api/maintenance", json=_body(server_ids=[own, other]), headers=auth_headers)).json()
    seen = (await client.get("/api/maintenance", headers=eh)).json()
    assert seen[0]["server_ids"] == [own]
    r = await client.put(f"/api/maintenance/{j['id']}", json=_body(server_ids=[own], title="x"), headers=eh)
    assert r.status_code == 200
    full = (await client.get("/api/maintenance", headers=auth_headers)).json()
    assert full[0]["server_ids"] == sorted([own, other])
    assert (await client.delete(f"/api/maintenance/{j['id']}", headers=eh)).status_code == 403


# ---------------------------------------------------------------- сборщик


async def _panel(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


async def test_server_quiet_in_window_and_alerts_after(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(
            name="web-a", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now, last_report={"disks": [{"mount": "/", "used": 92, "total": 100}]},
            cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=85, disk_alert_percent=90,
            disk_crit_percent=95, alert_sustain_seconds=0, offline_after_seconds=120,
        ))
        s.add(MaintenanceWindow(title="чистка диска", server_ids=[1], starts_at=now - timedelta(minutes=5),
                                ends_at=now + timedelta(minutes=30), repeat="", verify=False))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []
    later = now + timedelta(minutes=31)
    async with factory() as s:
        (await s.get(Server, 1)).last_seen = later
        await s.commit()
    await collector.evaluate_servers(factory, settings, later)
    assert sent == []  # окно как снуз: выдержка диска пошла с конца окна
    later += timedelta(minutes=31)
    async with factory() as s:
        (await s.get(Server, 1)).last_seen = later
        await s.commit()
    await collector.evaluate_servers(factory, settings, later)
    assert len(sent) == 1 and "92%" in sent[0]
    await engine.dispose()


async def test_check_quiet_in_window(client, monkeypatch):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        a = Check(name="site-a", type="http", target="a.example")
        s.add(a)
        await s.commit()
        s.add(MaintenanceWindow(title="переезд", check_ids=[a.id], starts_at=now - timedelta(minutes=1),
                                ends_at=now + timedelta(hours=1), repeat=""))
        await s.commit()
        cid = a.id
    sent = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    await collector._send_alerts(f, Settings(alert_webhook="http://hook"),
                                 [("bad", "site-a", "down", "HTTP 502", None, cid, None)], now)
    assert sent == []


async def test_verify_after_window(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name="web-a", token_hash="a", enabled=True, last_seen=now, offline_after_seconds=120,
                     alert_state={}))
        s.add(Server(name="web-b", token_hash="b", enabled=True, last_seen=now - timedelta(hours=1),
                     offline_after_seconds=120))
        s.add(Check(name="site-a", type="http", target="a.example", last_status="up"))
        s.add(MaintenanceWindow(title="ядро", server_ids=[1], check_ids=[1], starts_at=now - timedelta(hours=1),
                                ends_at=now - timedelta(minutes=6), repeat=""))
        s.add(MaintenanceWindow(title="второе", server_ids=[2], starts_at=now - timedelta(hours=1),
                                ends_at=now - timedelta(minutes=6), repeat=""))
        await s.commit()
    sent = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    settings = Settings(alert_webhook="http://hook")
    await collector.maintenance_tick(factory, settings, now)
    assert len(sent) == 2
    # в чат - коротко: окно, итог и что не вернулось; счетчики - в полном тексте
    assert sent[0] == "🚧 <b>ядро</b>\n<b>Обслуживание закончилось, все в норме</b>"
    assert full_text(sent[0]) == '🚧 после обслуживания &quot;ядро&quot; все в норме (серверов 1, мониторов 1)'
    assert sent[1] == "⚠️🚧 <b>второе</b>\n<b>Обслуживание закончилось, не все вернулось</b>\nweb-b - не на связи"
    assert full_text(sent[1]).startswith("⚠️ ") and "web-b - не на связи" in full_text(sent[1])
    # второй раз по тому же окну не сверяем
    await collector.maintenance_tick(factory, settings, now + timedelta(minutes=1))
    assert len(sent) == 2
    await engine.dispose()


async def test_bot_status_shows_window(client):
    from app import tg_bot
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        s.add(Server(name="web-a", token_hash="w", enabled=True, alert_state={"cpu": 1}))
        await s.commit()
        s.add(MaintenanceWindow(title="ядро", server_ids=[1], starts_at=now - timedelta(minutes=1),
                                ends_at=now + timedelta(hours=1), repeat=""))
        await s.commit()
        text = await tg_bot.status_text(s, None, now)
    assert "🚧 Обслуживание: ядро до" in text and "🚧 <b>web-a</b>" in text
