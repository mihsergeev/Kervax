"""Зависимости между объектами (app/parents.py): сервер замолчал - что на нем держится, не шумит
отдельными алертами, а названо строкой в его сообщении."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import collector, parents
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check, MaintenanceWindow, Server


def _srv(i, name, sites=(), ip="", parent=None):
    return SimpleNamespace(id=i, name=name, parent_id=parent, external_ip=ip, agent_ip="",
                           last_report={"web_services": [{"sites": list(sites)}]})


def _chk(**kw):
    base = dict(target="", type="http", probe_local=False, probe_server_id=None, parent_mode="auto",
                parent_server_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_parents_only_from_unambiguous_signs():
    idx = parents.Index([
        _srv(1, "web-a", ["a.example", "www.a.example:443", "shared.example"], "203.0.113.10"),
        _srv(2, "web-b", ["shared.example", "*.b.example"], "203.0.113.20"),
    ])
    assert parents.parents_of(_chk(target="https://a.example/health"), idx) == [(1, "site")]
    assert parents.parents_of(_chk(target="www.a.example"), idx) == [(1, "site")]
    # домен на двух серверах (фронт и бэкенд, балансировка) сам не связываем
    assert parents.parents_of(_chk(target="shared.example"), idx) == []
    assert parents.parents_of(_chk(type="tcp_port", target="203.0.113.20:22"), idx) == [(2, "ip")]
    assert parents.parents_of(_chk(type="cert", target="a.example:8443"), idx) == [(1, "site")]
    # DNS-монитор спрашивает резолвер, push стучится сам: от веб-сервера не зависят
    assert parents.parents_of(_chk(type="dns", target="a.example"), idx) == []
    assert parents.parents_of(_chk(type="push", target="push"), idx) == []
    # проверка изнутри сервера важнее домена: без этого агента монитор некому проверить
    assert parents.parents_of(_chk(target="a.example", probe_local=True, probe_server_id=2), idx) == [(2, "local")]
    assert parents.parents_of(_chk(target="a.example", parent_mode="off"), idx) == []
    assert parents.parents_of(_chk(target="z.example", parent_mode="server", parent_server_id=2), idx) == [(2, "manual")]
    assert parents.parents_of(_chk(target="z.example", parent_mode="server", parent_server_id=99), idx) == []
    assert parents.parents_of(_chk(target="z.example"), idx) == []


def test_cycle_and_list_text():
    assert parents.would_cycle(1, 2, {2: 3, 3: 1})
    assert not parents.would_cycle(1, 2, {2: 3})
    assert parents.would_cycle(1, 1, {})
    assert parents.list_text(["a", "b", "c", "d", "e", "f", "g"]) == "a, b, c, d, e и еще 2"
    assert parents.list_text(["a"]) == "a"


# ---------------------------------------------------------------- сборщик


async def _panel(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


def _server(name, seen, **kw):
    base = dict(name=name, token_hash="t-" + name, enabled=True, backup_not_required=True, last_seen=seen,
                last_report={}, alert_state={}, cpu_alert_percent=0, mem_alert_percent=0,
                disk_alert_percent=0, offline_after_seconds=120, alert_sustain_seconds=0)
    base.update(kw)
    return Server(**base)


def _capture(monkeypatch):
    sent: list = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    return sent


async def test_silent_node_keeps_its_cpu_alert_instead_of_a_false_recovery(tmp_path, monkeypatch):
    """CPU объявлен, нода пропала: раньше пороговые условия на молчащей ноде падали в ноль и
    приходило "CPU снова в норме" вместе с "недоступен"."""
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(_server("web-a", now - timedelta(minutes=10), cpu_alert_percent=80,
                      last_report={"cpu_percent": 97},
                      alert_state={"cpu": 1, "cpu_since": (now - timedelta(minutes=20)).isoformat()}))
        await s.commit()
    sent = _capture(monkeypatch)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    assert [m.kind for m in sent] == ["offline"], [str(m) for m in sent]
    assert not any(m.recovery for m in sent)
    async with factory() as s:
        st = (await s.get(Server, 1)).alert_state
    assert st["cpu"] == 1 and st["cpu_since"]  # замерло как было: вернется - судим дальше
    await engine.dispose()


async def test_node_behind_a_silent_gateway_is_a_line_in_its_message(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    gone = now - timedelta(minutes=10)
    async with factory() as s:
        s.add(_server("gw-a", gone, last_report={"web_services": [{"sites": ["a.example"]}]}))
        s.add(_server("node-b", gone, parent_id=1))
        s.add(Check(name="site-a", type="http", target="a.example", last_status="down"))
        s.add(Check(name="site-z", type="http", target="z.example", last_status="down"))
        await s.commit()
    sent = _capture(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1, [str(m) for m in sent]
    assert sent[0].target == "gw-a"
    assert "↳ за ним молчат: node-b" in sent[0]
    assert "↳ лежат мониторы: site-a" in sent[0] and "site-z" not in sent[0]
    # шлюз вернулся, а node-b все еще молчит - теперь свой "недоступен"
    async with factory() as s:
        (await s.get(Server, 1)).last_seen = now
        await s.commit()
    sent.clear()
    await collector.evaluate_servers(factory, settings, now)
    assert [m.target for m in sent if m.kind == "offline" and not m.recovery] == ["node-b"]
    await engine.dispose()


async def test_node_behind_a_gateway_in_maintenance_stays_quiet(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(_server("gw-a", now))
        s.add(_server("node-b", now - timedelta(minutes=10), parent_id=1))
        s.add(MaintenanceWindow(title="прошивка шлюза", server_ids=[1], starts_at=now - timedelta(minutes=5),
                                ends_at=now + timedelta(minutes=30), repeat="", verify=False))
        await s.commit()
    sent = _capture(monkeypatch)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    assert sent == []
    await engine.dispose()


async def test_monitor_alert_waits_while_its_server_is_silent(client, monkeypatch):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        srv = _server("web-a", now - timedelta(minutes=5),
                      last_report={"web_services": [{"sites": ["a.example"]}]})
        s.add(srv)
        other = Check(name="site-b", type="http", target="b.example")
        a = Check(name="site-a", type="http", target="a.example")
        s.add_all([srv, a, other])
        await s.commit()
        cid, oid, sid = a.id, other.id, srv.id
    sent = _capture(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    await collector._send_alerts(f, settings, [
        ("bad", "site-a", "down", "HTTP 502", None, cid, None),
        ("bad", "site-b", "down", "HTTP 502", None, oid, None),
    ], now)
    assert [m.target for m in sent] == ["site-b"]
    # сервер снова на связи, а сайт лежит - алерт уходит
    async with f() as s:
        (await s.get(Server, sid)).last_seen = now
        await s.commit()
    sent.clear()
    await collector._send_alerts(f, settings, [("bad", "site-a", "down", "HTTP 502", None, cid, None)], now)
    assert [m.target for m in sent] == ["site-a"]


async def test_monitor_quiet_while_its_server_is_in_maintenance(client, monkeypatch):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        srv = _server("web-a", now)
        s.add(srv)
        await s.commit()
        a = Check(name="cron", type="push", target="push", parent_mode="server", parent_server_id=srv.id)
        s.add(a)
        s.add(MaintenanceWindow(title="переезд", server_ids=[srv.id], starts_at=now - timedelta(minutes=1),
                                ends_at=now + timedelta(hours=1), repeat=""))
        await s.commit()
        cid = a.id
    sent = _capture(monkeypatch)
    await collector._send_alerts(f, Settings(alert_webhook="http://hook"),
                                 [("bad", "cron", "down", "сигнала нет", None, cid, None)], now)
    assert sent == []


# ---------------------------------------------------------------- API


async def _add_server(client, name, **kw) -> int:
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        srv = Server(name=name, token_hash="t-" + name, **kw)
        s.add(srv)
        await s.commit()
        return srv.id


async def test_api_server_parent(client, auth_headers):
    gw = await _add_server(client, "gw-a")
    node = await _add_server(client, "node-b")
    r = await client.patch(f"/api/servers/{node}", json={"parent_id": node}, headers=auth_headers)
    assert r.status_code == 400 and "сам за собой" in r.json()["detail"]
    r = await client.patch(f"/api/servers/{node}", json={"parent_id": gw}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["parent_id"] == gw
    r = await client.patch(f"/api/servers/{gw}", json={"parent_id": node}, headers=auth_headers)
    assert r.status_code == 400 and "замкнет" in r.json()["detail"]
    r = await client.patch(f"/api/servers/{node}", json={"parent_id": 9999}, headers=auth_headers)
    assert r.status_code == 404
    # родителя удалили - сервер ни за кем
    assert (await client.delete(f"/api/servers/{gw}", headers=auth_headers)).status_code == 204
    r = await client.get(f"/api/servers/{node}", headers=auth_headers)
    assert r.json()["parent_id"] is None


async def test_api_check_parent(client, auth_headers):
    web = await _add_server(client, "web-a", last_report={"web_services": [{"sites": ["a.example"]}]})
    r = await client.post("/api/checks", json={"name": "site-a", "type": "http", "target": "https://a.example"},
                          headers=auth_headers)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["parents"] == [{"id": web, "name": "web-a", "why": "site", "silent": False}]
    r = await client.patch(f"/api/checks/{cid}", json={"parent_mode": "server"}, headers=auth_headers)
    assert r.status_code == 400 and "Выберите сервер" in r.json()["detail"]
    r = await client.patch(f"/api/checks/{cid}", json={"parent_mode": "server", "parent_server_id": 9999},
                           headers=auth_headers)
    assert r.status_code == 400
    other = await _add_server(client, "db-a")
    r = await client.patch(f"/api/checks/{cid}", json={"parent_mode": "server", "parent_server_id": other},
                           headers=auth_headers)
    assert r.status_code == 200 and r.json()["parents"] == [{"id": other, "name": "db-a", "why": "manual", "silent": False}]
    r = await client.patch(f"/api/checks/{cid}", json={"parent_mode": "off"}, headers=auth_headers)
    assert r.json()["parents"] == [] and r.json()["parent_server_id"] is None
    lst = (await client.get("/api/checks", headers=auth_headers)).json()
    assert [c["parents"] for c in lst if c["id"] == cid] == [[]]
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert any(c["id"] == cid for c in ov["checks"])
    # удалили ручного родителя - монитор снова ищет сам
    await client.patch(f"/api/checks/{cid}", json={"parent_mode": "server", "parent_server_id": other},
                       headers=auth_headers)
    assert (await client.delete(f"/api/servers/{other}", headers=auth_headers)).status_code == 204
    j = (await client.get(f"/api/checks/{cid}", headers=auth_headers)).json()
    assert j["parent_mode"] == "auto" and j["parents"] == [{"id": web, "name": "web-a", "why": "site", "silent": False}]
