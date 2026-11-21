"""Чистка очереди уведомлений: старое закрытое уходит, все, что еще может понадобиться, остается.

Синтетические ленты и получатели, без сети. С KERVAX_TEST_PG - те же тесты на PostgreSQL."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select, text, update

from app import alert_delivery, collector, retention, tg_bot
from app import notification_store as store
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent, NotificationDelivery as Delivery, NotificationEvent as Event
from app.models import NotificationStream as Stream

pytestmark = pytest.mark.pg

NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=90)
STALE = NOW - timedelta(days=365)
OLD = NOW - timedelta(days=200)
RECENT = NOW - timedelta(days=1)


@pytest.fixture
async def db(tmp_path):
    pg = os.environ.get("KERVAX_TEST_PG", "")
    url = pg or f"sqlite+aiosqlite:///{(tmp_path / 'retention.db').as_posix()}"
    engine, factory = create_engine_and_factory(url)
    async with engine.begin() as connection:
        if pg:
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


OPS = dict(key="recipient:ops", transport="transport:example", target={"type": "webhook", "required": True})


async def admit(factory, stream, phase, at, recipients=(OPS,), **extra):
    spec = dict(stream=stream, phase=phase, level=0 if phase == "recovery" else 1, members=[],
                observed_at=at, payload={"message": {"text": f"{stream} {phase}", "kind": "disk",
                                                     "target": stream, "section": "servers"}},
                recipients=list(recipients))
    spec.update(extra)
    return (await store.admit(factory, [spec], at))[0]


async def deliver(factory, event_id, at, *, effects=True):
    """Настоящий путь: claim, подтверждение, последствия записаны (или еще нет)."""
    async with factory() as session:
        ids = list(await session.scalars(select(Delivery.id).where(Delivery.event_id == event_id)))
    await store.claim(factory, ids, "owner-" + str(event_id), at)
    await store.finish(factory, ids, "owner-" + str(event_id), "confirmed", at, message_id=7)
    if effects:
        await alert_delivery._effects_done(factory, ids)
    return ids


async def set_status(factory, event_id, **values):
    async with factory() as session, session.begin():
        await session.execute(update(Delivery).where(Delivery.event_id == event_id).values(**values))


async def episode(factory, stream, at, *, recovery=True, effects=True):
    fire = await admit(factory, stream, "fire", at)
    await deliver(factory, fire, at)
    if not recovery:
        return fire, None
    back = await admit(factory, stream, "recovery", at + timedelta(hours=1))
    await deliver(factory, back, at + timedelta(hours=1), effects=effects)
    return fire, back


async def present(factory):
    async with factory() as session:
        return (set(await session.scalars(select(Event.id))), set(await session.scalars(select(Stream.key))),
                set(await session.scalars(select(Delivery.event_id))))


async def test_prune_keeps_everything_that_may_still_be_needed(db):
    factory = db
    gone, kept = set(), set()
    # закрытая давно лента: уходит целиком, вместе с самой лентой
    gone |= set(await episode(factory, "closed", OLD))
    # старый закрытый эпизод уходит, открытый эпизод и последнее событие остаются
    gone |= set(await episode(factory, "reopened", OLD))
    kept.add((await episode(factory, "reopened", OLD + timedelta(hours=2), recovery=False))[0])
    # открытая тревога без отбоя: ее получатели нужны будущему отбою
    kept.add((await episode(factory, "open", OLD, recovery=False))[0])
    # требует действия: uncertain, attention без просмотра, rejected - весь эпизод остается
    for name, values in (("uncertain", dict(status="uncertain", uncertain_count=1)),
                         ("attention", dict(status="attention")),
                         ("rejected", dict(status="rejected", last_error="HTTP_403"))):
        fire, back = await episode(factory, name, OLD)
        await set_status(factory, fire, **values)
        kept |= {fire, back}
    # просмотренное давно - закрыто; просмотренное вчера пока видно в списке "Просмотренные"
    fire, back = await episode(factory, "reviewed", OLD)
    await set_status(factory, fire, status="attention", reviewed_at=OLD + timedelta(days=1), reviewed_by="admin")
    gone |= {fire, back}
    fire, back = await episode(factory, "reviewed-recently", OLD)
    await set_status(factory, fire, status="attention", reviewed_at=RECENT, reviewed_by="admin")
    kept |= {fire, back}
    # отбой ждет отправки (covered_events на старую тревогу): эпизод не трогаем
    fire, back = await episode(factory, "recovery-pending", OLD)
    await set_status(factory, back, status="pending")
    kept |= {fire, back}
    # подтверждено, но последствия (история, кнопки) не записаны: их допишет _reconcile_confirmed
    kept |= set(await episode(factory, "effects", OLD, effects=False))
    # свежее не трогаем
    kept |= set(await episode(factory, "fresh", RECENT))
    # разовые события: старые уходят, последнее держит ленту, пока она наблюдается
    once = []
    for index, at in enumerate((OLD, OLD + timedelta(hours=1), RECENT)):
        once.append(await admit(factory, "once", "event", at, occurrence=f"run:{index}"))
        await deliver(factory, once[-1], at)
    gone |= set(once[:2])
    kept.add(once[2])
    # разовые давно: уходят вместе с лентой
    for index in range(2):
        event_id = await admit(factory, "once-old", "event", OLD + timedelta(hours=index), occurrence=f"run:{index}")
        await deliver(factory, event_id, OLD + timedelta(hours=index))
        gone.add(event_id)
    events_before, _, _ = await present(factory)
    assert gone | kept == events_before and not gone & kept

    done = await store.prune(factory, CUTOFF, STALE, batch=3)
    events, streams, delivered = await present(factory)
    assert events == kept
    assert delivered == kept  # у каждого оставшегося события его доставка цела
    assert {store.key(name) for name in ("closed", "reviewed", "once-old")}.isdisjoint(streams)
    assert store.key("reopened") in streams and store.key("once") in streams
    assert done == dict(events=len(gone), deliveries=len(gone), streams=3)
    async with factory() as session:
        last = set(await session.scalars(select(Stream.last_event_id)))
        assert last <= events  # last_event_id не ссылается на удаленное
    # повторный запуск ничего не меняет
    assert await store.prune(factory, CUTOFF, STALE) == dict(events=0, deliveries=0, streams=0)
    assert await present(factory) == (events, streams, delivered)

    # удаленная лента начинается заново
    again = await admit(factory, "closed", "fire", NOW)
    async with factory() as session:
        assert (await session.get(Event, again)).episode == 1


async def test_recovery_finds_recipient_of_kept_open_episode(db):
    """Отбой без получателей в спецификации берет их из тревоги открытого эпизода (fan-out admit)."""
    factory = db
    await episode(factory, "fanout", OLD)  # старый закрытый эпизод уходит
    fire, _ = await episode(factory, "fanout", OLD + timedelta(hours=2), recovery=False)
    # усиление в том же эпизоде, без получателей: последнее событие ленты теперь оно, а первую
    # тревогу держит только правило открытого эпизода
    await admit(factory, "fanout", "fire", OLD + timedelta(hours=3), recipients=(), level=2)
    await store.prune(factory, CUTOFF, STALE)
    back = await admit(factory, "fanout", "recovery", NOW, recipients=())
    async with factory() as session:
        assert await session.get(Event, fire) is not None
        rows = list(await session.scalars(select(Delivery).where(Delivery.event_id == back)))
        assert (await session.get(Event, back)).episode == 2
    assert [(row.recipient_key, row.target.get("recovery_owned")) for row in rows] == [(store.key("recipient:ops"), True)]


async def test_one_off_events_do_not_wait_for_a_problem_neighbour(db):
    """Разовая лента: старый rejected держит только себя, соседние закрытые события уходят."""
    factory = db
    rejected = await admit(factory, "digest", "event", OLD, occurrence="run:0")
    await set_status(factory, rejected, status="rejected", last_error="HTTP_403")
    closed = []
    for index in (1, 2):
        closed.append(await admit(factory, "digest", "event", OLD + timedelta(hours=index), occurrence=f"run:{index}"))
        await deliver(factory, closed[-1], OLD + timedelta(hours=index))
    last = await admit(factory, "digest", "event", RECENT, occurrence="run:3")
    await deliver(factory, last, RECENT)
    done = await store.prune(factory, CUTOFF, STALE)
    events, streams, _ = await present(factory)
    assert events == {rejected, last} and store.key("digest") in streams
    assert done == dict(events=2, deliveries=2, streams=0)


async def test_rejected_with_confirmed_fallback_and_stale_problems_are_closed(db):
    factory = db
    group = dict(key="recipient:group", transport="transport:group",
                 target={"type": "telegram", "route": "group", "bot": "common", "chat": "-1001", "required": True})
    main = dict(key="recipient:main", transport="transport:main",
                target={"type": "telegram", "route": "fallback", "bot": "common", "chat": "-1002", "required": True})
    # группа отклонила, копия в основной чат подтверждена: решать нечего
    fire = await admit(factory, "fallback", "fire", OLD, recipients=(group, main))
    async with factory() as session, session.begin():
        await session.execute(update(Delivery).where(Delivery.event_id == fire, Delivery.recipient_key == store.key(
            "recipient:group")).values(status="rejected", last_error="HTTP_403"))
        await session.execute(update(Delivery).where(Delivery.event_id == fire, Delivery.recipient_key == store.key(
            "recipient:main")).values(status="confirmed", message_id=5))
    # отбой получают оба (fan-out берет и группу из тревоги), оба подтверждены
    back = await admit(factory, "fallback", "recovery", OLD + timedelta(hours=1), recipients=(main,))
    await set_status(factory, back, status="confirmed", message_id=6)
    # без копии rejected остается проблемой, и закрытый эпизод с ним целиком
    lone = await admit(factory, "lone", "fire", OLD, recipients=(group,))
    await set_status(factory, lone, status="rejected", last_error="HTTP_403")
    lone_back = await admit(factory, "lone", "recovery", OLD + timedelta(hours=1), recipients=(group,))
    await deliver(factory, lone_back, OLD + timedelta(hours=1))
    # требующее решения дольше года уже никто не разберет
    ancient = NOW - timedelta(days=400)
    for name, values in (("ancient-attention", dict(status="attention")),
                         ("ancient-rejected", dict(status="rejected", last_error="HTTP_403"))):
        fire_old, _ = await episode(factory, name, ancient)
        await set_status(factory, fire_old, **values)
    done = await store.prune(factory, CUTOFF, STALE)
    events, streams, _ = await present(factory)
    assert events == {lone, lone_back}
    assert done == dict(events=6, deliveries=8, streams=3)


async def test_reconcile_clears_flag_already_recorded_in_target(db):
    """Флаг без работы снимается, иначе 100 таких строк навсегда займут LIMIT reconcile."""
    factory = db
    specs = [dict(stream=f"stuck:{index}", phase="fire", level=1, members=[], observed_at=NOW,
                  payload={"message": {"text": f"stuck {index}", "kind": "disk", "target": "example"}},
                  recipients=[OPS]) for index in range(101)]
    await store.admit(factory, specs, NOW)
    async with factory() as session, session.begin():
        ids = list(await session.scalars(select(Delivery.id).order_by(Delivery.id)))
        await session.execute(update(Delivery).where(Delivery.id.in_(ids[:100])).values(
            status="confirmed", effects_pending=True, target={**OPS["target"], "effects_recorded": True}))
        await session.execute(update(Delivery).where(Delivery.id == ids[100]).values(
            status="confirmed", effects_pending=True))
    await alert_delivery._reconcile_confirmed(factory, {}, NOW)
    await alert_delivery._reconcile_confirmed(factory, {}, NOW)
    async with factory() as session:
        assert not set(await session.scalars(select(Delivery.id).where(Delivery.effects_pending.is_(True))))
        assert (await session.get(Delivery, ids[100])).target["effects_recorded"]
        assert await session.scalar(select(func.count()).select_from(AlertEvent)) == 1


async def test_prune_stops_on_budget_and_resumes(db, monkeypatch):
    factory = db
    for index in range(3):
        await episode(factory, f"budget:{index}", OLD)
    clock = iter(range(1000))
    monkeypatch.setattr(store, "_monotonic", lambda: next(clock))
    # бюджет на одну пачку: следующий запуск продолжает с того же места, а не с начала
    first = await store.prune(factory, CUTOFF, STALE, batch=2, budget=1.5)
    assert first == dict(events=2, deliveries=2, streams=1) and store._resume[factory] == 2
    while any((await store.prune(factory, CUTOFF, STALE, batch=2, budget=1.5)).values()):
        pass
    events, streams, _ = await present(factory)
    assert not events and not streams and factory not in store._resume


def test_keep_outlives_buttons():
    # к удалению события его кнопки (tg_alerts) уже нет: иначе _resolve_buttons приняла бы
    # кнопку без связанного события за старую и могла бы закрыть ее чужим отбоем
    assert retention.NOTIFY_KEEP > timedelta(days=tg_bot._KEEP_DAYS)
    assert retention.NOTIFY_STALE_KEEP > retention.NOTIFY_KEEP


@pytest.mark.skipif(not os.environ.get("KERVAX_TEST_PG"), reason="PostgreSQL row locks")
async def test_admit_survives_stream_pruned_while_waiting(db):
    """prune держит ленту и удаляет ее, пока admit ждет FOR UPDATE: admit создает ленту заново."""
    factory = db
    fire, back = await episode(factory, "race", OLD)
    key = store.key("race")
    async with factory() as session, session.begin():
        await session.scalar(select(Stream).where(Stream.key == key).with_for_update())
        task = asyncio.create_task(admit(factory, "race", "fire", NOW))
        for _ in range(200):
            async with factory() as probe:
                waiting = await probe.scalar(text(
                    "select count(*) from pg_stat_activity where datname = current_database() "
                    "and wait_event_type = 'Lock'"))
            if waiting or task.done():
                break
            await asyncio.sleep(0.05)
        assert waiting and not task.done()
        await session.execute(delete(Delivery).where(Delivery.event_id.in_([fire, back])))
        await session.execute(delete(Event).where(Event.stream_key == key))
        await session.execute(delete(Stream).where(Stream.key == key))
    event_id = await task
    async with factory() as session:
        stream = await session.get(Stream, key)
        assert stream.last_event_id == event_id and (stream.seq, stream.episode) == (1, 1)


async def test_effects_pending_follows_confirmation(db):
    factory = db
    event_id = await admit(factory, "flag", "fire", NOW)
    async with factory() as session:
        ids = list(await session.scalars(select(Delivery.id).where(Delivery.event_id == event_id)))
    await store.claim(factory, ids, "owner", NOW)
    await store.finish(factory, ids, "owner", "confirmed", NOW, message_id=1)
    async with factory() as session:
        row = await session.get(Delivery, ids[0])
        assert row.effects_pending and not row.target.get("effects_recorded")
    await alert_delivery._effects_done(factory, ids)
    async with factory() as session:
        row = await session.get(Delivery, ids[0])
        assert not row.effects_pending and row.target["effects_recorded"]


async def test_scheduler_prune_cleans_notification_outbox(db):
    """Плановая чистка (collector._prune) чистит и очередь уведомлений."""
    factory = db
    fire, back = await episode(factory, "scheduled", OLD)
    keep = (await episode(factory, "scheduled-fresh", RECENT))
    await collector._prune(factory, Settings())
    events, streams, _ = await present(factory)
    assert events == set(keep)
    assert store.key("scheduled") not in streams and fire not in events and back not in events


async def test_retry_between_check_and_delete_keeps_the_batch(db, monkeypatch):
    """Повтор оператора открыл доставку после перепроверки: пачка откатывается, ничего не удалено."""
    factory = db
    fire, back = await episode(factory, "retried", NOW - timedelta(days=400))
    await set_status(factory, fire, status="attention")  # требует решения, но старше года
    async with factory() as session:
        job = await session.scalar(select(Delivery.id).where(Delivery.event_id == fire))
    # оператор нажал "повтор": доставка снова pending. Перепроверка пачки этого "не увидела":
    # ее условие подменено старым снимком (только возраст события)
    assert await store.retry(factory, [job], NOW) == [job]
    monkeypatch.setattr(store, "_prunable", lambda cutoff, stale: Event.occurred_at < cutoff)
    assert await store.prune(factory, CUTOFF, STALE) == dict(events=0, deliveries=0, streams=0)
    events, streams, _ = await present(factory)
    assert {fire, back} <= events and store.key("retried") in streams
    async with factory() as session:
        assert (await session.get(Delivery, job)).status == "pending"
