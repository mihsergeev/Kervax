"""Confirmed receipts must preserve the current incident and concurrent admission."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import alert_delivery, alerts, collector, notification_store, site_group
from app.db import Base, create_engine_and_factory
from app.models import Check


NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
RECEIPT = {"chat": "example-chat", "id": 701}


@pytest.fixture
async def database(tmp_path):
    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{(tmp_path / 'site-receipts.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def member(number):
    host = f"monitor-{number}.example.test"
    return {"name": host, "host": host, "url": "", "why": "connection refused", "ments": []}


def group(gid):
    value = site_group.new_group("example-place", "down", "Example place", None, NOW)
    value.update(id=gid, members={"1": member(1)}, kind="site", first=member(1)["name"],
                 group_scoped=True)
    return value


def message(gid, number=1):
    result = alerts.Msg("Example monitor is unavailable", "sites", kind="site",
                        target=member(number)["name"], ref=f"chk:{number}")
    result.notice = {"check_id": number, "site_group": gid}
    return result


async def test_old_episode_receipt_does_not_attach_to_reopened_group(database):
    async with database() as session:
        await site_group.save(session, {"new-episode": group("new-episode")})

    await alert_delivery._site_receipt(database,
        [({"target": {"route": "main"}}, message("old-episode"), {})], [RECEIPT])

    async with database() as session:
        current = await site_group.load(session)
    assert current["new-episode"]["msgs"] == []
    assert set(current["new-episode"]["members"]) == {"1"}


async def test_receipt_and_collector_admission_preserve_new_member(database, monkeypatch):
    async with database() as session:
        session.add_all([Check(id=number, name=member(number)["name"], type="http",
                               target=f"https://monitor-{number}.example.test/",
                               enabled=True, last_status="down") for number in (1, 2)])
        await session.commit()
        await site_group.save(session, {"open-episode": group("open-episode")})

    # Build the real collector patch before receipt arrives, as a collector tick does.
    texts, after = await collector._site_groups(
        database, {"telegram_token": "fixture-token"}, [message("open-episode", 2)],
        {0: ("down", 2, member(2))}, {}, {2: ("example-place", "Example place", None)},
        [], NOW + timedelta(seconds=1), "")
    assert len(texts) == 1 and after is not None
    admission_written, release_admission = asyncio.Event(), asyncio.Event()
    admission_committed, receipt_snapshot = asyncio.Event(), asyncio.Event()
    original_load = site_group.load
    receipt_task = None

    async def observe_load(session):
        state = await original_load(session)
        if asyncio.current_task() is receipt_task:
            # An unsafe reader can capture old state while admission owns the write
            # transaction. Keep that snapshot until the new member is committed.
            receipt_snapshot.set()
            await admission_committed.wait()
        return state

    monkeypatch.setattr(site_group, "load", observe_load)

    async def admitted(session, _index, _event_id):
        await after({0}, session=session)
        await session.flush()
        admission_written.set()
        await release_admission.wait()

    async def enqueue():
        try:
            await notification_store.admit(database, [{
                "stream": "example:monitor:2", "phase": "fire", "members": ["down"],
                "level": 1, "observed_at": NOW, "payload": {}, "recipients": [],
            }], NOW, callback=admitted)
        finally:
            admission_committed.set()

    admission_task = asyncio.create_task(enqueue())
    try:
        await asyncio.wait_for(admission_written.wait(), timeout=5)
        receipt_task = asyncio.create_task(alert_delivery._site_receipt(database,
            [({"target": {"route": "main"}}, message("open-episode"), {})], [RECEIPT]))
        # The protected reader waits for the writer. The old implementation reads
        # immediately and is held above, reproducing its lost-update interleaving.
        try:
            await asyncio.wait_for(receipt_snapshot.wait(), timeout=0.2)
        except asyncio.TimeoutError:
            pass
        release_admission.set()
        await asyncio.wait_for(asyncio.gather(admission_task, receipt_task), timeout=5)
    finally:
        release_admission.set()
        admission_committed.set()
        for task in (admission_task, receipt_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (admission_task, receipt_task) if task is not None),
                             return_exceptions=True)

    async with database() as session:
        current = await original_load(session)
    assert set(current["open-episode"]["members"]) == {"1", "2"}
    assert current["open-episode"]["msgs"] == [RECEIPT]
