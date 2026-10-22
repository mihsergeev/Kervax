"""Штатная перезагрузка не алертит (app/going_down.py): агент перед остановкой говорит панели
"ухожу", и та ждет сервер дольше обычной выдержки "недоступен"."""
from datetime import datetime, timedelta, timezone

from app import collector, going_down, os_support
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check, Server

NOW = datetime(2026, 10, 22, 14, 30, tzinfo=timezone.utc)


def _gd(kind, at):
    return {going_down.KEY: going_down.make(kind, at)}


def test_notice_holds_only_a_system_stop_and_only_for_a_while():
    assert going_down.holds(_gd("reboot", NOW - timedelta(minutes=6)), NOW)
    assert going_down.holds(_gd("poweroff", NOW - timedelta(minutes=14)), NOW)
    assert not going_down.holds(_gd("reboot", NOW - timedelta(minutes=16)), NOW)
    # остановили один агент: сервер жив, ждать нечего
    assert not going_down.holds(_gd("stop", NOW - timedelta(minutes=6)), NOW)
    assert not going_down.holds({}, NOW) and not going_down.holds(None, NOW)
    assert not going_down.holds({going_down.KEY: {"at": "мусор", "kind": "reboot"}}, NOW)
    # неизвестный вид от агента - считаем остановкой системы
    assert going_down.make("что-то", NOW)["kind"] == "shutdown"


def test_notice_survives_only_a_report_that_raced_it():
    prev = _gd("reboot", NOW - timedelta(seconds=3))
    assert going_down.carry(prev, NOW, rebooted=False) == prev[going_down.KEY]
    assert going_down.carry(prev, NOW, rebooted=True) is None
    assert going_down.carry(_gd("stop", NOW - timedelta(minutes=1)), NOW, rebooted=False) is None
    assert going_down.carry({}, NOW, rebooted=False) is None


def test_boot_reason_says_how_long_it_was_away():
    prev = _gd("reboot", NOW - timedelta(minutes=2))
    seen = NOW - timedelta(minutes=2, seconds=10)
    assert going_down.boot_why(prev, "", seen, NOW) == "штатная, без связи 2 мин"
    assert going_down.boot_why(prev, "ждало новое ядро 6.8.0-50", seen, NOW) == \
        "ждало новое ядро 6.8.0-50, без связи 2 мин"
    assert going_down.boot_why(prev, "", NOW - timedelta(seconds=40), NOW) == "штатная, без связи меньше минуты"
    assert going_down.boot_why(_gd("poweroff", NOW), "", NOW - timedelta(hours=3, minutes=5), NOW) == \
        "выключали штатно, без связи 3 ч 5 мин"
    # без предупреждения - как раньше
    assert going_down.boot_why({}, "", seen, NOW) == ""
    assert going_down.boot_why(_gd("stop", NOW), "", seen, NOW) == ""


