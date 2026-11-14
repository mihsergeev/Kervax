"""Site observer state and group membership commit with durable ownership."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import alert_delivery, alerts, checks, collector, parents, site_group
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent, Check, CheckIncident, NotificationDelivery, NotificationEvent

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
SETTINGS = Settings(alert_telegram_token="fixture-token", alert_telegram_chat="100",
                    panel_url="https://panel.example.com")
REAL_DRAIN = alert_delivery.drain


@pytest.fixture
async def panel(tmp_path, monkeypatch):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'site.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def lookup(_host):
        return frozenset({"203.0.113.7"})

    async def offline(*_args, **_kwargs):
        pass

    monkeypatch.setattr(parents, "_lookup", lookup)
    monkeypatch.setattr(alert_delivery, "drain", offline)
    async with factory() as session:
        rows = [Check(name=f"site-{i}", type="http", target=f"https://site-{i}.example.com",
                      alert_after_failures=1) for i in range(3)]
        session.add_all(rows)
        await session.commit()
        ids = [row.id for row in rows]
    try:
        yield factory, ids
    finally:
        await engine.dispose()


async def fail(factory, ids, now=NOW):
    pending = []
    async with factory() as session:
        for cid in ids:
            row = await session.get(Check, cid)
            await collector.record_outcome(session, row, checks.CheckOutcome("down", message="HTTP 503"),
                                           now, pending)
        await session.commit()
    return pending


async def values(factory, model):
    async with factory() as session:
        return list(await session.scalars(select(model).order_by(model.id)))


async def test_fire_ownership_groups_and_flags_commit_before_transport(panel):
    factory, ids = panel
    pending = await fail(factory, ids[:2])
    await collector._send_alerts(factory, SETTINGS, pending, NOW)
    incidents = await values(factory, CheckIncident)
    assert all(row.notice_queued and not row.notified for row in incidents)
    async with factory() as session:
        group = next(iter((await site_group.load(session)).values()))
        assert set(group["members"]) == set(map(str, ids[:2]))
        assert not group.get("msgs")
        assert all([(await session.get(Check, cid)).flap_state for cid in ids[:2]])
    assert len(await values(factory, NotificationEvent)) == 2
    assert all(row.status == "pending" for row in await values(factory, NotificationDelivery))
    assert await values(factory, AlertEvent) == []


async def test_group_state_and_flags_rollback_with_rejected_admission(panel, monkeypatch):
    factory, ids = panel
    pending = await fail(factory, ids[:2])
    original = alerts.dispatch

    async def broken(*args, **kwargs):
        callback = kwargs["on_accept"]

        async def reject(session, owned=None):
            await callback(session, owned)
            if owned:
                assert (await session.scalar(select(CheckIncident))).notice_queued
                assert await site_group.load(session)
                raise ValueError("observer transaction rejected")

        kwargs["on_accept"] = reject
        return await original(*args, **kwargs)

    monkeypatch.setattr(alerts, "dispatch", broken)
    with pytest.raises(ValueError, match="observer transaction rejected"):
        await collector._send_alerts(factory, SETTINGS, pending, NOW)
    assert await values(factory, NotificationEvent) == []
    assert all(not row.notice_queued and not row.notified for row in await values(factory, CheckIncident))
    async with factory() as session:
        assert await site_group.load(session) == {}
        assert all([not (await session.get(Check, cid)).flap_state for cid in ids[:2]])


async def test_legacy_dispatch_adapter_preserves_group_receipts_and_ownership(panel, monkeypatch):
    factory, ids = panel

    async def legacy(_cfg, _can_send, messages, _threshold, **_kwargs):
        for message in messages:
            message.sent_to = [{"chat": "100", "id": 73}]
        return True

    monkeypatch.setattr(alerts, "dispatch", legacy)
    await collector._send_alerts(factory, SETTINGS, await fail(factory, ids[:2]), NOW)
    async with factory() as session:
        group = next(iter((await site_group.load(session)).values()))
        assert group["msgs"] == [{"chat": "100", "id": 73}]
        assert set(group["members"]) == set(map(str, ids[:2]))
    assert all(row.notice_queued and not row.notified for row in await values(factory, CheckIncident))


async def test_one_neighbour_is_queued_as_full_group_edit_without_fake_history(panel):
    factory, ids = panel
    await collector._send_alerts(factory, SETTINGS, await fail(factory, ids[:1]), NOW)
    receipt = {"chat": "100", "id": 71}
    async with factory() as session:
        groups = await site_group.load(session)
        gid, group = next(iter(groups.items()))
        group["msgs"] = [receipt]
        await site_group.save(session, groups)
    now = NOW + timedelta(minutes=1)
    await collector._send_alerts(factory, SETTINGS, await fail(factory, ids[1:2], now), now)
    events = await values(factory, NotificationEvent)
    assert len(events) == 2
    update = events[-1].payload
    atom = alert_delivery.unpack(update["message"])
    assert atom.notice["stream"] == f"check:{ids[1]}:down"
    assert atom.notice["site_group"] == atom.notice["edit_group"] == gid
    parent = alert_delivery.unpack(update["envelope"])
    assert "site-0" in parent and "site-1" in parent
    assert parent.notice["group_messages"] == [receipt]
    async with factory() as session:
        group = (await site_group.load(session))[gid]
        assert set(group["members"]) == set(map(str, ids[:2]))
        assert group["msgs"] == [receipt]
    assert await values(factory, AlertEvent) == []


async def test_recovery_closes_group_and_observer_queue_in_same_admission(panel):
    factory, ids = panel
    await collector._send_alerts(factory, SETTINGS, await fail(factory, ids[:2]), NOW)
    now = NOW + timedelta(minutes=3)
    async with factory() as session:
        for cid in ids[:2]:
            row = await session.get(Check, cid)
            await collector.record_outcome(session, row, checks.CheckOutcome("up", message="HTTP 200"), now, [])
        await session.commit()
    await collector._send_alerts(factory, SETTINGS, [], now)
    events = await values(factory, NotificationEvent)
    assert [event.phase for event in events] == ["fire", "fire", "recovery", "recovery"]
    async with factory() as session:
        assert await site_group.load(session) == {}
        assert (await collector._site_recoveries(session, now))[0] == []
    assert all(not row.notified for row in await values(factory, CheckIncident))
    assert await values(factory, AlertEvent) == []


async def test_group_callback_preserves_receipts_written_after_observation(panel):
    factory, ids = panel
    cid = ids[0]
    message = alerts.Msg("site-0", "sites", "", kind="down", target="site-0", ref=f"chk:{cid}")
    message.notice = collector._site_notice(collector.Pending("bad", "site-0", "down", "HTTP 503", 7, cid, None), NOW)
    src = {0: ("down", cid, collector._grp_member("site-0", "site-0", "", "HTTP 503"))}
    messages, after = await collector._site_groups(factory, {"telegram_token": "fixture-token"},
        [message], src, {}, {cid: ("fixture", "fixture", None)}, [], NOW, SETTINGS.panel_url)
    async with factory() as session:
        await after({0}, session=session)
        await session.commit()
    receipt = {"chat": "100", "id": 72}
    async with factory() as session:
        state = await site_group.load(session)
        next(iter(state.values()))["msgs"] = [receipt]
        await site_group.save(session, state)
    async with factory() as session:
        await after({0}, session=session)
        await session.commit()
    async with factory() as session:
        assert next(iter((await site_group.load(session)).values()))["msgs"] == [receipt]
    assert messages[0].notice["site_group"]


async def test_real_worker_sends_then_edits_one_receipt_and_closes_group(panel, monkeypatch):
    factory, ids = panel
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"message_id": 90}}

        def raise_for_status(self):
            pass

    class HTTP:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, json=None):
            calls.append((url.rsplit("/", 1)[-1], json))
            return Response()

    monkeypatch.setattr(alerts.httpx, "AsyncClient", HTTP)
    monkeypatch.setattr(alert_delivery, "drain", REAL_DRAIN)
    await collector._send_alerts(factory, SETTINGS, await fail(factory, ids[:1]), NOW)
    async with factory() as session:
        assert next(iter((await site_group.load(session)).values()))["msgs"] == [{"chat": "100", "id": 90}]
    now = NOW + timedelta(minutes=1)
    await collector._send_alerts(factory, SETTINGS, await fail(factory, ids[1:2], now), now)
    assert [method for method, _payload in calls] == ["sendMessage", "editMessageText"]
    assert calls[1][1]["message_id"] == 90
    assert "site-0" in calls[1][1]["text"] and "site-1" in calls[1][1]["text"]
    assert len(await values(factory, AlertEvent)) == 2
    assert all(row.notified for row in await values(factory, CheckIncident))
    now += timedelta(minutes=3)
    async with factory() as session:
        for cid in ids[:2]:
            await collector.record_outcome(session, await session.get(Check, cid),
                checks.CheckOutcome("up", message="HTTP 200"), now, [])
        await session.commit()
    await collector._send_alerts(factory, SETTINGS, [], now)
    assert [method for method, _payload in calls] == ["sendMessage", "editMessageText", "sendMessage"]
    assert len(await values(factory, AlertEvent)) == 4
    assert all(row.status == "confirmed" for row in await values(factory, NotificationDelivery))
    async with factory() as session:
        assert await site_group.load(session) == {}
