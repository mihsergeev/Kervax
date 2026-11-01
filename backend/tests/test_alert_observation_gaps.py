"""Recovery needs continuous observed health, not elapsed time over collection gaps."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import collector
from app.models import Server
from tests.test_alert_repeat import _env, _factory, _tick, _workers

pytestmark = pytest.mark.calm


def _unknown(t, reason):
    if reason == "missing":
        return {}
    rep = _workers(t)
    if reason == "stale":
        rep["extras"]["workers"]["ts"] -= 3600
    else:
        rep["extras"]["workers"]["daemons"] = []
        rep["extras"]["workers"]["skipped"] = [
            {"where": "container web-a-workers-1", "kind": "supervisor", "why": "down"}]
    return rep


async def _server(factory):
    async with factory() as session:
        return (await session.scalars(select(Server))).one()


async def _observe_until(factory, cfg, start, end, reporter=_workers):
    """A recovery hold consists of fresh observations, not just two distant points."""
    point = start
    while point < end:
        point = min(point + timedelta(minutes=2), end)
        await _tick(factory, cfg, point, reporter(point))


@pytest.mark.parametrize("reason", ["missing", "stale", "skipped"])
async def test_gap_breaks_recovery_hold_across_restart(tmp_path, monkeypatch, reason):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    healthy = t0 + timedelta(minutes=1)
    await _tick(factory, cfg, healthy, _workers(healthy))
    assert len(sent) == 1 and (await _server(factory)).alert_state.get("workers_calm")
    gap = t0 + timedelta(minutes=2)
    await _tick(factory, cfg, gap, _unknown(gap, reason))
    await engine.dispose()
    engine, factory = _factory(tmp_path / "s.db")
    # The gap lasted longer than the settle window, but is not observed health.
    resumed = t0 + timedelta(minutes=20)
    await _tick(factory, cfg, resumed, _workers(resumed))
    assert len(sent) == 1, "unknown collection time falsely satisfied the recovery hold"
    confirmed = resumed + timedelta(minutes=16)
    await _observe_until(factory, cfg, resumed, confirmed)
    assert len(sent) == 2 and sent[-1].recovery
    await engine.dispose()
    engine, factory = _factory(tmp_path / "s.db")
    await _tick(factory, cfg, confirmed + timedelta(minutes=1), _workers(confirmed + timedelta(minutes=1)))
    assert len(sent) == 2
    await engine.dispose()


@pytest.mark.parametrize("reason", ["missing", "stale", "skipped"])
async def test_unknown_collection_does_not_close_known_issue(tmp_path, monkeypatch, reason):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    async with factory() as session:
        server = (await session.scalars(select(Server))).one()
        server.known_issues = {"workers": {"text": "repair planned", "by": "admin", "at": t0.isoformat(),
                                          "level": 2, "sig": ["Supervisor, контейнер web-a-workers-1|mailer"]}}
        await session.commit()
    await _tick(factory, cfg, t0, _workers(t0))
    assert (await _server(factory)).known_issues["workers"].get("gone_at")
    unknown = t0 + timedelta(minutes=12)
    await _tick(factory, cfg, unknown, _unknown(unknown, reason))
    entry = ((await _server(factory)).known_issues or {}).get("workers")
    assert entry is not None and not entry.get("gone_at"), "unknown input closed a known problem"
    await engine.dispose()


async def test_partial_collection_still_announces_new_critical_worker(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    t1 = t0 + timedelta(minutes=1)
    rep = _workers(t1, fatal=["billing"])
    rep["extras"]["workers"]["skipped"] = [
        {"where": "container other-workers", "kind": "supervisor", "why": "down"}]
    await _tick(factory, cfg, t1, rep)
    assert len(sent) == 2 and "billing" in sent[-1]
    assert not sent[-1].recovery
    await engine.dispose()


@pytest.mark.parametrize("offline_after", [120, 3600])
async def test_replayed_healthy_report_cannot_fill_hold_after_restart(tmp_path, monkeypatch, offline_after):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    async with factory() as session:
        (await session.scalars(select(Server))).one().offline_after_seconds = offline_after
        await session.commit()
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    healthy = t0 + timedelta(minutes=1)
    old = {**_workers(healthy), "clock_unix": healthy.timestamp()}
    await _tick(factory, cfg, healthy, old)
    await engine.dispose()
    engine, factory = _factory(tmp_path / "s.db")
    for minute in (5, 10, 18):
        await _tick(factory, cfg, t0 + timedelta(minutes=minute), old)
    assert len(sent) == 1 and (await _server(factory)).alert_state["workers"] == 2
    fresh = t0 + timedelta(minutes=20)
    await _tick(factory, cfg, fresh, _workers(fresh))
    assert len(sent) == 1
    done = fresh + timedelta(minutes=16)
    await _observe_until(factory, cfg, fresh, done)
    assert len(sent) == 2 and sent[-1].recovery
    await engine.dispose()


async def test_offline_time_does_not_close_known_issue_after_return(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    async with factory() as session:
        server = (await session.scalars(select(Server))).one()
        server.known_issues = {"workers": {"text": "repair planned", "by": "admin", "at": t0.isoformat(),
                                          "level": 2, "sig": ["Supervisor, контейнер web-a-workers-1|mailer"]}}
        await session.commit()
    await _tick(factory, cfg, t0, _workers(t0))
    await collector.evaluate_servers(factory, cfg, t0 + timedelta(minutes=12))
    entry = ((await _server(factory)).known_issues or {}).get("workers")
    assert entry is not None and not entry.get("gone_at")
    returned = t0 + timedelta(minutes=13)
    await _tick(factory, cfg, returned, _workers(returned))
    assert ((await _server(factory)).known_issues or {}).get("workers")
    await engine.dispose()


async def test_mass_failure_is_one_message_and_one_recovery_across_restart(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    async with factory() as session:
        (await session.scalars(select(Server))).one().name = "worker-node-1"
        for i in range(2, 9):
            session.add(Server(name=f"worker-node-{i}", token_hash=f"fixture-{i}", enabled=True,
                               backup_not_required=True, last_seen=t0, cpu_alert_percent=0, mem_alert_percent=0))
        await session.commit()

    async def all_report(t, bad):
        async with factory() as session:
            for server in await session.scalars(select(Server)):
                server.last_seen = t
                server.last_report = {"uptime_seconds": 100000, "clock_unix": t.timestamp(),
                                      **_workers(t, fatal=["mailer"] if bad else [])}
            await session.commit()
        await collector.evaluate_servers(factory, cfg, t)

    await all_report(t0, True)
    assert len(sent) == 1 and len(sent[0].parts) == 8
    await all_report(t0 + timedelta(minutes=1), True)
    await engine.dispose()
    engine, factory = _factory(tmp_path / "s.db")
    await all_report(t0 + timedelta(minutes=2), True)
    assert len(sent) == 1
    await all_report(t0 + timedelta(minutes=3), False)
    for minute in range(5, 20, 2):
        await all_report(t0 + timedelta(minutes=minute), False)
    assert len(sent) == 2 and sent[-1].recovery and len(sent[-1].parts) == 8
    await all_report(t0 + timedelta(minutes=20), False)
    assert len(sent) == 2
    await engine.dispose()


async def test_alternating_delayed_healthy_reports_do_not_resolve_fresh_failure(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    healthy_reports = []
    for minute in (0, 1):
        t = t0 + timedelta(minutes=minute)
        report = {**_workers(t), "clock_unix": t.timestamp(), "uptime_seconds": 100000 + minute * 60}
        healthy_reports.append(report)
        await _tick(factory, cfg, t, report)
    failed = t0 + timedelta(minutes=2)
    await _tick(factory, cfg, failed, {**_workers(failed, fatal=["mailer"]), "uptime_seconds": 100120})
    assert len(sent) == 1
    await engine.dispose()
    engine, factory = _factory(tmp_path / "s.db")
    for minute in (3, 6, 10, 15, 21):
        await _tick(factory, cfg, t0 + timedelta(minutes=minute), healthy_reports[minute % 2])
    state = (await _server(factory)).alert_state
    assert len(sent) == 1 and state["workers"] == 2 and not state.get("workers_calm")
    fresh = t0 + timedelta(minutes=22)
    await _tick(factory, cfg, fresh, {**_workers(fresh), "uptime_seconds": 101320})
    assert len(sent) == 1
    done = fresh + timedelta(minutes=16)
    await _observe_until(factory, cfg, fresh, done,
                         lambda t: {**_workers(t), "uptime_seconds": 100000 + int((t - t0).total_seconds())})
    assert len(sent) == 2 and sent[-1].recovery
    await engine.dispose()


@pytest.mark.parametrize("reason", ["missing", "stale", "skipped"])
async def test_state_fixed_acknowledgment_does_not_confirm_unknown_source(tmp_path, monkeypatch, reason):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    async with factory() as session:
        (await session.scalars(select(Server))).one().alert_fixed = {
            "workers": {"at": t0.isoformat(), "by": "admin", "text": "repair attempted"}}
        await session.commit()
    t1 = t0 + timedelta(minutes=1)
    await _tick(factory, cfg, t1, _unknown(t1, reason))
    server = await _server(factory)
    assert not server.alert_fixed["workers"].get("done")
    assert server.alert_state["workers"] == 2 and len(sent) == 1
    await engine.dispose()


@pytest.mark.parametrize("reason", ["missing", "replayed"])
async def test_unit_own_recovery_hold_does_not_count_unknown_time(tmp_path, monkeypatch, reason):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)

    def units(t, failed=False):
        rows = [{"unit": "fixture-worker.service", "type": "simple", "result": "start-limit-hit",
                 "status": 1, "since": int(t.timestamp()) - 180, "log": []}] if failed else []
        return {"extras": {"units": {"v": 1, "ts": int(t.timestamp()), "units": rows}}}

    await _tick(factory, cfg, t0, units(t0, True))
    assert len(sent) == 1 and sent[0].kind == "units"
    healthy = t0 + timedelta(minutes=1)
    old = {**units(healthy), "clock_unix": healthy.timestamp()}
    await _tick(factory, cfg, healthy, old)
    gap = t0 + timedelta(minutes=5)
    await _tick(factory, cfg, gap, {} if reason == "missing" else old)
    await engine.dispose()
    engine, factory = _factory(tmp_path / "s.db")
    resumed = t0 + timedelta(minutes=30)
    await _tick(factory, cfg, resumed, units(resumed))
    assert len(sent) == 1 and (await _server(factory)).alert_state["units"]
    confirmed = resumed + timedelta(minutes=21)
    await _observe_until(factory, cfg, resumed, confirmed, units)
    assert len(sent) == 2 and sent[-1].recovery
    await engine.dispose()


async def test_new_healthy_report_after_scheduler_downtime_starts_hold_again(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    healthy = t0 + timedelta(minutes=1)
    await _tick(factory, cfg, healthy, _workers(healthy))
    await engine.dispose()
    engine, factory = _factory(tmp_path / "s.db")
    # The scheduler did not observe the intervening twenty minutes.
    resumed = t0 + timedelta(minutes=21)
    await _tick(factory, cfg, resumed, _workers(resumed))
    assert len(sent) == 1 and (await _server(factory)).alert_state["workers"] == 2
    await _observe_until(factory, cfg, resumed, resumed + timedelta(minutes=16))
    assert len(sent) == 2 and sent[-1].recovery
    await engine.dispose()


async def test_partial_warning_does_not_lower_unobserved_critical_level(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    t1 = t0 + timedelta(minutes=1)
    partial = _workers(t1, fatal=["billing"])
    block = partial["extras"]["workers"]
    block["daemons"][0]["progs"][0]["state"] = "EXITED"
    block["skipped"] = [{"where": "container unavailable-workers", "kind": "supervisor", "why": "down"}]
    await _tick(factory, cfg, t1, partial)
    state = (await _server(factory)).alert_state
    assert state["workers"] == 2 and any("mailer" in key for key in state["workers_sig"])
    assert not any(message.recovery for message in sent)
    await engine.dispose()


async def test_long_traffic_cooldown_does_not_count_scheduler_downtime(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    async with factory() as session:
        server = (await session.scalars(select(Server))).one()
        server.alert_state = {"traffic": 1, "traffic_cool": t0.isoformat(), "traffic_from": t0.isoformat(),
                              "report_progress": {"clock": t0.timestamp(), "up": 100000, "at": t0.isoformat()}}
        await session.commit()
    resumed = t0 + timedelta(hours=13)
    await _tick(factory, cfg, resumed, {})
    state = (await _server(factory)).alert_state
    assert state["traffic"] == 1 and state["traffic_cool"] == resumed.isoformat()
    await _observe_until(factory, cfg, resumed, resumed + timedelta(minutes=16))
    assert not sent and (await _server(factory)).alert_state["traffic"] == 1
    await engine.dispose()


@pytest.mark.parametrize("kind,block,marker,field,sig", [
    ("kube_restarts", "kube", "pods_complete", "pods", "pod:default/fixture-worker"),
    ("docker_restarts", "docker", "containers_complete", "containers", "docker:fixture-worker"),
])
async def test_partial_bulk_inventory_never_fills_restart_hold(tmp_path, monkeypatch, kind, block, marker, field, sig):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    async with factory() as session:
        server = (await session.scalars(select(Server))).one()
        server.alert_state = {kind: 1, kind + "_sig": [sig], "restarts": {},
                              kind + "_hold": {sig: t0.isoformat()}}
        await session.commit()
    for minute in range(0, 42, 2):
        now = t0 + timedelta(minutes=minute)
        await _tick(factory, cfg, now, {"agent_version": "2.62", block: {"access": True, marker: False, field: []}})
    assert (await _server(factory)).alert_state[kind] == 1 and not sent
    await engine.dispose()


async def test_reboot_with_clock_correction_can_report_new_critical_and_recover(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    future = t0 + timedelta(days=1)
    await _tick(factory, cfg, t0, {**_workers(future, fatal=["mailer"]),
                                 "clock_unix": future.timestamp(), "uptime_seconds": 7 * 86400})
    assert len(sent) == 1
    # The existing reboot warm-up intentionally holds worker alarms for ten
    # minutes; ordering must not extend that hold until the old clock catches up.
    for minute in range(1, 12):
        now = t0 + timedelta(minutes=minute)
        await _tick(factory, cfg, now, {**_workers(now, fatal=["billing"]), "uptime_seconds": minute * 60})
        if minute == 1:
            await engine.dispose()
            engine, factory = _factory(tmp_path / "s.db")
    assert len(sent) == 2 and "billing" in sent[-1]
    healthy = t0 + timedelta(minutes=12)
    await _tick(factory, cfg, healthy, {**_workers(healthy), "uptime_seconds": 720})
    await _observe_until(factory, cfg, healthy, healthy + timedelta(minutes=16),
                         lambda t: {**_workers(t), "uptime_seconds": int((t - t0).total_seconds())})
    assert len(sent) == 3 and sent[-1].recovery
    await engine.dispose()
