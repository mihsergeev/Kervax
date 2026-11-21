"""Crash windows and grouping boundaries around confirmed notification delivery."""
from datetime import timedelta

import pytest

from app import alert_delivery, alerts, notification_store as store
from app.models import AlertEvent, NotificationDelivery, NotificationEvent, TgAlert
from tests.test_alert_delivery import NOW, configuration, database, dispatch, http, message, rows  # noqa: F401

pytestmark = pytest.mark.pg


def test_verification_copy_keeps_critical_priority_inside_the_notification_budget():
    original = alerts.Msg("🚨 node\n" + ("Подробности " * 30 + "\n") * 6,
                          "servers", kind="disk", target="node", ref="srv:1",
                          full="Complete incident details", line="🚨 node: раздел заполнен")
    original.notice = {"stream": "server:1:disk", "level": 3}
    copy = alert_delivery._delivery_note(original, {
        "target": {}, "uncertain_count": 1, "occurred_at": NOW,
        "payload": {"parse_mode": "HTML"}}, NOW)
    assert alerts.critical(original) and alerts.critical(copy)
    assert len(str(copy).splitlines()) <= 4
    assert "доставка не подтверждена" in str(copy).splitlines()[0]
    assert "доставка не подтверждена" in alerts._digest([copy, message(2)])
    assert copy.ref == original.ref and copy.notice == original.notice
    assert "Complete incident details" in alerts.full_text(copy)


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.parametrize("threshold", [1, 6])
async def test_display_envelope_counts_once_and_keeps_retry_warning(database, http, uncertain, threshold):
    from tests.test_alert_delivery import lost_response

    factory, _ = database
    group = alerts.Msg("⚠️ <b>example.org</b>\n<b>Домен истекает через 4 дня</b>",
                       "services", "fixture", kind="queue_stuck", target="example.org",
                       full="Domain expiry; monitors: 5")
    group.parts = [message(source) for source in range(1, 6)]
    other = message(6)
    other.kind = "disk"
    if uncertain:
        http.answer = lost_response
    await dispatch(factory, [group, other], threshold=threshold)
    # Six journal events still represent two separate displayed notifications.
    # A display group must not trigger the six-message flood digest by itself.
    copies = 1 if threshold == 1 else 2
    assert len(http.sent()) == (1 if uncertain else copies)
    assert "<b>example.org</b>" in http.sent()[0]["text"]
    if uncertain:
        http.answer = None
        await alert_delivery.drain(factory, configuration(), NOW + timedelta(minutes=15, seconds=1))
        assert len(http.sent()) == 1 + copies
        grouped = next(item["text"] for item in http.sent()[1:] if "example.org" in item["text"])
        assert "Повтор: предыдущая доставка не подтверждена" in grouped
        assert len(grouped.splitlines()) <= 4
    assert len(await rows(factory, AlertEvent)) == 6


async def test_restart_repairs_history_and_original_buttons_without_another_post(database, http, monkeypatch):
    factory, _ = database
    cfg = configuration(tg_buttons=True)
    original = alert_delivery._history

    async def crashed(*args, **kwargs):
        raise RuntimeError("fixture crash after confirmed response")

    monkeypatch.setattr(alert_delivery, "_history", crashed)
    # сбой после подтвержденного POST пишется в лог и не выходит из доставки
    await dispatch(factory, message(), cfg=cfg)
    job = (await rows(factory, NotificationDelivery))[0]
    assert job.status == "confirmed" and len(http.sent()) == 1
    assert await rows(factory, AlertEvent) == []
    assert not (await rows(factory, TgAlert))[0].messages
    monkeypatch.setattr(alert_delivery, "_history", original)
    await alert_delivery.drain(factory, cfg, NOW + timedelta(seconds=1))
    assert len(http.sent()) == 1
    assert len(await rows(factory, AlertEvent)) == 1
    assert (await rows(factory, TgAlert))[0].messages == [{"chat": "-100", "id": job.message_id}]
    await alert_delivery.drain(factory, cfg, NOW + timedelta(seconds=2))
    assert len(http.sent()) == len(await rows(factory, AlertEvent)) == 1


async def test_delayed_recovery_does_not_resolve_new_episode_buttons(database, http):
    factory, _ = database
    cfg = configuration(tg_buttons=True)
    await dispatch(factory, message(), cfg=cfg)
    at = NOW + timedelta(minutes=1)
    await alert_delivery.admit(factory, cfg, [message(at=at, phase="recovery")], 0, "HTML", at)
    at += timedelta(seconds=1)
    await alert_delivery.admit(factory, cfg, [message(at=at)], 0, "HTML", at)
    # Deliver the new fire first, modelling separate recipient schedules.
    events = await rows(factory, NotificationEvent)
    jobs = await store.deliveries(factory, [events[-1].id])
    await alert_delivery._post(factory, cfg, jobs, at)
    buttons = await rows(factory, TgAlert)
    assert len(buttons) == 2 and all(row.resolved_at is None for row in buttons)
    await alert_delivery.drain(factory, cfg, at + timedelta(seconds=1))
    buttons = await rows(factory, TgAlert)
    assert buttons[0].resolved_at is not None and buttons[1].resolved_at is None


