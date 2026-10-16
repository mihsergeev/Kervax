"""Подключение нового сервера (app/onboard.py): пока не закрыто, алертом уходят только офлайн и
критичный диск; при закрытии то, что было, становится известной проблемой."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, onboard
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import AuditLog, Server


async def _panel(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'k.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


def _disk(pct: int) -> dict:
    return {"disks": [{"mount": "/", "used": pct, "total": 100}]}


def _srv(name: str, now: datetime, **kw) -> Server:
    base = dict(
        name=name, token_hash=name, enabled=True, backup_not_required=True,
        last_seen=now, last_report=_disk(86), cpu_alert_percent=0, mem_alert_percent=0,
        disk_warn_percent=85, disk_alert_percent=90, disk_crit_percent=95,
        alert_sustain_seconds=0, offline_after_seconds=120,
        created_at=now - timedelta(hours=1), first_report_at=now - timedelta(hours=1),
    )
    base.update(kw)
    return Server(**base)


def _sender(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    return sent


async def _set(factory, sid, **kw):
    async with factory() as s:
        srv = await s.get(Server, sid)
        for k, v in kw.items():
            setattr(srv, k, v)
        await s.commit()


def test_active_and_destinations():
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    s = Server(name="web-a", created_at=now - timedelta(days=10), first_report_at=now - timedelta(days=1))
    assert not onboard.active(s, now)  # заведен не через панель: метки нет
    s.alert_state = {"onboard": 1}
    assert onboard.active(s, now)  # отсчет с первого отчета агента, а не с заведения
    assert not onboard.active(s, now + timedelta(days=2, minutes=1))
    s.onboard_done_at = now
    assert not onboard.active(s, now)
    cfg = {"telegram_token": "t", "telegram_chat": "1",
           "routes": {"main": "rest", "chats": [{"name": "Инфра", "chat": "2", "server_groups": ["infra"]}]}}
    assert onboard.destinations(cfg, "infra") == {"tg": True, "main": False, "chats": ["Инфра"]}
    assert onboard.destinations(cfg, "") == {"tg": True, "main": True, "chats": []}
    assert onboard.destinations({}, "infra")["tg"] is False


async def test_onboarding_holds_what_was_there_and_turns_it_known(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        # web-a на подключении, web-b обычный: у обоих диск 86% и свежая перезагрузка
        s.add(_srv("web-a", now, onboard_done_at=None, alert_state={"onboard": 1}, rebooted_at=now))
        s.add(_srv("web-b", now, rebooted_at=now))
        await s.commit()
    sent = _sender(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    assert sent and all("web-b" in m for m in sent)  # от web-a ничего
    async with factory() as s:
        a = await s.get(Server, 1)
        assert not a.alert_state.get("disk")          # диск придержан без записи состояния
        assert a.alert_state.get("disk_from")         # но виден в "Что сломано"
        assert a.alert_state.get("reboot_at")         # событие отмечено, как будто ушло
    sent.clear()
    # закрыли подключение: диск стал известной проблемой, перезагрузка не пришла задним числом
    await _set(factory, 1, onboard_done_at=now, onboard_by="admin", last_seen=now + timedelta(minutes=1))
    await _set(factory, 2, last_seen=now + timedelta(minutes=1))
    await collector.evaluate_servers(factory, settings, now + timedelta(minutes=1))
    assert sent == []
    async with factory() as s:
        a = await s.get(Server, 1)
        e = a.known_issues["disk"]
        assert e["text"] == onboard.KNOWN_TEXT and e["by"] == "admin" and e["onboard"] is True
        assert not a.alert_state.get("onboard")
    # стало хуже - алерт как обычно
    await _set(factory, 1, last_seen=now + timedelta(minutes=2), last_report=_disk(92))
    await _set(factory, 2, last_seen=now + timedelta(minutes=2))
    await collector.evaluate_servers(factory, settings, now + timedelta(minutes=2))
    assert len(sent) == 1 and "web-a" in sent[0] and "92%" in sent[0]
    await engine.dispose()


async def test_onboarding_still_sends_offline_and_critical_disk(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(_srv("web-a", now, onboard_done_at=None, alert_state={"onboard": 1}, last_report=_disk(97)))
        await s.commit()
    sent = _sender(monkeypatch)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "97%" in sent[0]
    # замолчал: недоступен уходит и во время подключения
    later = now + timedelta(minutes=30)
    await collector.evaluate_servers(factory, settings, later)
    assert len(sent) == 2 and "web-a" in sent[1]
    async with factory() as s:
        assert (await s.get(Server, 1)).alert_state.get("offline") == 1
    await engine.dispose()


async def test_onboarding_closes_itself(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(_srv("web-a", now, onboard_done_at=None, alert_state={"onboard": 1},
                   created_at=now - timedelta(days=4), first_report_at=now - onboard.ONBOARD_MAX - timedelta(minutes=1)))
        await s.commit()
    sent = _sender(monkeypatch)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    assert sent == []
    async with factory() as s:
        a = await s.get(Server, 1)
        assert a.known_issues["disk"]["by"] == "авто"
        assert a.onboard_done_at is not None and a.onboard_by == "авто"
        log = list(await s.scalars(select(AuditLog).where(AuditLog.action == "onboard_done")))
        assert log and log[0].detail == "disk"
    await engine.dispose()


async def test_known_reminders_of_one_server_come_together(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=8)).isoformat()
    ent = {"text": onboard.KNOWN_TEXT, "by": "авто", "at": old, "until": None}
    async with factory() as s:
        rep = {"disks": [{"mount": "/", "used": 86, "total": 100, "inodes": 100, "inodes_used": 87}]}
        s.add(_srv("web-a", now, last_report=rep, known_issues={
            "disk": ent | {"level": 1}, "inode": ent | {"level": 1}}))
        await s.commit()
    sent = _sender(monkeypatch)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    reminds = [m for m in sent if "📌" in m]
    assert len(reminds) == 1 and "8 дн" in reminds[0]
    await engine.dispose()


async def test_create_opens_onboarding_and_done_closes_it(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "web-new"}, headers=auth_headers)
    assert r.status_code == 201, r.text
    srv = r.json()["server"]
    sid = srv["id"]
    assert srv["onboard_done_at"] is None
    # до первого отчета отсчет идет с заведения
    assert srv["onboard"] and srv["onboard"]["until"]
    r = await client.get("/api/servers", headers=auth_headers)
    row = next(x for x in r.json() if x["id"] == sid)
    assert row["onboard"]["tg"] in (True, False) and "chats" in row["onboard"]
    r = await client.post(f"/api/servers/{sid}/onboard/done", headers=auth_headers)
    assert r.status_code == 200 and r.json()["onboard"] is None and r.json()["onboard_by"]
