"""Real file-backed transactions; no network requests or delivery credentials."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import notification_store as store
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent, NotificationDelivery as Delivery, NotificationEvent as Event
from app.models import NotificationStream as Stream, NotificationTransport as Transport

T0 = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


@pytest.fixture
async def db(tmp_path):
    pg = os.environ.get("KERVAX_TEST_PG", "")
    url = pg or f"sqlite+aiosqlite:///{(tmp_path / 'notifications.db').as_posix()}"
    engine, factory = create_engine_and_factory(url)
    async with engine.begin() as connection:
        if pg:
            # Same dedicated, serial test database convention as conftest.client.
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield engine, factory, url
    await engine.dispose()


def spec(**changes):
    value = dict(stream="server:example:disk", phase="fire", level=1, members=["disk:a"],
                 observed_at=T0, payload={"text": "Example disk incident", "parts": [{"kind": "disk"}]},
                 recipients=[dict(key="recipient:primary", transport="transport:shared",
                                  target={"kind": "common", "chat": "example-chat"})])
    value.update(changes)
    return value


async def deliveries(factory):
    async with factory() as session:
        return list(await session.scalars(select(Delivery).order_by(Delivery.id)))


async def test_transition_identity_ignores_text_and_new_episode_survives(db):
    _, factory, _ = db
    first = (await store.admit(factory, [spec()], T0))[0]
    duplicate = spec(observed_at=T0 + timedelta(seconds=1),
                     payload={"text": "Updated duration and counters"})
    assert await store.admit(factory, [duplicate], T0 + timedelta(seconds=1)) == [first]
    escalation = (await store.admit(factory, [spec(level=2, observed_at=T0 + timedelta(seconds=2))], T0))[0]
    more = (await store.admit(factory, [spec(level=2, members=["disk:a", "disk:b"],
                                             observed_at=T0 + timedelta(seconds=3))], T0))[0]
    recovery = (await store.admit(factory, [spec(phase="recovery", level=0,
                                                 observed_at=T0 + timedelta(seconds=4))], T0))[0]
    next_fire = (await store.admit(factory, [spec(observed_at=T0 + timedelta(seconds=5))], T0))[0]
    assert len({first, escalation, more, recovery, next_fire}) == 5
    async with factory() as session:
        assert (await session.get(Stream, store.key("server:example:disk"))).seq == 5
        assert (await session.get(Event, first)).payload["text"] == "Updated duration and counters"
        assert not (await session.get(Event, next_fire)).history_recorded
    events = await store.load_events(factory, [first, escalation, more, recovery, next_fire])
    assert [events[i]["episode"] for i in [first, escalation, more, recovery, next_fire]] == [1, 1, 1, 1, 2]
    assert len(await store.deliveries(factory, [first, next_fire])) == 2


async def test_stale_observation_cannot_reopen_or_patch_recovered_stream(db):
    _, factory, _ = db
    await store.admit(factory, [spec()], T0)
    recovered = (await store.admit(factory, [spec(phase="recovery", observed_at=T0 + timedelta(minutes=1))], T0))[0]
    callbacks = []

    async def patch(session, index, event_id):
        callbacks.append(event_id)

    assert await store.admit(factory, [spec(payload={"text": "stale"})], T0, callback=patch) == [None]
    assert not callbacks
    async with factory() as session:
        stream = await session.get(Stream, store.key("server:example:disk"))
        assert stream.phase == "recovery" and stream.seq == 2
        assert (await session.get(Event, recovered)).payload["text"] != "stale"


async def test_open_episode_keeps_seen_facts_and_highest_level(db):
    _, factory, _ = db

    async def observe(seconds, members, level, **changes):
        now = T0 + timedelta(seconds=seconds)
        return (await store.admit(factory, [spec(observed_at=now, members=members, level=level, **changes)], now))[0]

    critical_a = "queue:a|level:2"
    warning_b = "queue:b|level:1"
    critical_b = "queue:b|level:2"
    first = await observe(0, [critical_a], 2)
    warning = await observe(1, [warning_b], 1)
    escalation = await observe(2, [critical_b], 2)
    assert len({first, warning, escalation}) == 3
    # Neither an older critical fact nor a new textual counter is new evidence.
    assert await observe(3, [critical_a], 2) == escalation
    assert await observe(4, [critical_b], 2, payload={"text": "new counter"}) == escalation
    async with factory() as session:
        stream = await session.get(Stream, store.key("server:example:disk"))
        assert stream.members == sorted([critical_a, warning_b, critical_b])
        assert stream.level == 2 and stream.seq == 3
        current = await session.get(Event, warning)
        assert current.members == [warning_b] and current.level == 1 and current.episode == 1
    recovered = await observe(5, [], 0, phase="recovery")
    async with factory() as session:
        stream = await session.get(Stream, store.key("server:example:disk"))
        assert stream.members == [] and stream.level == 0
    next_warning = await observe(6, [warning_b], 1)
    assert next_warning not in {first, warning, escalation, recovered}
    async with factory() as session:
        stream = await session.get(Stream, store.key("server:example:disk"))
        assert stream.members == [warning_b] and stream.level == 1 and stream.episode == 2
        assert (await session.get(Event, next_warning)).episode == 2


async def test_explicit_action_occurrence_and_input_order(db):
    _, factory, _ = db
    values = [spec(stream="z:action", occurrence="one"), spec(stream="a:action", occurrence="one")]
    first = await store.admit(factory, values, T0)
    assert await store.admit(factory, values, T0) == first
    second = (await store.admit(factory, [spec(stream="z:action", occurrence="two")], T0))[0]
    assert second != first[0]
    assert await store.admit(factory, [values[0]], T0) == [first[0]]


async def test_atomic_admission_callback_and_duplicate_callback(db):
    _, factory, _ = db
    seen = []

    async def observe(session, index, event_id):
        seen.append((index, event_id))
        session.add(AlertEvent(kind="observer", target="example", text="observed"))

    first = await store.admit(factory, [spec()], T0, callback=observe)
    assert await store.admit(factory, [spec()], T0, callback=observe) == first
    assert seen == [(0, first[0]), (0, first[0])]

    async def fail(session, index, event_id):
        session.add(AlertEvent(kind="observer", target="example", text="rolled back"))
        raise RuntimeError("observer patch failed")

    with pytest.raises(RuntimeError):
        await store.admit(factory, [spec(stream="different")], T0, callback=fail)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        assert await session.scalar(select(func.count()).select_from(AlertEvent)) == 2


async def test_concurrent_admission_is_one_event_and_delivery(db):
    _, factory, _ = db
    ids = await asyncio.gather(*(store.admit(factory, [spec()], T0) for _ in range(8)))
    assert len({row[0] for row in ids}) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        assert await session.scalar(select(func.count()).select_from(Delivery)) == 1


async def test_callbacks_keep_input_order_after_all_rows_are_admitted(db):
    _, factory, _ = db
    seen = []

    async def observe(session, index, event_id):
        assert await session.scalar(select(func.count()).select_from(Event)) == 2
        seen.append(index)

    await store.admit(factory, [spec(stream="z:stream"), spec(stream="a:stream")], T0, callback=observe)
    assert seen == [0, 1]


async def test_competing_claims_and_recipient_transport_isolation(db):
    _, factory, _ = db
    recipients = [dict(key="one", transport="shared", target={"chat": "one"}),
                  dict(key="two", transport="shared", target={"chat": "two"}),
                  dict(key="three", transport="independent", target={"chat": "three"})]
    await store.admit(factory, [spec(recipients=recipients)], T0)
    rows = await deliveries(factory)
    claims = await asyncio.gather(store.claim(factory, [rows[0].id], "worker-a", T0),
                                  store.claim(factory, [rows[0].id], "worker-b", T0))
    assert sorted(map(len, claims)) == [0, 1]
    assert await store.claim(factory, [rows[1].id], "worker-c", T0) == []
    assert len(await store.claim(factory, [rows[2].id], "worker-c", T0)) == 1
    with pytest.raises(ValueError):
        await store.claim(factory, [rows[0].id, rows[1].id], "worker-d", T0)


async def test_lost_post_response_survives_restart_and_never_records_history(db):
    engine, factory, url = db
    event = (await store.admit(factory, [spec()], T0))[0]
    row = (await store.due(factory, T0))[0]
    await store.claim(factory, [row["id"]], "worker-before-restart", T0)
    await store.finish(factory, [row["id"]], "worker-before-restart", "uncertain", T0,
                       error="ReadTimeout")
    await engine.dispose()
    restarted, fresh = create_engine_and_factory(url)
    try:
        assert await store.due(fresh, T0 + timedelta(minutes=14)) == []
        assert await store.mark_history(fresh, [event]) == []
        rows = await store.due(fresh, T0 + timedelta(minutes=15))
        assert len(rows) == 1 and rows[0]["uncertain_count"] == 1
        assert rows[0]["attempts"] == 1 and rows[0]["message_id"] is None
        async with fresh() as session:
            assert (await session.get(Delivery, row["id"])).status == "uncertain"
    finally:
        await restarted.dispose()


async def test_expired_lease_is_uncertain_and_late_finish_cannot_overwrite(db):
    _, factory, _ = db
    await store.admit(factory, [spec()], T0)
    row = (await store.due(factory, T0))[0]
    await store.claim(factory, [row["id"]], "old-worker", T0, lease_seconds=10)
    later = T0 + timedelta(seconds=11)
    assert await store.due(factory, later) == []
    assert await store.finish(factory, [row["id"]], "old-worker", "confirmed", later, message_id=77) == []
    persisted = (await deliveries(factory))[0]
    assert persisted.status == "uncertain" and persisted.uncertain_count == 1 and persisted.attempts == 1
    assert persisted.last_error == "LeaseExpired"
    retry_at = later + timedelta(minutes=15)
    assert len(await store.claim(factory, [row["id"]], "new-worker", retry_at)) == 1
    assert await store.finish(factory, [row["id"]], "old-worker", "rejected", retry_at) == []
    assert await store.finish(factory, [row["id"]], "new-worker", "confirmed", retry_at, message_id=78) == [row["id"]]


async def test_ambiguity_budget_attention_and_explicit_operator_retry(db):
    _, factory, _ = db
    await store.admit(factory, [spec()], T0)
    row = (await store.due(factory, T0))[0]
    for attempt in range(3):
        now = T0 + timedelta(minutes=15 * attempt)
        assert len(await store.claim(factory, [row["id"]], "worker", now)) == 1
        await store.finish(factory, [row["id"]], "worker", "uncertain", now, error="ReadTimeout")
    persisted = (await deliveries(factory))[0]
    assert persisted.status == "attention" and persisted.attempts == persisted.uncertain_count == 3
    assert await store.due(factory, T0 + timedelta(days=1)) == []
    manual = T0 + timedelta(minutes=31)
    assert await store.retry(factory, [row["id"]], manual) == [row["id"]]
    retried = await store.claim(factory, [row["id"]], "operator-probe", manual)
    assert retried[0]["attempts"] == 1 and retried[0]["uncertain_count"] == 0


async def test_retry_budget_backoff_and_retry_after(db):
    _, factory, _ = db
    await store.admit(factory, [spec()], T0)
    row = (await store.due(factory, T0))[0]
    now = T0
    for attempt in range(6):
        assert len(await store.claim(factory, [row["id"]], "worker", now)) == 1
        await store.finish(factory, [row["id"]], "worker", "retry", now,
                           error="HTTP_429", retry_after=60 if attempt == 0 else None)
        persisted = (await deliveries(factory))[0]
        if attempt == 0:
            assert await store.due(factory, now + timedelta(seconds=59)) == []
        now = persisted.next_attempt_at.replace(tzinfo=timezone.utc)
    assert persisted.status == "attention" and persisted.attempts == 6 and persisted.uncertain_count == 0
    assert persisted.last_error == "HTTP_429"


async def test_partial_recipient_success_keeps_confirmed_mid_payload_and_button_identity(db):
    _, factory, _ = db
    recipients = [dict(key="one", transport="one", target={"chat": "one"}),
                  dict(key="two", transport="two", target={"chat": "two"})]
    event = (await store.admit(factory, [spec(recipients=recipients)], T0))[0]
    a, b = await store.due(factory, T0)
    aids = await asyncio.gather(store.set_tg_alert(factory, event, 10), store.set_tg_alert(factory, event, 11))
    assert len(set(aids)) == 1
    await store.claim(factory, [a["id"]], "worker", T0)
    await store.finish(factory, [a["id"]], "worker", "confirmed", T0, message_id=90)
    await store.claim(factory, [b["id"]], "worker", T0)
    await store.finish(factory, [b["id"]], "worker", "retry", T0, error="ConnectError")
    assert await store.admit(factory, [spec(recipients=recipients, payload={"text": "new counters"})], T0) == [event]
    assert [row["id"] for row in await store.due(factory, T0 + timedelta(minutes=1))] == [b["id"]]
    assert await store.claim(factory, [a["id"]], "worker", T0 + timedelta(days=1)) == []
    assert await store.retry(factory, [a["id"]], T0) == []
    async with factory() as session:
        assert (await session.get(Delivery, a["id"])).message_id == 90
        persisted = await session.get(Event, event)
        assert persisted.tg_alert_id == aids[0] and persisted.payload["text"] == "Example disk incident"


async def test_partial_batch_finalization_does_not_release_remaining_lease(db):
    _, factory, _ = db
    await store.admit(factory, [spec(stream="a"), spec(stream="b")], T0)
    a, b = await store.due(factory, T0)
    assert len(await store.claim(factory, [a["id"], b["id"]], "worker", T0)) == 2
    assert await store.finish(factory, [a["id"]], "worker", "confirmed", T0, message_id=5) == [a["id"]]
    async with factory() as session:
        assert (await session.get(Transport, a["transport_key"])).lease_owner == "worker"
    assert await store.finish(factory, [b["id"]], "worker", "confirmed", T0, message_id=6) == [b["id"]]
    async with factory() as session:
        assert (await session.get(Transport, a["transport_key"])).lease_owner == ""


async def test_history_marker_and_rows_commit_once_or_rollback_together(db):
    _, factory, _ = db
    event = (await store.admit(factory, [spec()], T0))[0]
    row = (await store.due(factory, T0))[0]
    await store.claim(factory, [row["id"]], "worker", T0)
    await store.finish(factory, [row["id"]], "worker", "confirmed", T0, message_id=2)

    async def fail(session, ids):
        session.add(AlertEvent(kind="disk", target="example", text="rolled back"))
        raise RuntimeError("history failed")

    with pytest.raises(RuntimeError):
        await store.mark_history(factory, [event], callback=fail)

    async def history(session, ids):
        assert ids == [event]
        session.add(AlertEvent(kind="disk", target="example", text="confirmed history"))

    results = await asyncio.gather(store.mark_history(factory, [event], callback=history),
                                   store.mark_history(factory, [event], callback=history))
    assert sorted(map(len, results)) == [0, 1]
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AlertEvent)) == 1


@pytest.mark.parametrize("target", [{"token": "synthetic"}, {"nested": {"password": "synthetic"}},
                                     {"url": "https://" + "user:synthetic" + "@example.org"},
                                     {"url": "https://example.org/bot123:synthetic/sendMessage"}])
async def test_credentials_rejected_transactionally(db, target):
    _, factory, _ = db
    with pytest.raises(ValueError):
        await store.admit(factory, [spec(recipients=[dict(key="one", transport="shared", target=target)])], T0)
    assert await store.due(factory, T0) == []
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


async def test_safe_errors_and_permanent_rejection_do_not_retry(db):
    _, factory, _ = db
    await store.admit(factory, [spec()], T0)
    row = (await store.due(factory, T0))[0]
    await store.claim(factory, [row["id"]], "worker", T0)
    await store.finish(factory, [row["id"]], "worker", "rejected", T0,
                       error="https://example.org/private?secret=synthetic")
    assert await store.due(factory, T0 + timedelta(days=1)) == []
    persisted = (await deliveries(factory))[0]
    assert persisted.status == "rejected" and persisted.last_error == "DeliveryError"


def test_migration_upgrade_downgrade_and_notice_default(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    path = Path(__file__).parents[1] / "alembic" / "versions" / "0149_notification_outbox.py"
    module_spec = importlib.util.spec_from_file_location("notification_outbox_migration", path)
    migration = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{(tmp_path / 'migration.db').as_posix()}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE check_incidents (id INTEGER PRIMARY KEY, notified BOOLEAN)"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            connection.execute(text("INSERT INTO check_incidents (id, notified) VALUES (1, 0)"))
            assert connection.scalar(text("SELECT notice_queued FROM check_incidents WHERE id = 1")) == 0
            inspector = inspect(connection)
            for model in (Stream, Event, Delivery, Transport):
                columns = {column["name"] for column in inspector.get_columns(model.__tablename__)}
                assert columns == set(model.__table__.columns.keys())
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert set(inspect(connection).get_table_names()) == {"check_incidents"}
            assert "notice_queued" not in {column["name"] for column in inspect(connection).get_columns("check_incidents")}
    finally:
        engine.dispose()