async def test_digest_receipt_cannot_become_an_editable_copy_of_each_member(database, http):
    factory, _ = database
    cfg = configuration(tg_buttons=True)
    await dispatch(factory, [message(1), message(2)], cfg=cfg, threshold=2)
    jobs = await rows(factory, NotificationDelivery)
    assert len(http.sent()) == 1 and all(not row.target["editable"] for row in jobs)
    assert await rows(factory, TgAlert) == []
    first = message(1, at=NOW + timedelta(seconds=1))
    first.sent_to = []
    await dispatch(factory, first, NOW + timedelta(seconds=1), cfg=cfg)
    assert first.sent_to == []


async def test_subset_observation_cannot_overwrite_a_new_pending_fact(database, http):
    factory, _ = database
    await alert_delivery.admit(factory, configuration(), [message(members=["disk:a"])], 0, "HTML", NOW)
    at = NOW + timedelta(seconds=1)
    latest = message(at=at, counter=92, members=["disk:b"])
    _, mapped = await alert_delivery.admit(factory, configuration(), [latest], 0, "HTML", at)
    later = at + timedelta(seconds=1)
    await alert_delivery.admit(factory, configuration(), [message(at=later, counter=7, members=["disk:a"])],
                               0, "HTML", later)
    event = (await store.load_events(factory, mapped[0]))[mapped[0][0]]
    assert "92" in event["payload"]["message"]["text"]
    assert event["members"] == ["disk:b"]


async def test_api_retry_guard_survives_a_changed_uncertain_outcome(database, http):
    factory, _ = database
    _, mapped = await alert_delivery.admit(factory, configuration(), [message()], 0, "HTML", NOW)
    job = (await store.deliveries(factory, mapped[0]))[0]
    await store.claim(factory, [job["id"]], "fixture-worker", NOW)
    await store.finish(factory, [job["id"]], "fixture-worker", "uncertain", NOW)
    assert await store.retry(factory, [job["id"]], NOW, acknowledge_uncertain=False) == []
    assert (await store.deliveries(factory, mapped[0]))[0]["status"] == "uncertain"


async def test_retry_after_is_durable_and_isolated_from_other_bots(database, http):
    from tests.test_alert_delivery import Response
    factory, _ = database
    http.answer = lambda *_: Response({"ok": False, "parameters": {"retry_after": 600}}, 429)
    await dispatch(factory, message())
    job = (await rows(factory, NotificationDelivery))[0]
    assert job.status == "pending" and job.uncertain_count == 0
    http.answer = None
    await alert_delivery.drain(factory, configuration(), NOW + timedelta(seconds=599))
    assert len(http.sent()) == 1
    await alert_delivery.drain(factory, configuration(), NOW + timedelta(seconds=600))
    assert len(http.sent()) == 2


async def test_sequential_posts_get_fresh_lease_time(database, http, monkeypatch):
    from tests.test_alert_delivery import Response
    factory, _ = database
    clock, claims = [0.0], []
    monkeypatch.setattr(alert_delivery, "_monotonic", lambda: clock[0])
    original = store.claim

    async def claimed(factory, ids, owner, now, **kwargs):
        claims.append(now)
        return await original(factory, ids, owner, now, **kwargs)

    def delayed(*_args):
        clock[0] += 80
        return Response({"ok": True, "result": {"message_id": len(claims)}})

    monkeypatch.setattr(store, "claim", claimed)
    http.answer = delayed
    await dispatch(factory, [message(1), message(2)])
    assert len(claims) == 2 and claims[1] - claims[0] == timedelta(seconds=80)
    assert all(row.status == "confirmed" for row in await rows(factory, NotificationDelivery))


async def test_recovery_carries_route_without_old_confirmation_metadata(database, http):
    factory, _ = database
    cfg = configuration(routes={"main": "rest", "chats": [{"chat": "-200", "server_groups": ["fixture"]}]})
    await dispatch(factory, message(), cfg=cfg)
    old = (await rows(factory, NotificationDelivery))[0]
    assert old.target["effects_recorded"]
    at = NOW + timedelta(seconds=1)
    recovered = message(at=at, phase="recovery")
    # A disappeared route is still represented for the final configuration
    # check, but it must acquire its own confirmation and effects receipts.
    await alert_delivery.admit(factory, configuration(telegram_token=""), [recovered], 0, "HTML", at)
    # No active destinations causes the wrapper to skip admission, as intended.
    notice = alert_delivery.metadata(recovered, at)
    ids = await store.admit(factory, [{**notice, "payload": {"message": alert_delivery.pack(recovered)},
                                       "recipients": []}], at)
    new = (await store.deliveries(factory, ids))[0]
    assert new["target"]["recovery_owned"] and new["status"] == "pending"
    assert not {"effects_recorded", "editable", "covered_events", "superseded_by"} & new["target"].keys()
