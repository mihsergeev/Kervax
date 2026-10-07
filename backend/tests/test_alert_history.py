"""История алертов: пишется только доставленное, отдается под права учетки."""

import os
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app import alerts
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent

CFG = {"telegram_token": "t", "telegram_chat": "c"}


async def _factory(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'h.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


async def test_only_delivered_alerts_go_to_history(tmp_path, monkeypatch):
    """Недоставленное повторится на следующем тике и запишется тогда: писать его сейчас -
    это два одинаковых события в истории на одно сообщение в чате."""
    engine, factory = await _factory(tmp_path)

    async def fake_send(cfg, text, parse_mode=None):
        return ["Telegram: timeout"] if "web-02" in text else []

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
    ok = await alerts.dispatch(CFG, True, msgs, 0, parse_mode="HTML", session_factory=factory)
    assert ok is False  # второй не ушел - коллектор повторит
    async with factory() as s:
        rows = list(await s.scalars(select(AlertEvent)))
    assert [(r.kind, r.target, r.grp, r.recovery) for r in rows] == [("disk", "web-01", "prod", False)]
    assert rows[0].text == "🔴 web-01 - диск 91% > 90%"  # без разметки, как видит человек

    # дайджестом ушло - в истории каждое сообщение по отдельности
    ok = await alerts.dispatch(CFG, True, [msgs[0], msgs[0]], 2, session_factory=factory)
    async with factory() as s:
        assert len(list(await s.scalars(select(AlertEvent)))) == 3
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
    assert d["summary"]["targets"][0] == {"target": "db-01", "n": 2}
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
