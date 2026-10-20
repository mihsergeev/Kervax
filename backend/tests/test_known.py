"""Известные проблемы: алерт молчит, пока не стало хуже, отметка сама закрывается, когда
проблема ушла, по сроку снимается, раз в неделю напоминает о себе."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, known
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import AuditLog, Check, Server

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _entry(**kw):
    e = {"text": "диск под замену", "by": "admin", "at": NOW.isoformat(), "until": None}
    e.update(kw)
    return e


# ---------------------------------------------------------------- логика


def test_covers_level_and_sig():
    assert known.covers(_entry(), 2)  # снимка еще нет - молчим
    e = _entry(level=1, sig=["sda"])
    assert known.covers(e, 1, ["sda"])
    assert not known.covers(e, 2, ["sda"])         # стало хуже
    assert not known.covers(e, 1, ["sda", "sdb"])  # сломалось новое
    assert not known.covers(e, 0)                  # отбой не глушим
    assert not known.covers(None, 1)


def test_step_snapshot_close_expire():
    new, what = known.step(_entry(), 1, ["sda"], NOW)
    assert new["level"] == 1 and new["sig"] == ["sda"] and new["worse"] is False and what == ""
    new2, _ = known.step(new, 2, ["sda"], NOW)
    assert new2["worse"] is True and new2["level"] == 1
    # ушла: сначала отсчет, через 10 минут без возврата - закрыть
    g, what = known.step(new, 0, None, NOW)
    assert g["gone_at"] and what == ""
    back, _ = known.step(g, 1, ["sda"], NOW + timedelta(minutes=5))
    assert "gone_at" not in back
    assert known.step(g, 0, None, NOW + timedelta(minutes=11)) == (None, "closed")
    # срок вышел
    e = _entry(until=(NOW - timedelta(minutes=1)).isoformat())
    assert known.step(e, 1, None, NOW) == (None, "expired")


def test_remind_weekly():
    e = _entry(at=(NOW - timedelta(days=8)).isoformat(), level=1)
    new, what = known.step(e, 1, None, NOW)
    assert what == "remind" and new["reminded_at"] == NOW.isoformat()
    assert known.step(new, 1, None, NOW + timedelta(days=1))[1] == ""
    assert known.remind_text("Диск", new, NOW) == "известная проблема висит 8 дн: Диск - диск под замену (admin)"


def test_step_check():
    e = _entry()
    new, what = known.step_check(e, True, NOW)
    assert new["gone_at"] and what == ""
    assert known.step_check(new, True, NOW + timedelta(minutes=11)) == (None, "closed")
    again, _ = known.step_check(new, False, NOW + timedelta(minutes=3))
    assert "gone_at" not in again


# ---------------------------------------------------------------- API


async def test_server_known_api(client, auth_headers):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        srv = Server(name="backup-a", token_hash="k1", alert_state={"disk_from": NOW.isoformat()})
        s.add(srv)
        await s.commit()
        sid = srv.id
    r = await client.put(f"/api/servers/{sid}/known/disk", json={"text": "чистим", "days": 3},
                         headers=auth_headers)
    assert r.status_code == 200, r.text
    e = r.json()["known_issues"]["disk"]
    assert e["text"] == "чистим" and e["by"] and e["until"]
    # проблемы этого вида сейчас нет / недоступность так не отмечают
    assert (await client.put(f"/api/servers/{sid}/known/cpu", json={"text": "x"},
                             headers=auth_headers)).status_code == 400
    assert (await client.put(f"/api/servers/{sid}/known/offline", json={"text": "x"},
                             headers=auth_headers)).status_code == 400
    r = await client.delete(f"/api/servers/{sid}/known/disk", headers=auth_headers)
    assert r.status_code == 200 and r.json()["known_issues"] is None


async def test_check_known_api(client, auth_headers):
    r = await client.post("/api/checks", json={"name": "site", "type": "http", "target": "a.example"},
                          headers=auth_headers)
    cid = r.json()["id"]
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        (await s.get(Check, cid)).last_status = "up"
        await s.commit()
    assert (await client.put(f"/api/checks/{cid}/known", json={"text": "x"},
                             headers=auth_headers)).status_code == 400
    async with f() as s:
        (await s.get(Check, cid)).last_status = "down"
        await s.commit()
    r = await client.put(f"/api/checks/{cid}/known", json={"text": "чинит подрядчик"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["known_issue"]["text"] == "чинит подрядчик"
    assert r.json()["known_issue"]["until"] is None
    r = await client.delete(f"/api/checks/{cid}/known", headers=auth_headers)
    assert r.json()["known_issue"] is None


# ---------------------------------------------------------------- цикл алертов серверов


async def _panel(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'k.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


def _disk(pct: int) -> dict:
    return {"disks": [{"mount": "/", "used": pct, "total": 100}]}


async def _set(factory, **kw):
    async with factory() as s:
        srv = await s.get(Server, 1)
        for k, v in kw.items():
            setattr(srv, k, v)
        await s.commit()


async def test_server_known_silences_until_worse_then_closes(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(
            name="backup-a", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now, last_report=_disk(86), cpu_alert_percent=0, mem_alert_percent=0,
            disk_warn_percent=85, disk_alert_percent=90, disk_crit_percent=95,
            alert_sustain_seconds=0, offline_after_seconds=120,
            known_issues={"disk": _entry(at=now.isoformat())},
            # выше порога давно: выдержка диска (app/fill_alert.py) уже прошла
            alert_state={"disk_since": (now - timedelta(hours=1)).isoformat()},
        ))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    async def tick(t, **kw):
        if kw:
            await _set(factory, last_seen=t, **kw)
        else:
            await _set(factory, last_seen=t)
        await collector.evaluate_servers(factory, settings, t)

    await tick(now)
    assert sent == []  # известная - молчим
    async with factory() as s:
        e = (await s.get(Server, 1)).known_issues["disk"]
        assert e["level"] == 1 and e["worse"] is False
    # стало хуже: 92% - уже "проблема", алерт уходит
    await tick(now + timedelta(minutes=1), last_report=_disk(92))
    assert len(sent) == 1 and "92%" in sent[0]
    async with factory() as s:
        assert (await s.get(Server, 1)).known_issues["disk"]["worse"] is True
    # ушла совсем: отметка ждет 10 минут и закрывается, отбой (алерт же уходил) - после
    # получаса ниже линии отбоя
    await tick(now + timedelta(minutes=2), last_report=_disk(40))
    assert len(sent) == 1
    await tick(now + timedelta(minutes=13))
    async with factory() as s:
        assert (await s.get(Server, 1)).known_issues is None
        log = list(await s.scalars(select(AuditLog).where(AuditLog.action == "known_closed")))
        assert log and log[0].username == "авто"
    await tick(now + timedelta(minutes=33))
    assert len(sent) == 2 and "снова в норме" in sent[1]
    # та же беда снова - уже обычный алерт, с выдержкой
    await tick(now + timedelta(minutes=34), last_report=_disk(86))
    assert len(sent) == 2
    await tick(now + timedelta(minutes=65))
    assert len(sent) == 3
    await engine.dispose()


async def test_server_known_expired_and_reminder(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(
            name="backup-a", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now, last_report=_disk(86), cpu_alert_percent=0, mem_alert_percent=0,
            disk_warn_percent=85, disk_alert_percent=90, disk_crit_percent=95,
            alert_sustain_seconds=0, offline_after_seconds=120,
            known_issues={"disk": _entry(at=(now - timedelta(days=8)).isoformat(), level=1)},
            alert_state={"disk_since": (now - timedelta(days=8)).isoformat()},
        ))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    # висит неделю - напоминание, сам алерт молчит
    assert len(sent) == 1 and "📌" in sent[0] and "известная проблема висит 8 дн" in sent[0]
    # срок вышел, проблема осталась - алерт как обычно
    await _set(factory, last_seen=now + timedelta(minutes=1), known_issues={
        "disk": _entry(level=1, until=now.isoformat())})
    await collector.evaluate_servers(factory, settings, now + timedelta(minutes=1))
    assert len(sent) == 2 and "86%" in sent[1]
    async with factory() as s:
        assert (await s.get(Server, 1)).known_issues is None
    await engine.dispose()


# ---------------------------------------------------------------- мониторы


async def test_check_known_skips_down_alert_and_sends_reminder(client, monkeypatch):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        a = Check(name="site-a", type="http", target="a.example", known_issue=_entry())
        b = Check(name="site-b", type="http", target="b.example")
        s.add_all([a, b])
        await s.commit()
        ia, ib = a.id, b.id
    sent = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    now = datetime.now(timezone.utc)
    pending = [("bad", "site-a", "down", "HTTP 502", None, ia, None),
               ("bad", "site-b", "down", "HTTP 502", None, ib, None),
               ("known", "site-a", "down", "известная проблема висит 7 дн: HTTP 502", None, ia, None, "📌")]
    await collector._send_alerts(f, Settings(alert_webhook="http://hook"), pending, now)
    assert len(sent) == 2
    # про site-a с известной проблемой сбой не пришел, про site-b - пришел
    assert "b.example" in sent[0] and not any("a.example" in m for m in sent)
    assert sent[1].startswith("📌 <b>site-a</b>") and sent[1].kind == "known" and not sent[1].ref


async def test_check_known_closes_after_recovery(client):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        c = Check(name="site-a", type="http", target="a.example", known_issue=_entry(at=now.isoformat()),
                  last_status="down")
        s.add(c)
        await s.commit()
        cid = c.id
    from app.checks import CheckOutcome
    for minutes in (0, 11):
        async with f() as s:
            row = await s.get(Check, cid)
            await collector.record_outcome(s, row, CheckOutcome("up", message="HTTP 200"),
                                           now + timedelta(minutes=minutes), [])
            await s.commit()
    async with f() as s:
        assert (await s.get(Check, cid)).known_issue is None
        assert (await s.scalars(select(AuditLog).where(AuditLog.action == "known_closed"))).first()


async def test_bot_status_marks_known(client):
    from app import tg_bot
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        s.add(Server(name="backup-a", token_hash="kb", enabled=True, alert_state={"disk": 2},
                     known_issues={"disk": _entry(level=2)}))
        s.add(Check(name="site-a", type="http", target="a.example", last_status="down", known_issue=_entry()))
        await s.commit()
        text = await tg_bot.status_text(s, None, datetime.now(timezone.utc))
    assert "<b>backup-a</b>: 📌 " in text and "📌 site-a" in text


async def test_server_known_kept_while_node_silent(tmp_path, monkeypatch):
    """Нода замолчала: условий по диску нет, но это не значит, что проблема ушла."""
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(
            name="backup-a", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now - timedelta(hours=1), last_report=_disk(86), cpu_alert_percent=0,
            mem_alert_percent=0, disk_warn_percent=85, disk_alert_percent=90, disk_crit_percent=95,
            alert_sustain_seconds=0, offline_after_seconds=120,
            known_issues={"disk": _entry(level=1, at=now.isoformat())},
        ))
        await s.commit()

    async def fake_send(cfg, text, parse_mode=None):
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    for minutes in (0, 11, 30):
        await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"),
                                         now + timedelta(minutes=minutes))
    async with factory() as s:
        assert (await s.get(Server, 1)).known_issues["disk"]["text"] == "диск под замену"
    await engine.dispose()
