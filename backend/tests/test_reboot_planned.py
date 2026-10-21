"""Перезагрузка: алерт только на неожиданную. После обновлений ее ждали (агент до перезагрузки
показывал reboot-required или ядро новее работающего), в окне обслуживания она плановая: такие
видны только в хронологии."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, os_support, timeline
from app.alerts import full_text
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import MaintenanceWindow, Server, ServerEvent

UPD = {"reboot": True, "reboot_ts": 1, "reboot_pkgs": ["linux-image-6.8.0-147-generic", "linux-base"]}


def _reboots(sent: list) -> list:
    """Алерты о перезагрузке: в чате "Перезагрузился", "перезагружен" - только в полном тексте."""
    return [m for m in sent if getattr(m, "kind", "") == "reboot" or "перезагружен" in full_text(m)]


def test_reboot_why():
    assert os_support.reboot_why({"os_upd": UPD}) == (
        "после обновления ее ждали (linux-image-6.8.0-147-generic, linux-base)")
    assert os_support.reboot_why({"os_upd": {"kernel_new": "7.0.0-38-generic", "kernel": "7.0.0-30-generic"}}) == (
        "ждало новое ядро 7.0.0-38-generic")
    assert os_support.reboot_why({"os_upd": {"reboot": False, "updates": 3}}) == ""
    assert os_support.reboot_why({}) == ""
    at = datetime(2026, 10, 16, 14, 7, tzinfo=timezone.utc)
    rep = {"boot": {"at": at.isoformat(), "why": "ждало новое ядро 7.0.0-38-generic"}}
    assert os_support.planned_reboot(rep, at) == "ждало новое ядро 7.0.0-38-generic"
    assert os_support.planned_reboot(rep, at.replace(tzinfo=None)) == "ждало новое ядро 7.0.0-38-generic"
    # причина от прошлой перезагрузки к новой не относится
    assert os_support.planned_reboot(rep, at + timedelta(hours=1)) == ""
    assert os_support.planned_reboot({"boot": {"at": at.isoformat(), "why": ""}}, at) == ""


async def _reboot(client, auth_headers, name, before):
    """Сервер с отчетом before до перезагрузки, потом два отчета после: аптайм сброшен"""
    r = await client.post("/api/servers", json={"name": name}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}
    base = {"agent_version": "2.41", "mem_used": 1, "mem_total": 2, "disks": [{"mount": "/", "used": 1, "total": 2}]}
    await client.post("/api/agent/report", json=dict(base, uptime_seconds=900000, **before), headers=ah)
    await client.post("/api/agent/report", json=dict(base, uptime_seconds=40), headers=ah)
    # следующий отчет до тика collector'а: причина не теряется
    await client.post("/api/agent/report", json=dict(base, uptime_seconds=55), headers=ah)
    return sid


async def test_reboot_after_updates_is_not_an_alert(client, auth_headers, monkeypatch):
    planned = await _reboot(client, auth_headers, "web-a", {"os_upd": UPD})
    sudden = await _reboot(client, auth_headers, "web-b", {"os_upd": {"reboot": False, "updates": 0}})
    factory = client._transport.app.state.session_factory
    async with factory() as s:
        a, b = await s.get(Server, planned), await s.get(Server, sudden)
        assert a.rebooted_at is not None and a.last_report["boot"]["why"].startswith("после обновления ее ждали")
        assert b.rebooted_at is not None and b.last_report["boot"]["why"] == ""
        for x in (a, b):  # подключение закрыто: события на подключении молча двигают отметку
            x.onboard_done_at, x.alert_state = datetime.now(timezone.utc), {}
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    now = datetime.now(timezone.utc)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    rb = _reboots(sent)
    assert len(rb) == 1 and "web-b" in rb[0] and "web-a" not in rb[0], sent
    assert rb[0].startswith("⚠️🔄 web-b\n<b>Перезагрузился</b>") and "перезагружен" in full_text(rb[0])
    async with factory() as s:
        a = await s.get(Server, planned)
        assert a.alert_state.get("reboot_at")  # отмечена, потом не придет
    sent.clear()
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now + timedelta(minutes=1))
    assert not _reboots(sent)


def test_timeline_says_planned():
    t = datetime(2026, 10, 18, 13, 19, tzinfo=timezone.utc)
    row = type("R", (), {"last_seen": t, "rebooted_at": t, "os": "Ubuntu", "agent_version": "2.41"})()
    prev = timeline.snapshot(type("R", (), {"last_seen": t - timedelta(hours=1), "rebooted_at": t - timedelta(days=9),
                                            "os": "Ubuntu", "agent_version": "2.41"})(), {})
    cur = timeline.snapshot(row, {"boot": {"at": t.isoformat(), "why": "ждало новое ядро 7.0.0-38-generic"}})
    ev, _ = timeline.diff(prev, cur, {})
    assert [e["text"] for e in ev if e["kind"] == "reboot"] == [
        "плановая перезагрузка: ждало новое ядро 7.0.0-38-generic"]
    cur = timeline.snapshot(row, {})
    ev, _ = timeline.diff(prev, cur, {})
    assert [e["text"] for e in ev if e["kind"] == "reboot"] == ["перезагрузка"]


async def test_reboot_in_maintenance_window_stays_silent_after_it(tmp_path, monkeypatch):
    """Сервер в окне обслуживания молчит, а "перезагружен" раньше приходил сразу после окна."""
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(id=1, name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=0, disk_alert_percent=0,
                     disk_crit_percent=0, rebooted_at=now - timedelta(minutes=3), last_report={"uptime_seconds": 180}))
        s.add(MaintenanceWindow(title="ядро", server_ids=[1], starts_at=now - timedelta(minutes=30),
                                ends_at=now + timedelta(minutes=10), verify=False))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, cfg, now)
    await collector.evaluate_servers(factory, cfg, now + timedelta(minutes=20))  # окно кончилось
    assert not _reboots(sent), sent
    async with factory() as s:
        assert (await s.execute(select(Server))).scalar_one().alert_state.get("reboot_at")
        assert ServerEvent  # хронология пишет перезагрузку сама (timeline.track)
    await engine.dispose()
