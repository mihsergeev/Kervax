"""Notification lifecycle: brief incidents stay silent, sustained ones remain actionable."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import collector, forecast_notice
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Server

START = datetime(2026, 11, 14, 6, tzinfo=timezone.utc)
pytestmark = pytest.mark.calm


async def _panel(tmp_path, monkeypatch):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'panel.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Server(name="node-a", token_hash="x", enabled=True, backup_not_required=True,
                           last_seen=START, cpu_alert_percent=0, mem_alert_percent=0,
                           offline_after_seconds=120, alert_sustain_seconds=0))
        await session.commit()
    sent = []

    async def send(cfg, message, parse_mode=None):
        sent.append(message)
        return []

    monkeypatch.setattr("app.alerts.send_alert", send)
    monkeypatch.setattr(collector, "_daytime", lambda *args: True)
    return engine, factory, sent


async def _tick(factory, minute, *, flux=None, forecast=False, eta=72, mount="/data", pct=None):
    now = START + timedelta(minutes=minute)
    pct = 78 + minute / 300 if pct is None else pct
    report = {"clock_unix": now.timestamp(), "uptime_seconds": 100000 + minute * 60,
              "disks": [{"mount": mount, "used": round(pct * 100), "total": 10000}]}
    if flux is not None:
        report["flux"] = flux
    async with factory() as session:
        srv = await session.scalar(select(Server))
        srv.last_seen, srv.last_report = now, report
        if forecast:
            # Production recomputes the forecast every half-hour; fresh reports
            # in between cannot lend the same snapshot more confirmation time.
            stamp = START + timedelta(minutes=minute // 30 * 30)
            sample_pct = 78 + (minute // 30 * 30) / 300 if pct is None else pct
            srv.disk_forecast = {"ts": stamp.timestamp(), "items": [] if eta is None else [
                {"mount": mount, "kind": "space", "pct": sample_pct, "rate": 4.8, "eta_h": eta}]}
        await session.commit()
    await collector.evaluate_servers(factory, Settings(alert_webhook="https://hook.example"), now)


def _flux(where="flux-system/source", ready=False):
    return [{"kind": "GitRepository", "where": where, "ready": ready,
             "reason": "Succeeded" if ready else "GitOperationFailed", "message": "i/o timeout"}]


async def test_eighteen_minute_flux_failure_has_no_alert_or_recovery(tmp_path, monkeypatch):
    engine, factory, sent = await _panel(tmp_path, monkeypatch)
    for minute in range(18):
        await _tick(factory, minute, flux=_flux())
    for minute in range(18, 40):
        await _tick(factory, minute, flux=_flux(ready=True))
    assert sent == []
    await engine.dispose()


async def test_flux_hour_new_object_and_recovery_are_not_duplicates(tmp_path, monkeypatch):
    engine, factory, sent = await _panel(tmp_path, monkeypatch)
    for minute in range(61):
        await _tick(factory, minute, flux=_flux())
    assert len(sent) == 1 and "Flux не выкатывает" in sent[0]
    assert "токен" not in sent[0]  # a generic Git error does not establish an authentication failure
    for minute in range(61, 80):
        await _tick(factory, minute, flux=_flux("flux-system/other"))
    assert len(sent) == 1
    for minute in range(80, 122):
        await _tick(factory, minute, flux=_flux("flux-system/other"))
    assert len(sent) == 2 and "flux-system/other" in sent[-1]
    for minute in range(122, 145):
        await _tick(factory, minute, flux=_flux("flux-system/other", ready=True))
    assert len(sent) == 3 and sent[-1].startswith("✅")
    await engine.dispose()


async def test_short_forecast_has_no_alert_or_recovery(tmp_path, monkeypatch):
    engine, factory, sent = await _panel(tmp_path, monkeypatch)
    for minute in range(49):
        await _tick(factory, minute, forecast=True)
    for minute in range(49, 91):
        await _tick(factory, minute, forecast=True, eta=None)
    assert sent == []
    await engine.dispose()


async def test_sustained_growth_sends_one_forecast_then_one_observed_recovery(tmp_path, monkeypatch):
    engine, factory, sent = await _panel(tmp_path, monkeypatch)
    for minute in range(361):
        await _tick(factory, minute, forecast=True)
    assert len(sent) == 1 and "Диск /data заполнится" in sent[0]
    async with factory() as session:
        srv = await session.scalar(select(Server))
        assert srv.alert_state[forecast_notice.STATE]["members"]["space:/data"]["active"] == 1
    for minute in range(361, 380):
        await _tick(factory, minute, forecast=True)
    assert len(sent) == 1
    for minute in range(380, 421):
        await _tick(factory, minute, forecast=True, eta=None)
    assert len(sent) == 2 and sent[-1].startswith("✅")
    await engine.dispose()


async def test_imminent_forecast_and_critical_fullness_do_not_wait_six_hours(tmp_path, monkeypatch):
    engine, factory, sent = await _panel(tmp_path, monkeypatch)
    await _tick(factory, 0, forecast=True, eta=5)
    assert len(sent) == 1 and "Диск /data заполнится через 5 ч" in sent[0]
    await _tick(factory, 1, forecast=False, pct=97)
    assert len(sent) == 2 and "заполнен на 97%" in sent[-1]
    await engine.dispose()


async def test_new_warning_keeps_its_severity_and_delivery_state(tmp_path, monkeypatch):
    engine, factory, sent = await _panel(tmp_path, monkeypatch)
    state = None
    for minute in range(0, 361, 30):
        at = START + timedelta(minutes=minute)
        items = [{"mount": "/old", "kind": "space", "pct": 70, "rate": 12, "eta_h": 5},
                 {"mount": "/new", "kind": "space", "pct": 78 + minute / 300, "rate": 4.8, "eta_h": 72}]
        result = forecast_notice.evaluate(state, items, at.timestamp(), at,
                                          announced_level=2 if minute else 0,
                                          observed={("/old", "space"), ("/new", "space")})
        state = forecast_notice.mark_announced(result.state) if minute == 0 else result.state
    assert state["members"]["space:/new"]["ready"] == 1
    before = START + timedelta(minutes=359)
    async with factory() as session:
        srv = await session.scalar(select(Server))
        srv.alert_state = {"disk_forecast": 2, "disk_forecast_sig": ["space:/old"],
                           "disk_forecast_told": ["space:/old"], forecast_notice.STATE: state,
                           "report_progress": {"clock": before.timestamp(), "up": 100000 + 359 * 60,
                                               "at": before.isoformat()}}
        await session.commit()
    monkeypatch.setattr(collector, "_daytime", lambda *args: False)
    await _tick(factory, 360, forecast=True, mount="/new")
    assert sent == []  # an old critical incident does not make a new warning urgent
    monkeypatch.setattr(collector, "_daytime", lambda *args: True)
    await _tick(factory, 361, forecast=True, mount="/new")
    assert len(sent) == 1 and sent[0].startswith("⚠️💽") and "Диск /new" in sent[0]
    async with factory() as session:
        srv = await session.scalar(select(Server))
        assert srv.alert_state[forecast_notice.STATE]["members"]["space:/new"]["active"] == 1
        srv.disk_forecast = {"ts": START.timestamp(), "items": []}
        await session.commit()
    await _tick(factory, 362, mount="/new")
    async with factory() as session:
        srv = await session.scalar(select(Server))
        assert srv.alert_state[forecast_notice.STATE]["members"]["space:/new"]["active"] == 1
    assert len(sent) == 1  # a source gap cannot drop an announced member or claim recovery
    await engine.dispose()
