"""Exercise durable delivery through the real dispatcher and fake HTTP only."""
import asyncio
import inspect
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app import alert_delivery, alerts, notification_store as store
from app.db import Base, create_engine_and_factory
from app.models import AppSetting, AlertEvent, NotificationDelivery, NotificationEvent, NotificationStream, TgAlert, User

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


class Response:
    def __init__(self, data, status_code=200):
        self.data, self.status_code = data, status_code

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def http(monkeypatch):
    # The dispatcher receives an explicit observation clock. Freeze elapsed HTTP
    # time too; lease-expiry tests advance the durable clock independently.
    monkeypatch.setattr(alert_delivery, "_monotonic", lambda: 0.0)
    class HTTP:
        calls = []
        next_id = 400
        answer = None

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            payload = dict(json or {})
            HTTP.calls.append((url, payload))
            if HTTP.answer is not None:
                result = HTTP.answer(url, payload)
                return await result if inspect.isawaitable(result) else result
            HTTP.next_id += 1
            return Response({"ok": True, "result": {"message_id": HTTP.next_id}})

        @classmethod
        def sent(cls):
            return [payload for url, payload in cls.calls if url.endswith("/sendMessage")]

    monkeypatch.setattr(alerts.httpx, "AsyncClient", HTTP)
    return HTTP


@pytest.fixture
async def database(tmp_path):
    pg = os.environ.get("KERVAX_TEST_PG", "")
    url = pg or f"sqlite+aiosqlite:///{(tmp_path / 'delivery.db').as_posix()}"
    engine, factory = create_engine_and_factory(url)
    async with engine.begin() as connection:
        if pg:
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory, url
    finally:
        await engine.dispose()


def configuration(**changes):
    return {"telegram_token": "fixture-bot-token", "telegram_chat": "-100", "telegram_api": "",
            "webhook": "", "tg_buttons": False, "routes": {}, **changes}


def message(source=1, *, at=NOW, phase="fire", counter=3, members=None):
    recovery = phase == "recovery"
    text = f"✅ node-{source}: очередь jobs восстановилась" if recovery else \
           f"🔴 <b>node-{source}</b>: очередь jobs стоит, задач {counter}"
    result = alerts.Msg(text, "services", "fixture", kind="queue_stuck", target=f"node-{source}",
                        recovery=recovery, ref=f"srv:{source}", mutes=["queue:jobs"])
    result.notice = {"stream": f"server:{source}:queue_stuck", "phase": phase,
                     "observed_at": at.isoformat(), "members": [] if recovery else members or ["queue:jobs"],
                     "level": 0 if recovery else 2}
    return result


async def dispatch(factory, item, at=NOW, cfg=None, threshold=0, **kwargs):
    return await alerts.dispatch(cfg or configuration(), True, item if isinstance(item, list) else [item],
                                 threshold, parse_mode="HTML", session_factory=factory, now=at, **kwargs)


async def rows(factory, model):
    async with factory() as session:
        return list(await session.scalars(select(model).order_by(model.id)))


def lost_response(_url, _payload):
    # Recording the POST above models an accepted message whose response was lost.
    raise httpx.ReadTimeout("fixture response lost")


async def test_lost_response_repeated_observations_and_restart_keep_one_event(database, http):
    factory, url = database
    http.answer = lost_response
    accepted, finished = set(), set()
    assert not await dispatch(factory, message(), accepted=accepted, finished=finished)
    assert accepted == {0} and finished == set()
    for second in range(1, 5):
        at = NOW + timedelta(seconds=second)
        assert not await dispatch(factory, message(at=at, counter=100 + second))
    second_engine, second_factory = create_engine_and_factory(url)
    try:
        at = NOW + timedelta(seconds=10)
        assert not await dispatch(second_factory, message(at=at, counter=900))
    finally:
        await second_engine.dispose()
    assert len(http.sent()) == 1
    assert len(await rows(factory, NotificationEvent)) == 1
    jobs = await rows(factory, NotificationDelivery)
    assert len(jobs) == 1 and jobs[0].status == "uncertain"
    assert jobs[0].attempts == jobs[0].uncertain_count == 1
    assert await rows(factory, AlertEvent) == []


