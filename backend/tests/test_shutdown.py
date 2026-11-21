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
