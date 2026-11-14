"""История алертов: пишется только доставленное, отдается под права учетки."""

import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app import alert_delivery, alerts
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent, NotificationDelivery

CFG = {"telegram_token": "t", "telegram_chat": "c"}


async def _factory(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'h.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


async def test_only_delivered_alerts_go_to_history(tmp_path, monkeypatch):
    """Admission never writes history; confirmation writes each transition once."""
    engine, factory = await _factory(tmp_path)
    now = datetime.now(timezone.utc)
    failed = True
    sent = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        errors = alerts.SendErrors()
        if failed and "web-02" in text:
            errors.append("Telegram: timeout")
            errors.uncertain = True
        else:
            errors.receipts.append({"chat": "c", "id": len(sent)})
        return errors

    async def no_personal(*a, **kw):
        return None

    monkeypatch.setattr(alerts, "send_alert", fake_send)
    monkeypatch.setattr(alerts, "send_personal", no_personal)
    msgs = [
        alerts.Msg('🔴 <a href="https://p/?server=1">web-01</a> - диск 91% &gt; 90%', "servers", "prod",
                   kind="disk", target="web-01"),
        alerts.Msg("✅ web-02 - CPU снова в норме", "servers", "", kind="cpu", target="web-02",
                   recovery=True),
    ]
    accepted = set()
    ok = await alerts.dispatch(CFG, True, msgs, 0, parse_mode="HTML", session_factory=factory,
                               accepted=accepted, now=now)
    assert ok is False and accepted == {0, 1}
    async with factory() as s:
        rows = list(await s.scalars(select(AlertEvent)))
    assert [(r.kind, r.target, r.grp, r.recovery) for r in rows] == [("disk", "web-01", "prod", False)]
    assert rows[0].text == "🔴 web-01 - диск 91% > 90%"  # без разметки, как видит человек
    async with factory() as s:
        assert sorted(await s.scalars(select(NotificationDelivery.status))) == ["confirmed", "uncertain"]

    # Reobserving the same transition never creates two extra history records.
    assert await alerts.dispatch(CFG, True, [msgs[0], msgs[0]], 2, session_factory=factory, now=now)
    async with factory() as s:
        assert len(list(await s.scalars(select(AlertEvent)))) == 1
    assert len(sent) == 2
    failed = False
    later = now + timedelta(minutes=16)
    await alert_delivery.drain(factory, CFG, later)
    async with factory() as s:
        assert len(list(await s.scalars(select(AlertEvent)))) == 2
    # A digest containing two genuinely new incidents still records both atoms.
    new = [alerts.Msg(f"🔴 web-{number} - память", "servers", kind="mem", target=f"web-{number}")
           for number in (3, 4)]
    assert await alerts.dispatch(CFG, True, new, 2, session_factory=factory, now=later)
    async with factory() as s:
        assert len(list(await s.scalars(select(AlertEvent)))) == 4
    assert len(sent) == 4
    await engine.dispose()


async def test_history_is_cut_by_the_users_scope(client: httpx.AsyncClient, auth_headers):
    """Учетка видит в истории только то, что видит в панели: свои разделы и группы."""
    _, factory = create_engine_and_factory(os.environ["KERVAX_DB_URL"])
    await alerts.record_history(factory, [
        alerts.Msg("🔴 shop - недоступен", "sites", "prod", kind="down", target="shop"),
        alerts.Msg("✅ shop - снова доступен", "sites", "prod", kind="down", target="shop", recovery=True),
        alerts.Msg("⚠️ db-01 - диск 91%", "servers", "infra", kind="disk", target="db-01"),
        alerts.Msg("⚠️ db-01 - диск 93%", "servers", "infra", kind="disk", target="db-01"),
        alerts.Msg("🧩 новое без покрытия", "servers", "", kind="uncovered"),
        alerts.Msg("общее сообщение без раздела", "", ""),
    ])

    r = await client.get("/api/alert-history", headers=auth_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["events"]) == 6 and d["summary"]["fires"] == 5 and d["summary"]["recoveries"] == 1
    # больше всего шумит db-01: сводка считает срабатывания, отбои не шум
    assert d["summary"]["targets"][0] == {"target": "db-01", "section": "servers", "n": 2,
                                          "srv_id": None, "chk_id": None}
    # что шумело - что именно и где: два "диск" на db-01 одной строкой, отбой не шум, сообщение
    # без объекта (про саму панель) не беда объекта
    items = {(x["target"], x["kind"]): x for x in d["summary"]["items"]}
    assert items[("db-01", "disk")]["n"] == 2 and items[("shop", "down")]["n"] == 1
    assert len(items) == 2 and d["summary"]["more_items"] == 0
    assert d["summary"]["kinds"][0]["kind"] == "disk" and d["summary"]["kinds"][0]["label"] == "Диск"
    datetime.fromisoformat(d["events"][0]["ts"]).astimezone(timezone.utc)

    r = await client.get("/api/alert-history?target=shop&only=recoveries", headers=auth_headers)
    assert [e["text"] for e in r.json()["events"]] == ["✅ shop - снова доступен"]

    r = await client.post("/api/users", headers=auth_headers, json={
        "username": "siteview", "password": "Str0ng-pass-123!", "role": "viewer", "sections": ["sites"],
    })
    assert r.status_code in (200, 201), r.text
    r = await client.post("/api/auth/login", json={"username": "siteview", "password": "Str0ng-pass-123!"})
    hdr = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.get("/api/alert-history", headers=hdr)
    assert r.status_code == 200, r.text
    # сайты свои, сводка "без покрытия" - про серверы, общее сообщение видно всем
    assert sorted(e["target"] or e["text"] for e in r.json()["events"]) == [
        "shop", "shop", "общее сообщение без раздела"]
