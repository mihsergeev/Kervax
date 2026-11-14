"""Bot reminders own queue jobs, never invent delivery receipts or orphan alerts."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import alert_delivery, alert_delivery_actions, settings_store, tg_bot
from app.config import Settings
from app.db import create_engine_and_factory
from app.models import AlertEvent, NotificationDelivery, NotificationEvent, Server, TgAlert, User
from tests.test_alert_delivery import configuration, database, http, lost_response  # noqa: F401

NOW = datetime(2026, 11, 15, 16, tzinfo=timezone.utc)
SETTINGS = Settings()


async def setup(factory, *, messages=True, held=False, kind="offline", users=None):
    cfg = configuration(tg_buttons=True, tz="Europe/Moscow")
    async with factory() as session:
        await settings_store.set_raw(session, settings_store.ALERTS_KEY, json.dumps(cfg))
        await settings_store.set_tg_poll(session, {"status": "ok"})
        await settings_store.set_escalation(session, {"enabled": True, "after_min": 15, "repeat": 2,
                                                     "chat": "" if users else "-200", "users": users or [],
                                                     "kinds": ["offline", "down", "listen_gone"]})
        server = Server(name="node", token_hash="fixture", enabled=True, alert_state={kind: 1})
        session.add(server)
        await session.flush()
        row = TgAlert(ref=f"srv:{server.id}", kind=kind, target="node", section="servers", grp="fixture",
                      text="🔴 node: недоступен", ts=NOW - timedelta(minutes=20),
                      messages=[{"chat": "-300", "id": 71}] if messages else [],
                      undo={"held": "-100", "keys": ["offline"]} if held else None)
        session.add(row)
        await session.commit()
        return cfg, row.id, server.id


async def values(factory, model):
    async with factory() as session:
        return list(await session.scalars(select(model).order_by(model.id)))


@pytest.fixture
def paused_worker(monkeypatch):
    original = alert_delivery.drain

    async def paused(*args, **kwargs):
        return None

    monkeypatch.setattr(alert_delivery, "drain", paused)
    return original


@pytest.mark.parametrize("kind", ["offline", "down", "listen_gone"])
async def test_no_initial_receipt_means_no_escalation_or_held_copy(database, http, kind):
    factory, _url = database
    _cfg, _aid, _sid = await setup(factory, messages=False, held=True, kind=kind)
    assert await tg_bot.escalate(factory, SETTINGS, NOW) == 0
    assert await tg_bot.dup_catchup(factory, SETTINGS, NOW) == 0
    assert http.sent() == []
    assert await values(factory, NotificationEvent) == []


async def test_escalation_admission_updates_original_row_without_inventing_receipts(database, paused_worker):
    factory, _url = database
    _cfg, aid, _sid = await setup(factory)
    assert await tg_bot.escalate(factory, SETTINGS, NOW) == 1
    event = (await values(factory, NotificationEvent))[0]
    assert event.tg_alert_id == aid
    assert event.payload["skip_history"] is True
    assert event.payload["action"] == {"row_id": aid, "kind": "escalation", "level": 1}
    row = (await values(factory, TgAlert))[0]
    assert row.esc_level == 1 and row.messages == [{"chat": "-300", "id": 71}]
    assert tg_bot._aware(row.esc_at) == NOW
    assert await tg_bot.escalate(factory, SETTINGS, NOW + timedelta(seconds=1)) == 0
    assert len(await values(factory, NotificationEvent)) == 1
    assert await values(factory, AlertEvent) == []


async def test_held_admission_keeps_recovery_route_intent_and_original_receipts(database, paused_worker):
    factory, _url = database
    _cfg, aid, _sid = await setup(factory, held=True)
    async with factory() as session:
        row = await session.get(TgAlert, aid)
        row.target = "node-a, node-b"
        await session.commit()
    assert await tg_bot.dup_catchup(factory, SETTINGS, NOW) == 1
    row = (await values(factory, TgAlert))[0]
    assert "held" not in row.undo and row.undo["keys"] == ["offline"]
    assert row.messages == [{"chat": "-300", "id": 71}]
    async with factory() as session:
        assert await settings_store.get_main_passed(session) == [["offline", "node-a"], ["offline", "node-b"]]
    assert await tg_bot.dup_catchup(factory, SETTINGS, NOW + timedelta(seconds=1)) == 0
    event = (await values(factory, NotificationEvent))[0]
    assert event.tg_alert_id == aid and event.payload["action"]["kind"] == "held"


async def test_concurrent_escalation_pollers_share_one_owned_action(database, paused_worker):
    factory, url = database
    _cfg, _aid, _sid = await setup(factory)
    engine, other = create_engine_and_factory(url)
    try:
        await asyncio.gather(tg_bot.escalate(factory, SETTINGS, NOW), tg_bot.escalate(other, SETTINGS, NOW))
    finally:
        await engine.dispose()
    assert len(await values(factory, NotificationEvent)) == 1
    assert len(await values(factory, NotificationDelivery)) == 1
    assert (await values(factory, TgAlert))[0].esc_level == 1


@pytest.mark.parametrize("change", ["acked", "resolved", "muted", "gone", "no_receipt", "old_level"])
async def test_worker_check_rejects_actions_that_no_longer_apply(database, paused_worker, change):
    factory, _url = database
    _cfg, aid, sid = await setup(factory)
    await tg_bot.escalate(factory, SETTINGS, NOW)
    action = (await values(factory, NotificationEvent))[0].payload["action"]
    async with factory() as session:
        row = await session.get(TgAlert, aid)
        if change == "acked":
            row.acked_by = "operator"
        elif change == "resolved":
            row.resolved_at = NOW
        elif change == "muted":
            row.undo = {"mute": {"type": "snooze"}}
        elif change == "no_receipt":
            row.messages = []
        elif change == "old_level":
            row.esc_level = 2
        else:
            (await session.get(Server, sid)).alert_state = {"offline": 0}
        await session.commit()
    async with factory() as session:
        assert not await alert_delivery_actions.wanted(session, action, NOW)


async def test_escalation_uncertainty_retries_original_action_and_callback_row(database, http):
    factory, _url = database
    cfg, aid, _sid = await setup(factory)
    http.answer = lost_response
    assert await tg_bot.escalate(factory, SETTINGS, NOW) == 1
    assert (await values(factory, NotificationDelivery))[0].status == "uncertain"
    assert (await values(factory, TgAlert))[0].messages == [{"chat": "-300", "id": 71}]
    await tg_bot.escalate(factory, SETTINGS, NOW + timedelta(seconds=1))
    assert len(http.sent()) == 1 and len(await values(factory, NotificationEvent)) == 1
    http.answer = None
    await alert_delivery.drain(factory, cfg, NOW + timedelta(minutes=15))
    row = (await values(factory, TgAlert))[0]
    assert row.id == aid and len(row.messages) == 2
    assert row.messages[0] == {"chat": "-300", "id": 71}
    assert row.messages[1]["chat"] == "-200"
    assert len(await values(factory, TgAlert)) == 1
    assert await values(factory, AlertEvent) == []


async def test_held_uncertainty_retains_main_intent_and_does_not_enqueue_again(database, http):
    factory, _url = database
    _cfg, aid, _sid = await setup(factory, held=True)
    http.answer = lost_response
    assert await tg_bot.dup_catchup(factory, SETTINGS, NOW) == 1
    assert (await values(factory, NotificationDelivery))[0].status == "uncertain"
    assert await tg_bot.dup_catchup(factory, SETTINGS, NOW + timedelta(seconds=1)) == 0
    assert len(http.sent()) == 1 and len(await values(factory, NotificationEvent)) == 1
    async with factory() as session:
        assert await settings_store.get_main_passed(session) == [["offline", "node"]]
        assert (await session.get(TgAlert, aid)).messages == [{"chat": "-300", "id": 71}]


async def test_ack_after_admission_stops_worker_post(database, http, paused_worker, monkeypatch):
    factory, _url = database
    cfg, aid, _sid = await setup(factory)
    await tg_bot.escalate(factory, SETTINGS, NOW)
    async with factory() as session:
        (await session.get(TgAlert, aid)).acked_by = "operator"
        await session.commit()
    monkeypatch.setattr(alert_delivery, "drain", paused_worker)
    await alert_delivery.drain(factory, cfg, NOW)
    assert http.sent() == []
    assert (await values(factory, NotificationDelivery))[0].status in ("rejected", "superseded")


async def test_escalation_preserves_common_bot_only_personal_recipient_rule(database, paused_worker):
    factory, _url = database
    await setup(factory, users=["common-user", "own-bot-user"])
    async with factory() as session:
        session.add_all([User(username="common-user", password_hash="fixture", tg_chat_id="775"),
                         User(username="own-bot-user", password_hash="fixture", tg_chat_id="996",
                              tg_token="fixture-own-bot")])
        await session.commit()
    assert await tg_bot.escalate(factory, SETTINGS, NOW) == 1
    assert [job.target["chat"] for job in await values(factory, NotificationDelivery)] == ["775"]
