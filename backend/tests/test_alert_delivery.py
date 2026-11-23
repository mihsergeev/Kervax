"""Exercise durable delivery through the real dispatcher and fake HTTP only."""
import asyncio
import inspect
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app import alert_delivery, alerts, heartbeat, notification_store as store
from app.db import Base, create_engine_and_factory
from app.models import (AppSetting, AlertEvent, NotificationDelivery, NotificationEvent, NotificationStream,
                        NotificationTransport, Server, TgAlert, User)

pytestmark = pytest.mark.pg

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
    # Delivery snapshots refer to real sources. Allocate normally so PostgreSQL
    # sequences also stay valid when a scenario adds another server.
    async with factory() as session:
        session.add_all([Server(name=f"node-{index}", token_hash=f"fixture-{index}",
                                enabled=True, group_name="fixture") for index in range(1, 7)])
        await session.commit()
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


async def test_late_stage_clock_does_not_expire_the_lease_before_post(database, http, monkeypatch):
    # Стадия сборщика шла 10 минут: now ее начала отстал, аренда от него истекла бы до POST
    factory, _url = database
    wall = NOW + timedelta(minutes=10)
    monkeypatch.setattr(alert_delivery, "_wall", lambda: wall, raising=False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated(_url, _payload):
        entered.set()
        await release.wait()
        return Response({"ok": True, "result": {"message_id": 652}})

    http.answer = gated
    first = asyncio.create_task(dispatch(factory, message()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        job = (await rows(factory, NotificationDelivery))[0]
        assert job.status == "sending" and store._aware(job.lease_until) > wall
        # параллельный drain бота (tg_bot) с реальными часами
        await alert_delivery.drain(factory, configuration(), wall + timedelta(seconds=1))
        job = (await rows(factory, NotificationDelivery))[0]
        assert job.status == "sending" and job.uncertain_count == 0
    finally:
        release.set()
        result = await asyncio.wait_for(first, timeout=5)
    assert result
    job = (await rows(factory, NotificationDelivery))[0]
    assert job.status == "confirmed" and job.uncertain_count == 0 and len(http.sent()) == 1


async def test_failed_row_before_post_is_set_aside_and_others_are_sent(database, http, monkeypatch):
    factory, _url = database
    cfg = configuration(webhook="https://hook.example.invalid/kervax")
    render = alert_delivery._render

    async def broken(factory, row, now):
        if row["target"]["type"] == "webhook":
            raise RuntimeError("fixture render failure")
        return await render(factory, row, now)

    monkeypatch.setattr(alert_delivery, "_render", broken)
    await dispatch(factory, message(), cfg=cfg)
    jobs = {job.target["type"]: job for job in await rows(factory, NotificationDelivery)}
    assert jobs["telegram"].status == "confirmed" and len(http.sent()) == 1
    hook = jobs["webhook"]
    assert hook.status == "pending" and hook.attempts == hook.uncertain_count == 0
    assert not hook.lease_owner and hook.lease_until is None and hook.target["prepare_failures"] == 1
    later = NOW + timedelta(seconds=store.PREPARE_DELAY)
    assert store._aware(hook.next_attempt_at) == later
    async with factory() as session:
        transport = await session.get(NotificationTransport, hook.transport_key)
        assert not transport.lease_owner and transport.blocked_until is None
    monkeypatch.setattr(alert_delivery, "_render", render)
    await alert_delivery.drain(factory, cfg, later)
    assert all(job.status == "confirmed" for job in await rows(factory, NotificationDelivery))


async def test_failure_after_post_stays_ambiguous_without_leaving_dispatch(database, http, monkeypatch, caplog):
    factory, _url = database
    finish = store.finish

    async def broken(*_args, **_kwargs):
        raise RuntimeError("fixture database failure after POST")

    monkeypatch.setattr(store, "finish", broken)
    assert not await dispatch(factory, message())
    assert "исход неизвестен" in caplog.text
    job = (await rows(factory, NotificationDelivery))[0]
    assert len(http.sent()) == 1 and job.status == "sending" and job.lease_owner
    monkeypatch.setattr(store, "finish", finish)
    await alert_delivery.drain(factory, configuration(), NOW + timedelta(seconds=121))
    job = (await rows(factory, NotificationDelivery))[0]
    assert job.status == "uncertain" and job.last_error == "LeaseExpired" and len(http.sent()) == 1


async def _jobs_by_target(factory):
    events = [event.id for event in await rows(factory, NotificationEvent)]
    return {job["payload"]["message"]["target"]: job for job in await store.deliveries(factory, events)}


@pytest.mark.parametrize("threshold", [0, 1])
async def test_poison_row_is_set_aside_then_quarantined_and_neighbours_go(database, http, monkeypatch, threshold):
    factory, _url = database
    render = alert_delivery._render

    async def broken(factory, row, now):
        if row["payload"]["message"]["target"] == "node-2":
            raise KeyError("fixture corrupt payload")
        return await render(factory, row, now)

    monkeypatch.setattr(alert_delivery, "_render", broken)
    heartbeat.reset()
    await dispatch(factory, [message(1), message(2)], threshold=threshold)
    assert len(http.sent()) == 1 and "node-1" in http.sent()[0]["text"]
    jobs = await _jobs_by_target(factory)
    assert jobs["node-1"]["status"] == "confirmed"
    poison = jobs["node-2"]
    assert poison["status"] == "pending" and poison["attempts"] == poison["uncertain_count"] == 0
    assert poison["target"]["prepare_failures"] == 1 and poison["last_error"] == "PrepareError"
    assert poison["next_attempt_at"] == NOW + timedelta(seconds=store.PREPARE_DELAY) and not poison["lease_owner"]
    async with factory() as session:
        transport = await session.get(NotificationTransport, poison["transport_key"])
        assert not transport.lease_owner and transport.blocked_until is None
    assert await store.due(factory, NOW + timedelta(seconds=1)) == []  # не стоит в голове due
    heartbeat.stage_done(alert_delivery.DELIVERY_STAGE, True)
    assert alert_delivery.DELIVERY_STAGE in heartbeat.stage_fail_line()
    at = NOW
    for _ in range(store.MAX_PREPARE - 1):
        at += timedelta(seconds=store.PREPARE_DELAY)
        await alert_delivery.drain(factory, configuration(), at)
    poison = (await _jobs_by_target(factory))["node-2"]
    assert poison["status"] == "attention" and poison["last_error"] == "PrepareError"
    assert poison["target"]["prepare_failures"] == store.MAX_PREPARE and poison["attempts"] == 0
    assert await store.due(factory, at + timedelta(days=1)) == [] and len(http.sent()) == 1
    heartbeat.reset()


def _poisoned(allowed):
    if any(message.target == "node-2" for _row, message, _cfg in allowed):
        raise RuntimeError("fixture poison breaks the bundle")


@pytest.mark.parametrize("hook", ["_filter_envelopes", "_save_policy_messages"])
async def test_poison_breaking_the_bundle_goes_solo_and_only_it_is_quarantined(database, http, monkeypatch, hook):
    factory, _url = database
    original = getattr(alert_delivery, hook)

    async def broken(factory, allowed, *args):
        _poisoned(allowed)
        return await original(factory, allowed, *args)

    monkeypatch.setattr(alert_delivery, hook, broken)
    await dispatch(factory, [message(1), message(2)], threshold=1)
    jobs = await _jobs_by_target(factory)
    assert http.sent() == []
    for job in jobs.values():  # из пачки виновную не узнать: без счетчика, следующий проход - по одной
        assert job["status"] == "pending" and "prepare_failures" not in job["target"]
        assert job["target"]["solo_until"] and job["next_attempt_at"] == NOW + timedelta(seconds=alert_delivery._BUNDLE_DELAY)
    at = NOW + timedelta(seconds=alert_delivery._BUNDLE_DELAY)
    await alert_delivery.drain(factory, configuration(), at)
    jobs = await _jobs_by_target(factory)
    assert len(http.sent()) == 1 and "node-1" in http.sent()[0]["text"]
    assert jobs["node-1"]["status"] == "confirmed" and "solo_until" not in jobs["node-1"]["target"]
    assert jobs["node-2"]["target"]["prepare_failures"] == 1
    for _ in range(store.MAX_PREPARE - 1):
        at = (await _jobs_by_target(factory))["node-2"]["next_attempt_at"]
        await alert_delivery.drain(factory, configuration(), at)
    poison = (await _jobs_by_target(factory))["node-2"]
    assert poison["status"] == "attention" and poison["target"]["prepare_failures"] == store.MAX_PREPARE
    assert len(http.sent()) == 1
    # ручной повтор - с чистого листа: метки сбоев подготовки сняты
    assert await store.retry(factory, [poison["id"]], at) == [poison["id"]]
    poison = (await _jobs_by_target(factory))["node-2"]
    assert poison["status"] == "pending" and not {"prepare_failures", "solo_until"} & poison["target"].keys()


def _database_down():
    from sqlalchemy.exc import OperationalError

    return OperationalError("SELECT 1", {}, ConnectionError("fixture database unavailable"))


@pytest.mark.parametrize("level", ["bundle", "row"])
async def test_infrastructure_failure_never_quarantines_healthy_rows(database, http, monkeypatch, level):
    factory, _url = database
    name = "_filter_envelopes" if level == "bundle" else "_render"
    original = getattr(alert_delivery, name)

    async def down(factory, first, *args):
        if level == "bundle" or first["payload"]["message"]["target"] == "node-2":
            raise _database_down()
        return await original(factory, first, *args)

    monkeypatch.setattr(alert_delivery, name, down)
    await dispatch(factory, [message(1), message(2)], threshold=1)
    at = NOW
    for _ in range(store.MAX_PREPARE + 2):
        at += timedelta(seconds=store.PREPARE_DELAY)
        await alert_delivery.drain(factory, configuration(), at)
    jobs = await _jobs_by_target(factory)
    assert all(job["status"] != "attention" and "prepare_failures" not in job["target"]
               and "solo_until" not in job["target"] for job in jobs.values() if job["status"] != "confirmed")
    assert jobs["node-2"]["status"] == "pending"
    monkeypatch.setattr(alert_delivery, name, original)
    await alert_delivery.drain(factory, configuration(), at + timedelta(seconds=store.PREPARE_DELAY))
    assert all(job["status"] == "confirmed" for job in (await _jobs_by_target(factory)).values())


async def test_unparsable_row_is_split_out_and_the_storm_stays_one_post(database, http, monkeypatch):
    factory, _url = database
    original = alert_delivery.unpack
    await alert_delivery.admit(factory, configuration(), [message(source) for source in (1, 2, 3)], 1, "HTML", NOW)

    def corrupt(payload):
        if payload.get("target") == "node-2":
            raise KeyError("fixture corrupt payload")
        return original(payload)

    monkeypatch.setattr(alert_delivery, "unpack", corrupt)
    await alert_delivery.drain(factory, configuration(), NOW)
    jobs = await _jobs_by_target(factory)
    assert len(http.sent()) == 1 and "node-2" not in http.sent()[0]["text"]
    assert jobs["node-1"]["status"] == jobs["node-3"]["status"] == "confirmed"
    assert jobs["node-2"]["status"] == "pending" and jobs["node-2"]["target"]["prepare_failures"] == 1


@pytest.mark.parametrize("where", ["due", "claim", "bundles"])
async def test_drain_failures_stay_inside_and_reach_the_heartbeat(database, http, monkeypatch, where):
    factory, _url = database
    await alert_delivery.admit(factory, configuration(), [message()], 0, "HTML", NOW)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("fixture queue failure")

    def fail_now(*_args, **_kwargs):
        raise RuntimeError("fixture grouping failure")

    target, name, value = {"due": (store, "due", fail), "claim": (store, "claim", fail),
                           "bundles": (alert_delivery, "_bundles", fail_now)}[where]
    monkeypatch.setattr(target, name, value)
    heartbeat.reset()
    await alert_delivery.drain(factory, configuration(), NOW)
    heartbeat.stage_done(alert_delivery.DELIVERY_STAGE, True)
    assert alert_delivery.DELIVERY_STAGE in heartbeat.stage_fail_line()
    heartbeat.reset()
    job = (await rows(factory, NotificationDelivery))[0]
    # не разобрали пачку - шлем по одной; очередь и claim сломаны - строка не тронута
    assert (job.status, len(http.sent())) == (("confirmed", 1) if where == "bundles" else ("pending", 0))
    assert job.uncertain_count == 0 and not job.lease_owner


async def test_cancel_before_post_returns_rows_without_uncertainty(database, http, monkeypatch):
    factory, _url = database
    entered, gate = asyncio.Event(), asyncio.Event()

    async def slow(*_args):
        entered.set()
        await gate.wait()

    monkeypatch.setattr(alert_delivery, "_wanted", slow)
    task = asyncio.create_task(dispatch(factory, message()))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    job = (await rows(factory, NotificationDelivery))[0]
    assert job.status == "pending" and job.attempts == job.uncertain_count == 0
    assert not job.lease_owner and job.lease_until is None and http.sent() == []
    async with factory() as session:
        assert not (await session.get(NotificationTransport, job.transport_key)).lease_owner


@pytest.mark.wall_clock
async def test_dispatch_from_a_long_stage_keeps_its_lease_against_the_bot(database, http):
    # настоящая граница реальным временем: стадия сборщика началась 10 минут назад
    factory, _url = database
    stage_now = datetime.now(timezone.utc) - timedelta(minutes=10)
    entered, release = asyncio.Event(), asyncio.Event()

    async def gated(_url, _payload):
        entered.set()
        await release.wait()
        return Response({"ok": True, "result": {"message_id": 653}})

    http.answer = gated
    first = asyncio.create_task(dispatch(factory, message(at=stage_now), stage_now))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        job = (await rows(factory, NotificationDelivery))[0]
        assert store._aware(job.lease_until) > datetime.now(timezone.utc)
        await alert_delivery.drain(factory, configuration())  # бот: реальное время
        job = (await rows(factory, NotificationDelivery))[0]
        assert job.status == "sending" and job.uncertain_count == 0
    finally:
        release.set()
        result = await asyncio.wait_for(first, timeout=5)
    assert result
    job = (await rows(factory, NotificationDelivery))[0]
    assert job.status == "confirmed" and job.uncertain_count == 0 and len(http.sent()) == 1


async def _two_bundles(factory):
    # два источника одного получателя - две пачки, два POST за проход
    await alert_delivery.admit(factory, configuration(), [message(1), message(2)], 0, "HTML", NOW)


def _stopped(stop):
    """Проход доставки под сигналом остановки, как в collector_loop."""
    return alert_delivery.STOP.set(stop)


async def test_stop_during_a_pass_starts_no_further_bundle(database, http, monkeypatch):
    # SIGTERM пришел, пока первый POST в полете: он доводится и записывается, вторая пачка ждет старта
    factory, _url = database
    await _two_bundles(factory)
    stop, claims, repairs = asyncio.Event(), [], []
    claim, reconcile = store.claim, alert_delivery._reconcile_confirmed

    async def counted_claim(_factory, ids, *args, **kwargs):
        claims.append(ids)
        return await claim(_factory, ids, *args, **kwargs)

    async def counted_reconcile(*args):
        repairs.append(args)
        return await reconcile(*args)

    monkeypatch.setattr(store, "claim", counted_claim)
    monkeypatch.setattr(alert_delivery, "_reconcile_confirmed", counted_reconcile)

    def answer(_url, _payload):
        stop.set()
        return Response({"ok": True, "result": {"message_id": 702}})

    http.answer = answer
    token = _stopped(stop)
    try:
        await alert_delivery.drain(factory, configuration())
        assert len(claims) == len(repairs) == 1  # вторую пачку даже не захватывали
        await alert_delivery.drain(factory, configuration())  # бот после сигнала: прохода нет
        assert len(claims) == len(repairs) == 1
    finally:
        alert_delivery.STOP.reset(token)
    first, second = await rows(factory, NotificationDelivery)
    assert (first.status, first.message_id, first.uncertain_count) == ("confirmed", 702, 0)
    assert (second.status, second.attempts, second.uncertain_count, second.lease_owner) == ("pending", 0, 0, "")
    assert len(http.sent()) == 1
    http.answer = None
    await alert_delivery.drain(factory, configuration())  # старт после выкладки
    assert [job.status for job in await rows(factory, NotificationDelivery)] == ["confirmed", "confirmed"]
    assert len(http.sent()) == 2


async def test_stop_after_claim_returns_rows_without_post(database, http, monkeypatch):
    # остановка между захватом и POST: попытки не было, строка в очереди без uncertain и аренды
    factory, _url = database
    await alert_delivery.admit(factory, configuration(), [message()], 0, "HTML", NOW)
    stop, wanted = asyncio.Event(), alert_delivery._wanted

    async def stopping(*args):
        stop.set()
        return await wanted(*args)

    monkeypatch.setattr(alert_delivery, "_wanted", stopping)
    token = _stopped(stop)
    try:
        await alert_delivery.drain(factory, configuration())
    finally:
        alert_delivery.STOP.reset(token)
    job, = await rows(factory, NotificationDelivery)
    assert (job.status, job.attempts, job.uncertain_count, job.lease_owner, job.lease_until) == (
        "pending", 0, 0, "", None)
    assert http.sent() == []
    async with factory() as session:
        assert not (await session.get(NotificationTransport, job.transport_key)).lease_owner
    monkeypatch.setattr(alert_delivery, "_wanted", wanted)
    await alert_delivery.drain(factory, configuration())
    job, = await rows(factory, NotificationDelivery)
    assert job.status == "confirmed" and len(http.sent()) == 1


@pytest.mark.parametrize("in_flight", [True, False])
async def test_shutdown_finishes_the_bot_send_and_starts_no_new_one(database, http, monkeypatch, in_flight):
    # Выкладка, пока бот шлет первую из двух пачек. in_flight: планировщик выходит и отменяет бота
    # посреди POST - он доводится и записывается (а не sending -> uncertain -> дубль "Повтор").
    # Иначе бот успевает дойти до следующей пачки, пока планировщик доделывает стадию: ее не шлет
    from app import collector, tg_bot
    from app.config import Settings
    factory, _url = database
    await _two_bundles(factory)
    entered, release, stop, drained = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def gated(_url, _payload):
        entered.set()
        await release.wait()
        return Response({"ok": True, "result": {"message_id": 703}})

    async def bot(session_factory, _settings):
        await alert_delivery.drain(session_factory, configuration())
        drained.set()
        await asyncio.Event().wait()  # long polling до отмены

    async def first_stage(*_args):
        await asyncio.wait_for(entered.wait(), 5)
        stop.set()  # SIGTERM, пока POST бота в полете
        if in_flight:
            asyncio.get_running_loop().call_later(0.3, release.set)
        else:
            release.set()
            await asyncio.wait_for(drained.wait(), 5)

    http.answer = gated
    monkeypatch.setattr(tg_bot, "poll_loop", bot)
    monkeypatch.setattr(collector, "rebind_local_probes", first_stage)
    await asyncio.wait_for(collector.collector_loop(factory, Settings(scheduler_tick=30), stop), 30)
    first, second = await rows(factory, NotificationDelivery)
    assert (first.status, first.message_id, first.uncertain_count, first.lease_owner) == ("confirmed", 703, 0, "")
    assert (second.status, second.attempts, second.lease_owner) == ("pending", 0, "")
    assert len(http.sent()) == 1
    http.answer = None
    await alert_delivery.drain(factory, configuration())  # старт после выкладки: без потери и дубля
    assert [job.status for job in await rows(factory, NotificationDelivery)] == ["confirmed", "confirmed"]
    assert len(http.sent()) == 2


async def _mute(factory, muted=True):
    from app import settings_store
    async with factory() as session:
        await settings_store.set_muted(session, muted)
        await session.commit()


@pytest.mark.parametrize("when", ["start", "between", "before_post"])
async def test_pause_mid_pass_leaves_rows_like_a_pause_at_start(database, http, monkeypatch, when):
    # пауза "не слать" посреди прохода: новых POST нет, идущий доведен. Неотправленные строки - как при
    # паузе в начале прохода: в очереди, без попытки, uncertain и attention, уходят после снятия паузы
    factory, _url = database
    await _two_bundles(factory)
    wanted = alert_delivery._wanted

    async def answer(_url, _payload):
        await _mute(factory)
        return Response({"ok": True, "result": {"message_id": 704}})

    async def muting(*args):
        await _mute(factory)
        return await wanted(*args)

    if when == "start":
        await _mute(factory)
    elif when == "between":
        http.answer = answer
    else:
        monkeypatch.setattr(alert_delivery, "_wanted", muting)
    await alert_delivery.drain(factory, configuration())
    jobs = await rows(factory, NotificationDelivery)
    sent = 1 if when == "between" else 0
    assert len(http.sent()) == sent
    for job in jobs[sent:]:
        assert (job.status, job.attempts, job.uncertain_count, job.lease_owner, job.last_error) == (
            "pending", 0, 0, "", "")
    monkeypatch.setattr(alert_delivery, "_wanted", wanted)
    http.answer = None
    await _mute(factory, False)
    await alert_delivery.drain(factory, configuration())
    assert [job.status for job in await rows(factory, NotificationDelivery)] == ["confirmed", "confirmed"]
    assert len(http.sent()) == 2


async def test_changed_destination_is_recorded_as_its_own_code(database, http):
    # основной чат сменили до отправки: отказ с кодом DestinationChanged, а не общий DeliveryError
    factory, _url = database
    await alert_delivery.admit(factory, configuration(), [message()], 0, "HTML", NOW)
    await alert_delivery.drain(factory, configuration(telegram_chat="-200"))
    job, = await rows(factory, NotificationDelivery)
    assert (job.status, job.last_error, store.reason(job.last_error)) == (
        "rejected", "DestinationChanged", "DestinationChanged")
    assert http.sent() == []
