"""PostgreSQL: параллельные писатели app_settings не теряют записи, бэкап читает один снимок."""
import asyncio
import json
import os

import pytest
from sqlalchemy import event, select

from app import alert_delivery, alerts, backup, collector, settings_store
from app.models import AppSetting
from tests.test_alert_delivery import NOW, database  # noqa: F401
from tests.test_alert_routes import _cfg

pytestmark = pytest.mark.skipif(not os.getenv("KERVAX_TEST_PG"), reason="PostgreSQL fixture only")

KEY = settings_store.MAIN_PASSED_KEY


async def _passed(factory):
    async with factory() as session:
        return await settings_store.get_main_passed(session)


async def _race(factory, writer):
    """Другой писатель держит строку (FOR UPDATE) и дописывает свою пару, пока второй уже пишет."""
    async with factory() as session:
        session.add(AppSetting(key=KEY, value="[]"))
        await session.commit()
    locked, release = asyncio.Event(), asyncio.Event()

    async def holder():
        async with factory() as session, session.begin():
            row = await session.scalar(select(AppSetting).where(AppSetting.key == KEY).with_for_update())
            row.value = json.dumps(json.loads(row.value) + [["disk", "held"]])
            await session.flush()
            locked.set()
            await release.wait()

    first = asyncio.create_task(holder())
    await asyncio.wait_for(locked.wait(), 5)
    second = asyncio.create_task(writer())
    await asyncio.sleep(0.5)  # второй прочитал (старый код) или ждет блокировку (новый)
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 10)
    return await _passed(factory)


async def test_note_passed_waits_for_a_concurrent_writer(database):
    factory, _url = database
    passed = await _race(factory, lambda: alerts.note_passed(factory, [["offline", "web-a"]]))
    assert passed == [["disk", "held"], ["offline", "web-a"]]


async def test_admission_patch_waits_for_a_concurrent_writer(database):
    factory, _url = database
    cfg = _cfg("all", dup_pause={"on": True, "until": ""})
    message = collector._server_alert_text("offline", "node-1", "агент не шлет метрики", group="web", ref="srv:1")
    passed = await _race(factory, lambda: alert_delivery.admit(factory, cfg, [message], 0, "HTML", NOW))
    assert passed == [["disk", "held"], ["offline", "node-1"]]


async def test_first_writers_insert_the_row_once(database):
    factory, _url = database
    inserted, release = asyncio.Event(), asyncio.Event()

    async def first():
        async with factory() as session, session.begin():
            await settings_store.update_main_passed(session, lambda current: current + [["disk", "a"]])
            await session.flush()
            inserted.set()
            await release.wait()

    async def second():
        async with factory() as session, session.begin():
            await settings_store.update_main_passed(session, lambda current: current + [["disk", "b"]])

    one = asyncio.create_task(first())
    await asyncio.wait_for(inserted.wait(), 5)
    two = asyncio.create_task(second())
    await asyncio.sleep(0.5)
    assert not two.done()  # ждет вставку первого, а не падает на ключе
    release.set()
    await asyncio.wait_for(asyncio.gather(one, two), 10)
    assert await _passed(factory) == [["disk", "a"], ["disk", "b"]]


async def test_backup_export_reads_one_snapshot(database):
    from sqlalchemy.util import await_only

    factory, _url = database
    engine = factory.kw["bind"]
    late = []

    async def write_late():
        async with factory() as session:
            session.add(AppSetting(key="fixture_late", value="written during export"))
            await session.commit()

    def before(_conn, _cursor, statement, *_args):
        # первые таблицы уже прочитаны, app_settings - последняя: запись между SELECT экспорта
        if not late and "FROM app_settings" in statement:
            late.append(True)
            await_only(write_late())

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        async with factory() as session:
            data = await backup.export_data(session)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    assert late
    async with factory() as session:
        assert await session.get(AppSetting, "fixture_late") is not None
    assert "fixture_late" not in {row["key"] for row in data["tables"]["app_settings"]}


async def test_two_writers_meet_at_the_write_and_keep_both_pairs(database):
    # Без внешнего держателя: оба писателя доходят до своей записи, если строку не заперли.
    # Барьер перед UPDATE ждет второго: без FOR UPDATE оба прочитали пустой список
    from sqlalchemy.util import await_only

    factory, _url = database
    engine = factory.kw["bind"]
    async with factory() as session:
        session.add(AppSetting(key=KEY, value="[]"))
        await session.commit()
    arrived, both = [], asyncio.Event()

    async def barrier():
        arrived.append(True)
        if len(arrived) == 2:
            both.set()
        try:
            await asyncio.wait_for(both.wait(), 1)
        except asyncio.TimeoutError:
            pass  # второй ждет блокировку строки: так и должно быть

    def before(_conn, _cursor, statement, *_args):
        if statement.startswith("UPDATE app_settings"):
            await_only(barrier())

    async def writer(pair):
        async with factory() as session, session.begin():
            await settings_store.update_main_passed(session, lambda current: current + [pair])

    event.listen(engine.sync_engine, "before_cursor_execute", before)
    try:
        await asyncio.wait_for(asyncio.gather(writer(["disk", "a"]), writer(["disk", "b"])), 10)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before)
    assert sorted(await _passed(factory)) == [["disk", "a"], ["disk", "b"]]
