"""Остановка приложения (выкладка): планировщик доделывает отправку, отмена дожидается."""
import asyncio
import secrets

import pytest

from app import config


@pytest.fixture
async def main(tmp_path, monkeypatch):
    monkeypatch.setenv("KERVAX_DB_URL", f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    monkeypatch.setenv("KERVAX_DATA_DIR", (tmp_path / "data").as_posix())
    monkeypatch.setenv("KERVAX_ADMIN_USER", "admin")
    monkeypatch.setenv("KERVAX_ADMIN_PASSWORD", "testpass-2026")
    monkeypatch.setenv("KERVAX_JWT_SECRET", secrets.token_hex(32))
    monkeypatch.setenv("KERVAX_RUN_SCHEDULER", "1")
    config.get_settings.cache_clear()
    from app import main

    yield main
    config.get_settings.cache_clear()


async def _run(main):
    from app.db import Base

    app = main.create_app()
    async with app.state.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)


async def test_shutdown_lets_the_running_send_finish(main, monkeypatch):
    events = []

    async def loop(_factory, _settings, stop=None):
        events.append("started")
        await stop.wait()
        await asyncio.sleep(0.2)  # POST, начатый до сигнала
        events.append("finished")

    monkeypatch.setattr(main, "collector_loop", loop)
    await _run(main)
    assert events == ["started", "finished"]


async def test_shutdown_cancels_a_stuck_tick_and_waits_for_it(main, monkeypatch):
    events = []

    async def loop(_factory, _settings, stop=None):
        try:
            await asyncio.sleep(3600)
        finally:
            await asyncio.sleep(0.3)  # отмена тоже доделывает работу
            events.append("cleaned")

    monkeypatch.setattr(main, "collector_loop", loop)
    monkeypatch.setattr(main, "_STOP_GRACE", 0.1, raising=False)
    await _run(main)
    assert events == ["cleaned"]


async def test_stop_signal_skips_the_rest_of_the_tick(tmp_path, monkeypatch):
    # SIGTERM пришел во время первой стадии: она доделывается, следующие ждут нового старта
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'tick.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    stop, ran = asyncio.Event(), []

    async def first(*_args):
        await asyncio.sleep(0.05)
        stop.set()
        ran.append("first")

    async def later(*_args, **_kwargs):
        ran.append("later")
        return 0

    monkeypatch.setattr(collector, "rebind_local_probes", first)
    for name in ("run_due_checks", "evaluate_servers", "_maybe_auto_backup", "_deliver_pending"):
        monkeypatch.setattr(collector, name, later)
    try:
        await asyncio.wait_for(collector.collector_loop(factory, Settings(scheduler_tick=30), stop), 30)
    finally:
        await engine.dispose()
    assert ran == ["first"]


@pytest.mark.parametrize("elapsed, expected", [
    (None, 42.0), (-5.0, 42.0), (0.0, 42.0), (12.0, 30.0), (20.0, 22.0), (41.5, 0.5), (42.0, 0.0), (90.0, 0.0)])
def test_delivery_wait_fits_the_rest_of_the_stop_budget(main, elapsed, expected):
    # худшая пачка (правка группы, повтор после 400, вебхук) идет ~42 с: ждем ее весь остаток 45 с
    # минус запас на закрытие базы, но не дольше - после 45 с main отменяет планировщик
    from app import collector

    assert main._STOP_GRACE == collector.STOP_BUDGET == 45.0
    assert collector._SETTLE_DELIVERY == collector.STOP_BUDGET - collector._CLOSE_RESERVE
    assert collector._settle_timeout(elapsed) == expected


async def test_stop_does_not_wait_for_a_send_past_the_budget(tmp_path, monkeypatch):
    # POST завис: планировщик ждет его только остаток бюджета, а не фиксированные 30 с
    from app import alert_delivery, collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'settle.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(collector, "_CLOSE_RESERVE", collector.STOP_BUDGET - 0.5)
    stuck = asyncio.get_running_loop().create_future()
    alert_delivery._in_flight.add(stuck)
    stop = asyncio.Event()
    stop.set()
    started = asyncio.get_running_loop().time()
    try:
        await asyncio.wait_for(collector.collector_loop(factory, Settings(scheduler_tick=30), stop), 10)
    finally:
        alert_delivery._in_flight.discard(stuck)
        stuck.cancel()
        await engine.dispose()
    assert asyncio.get_running_loop().time() - started < 5


async def test_repeated_cancel_during_shutdown_still_resets_the_delivery_stop(tmp_path, monkeypatch):
    # отмену планировщика повторили, пока он ждет выхода бота: сигнал остановки доставки все равно снят
    from app import alert_delivery, collector, tg_bot
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'reset.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    for name in ("rebind_local_probes", "run_due_checks", "evaluate_servers", "_maybe_auto_backup",
                 "_deliver_pending"):
        monkeypatch.setattr(collector, name, _idle)
    cancelled, bots = asyncio.Event(), []

    async def slow_bot(*_args, **_kwargs):
        bots.append(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await asyncio.sleep(30)  # выход бота затянулся: планировщик ждет его в finally

    monkeypatch.setattr(tg_bot, "poll_loop", slow_bot)
    seen = []

    async def run():
        try:
            await collector.collector_loop(factory, Settings(scheduler_tick=30), asyncio.Event())
        finally:
            seen.append(alert_delivery.STOP.get())

    task = asyncio.create_task(run())
    try:
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.wait_for(cancelled.wait(), 5)
        await asyncio.sleep(0.05)
        task.cancel()  # повторная отмена посреди ожидания бота
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
    finally:
        for bot in bots:
            bot.cancel()
        await asyncio.gather(*bots, return_exceptions=True)
        await engine.dispose()
    assert seen == [None]


async def _idle(*_args, **_kwargs):
    return 0