async def test_observer_patch_and_admission_commit_together_before_post(database, http):
    factory, _url = database

    async def rejected_patch(session):
        session.add(AppSetting(key="fixture_admission", value="rolled back"))
        raise ValueError("fixture observer transaction rejected")

    accepted = set()
    with pytest.raises(ValueError, match="fixture observer transaction rejected"):
        await dispatch(factory, message(), accepted=accepted, on_accept=rejected_patch)
    assert accepted == set() and http.sent() == []
    assert await rows(factory, NotificationEvent) == []
    assert await rows(factory, NotificationDelivery) == []
    async with factory() as session:
        assert await session.get(AppSetting, "fixture_admission") is None

    async def patch(session):
        session.add(AppSetting(key="fixture_admission", value="owned"))

    http.answer = lost_response
    assert not await dispatch(factory, message(), accepted=accepted, on_accept=patch)
    assert accepted == {0} and len(http.sent()) == 1
    async with factory() as session:
        assert (await session.get(AppSetting, "fixture_admission")).value == "owned"
    assert (await rows(factory, NotificationDelivery))[0].status == "uncertain"


async def test_connect_timeout_retries_only_after_backoff(database, http):
    factory, _url = database

    def unavailable(_url, _payload):
        raise httpx.ConnectTimeout("fixture connection unavailable")

    http.answer = unavailable
    accepted = set()
    assert not await dispatch(factory, message(), accepted=accepted)
    assert accepted == {0}
    job = (await rows(factory, NotificationDelivery))[0]
    assert job.status == "pending" and job.uncertain_count == 0
    http.answer = None
    assert not await dispatch(factory, message(at=NOW + timedelta(seconds=29)), NOW + timedelta(seconds=29))
    assert len(http.sent()) == 1
    finished = set()
    assert await dispatch(factory, message(at=NOW + timedelta(seconds=30)), NOW + timedelta(seconds=30), finished=finished)
    assert finished == {0} and len(http.sent()) == 2
    job = (await rows(factory, NotificationDelivery))[0]
    assert job.status == "confirmed" and job.attempts == 2 and job.message_id
    assert len(await rows(factory, AlertEvent)) == 1


async def test_recipient_outcomes_and_shared_chat_are_independent(database, http):
    factory, _url = database
    async with factory() as session:
        session.add(User(username="subscriber", password_hash="fixture", role="viewer", sections=["services"],
                         server_groups=["fixture"], tg_chat_id="-100", tg_alerts=True))
        await session.commit()
    cfg = configuration(routes={"chats": [{"chat": "-100", "server_groups": ["fixture"]},
                                           {"chat": "-200", "server_groups": ["fixture"]}]})

    def answer(_url, payload):
        if payload["chat_id"] == "-200":
            raise httpx.ConnectTimeout("fixture second recipient unavailable")
        return Response({"ok": True, "result": {"message_id": 501}})

    http.answer = answer
    await dispatch(factory, message(), cfg=cfg)
    jobs = await rows(factory, NotificationDelivery)
    assert len(jobs) == 2
    assert {job.target["chat"]: job.status for job in jobs} == {"-100": "confirmed", "-200": "pending"}
    assert [payload["chat_id"] for payload in http.sent()] == ["-100", "-200"]
    http.answer = None
    await dispatch(factory, message(at=NOW + timedelta(seconds=31)), NOW + timedelta(seconds=31), cfg=cfg)
    assert [payload["chat_id"] for payload in http.sent()] == ["-100", "-200", "-200"]
    assert all(job.status == "confirmed" for job in await rows(factory, NotificationDelivery))
    assert len(await rows(factory, AlertEvent)) == 1


