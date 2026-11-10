"""Observation/recovery state survives PostgreSQL sessions and engine replacement."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app import collector
from app.checks import CheckOutcome
from app.config import Settings
from app.db import create_engine_and_factory
from app.models import Check, CheckIncident, Server
from tests.test_alert_repeat import _tick, _workers
from tests.test_alert_observation_gaps import _observe_until, _server

pytestmark = [pytest.mark.calm, pytest.mark.skipif(not os.getenv("KERVAX_TEST_PG"), reason="PostgreSQL fixture only")]


async def test_pg_workers_do_not_send_same_site_recovery_twice(client, monkeypatch):
    factory = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with factory() as session:
        check = Check(name="shop", type="http", target="https://example.com")
        session.add(check)
        await session.flush()
        session.add(CheckIncident(check_id=check.id, status="down", started_at=now - timedelta(minutes=1),
                                  last_message="HTTP 503", notified=True))
        pending = []
        await collector.record_outcome(session, check, CheckOutcome("up", message="HTTP 200"),
                                       now, pending, manual=True)
        await session.commit()

    sending = asyncio.Event()
    finish = asyncio.Event()
    sent = []

    async def capture(cfg, text, parse_mode=None):
        sent.append(text)
        sending.set()
        await finish.wait()
        return []

    monkeypatch.setattr("app.alerts.send_alert", capture)
    cfg = Settings(alert_webhook="http://fixture.invalid")
    engine, other_worker = create_engine_and_factory(os.environ["KERVAX_TEST_PG"])
    first = asyncio.create_task(collector._send_alerts(factory, cfg, pending, now))
    second = None
    try:
        await asyncio.wait_for(sending.wait(), timeout=5)
        second = asyncio.create_task(collector._send_alerts(other_worker, cfg, [], now))
        # Второй воркер уже дошел до advisory lock, пока первый держит доставку.
        async with factory() as session:
            async with asyncio.timeout(5):
                while not await session.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_locks "
                                                   "WHERE locktype = 'advisory' AND NOT granted)")):
                    await asyncio.sleep(0.01)
        assert len(sent) == 1
    finally:
        finish.set()
        await asyncio.gather(*(task for task in (first, second) if task is not None))
        await engine.dispose()
    assert len(sent) == 1


async def _env(client, monkeypatch):
    factory = client._transport.app.state.session_factory  # noqa: SLF001
    async with factory() as session:
        session.add(Server(name="worker-node", token_hash="fixture", enabled=True, backup_not_required=True,
                           last_seen=datetime.now(timezone.utc), cpu_alert_percent=0, mem_alert_percent=0))
        await session.commit()
    sent = []

    async def capture(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", capture)
    return factory, Settings(alert_webhook="http://fixture.invalid"), sent


async def test_pg_replay_gap_restart_and_confirmed_recovery(client, monkeypatch):
    factory, cfg, sent = await _env(client, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    healthy = t0 + timedelta(minutes=1)
    replay = {**_workers(healthy), "clock_unix": healthy.timestamp()}
    await _tick(factory, cfg, healthy, replay)
    await _tick(factory, cfg, t0 + timedelta(minutes=5), replay)
    # Replace the engine; progress and calm are SQL state, not process-local state.
    engine, factory = create_engine_and_factory(os.environ["KERVAX_TEST_PG"])
    try:
        await _tick(factory, cfg, t0 + timedelta(minutes=20), replay)
        assert len(sent) == 1 and (await _server(factory)).alert_state["workers"] == 2
        fresh = t0 + timedelta(minutes=21)
        await _tick(factory, cfg, fresh, _workers(fresh))
        assert len(sent) == 1
        done = t0 + timedelta(minutes=37)
        await _observe_until(factory, cfg, fresh, done)
        assert len(sent) == 2 and sent[-1].recovery
        assert not (await _server(factory)).alert_state.get("workers")
    finally:
        await engine.dispose()


async def test_pg_known_confirmation_gap_preserves_note_and_state(client, monkeypatch):
    factory, cfg, sent = await _env(client, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    async with factory() as session:
        server = (await session.scalars(select(Server))).one()
        server.known_issues = {"workers": {"text": "repair planned", "by": "admin", "at": t0.isoformat(),
                                          "level": 2, "sig": ["Supervisor, контейнер web-a-workers-1|mailer"]}}
        server.alert_state = {"fixture_marker": "keep"}
        await session.commit()
    await _tick(factory, cfg, t0, _workers(t0))
    await _tick(factory, cfg, t0 + timedelta(minutes=12), {})
    engine, factory = create_engine_and_factory(os.environ["KERVAX_TEST_PG"])
    try:
        server = await _server(factory)
        assert server.known_issues["workers"]["text"] == "repair planned"
        assert not server.known_issues["workers"].get("gone_at")
        assert server.alert_state["fixture_marker"] == "keep"
        returned = t0 + timedelta(minutes=13)
        await _tick(factory, cfg, returned, _workers(returned))
        assert (await _server(factory)).known_issues
        confirmed = returned + timedelta(minutes=11)
        await _observe_until(factory, cfg, returned, confirmed)
        server = await _server(factory)
        assert not server.known_issues and server.alert_state["fixture_marker"] == "keep"
    finally:
        await engine.dispose()