def _server(name, seen, **kw):
    base = dict(name=name, token_hash="t-" + name, enabled=True, backup_not_required=True, last_seen=seen,
                last_report={"uptime_seconds": 86400}, alert_state={}, cpu_alert_percent=0, mem_alert_percent=0,
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


async def _panel(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'g.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


async def test_server_away_for_a_reboot_is_not_offline_until_it_fails_to_return(tmp_path, monkeypatch):
    """Живой случай: железный сервер перезагрузили руками, POST и загрузка заняли шесть минут -
    пришло "не на связи" и через минуту "снова на связи"."""
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    seen = now - timedelta(minutes=6)
    async with factory() as s:
        s.add(_server("hw-a", seen, last_report={"uptime_seconds": 86400} | _gd("reboot", seen)))
        s.add(_server("hw-b", seen))
        s.add(_server("hw-c", seen, last_report={"uptime_seconds": 86400} | _gd("stop", seen)))
        await s.commit()
    sent = _capture(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    off = {m.target: m for m in sent if m.kind == "offline"}
    # предупредивший о перезагрузке ждет; молчащий без предупреждения - как раньше
    assert set(off) == {"hw-b", "hw-c"}, [str(m) for m in sent]
    assert "Не на связи" in str(off["hw-b"])
    # остановили один агент: алерт в обычный срок, но сказано, что это агент
    assert "Агент остановлен" in str(off["hw-c"])
    assert "systemctl stop" in off["hw-c"].full
    async with factory() as s:
        assert not (await s.get(Server, 1)).alert_state.get("offline")

    # за 15 минут так и не вернулся - теперь алерт, и понятно, что после перезагрузки
    later = seen + timedelta(seconds=going_down.WAIT + 30)
    sent.clear()
    await collector.evaluate_servers(factory, settings, later)
    off = [m for m in sent if m.kind == "offline"]
    assert [m.target for m in off] == ["hw-a"], [str(m) for m in sent]
    assert "Не вернулся после перезагрузки" in str(off[0])
    assert "агент предупредил про перезагрузку 15 мин назад" in off[0].full
    await engine.dispose()


async def test_quick_reboot_with_a_notice_is_silent_end_to_end(client, auth_headers, monkeypatch):
    r = await client.post("/api/servers", headers=auth_headers, json={"name": "hw-a"})
    assert r.status_code in (200, 201), r.text
    token, sid = r.json()["token"], r.json()["server"]["id"]
    hdr = {"Authorization": f"Bearer {token}"}
    body = {"hostname": "hw-a", "os": "Ubuntu", "agent_version": "2.47", "cpu_percent": 1.0, "mem_used": 1,
            "mem_total": 2, "disks": [], "uptime_seconds": 864000}
    assert (await client.post("/api/agent/report", headers=hdr, json=body)).status_code == 200
    # чужой токен не пройдет
    bad = await client.post("/api/agent/going-down", headers={"Authorization": "Bearer x"}, json={"kind": "reboot"})
    assert bad.status_code == 401
    assert (await client.post("/api/agent/going-down", headers=hdr, json={"kind": "reboot"})).status_code == 200
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        srv = await s.get(Server, sid)
        assert srv.last_report[going_down.KEY]["kind"] == "reboot"
        # подключение закрыто: события вроде перезагрузки уходят как у всех
        srv.onboard_done_at = datetime.now(timezone.utc)
        await s.commit()

    # карточка: молчит после перезагрузки, ждем
    async with f() as s:
        srv = await s.get(Server, sid)
        srv.last_seen = datetime.now(timezone.utc) - timedelta(minutes=6)
        await s.commit()
    one = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    assert one["going_down"]["kind"] == "reboot" and one["going_down"]["wait"] is True

    sent = _capture(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    factory = f
    await collector.evaluate_servers(factory, settings, datetime.now(timezone.utc))
    assert not [m for m in sent if m.kind == "offline"], [str(m) for m in sent]

    # вернулся с новым аптаймом: перезагрузка плановая, отметка снята
    assert (await client.post("/api/agent/report", headers=hdr,
                              json=body | {"uptime_seconds": 60})).status_code == 200
    async with f() as s:
        srv = await s.get(Server, sid)
        assert going_down.KEY not in srv.last_report
        assert srv.rebooted_at is not None
        why = os_support.planned_reboot(srv.last_report, srv.rebooted_at)
    assert why.startswith("штатная, без связи 6 мин"), why
    await collector.evaluate_servers(factory, settings, datetime.now(timezone.utc))
    assert not [m for m in sent if m.kind in ("offline", "reboot")], [str(m) for m in sent]
    one = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    assert one["going_down"] is None


async def test_report_that_raced_the_notice_does_not_erase_it(client, auth_headers):
    r = await client.post("/api/servers", headers=auth_headers, json={"name": "hw-a"})
    token, sid = r.json()["token"], r.json()["server"]["id"]
    hdr = {"Authorization": f"Bearer {token}"}
    body = {"hostname": "hw-a", "os": "Ubuntu", "agent_version": "2.47", "cpu_percent": 1.0, "mem_used": 1,
            "mem_total": 2, "disks": [], "uptime_seconds": 864000}
    assert (await client.post("/api/agent/report", headers=hdr, json=body)).status_code == 200
    assert (await client.post("/api/agent/going-down", headers=hdr, json={"kind": "reboot"})).status_code == 200
    # отчет ушел до SIGTERM, а дошел после предупреждения
    assert (await client.post("/api/agent/report", headers=hdr, json=body)).status_code == 200
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        rep = (await s.get(Server, sid)).last_report
        assert rep[going_down.KEY]["kind"] == "reboot"
        # агент так и отчитывается дальше (перезапустили его, а не сервер): отметка уходит
        rep = dict(rep)
        rep[going_down.KEY] = going_down.make("reboot", datetime.now(timezone.utc) - timedelta(minutes=1))
        (await s.get(Server, sid)).last_report = rep
        await s.commit()
    assert (await client.post("/api/agent/report", headers=hdr, json=body)).status_code == 200
    async with f() as s:
        assert going_down.KEY not in (await s.get(Server, sid)).last_report


async def test_sites_of_a_server_away_for_a_reboot_wait(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    seen = now - timedelta(minutes=8)
    async with factory() as s:
        srv = _server("hw-a", seen, last_report={"uptime_seconds": 86400} | _gd("reboot", seen))
        s.add(srv)
        await s.commit()
        c = Check(name="example.com", type="http", target="https://example.com", probe_local=True,
                  probe_server_id=srv.id)
        s.add(c)
        await s.commit()
        cid = c.id
    sent = _capture(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    await collector._send_alerts(factory, settings, [
        ("bad", "example.com", "down", "сервер не принимает соединение", None, cid, None)], now)
    assert not sent, [str(m) for m in sent]
    await engine.dispose()