async def test_two_dispatchers_cannot_post_the_same_claim(database, http):
    factory, url = database
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated(_url, _payload):
        entered.set()
        await release.wait()
        return Response({"ok": True, "result": {"message_id": 601}})

    http.answer = gated
    engine, other = create_engine_and_factory(url)
    first = asyncio.create_task(dispatch(factory, message()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        accepted, finished = set(), set()
        assert not await dispatch(other, message(at=NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1),
                                  accepted=accepted, finished=finished)
        assert accepted == {0} and finished == set() and len(http.sent()) == 1
    finally:
        release.set()
        await asyncio.wait_for(first, timeout=5)
        await engine.dispose()
    assert len(http.sent()) == 1
    assert (await rows(factory, NotificationDelivery))[0].status == "confirmed"


async def test_expired_post_lease_becomes_uncertain_and_fences_late_response(database, http):
    factory, _url = database
    accepted, mapping = await alert_delivery.admit(factory, configuration(), [message()], 0, "HTML", NOW)
    assert accepted == {0}
    event_id = mapping[0][0]
    job = (await store.deliveries(factory, [event_id]))[0]
    assert await store.claim(factory, [job["id"]], "lost-worker", NOW, lease_seconds=10)
    at = NOW + timedelta(seconds=11)
    await alert_delivery.drain(factory, configuration(), at)
    assert http.sent() == []
    job = (await store.deliveries(factory, [event_id]))[0]
    assert job["status"] == "uncertain" and job["uncertain_count"] == 1
    assert await store.finish(factory, [job["id"]], "lost-worker", "confirmed", at, message_id=777) == []
    assert (await store.deliveries(factory, [event_id]))[0]["status"] == "uncertain"
    await alert_delivery.drain(factory, configuration(), job["next_attempt_at"])
    assert len(http.sent()) == 1
    assert (await store.deliveries(factory, [event_id]))[0]["status"] == "confirmed"


async def test_worker_late_http_response_cannot_confirm_an_expired_lease(database, http):
    factory, _url = database
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated(_url, _payload):
        entered.set()
        await release.wait()
        return Response({"ok": True, "result": {"message_id": 651}})

    http.answer = gated
    first = asyncio.create_task(dispatch(factory, message()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await alert_delivery.drain(factory, configuration(), NOW + timedelta(seconds=121))
        job = (await rows(factory, NotificationDelivery))[0]
        assert job.status == "uncertain" and job.last_error == "LeaseExpired"
        assert len(http.sent()) == 1
    finally:
        release.set()
        result = await asyncio.wait_for(first, timeout=5)
    assert not result
    assert (await rows(factory, NotificationDelivery))[0].status == "uncertain"
    assert await rows(factory, AlertEvent) == []


async def test_offline_completed_episode_is_summarized_and_new_incident_survives(database, http):
    factory, _url = database

    def unavailable(_url, _payload):
        raise httpx.ConnectTimeout("fixture offline")

    http.answer = unavailable
    await dispatch(factory, message())
    recovered = NOW + timedelta(minutes=1)
    await dispatch(factory, message(at=recovered, phase="recovery"), recovered)
    again = recovered + timedelta(seconds=10)
    await dispatch(factory, message(at=again, counter=91), again)
    events = await rows(factory, NotificationEvent)
    assert [event.phase for event in events] == ["fire", "recovery", "fire"]
    assert [event.episode for event in events] == [1, 1, 2]
    http.answer = None
    before = len(http.sent())
    await alert_delivery.drain(factory, configuration(), NOW + timedelta(minutes=3))
    emitted = http.sent()[before:]
    assert len(emitted) == 2
    assert "Пока уведомления не доходили" in emitted[0]["text"] and "jobs" in emitted[0]["text"]
    assert "восстанов" in emitted[0]["text"] and "91" in emitted[1]["text"]
    jobs = await rows(factory, NotificationDelivery)
    assert [job.status for job in jobs] == ["superseded", "confirmed", "confirmed"]
    assert len(await rows(factory, NotificationEvent)) == 3
    async with factory() as session:
        stream = (await session.scalars(select(NotificationStream))).one()
        assert stream.phase == "fire" and stream.episode == 2


async def test_three_uncertain_attempts_require_explicit_operator_retry(database, http):
    factory, _url = database
    http.answer = lost_response
    for attempt in range(3):
        at = NOW + timedelta(seconds=attempt * store.UNCERTAIN_DELAY)
        assert not await dispatch(factory, message(at=at, counter=attempt + 1), at)
    job = (await rows(factory, NotificationDelivery))[0]
    assert job.status == "attention" and job.uncertain_count == job.attempts == 3
    later = NOW + timedelta(hours=3)
    assert not await dispatch(factory, message(at=later), later)
    assert len(http.sent()) == 3 and await rows(factory, AlertEvent) == []
    assert await store.retry(factory, [job.id], later) == [job.id]
    http.answer = None
    await alert_delivery.drain(factory, configuration(), later)
    assert len(http.sent()) == 4
    assert (await rows(factory, NotificationDelivery))[0].status == "confirmed"
    assert len(await rows(factory, AlertEvent)) == 1


async def test_button_identity_is_reused_and_recovery_resolves_confirmed_message(database, http):
    factory, _url = database
    cfg = configuration(tg_buttons=True)
    http.answer = lost_response
    await dispatch(factory, message(), cfg=cfg)
    original = (await rows(factory, TgAlert))[0]
    aid = original.id
    assert not original.messages
    http.answer = None
    later = NOW + timedelta(seconds=store.UNCERTAIN_DELAY)
    await dispatch(factory, message(at=later, counter=18), later, cfg=cfg)
    buttons = await rows(factory, TgAlert)
    assert len(buttons) == 1 and buttons[0].id == aid
    callbacks = [payload["reply_markup"]["inline_keyboard"][0][0]["callback_data"] for payload in http.sent()]
    assert callbacks[0] == callbacks[1] and f":{aid}:" in callbacks[0]
    job = (await rows(factory, NotificationDelivery))[0]
    assert buttons[0].messages == [{"chat": "-100", "id": job.message_id}]
    at = later + timedelta(seconds=1)
    assert await dispatch(factory, message(at=at, phase="recovery"), at, cfg=cfg)
    assert (await rows(factory, TgAlert))[0].resolved_at is not None
    assert len(await rows(factory, TgAlert)) == 1


async def test_digest_preserves_membership_and_does_not_resend_old_constituents(database, http):
    factory, _url = database
    batch = alerts.Msg("three independent sources", kind="queue_stuck")
    batch.parts = [message(source) for source in (1, 2, 3)]
    accepted, finished = set(), set()
    assert await dispatch(factory, batch, threshold=2, accepted=accepted, finished=finished)
    assert accepted == finished == {0}
    assert len(http.sent()) == 1
    jobs = await rows(factory, NotificationDelivery)
    assert len(jobs) == 3 and all(job.status == "confirmed" for job in jobs)
    assert len({job.message_id for job in jobs}) == 1
    assert len(await rows(factory, AlertEvent)) == 3
    at = NOW + timedelta(seconds=1)
    await dispatch(factory, [message(source, at=at, counter=92) for source in (1, 2, 3, 4)], at, threshold=2)
    assert len(http.sent()) == 2
    assert len(await rows(factory, NotificationEvent)) == len(await rows(factory, AlertEvent)) == 4


async def test_http_200_without_message_id_is_uncertain_not_history(database, http):
    factory, _url = database
    http.answer = lambda _url, _payload: Response({"ok": True, "result": {}})
    accepted, finished = set(), set()
    assert not await dispatch(factory, message(), accepted=accepted, finished=finished)
    assert accepted == {0} and finished == set()
    job = (await rows(factory, NotificationDelivery))[0]
    assert job.status == "uncertain" and job.message_id is None and job.uncertain_count == 1
    assert await rows(factory, AlertEvent) == []
    await dispatch(factory, message(at=NOW + timedelta(seconds=1)), NOW + timedelta(seconds=1))
    assert len(http.sent()) == 1
