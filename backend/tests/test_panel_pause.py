"""Панель сама лежала (ребут машины, деплой, заморозка): серверы молчали из-за нее.

Сразу после подъема у всех нод last_seen на время простоя назад. Раньше главная показывала
их оффлайном, а сайты с проверкой через агента краснели с "агент не присылает результат"
и открывали инциденты, хотя отчеты просто еще не успели дойти. Льгота в несколько минут
после подъема снимает этот шум, а настоящий оффлайн после нее алертит как раньше.
"""
import asyncio
from itertools import chain, repeat
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import collector


def _srv(seen: datetime, state: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(last_seen=seen, alert_state=state or {}, offline_after_seconds=120)


def test_awaiting_after_pause(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(collector, "_PANEL_STARTED", now - timedelta(seconds=30))
    silent = _srv(now - timedelta(minutes=53))
    assert collector.awaiting_after_pause(silent, now), "молчание за простой панели принято за оффлайн"
    # уже был недоступен до простоя - так и остается недоступным
    assert not collector.awaiting_after_pause(_srv(now - timedelta(minutes=53), {"offline": 1}), now)
    # после подъема уже отчитался - судим как обычно
    assert not collector.awaiting_after_pause(_srv(now - timedelta(seconds=5)), now)
    # агента не было никогда - не повод для льготы
    assert not collector.awaiting_after_pause(_srv(None), now)
    # льгота кончилась, а отчета нет - это настоящий оффлайн
    later = now + timedelta(seconds=collector._START_GRACE_SECONDS)
    assert not collector.awaiting_after_pause(silent, later)


def test_list_shows_online_during_grace(monkeypatch):
    from app.api.servers import _is_online

    now = datetime.now(timezone.utc)
    monkeypatch.setattr(collector, "_PANEL_STARTED", now - timedelta(seconds=30))
    assert _is_online(_srv(now - timedelta(minutes=53)), now)
    assert not _is_online(_srv(now - timedelta(minutes=53), {"offline": 1}), now)
    later = now + timedelta(seconds=collector._START_GRACE_SECONDS)
    assert not _is_online(_srv(now - timedelta(minutes=53)), later)


def test_offline_alert_waits_for_grace(monkeypatch):
    """Оффлайн не алертим в льготу, после нее - алертим."""
    from app.models import Server

    now = datetime.now(timezone.utc)
    s = Server(name="node-a", token_hash="a", enabled=True, last_seen=now - timedelta(minutes=53),
               offline_after_seconds=120, alert_state={}, last_report={})
    monkeypatch.setattr(collector, "_PANEL_STARTED", now - timedelta(seconds=30))
    assert "offline" not in collector._server_conditions(s, now)
    later = now + timedelta(seconds=collector._START_GRACE_SECONDS)
    assert collector._server_conditions(s, later)["offline"][0] == 1


def test_pause_watch_restarts_grace(monkeypatch):
    """Процесс проспал больше минуты (машину замораживали) - льгота с момента пробуждения."""
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(collector, "_PANEL_STARTED", old)
    # хвост повторяется: часы читает и logging, когда пишет предупреждение
    clock = chain([1000.0, 1010.0, 1020.0], repeat(1500.0))
    monkeypatch.setattr(collector.time, "time", lambda: next(clock))
    sleeps = 0

    async def fake_sleep(_s):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(collector.pause_watch())
    except asyncio.CancelledError:
        pass
    assert collector._PANEL_STARTED > old + timedelta(minutes=59)
    assert collector.panel_just_started(datetime.now(timezone.utc))


def test_pause_watch_ignores_short_delays(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(collector, "_PANEL_STARTED", old)
    clock = chain([1000.0, 1010.0, 1035.0], repeat(1045.0))
    monkeypatch.setattr(collector.time, "time", lambda: next(clock))
    sleeps = 0

    async def fake_sleep(_s):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(collector.asyncio, "sleep", fake_sleep)
    try:
        asyncio.run(collector.pause_watch())
    except asyncio.CancelledError:
        pass
    assert collector._PANEL_STARTED == old


async def test_stale_agent_probe_after_pause_is_not_an_outage(tmp_path, monkeypatch):
    """Результат агента старше подъема панели: ждем свежий, сайт не красим и инцидент не открываем."""
    from sqlalchemy import select

    from app.config import Settings
    from app.db import create_engine_and_factory
    from app.models import AgentProbe, Base, Check, CheckIncident, CheckSample, Server

    db = (tmp_path / "pause.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    before = now - timedelta(minutes=53)
    async with factory() as s:
        srv = Server(name="node-a", token_hash="a", enabled=True, last_seen=before,
                     offline_after_seconds=120, last_report={})
        s.add(srv)
        await s.flush()
        chk = Check(name="app", type="http", target="https://app.example.ru", enabled=True,
                    probe_local=True, interval_seconds=60, probe_server_id=srv.id,
                    probe_bound_at=now - timedelta(days=1), created_at=now - timedelta(days=1))
        s.add(chk)
        await s.flush()
        s.add(AgentProbe(check_id=chk.id, server_id=srv.id, ts=before, code=200, latency_ms=50))
        await s.commit()

    settings = Settings()
    monkeypatch.setattr(collector, "_PANEL_STARTED", now - timedelta(seconds=20))
    assert await collector.run_due_checks(factory, settings) == 0, "старый результат принят за сбой"
    async with factory() as s:
        assert (await s.scalars(select(CheckSample))).all() == []
        assert (await s.scalars(select(CheckIncident))).all() == []

    # льгота вышла, свежего результата так и нет - это уже настоящая проблема
    monkeypatch.setattr(collector, "_PANEL_STARTED", now - timedelta(hours=1))
    assert await collector.run_due_checks(factory, settings) == 1
    async with factory() as s:
        rows = (await s.scalars(select(CheckSample))).all()
        assert len(rows) == 1 and rows[0].status == "down" and "агент" in rows[0].message
    await engine.dispose()


async def test_fresh_agent_probe_during_grace_is_judged(tmp_path, monkeypatch):
    """Агент уже прислал результат после подъема - судим его сразу, льгота не мешает."""
    from sqlalchemy import select

    from app.config import Settings
    from app.db import create_engine_and_factory
    from app.models import AgentProbe, Base, Check, CheckSample, Server

    db = (tmp_path / "fresh.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        srv = Server(name="node-a", token_hash="a", enabled=True, last_seen=now,
                     offline_after_seconds=120, last_report={})
        s.add(srv)
        await s.flush()
        chk = Check(name="app", type="http", target="https://app.example.ru", enabled=True,
                    probe_local=True, interval_seconds=60, probe_server_id=srv.id,
                    probe_bound_at=now - timedelta(days=1), created_at=now - timedelta(days=1))
        s.add(chk)
        await s.flush()
        s.add(AgentProbe(check_id=chk.id, server_id=srv.id, ts=now - timedelta(seconds=5),
                         code=200, latency_ms=50))
        await s.commit()

    monkeypatch.setattr(collector, "_PANEL_STARTED", now - timedelta(seconds=20))
    assert await collector.run_due_checks(factory, Settings()) == 1
    async with factory() as s:
        rows = (await s.scalars(select(CheckSample))).all()
        assert len(rows) == 1 and rows[0].status == "up"
    await engine.dispose()
