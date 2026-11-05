"""Opt-in Qdrant alerts use known facts and require confirmed recovery."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import collector, qdrant_watch
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent, Server, TgAlert


def service(state="ok", *, source="контейнер vector-a", status="green", optimizer="ok", **kw):
    return {"kind": "qdrant", "source": source, "qdrant": {
        "state": state, "collections_total": 1,
        "collections": [{"name": "catalog", "status": status, "optimizer_status": optimizer}], **kw}}


@pytest.mark.parametrize("status", ["green", "yellow", "grey"])
def test_optimization_is_healthy(status):
    assert qdrant_watch.observe({"services": [service(status=status)]}) == ([], True)


@pytest.mark.parametrize("change", [
    {"state": "auth_required"}, {"state": "partial"}, {"truncated": True},
    {"collections": []}, {"collections_total": None}, {"collections_total": True},
    {"state": "unavailable", "reason": "limit"},
    {"collections": [{"name": "catalog"}]},
])
def test_unknown_cannot_recover(change):
    assert qdrant_watch.observe({"services": [service(**change)]}, ["контейнер vector-a"]) == ([], False)


def test_recovery_requires_every_former_source():
    assert qdrant_watch.observe({"services": [service()]}, ["контейнер vector-b"]) == ([], False)
    assert qdrant_watch.observe({"services": []}) == ([], False)
    assert qdrant_watch.observe({}) == ([], False)
    assert qdrant_watch.observe({"services": [service(), service()]}) == ([], False)


@pytest.mark.parametrize("bad", [service("unavailable", reason="timeout"),
                                      service(status="red"), service(optimizer="error")])
def test_known_failure(bad):
    problems, complete = qdrant_watch.observe({"services": [bad]}, ["контейнер vector-a"])
    assert problems and problems[0][0] == 2 and not complete
    assert qdrant_watch.tg_short(problems)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'vector.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Server(name="node-a", token_hash="x", enabled=True, backup_not_required=True,
                           qdrant_monitor_enabled=True, alert_sustain_seconds=0,
                           cpu_alert_percent=0, mem_alert_percent=0))
        await session.commit()
    sent, failures = [], []

    async def send(cfg, text, parse_mode=None):
        if failures:
            return ["delivery unavailable"]
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", send)

    async def tick(at, items, *, enabled=True, frozen=False):
        async with factory() as session:
            srv = await session.scalar(select(Server))
            srv.last_seen = at
            report = {"uptime_seconds": int(at.timestamp()), "clock_unix": at.timestamp()}
            if items is not None:
                report["services"] = items
            srv.last_report = dict(srv.last_report) if frozen else report
            srv.qdrant_monitor_enabled = enabled
            await session.commit()
        await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), at)
        async with factory() as session:
            srv = await session.scalar(select(Server))
            events = list(await session.scalars(select(AlertEvent).where(AlertEvent.kind == "qdrant")
                                                .order_by(AlertEvent.id)))
            return dict(srv.alert_state or {}), events

    yield tick, sent, failures
    await engine.dispose()


@pytest.mark.calm
async def test_opt_in_delivery_retry_no_repeats_and_one_recovery(env):
    tick, sent, failures = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    bad = [service("unavailable", reason="timeout")]
    _, events = await tick(at, bad, enabled=False)
    assert not events and not sent
    failures.append(True)
    state, events = await tick(at + timedelta(minutes=1), bad)
    assert not events and not state.get("qdrant")
    failures.clear()
    at += timedelta(minutes=2)
    state, events = await tick(at, bad)
    assert state["qdrant"] == 2 and len(events) == 1 and len(sent) == 1
    assert state["qdrant_who"] == ["контейнер vector-a"]
    assert "Qdrant недоступен" in sent[0]
    for minute in (1, 20, 120):
        state, events = await tick(at + timedelta(minutes=minute), bad)
        assert len(events) == 1 and len(sent) == 1
    at += timedelta(minutes=121)
    for minute in range(15):
        state, events = await tick(at + timedelta(minutes=minute), [service()])
        assert state["qdrant"] == 2 and len(events) == 1
    failures.append(True)
    state, events = await tick(at + timedelta(minutes=15), [service()])
    assert state["qdrant"] == 2 and len(events) == 1
    failures.clear()
    state, events = await tick(at + timedelta(minutes=16), [service()])
    assert state["qdrant"] == 0 and [e.recovery for e in events] == [False, True]
    assert "Qdrant снова в порядке" in sent[-1]
    _, events = await tick(at + timedelta(minutes=17), [service()])
    assert len(events) == 2 and len(sent) == 2


@pytest.mark.calm
async def test_unknown_resets_recovery_hold(env):
    tick, sent, _ = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    await tick(at, [service(status="red")])
    unknown = [None, [], [service("auth_required")], [service("partial")],
               [service(truncated=True)], [service(source="контейнер other")]]
    for items in unknown:
        at += timedelta(minutes=1)
        state, _ = await tick(at, [service()])
        assert state.get("qdrant_calm")
        at += timedelta(minutes=10)
        state, events = await tick(at, items)
        assert state["qdrant"] == 2 and not state.get("qdrant_calm") and len(events) == 1
    at += timedelta(minutes=1)
    await tick(at, [service()])
    state, events = await tick(at + timedelta(minutes=3), [service()], frozen=True)
    assert state["qdrant"] == 2 and not state.get("qdrant_calm") and len(events) == 1
    assert len([x for x in sent if x.kind == "qdrant"]) == 1


async def test_api_opt_in_persists_and_opt_out_is_not_recovery(client, auth_headers):
    from app.config import get_settings
    from app.db import create_engine_and_factory

    created = await client.post("/api/servers", json={"name": "vector-node"}, headers=auth_headers)
    assert created.status_code == 201
    sid = created.json()["server"]["id"]
    path = f"/api/servers/{sid}"
    assert created.json()["server"]["qdrant_monitor_enabled"] is False
    r = await client.patch(path, json={"qdrant_monitor_enabled": True}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["qdrant_monitor_enabled"] is True
    engine, factory = create_engine_and_factory(get_settings().db_url)
    try:
        async with factory() as session:
            srv = await session.get(Server, sid)
            srv.alert_state = {"qdrant": 2, "qdrant_who": ["контейнер vector-a"], "cpu": 1}
            session.add(TgAlert(ref=f"srv:{sid}", kind="qdrant", target="vector-node", text="failure"))
            await session.commit()
        r = await client.patch(path, json={"qdrant_monitor_enabled": None}, headers=auth_headers)
        assert r.status_code == 200 and r.json()["qdrant_monitor_enabled"] is True
        r = await client.patch(path, json={"qdrant_monitor_enabled": False}, headers=auth_headers)
        assert r.status_code == 200 and r.json()["qdrant_monitor_enabled"] is False
        async with factory() as session:
            srv = await session.get(Server, sid)
            assert srv.alert_state == {"cpu": 1}
            assert (await session.scalar(select(TgAlert))).resolved_at is not None
            assert not list(await session.scalars(select(AlertEvent)))
    finally:
        await engine.dispose()


async def test_qdrant_setting_respects_viewer_and_group_scope(client, auth_headers):
    from tests.test_api_access import _server, _user

    sid, _ = await _server(client, auth_headers, "vector-private", "group-a")
    viewer = await _user(client, auth_headers, "observer", role="viewer")
    outsider = await _user(client, auth_headers, "operator", server_groups=["group-b"])
    path = f"/api/servers/{sid}"
    assert (await client.patch(path, json={"qdrant_monitor_enabled": True}, headers=viewer)).status_code == 403
    assert (await client.patch(path, json={"qdrant_monitor_enabled": True}, headers=outsider)).status_code == 404
