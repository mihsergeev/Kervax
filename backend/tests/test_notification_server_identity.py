"""Real server producers keep one-off events and per-instance episodes distinct."""
import copy
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import alerts, collector, settings_store
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import NotificationEvent, NotificationStream, Server, TgAlert

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
SETTINGS = Settings(alert_telegram_token="fixture-token", alert_telegram_chat="100",
                    panel_url="https://panel.example.com")
FAMILIES = ("docker_down", "docker_loop", "unit_loop", "queue")


@pytest.fixture
async def panel(tmp_path, monkeypatch):
    # This scenario uses a fixed observation clock. The shared wall-clock
    # startup fixture eventually moves after NOW and silences ingest-gap alerts.
    monkeypatch.setattr(collector, "_PANEL_STARTED", NOW - timedelta(hours=1))
    pg = os.environ.get("KERVAX_TEST_PG", "")
    engine, factory = create_engine_and_factory(pg or f"sqlite+aiosqlite:///{(tmp_path / 'identity.db').as_posix()}")
    async with engine.begin() as connection:
        if pg:
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    calls = []

    class Response:
        status_code = 200

        def __init__(self, mid):
            self.mid = mid

        def json(self):
            return {"ok": True, "result": {"message_id": self.mid}}

        def raise_for_status(self):
            pass

    class HTTP:
        mid = 100

        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, json=None):
            method = url.rsplit("/", 1)[-1]
            calls.append((method, json))
            if method == "sendMessage":
                HTTP.mid += 1
            return Response(HTTP.mid)

    monkeypatch.setattr(alerts.httpx, "AsyncClient", HTTP)
    original_rules = settings_store.get_server_alert_rules
    enabled = set()

    async def rules(session):
        return {key: {**rule, "enabled": key in enabled} for key, rule in (await original_rules(session)).items()}

    monkeypatch.setattr(settings_store, "get_server_alert_rules", rules)
    async with factory() as session:
        row = Server(name="node", token_hash="fixture", enabled=True, backup_not_required=True,
                     last_seen=NOW, last_report={"uptime_seconds": 100000}, alert_sustain_seconds=0,
                     cpu_alert_percent=0, mem_alert_percent=0, queue_alert_depth=100)
        session.add(row)
        await session.commit()
        sid = row.id
        await settings_store.set_alert_config(session, "fixture-token", "100", "", flood_threshold=0, tg_buttons=True)
    try:
        yield factory, sid, enabled, calls
    finally:
        await engine.dispose()


def instance(kind, name):
    return f"broker|//{name}" if kind == "queue" else f"{name}.service" if kind == "unit_loop" else name


def report(kind, bad, at):
    rep = {"uptime_seconds": 100000, "clock_unix": int(at.timestamp()), "agent_version": "2.29"}
    if kind.startswith("docker"):
        rep["docker"] = {"access": True, "containers": [
            {"name": name, "state": "exited" if kind == "docker_down" and name in bad else "running",
             "policy": "always", "restarts": 1,
             "err": "fixture startup failure" if kind == "docker_down" and name in bad else ""}
            for name in ("alpha", "beta")]}
    elif kind == "unit_loop":
        rep["extras"] = {"units": {"v": 1, "ts": int(at.timestamp()), "units": [],
            "restarting": [{"unit": name + ".service", "n": 1, "result": "exit-code", "status": 1}
                           for name in ("alpha", "beta")]}}
    elif kind == "queue":
        rep["services"] = [{"kind": "rabbitmq", "source": "broker", "queues": [
            {"name": name, "vhost": "/", "ready": 200 if name in bad else 0, "unacked": 0}
            for name in ("alpha", "beta")]}]
    return rep


def primed(kind, bad, at, alerted=False):
    recent = [(at - timedelta(seconds=n)).isoformat() for n in (120, 60, 1)]
    if kind.startswith("docker"):
        return {"docker": {name: {"rc": 1, "loops": recent if kind == "docker_loop" else [],
            "alerted_down": alerted if kind == "docker_down" else False,
            "alerted_loop": alerted if kind == "docker_loop" else False,
            "down_since": (at - timedelta(hours=1)).isoformat()} for name in bad}}
    if kind == "unit_loop":
        return {"unit_loops": {name + ".service": {"n": 1, "hits": [[stamp, 1] for stamp in recent],
                                                    "alerted": alerted} for name in bad}}
    return {"queues": {instance(kind, name): alerted for name in bad},
            "queues_since": {instance(kind, name): (at - timedelta(minutes=11)).isoformat() for name in bad}}


async def tick(panel, kind, bad, at, state=None, **fields):
    factory, sid, enabled, _calls = panel
    enabled.add(kind)
    async with factory() as session:
        row = await session.get(Server, sid)
        row.last_seen, row.last_report = at, report(kind, bad, at)
        if state is not None:
            row.alert_state = state
        for key, value in fields.items():
            setattr(row, key, value)
        await session.commit()
    await collector.evaluate_servers(factory, SETTINGS, at)


async def events(panel):
    async with panel[0]() as session:
        return list(await session.scalars(select(NotificationEvent).order_by(NotificationEvent.id)))


async def phases(panel):
    async with panel[0]() as session:
        rows = await session.execute(select(NotificationStream.phase, NotificationEvent.payload).join(
            NotificationEvent, NotificationEvent.id == NotificationStream.last_event_id))
        return {payload["message"]["notice"]["instance"]: phase for phase, payload in rows}


@pytest.mark.parametrize("kind", FAMILIES)
async def test_instances_recover_and_reopen_independently(panel, kind):
    await tick(panel, kind, {"alpha", "beta"}, NOW, primed(kind, {"alpha", "beta"}, NOW))
    initial = await events(panel)
    assert len(initial) == 2 and all(event.phase == "fire" for event in initial)
    assert len({event.stream_key for event in initial}) == 2
    async with panel[0]() as session:
        rows = list(await session.scalars(select(TgAlert)))
        assert len(rows) == 2 and len({row.id for row in rows}) == 2
    at = NOW + timedelta(minutes=16)
    # Keep beta's actual restart window active while alpha has stopped restarting.
    state = primed(kind, {"beta"}, at, alerted=True)
    if kind == "docker_down":
        state["docker"]["alpha"] = {"alerted_down": True}
    await tick(panel, kind, {"beta"}, at, state)
    assert await phases(panel) == {instance(kind, "alpha"): "recovery", instance(kind, "beta"): "fire"}
    current = await events(panel)
    assert len(current) == 3 and current[-1].phase == "recovery"
    alpha_fire = next(event for event in initial if event.payload["message"]["notice"]["instance"] == instance(kind, "alpha"))
    beta_fire = next(event for event in initial if event.id != alpha_fire.id)
    async with panel[0]() as session:
        assert (await session.get(TgAlert, alpha_fire.tg_alert_id)).resolved_at is not None
        assert (await session.get(TgAlert, beta_fire.tg_alert_id)).resolved_at is None
    at += timedelta(minutes=16)
    await tick(panel, kind, set(), at)
    assert set((await phases(panel)).values()) == {"recovery"}
    at += timedelta(minutes=1)
    await tick(panel, kind, {"beta"}, at, primed(kind, {"beta"}, at))
    current = await events(panel)
    assert len(current) == 5 and current[-1].phase == "fire" and current[-1].episode == 2
    assert current[-1].stream_key == beta_fire.stream_key
    assert await phases(panel) == {instance(kind, "alpha"): "recovery", instance(kind, "beta"): "fire"}


@pytest.mark.parametrize("kind", FAMILIES)
async def test_stream_ownership_survives_lost_fire_and_recovery_observer_flags(panel, kind):
    before = primed(kind, {"alpha"}, NOW)
    await tick(panel, kind, {"alpha"}, NOW, copy.deepcopy(before))
    async with panel[0]() as session:
        stale_fire = copy.deepcopy((await session.get(Server, panel[1])).alert_state)
    at = NOW + timedelta(minutes=16)
    # Admission committed, but the trailing observer-state transaction was lost.
    await tick(panel, kind, set(), at, before)
    assert [event.phase for event in await events(panel)] == ["fire", "recovery"]
    # Likewise, do not let a lost recovery-state commit resurrect the old flag.
    at += timedelta(minutes=1)
    stale = primed(kind, {"alpha"}, at, alerted=True)
    if kind == "docker_down":
        stale = stale_fire
    await tick(panel, kind, {"alpha"}, at, stale)
    if kind == "queue":
        assert [event.phase for event in await events(panel)] == ["fire", "recovery"]
        at += timedelta(minutes=11)
        await tick(panel, kind, {"alpha"}, at)
    assert [event.phase for event in await events(panel)] == ["fire", "recovery", "fire"]
    assert (await events(panel))[-1].episode == 2
    await tick(panel, kind, {"alpha"}, at + timedelta(seconds=1))
    assert len(await events(panel)) == 3


@pytest.mark.parametrize("kind", ("reboot", "port_new", "access_change", "kube_job"))
async def test_one_off_reobservations_are_quiet_but_next_real_event_is_new(panel, kind):
    factory, sid, enabled, calls = panel
    enabled.add(kind)
    if kind == "kube_job":
        async with factory() as session:
            (await session.get(Server, sid)).alert_state = {"kube_jobs_seen": []}
            await session.commit()
    for index in range(2):
        at = NOW + timedelta(minutes=index * 2)
        stamp = at.isoformat()
        fields = {}
        rep = report(kind, set(), at)
        if kind == "reboot":
            fields["rebooted_at"] = at
        elif kind == "port_new":
            fields["exposure"] = {"new_ts": stamp, "new": [{"port": 8080, "ip": "203.0.113.7", "name": "HTTP"}],
                                  "from": "fixture-proxy"}
        elif kind == "access_change":
            fields["changes"] = {"alert_ts": stamp, "log": [{"ts": stamp, "alert": True,
                "kind": "user", "act": "add", "text": "новый пользователь service-user (uid 1001, /bin/sh)"}]}
        else:
            rep["kube"] = {"access": True, "failed_jobs": [{"ns": "fixture", "name": "daily",
                "ts": int(at.timestamp()), "reason": "BackoffLimitExceeded"}]}
        async with factory() as session:
            row = await session.get(Server, sid)
            row.last_seen, row.last_report = at, rep
            for key, value in fields.items():
                setattr(row, key, value)
            await session.commit()
        await collector.evaluate_servers(factory, SETTINGS, at)
        assert len(await events(panel)) == index + 1
        for seconds in (1, 2):
            await collector.evaluate_servers(factory, SETTINGS, at + timedelta(seconds=seconds))
        assert len(await events(panel)) == index + 1
    actual = await events(panel)
    assert all(event.phase == "event" and event.occurrence for event in actual)
    assert actual[0].occurrence != actual[1].occurrence
    assert len([payload for method, payload in calls if method == "sendMessage"]) == 2


async def test_equal_queue_names_in_different_brokers_have_distinct_ownership(panel):
    at = NOW
    keys = ["broker-a|//tasks", "broker-b|//tasks"]

    def queues(cleared=False):
        rep = report("queue", set(), at)
        rep["services"] = [{"kind": "rabbitmq", "source": broker, "queues": [
            {"name": "tasks", "vhost": "/", "ready": 0 if cleared and i == 0 else 200}]}
            for i, broker in enumerate(("broker-a", "broker-b"))]
        return rep

    state = {"queues_since": {key: (at - timedelta(minutes=11)).isoformat() for key in keys}}
    await tick(panel, "queue", set(), at, state, last_report=queues())
    assert await phases(panel) == dict.fromkeys(keys, "fire")
    at += timedelta(minutes=1)
    await tick(panel, "queue", set(), at, last_report=queues(cleared=True))
    assert await phases(panel) == {keys[0]: "recovery", keys[1]: "fire"}


async def test_pressure_records_cause_and_own_level_without_dynamic_detail(panel, monkeypatch):
    choice = {"level": 2, "cause": "io", "detail": "first sample 900"}

    def conditions(*_args, **_kwargs):
        level = choice["level"]
        cause = choice["cause"]
        return {"pressure": (level, {"detail": choice["detail"], "res": cause,
                                    "items": [(level, cause, choice["detail"])]})}

    monkeypatch.setattr(collector, "_server_conditions", conditions)
    for index, (level, cause) in enumerate(((2, "io"), (1, "io"), (2, "dstate"), (1, "io"), (2, "dstate"))):
        choice.update(level=level, cause=cause, detail=f"sample {index * 900}")
        await tick(panel, "pressure", set(), NOW + timedelta(minutes=index))
    actual = await events(panel)
    assert len(actual) == 2
    assert [event.members for event in actual] == [["io|level:2"], ["dstate|level:2"]]


async def test_known_reminder_uses_scheduled_occurrence_across_lost_observer_commit(panel, monkeypatch):
    from app import known

    entry = {"at": (NOW - timedelta(seconds=known.REMIND_S + 3600)).isoformat(), "text": "tracked issue", "level": 1}
    monkeypatch.setattr(collector, "_server_conditions", lambda *_args, **_kwargs: {"disk": (1, {})})
    await tick(panel, "disk", set(), NOW, known_issues={"disk": copy.deepcopy(entry)})
    assert len(await events(panel)) == 1
    assert (await events(panel))[0].phase == "event"
    # Lose only the observer reminder timestamp after admission. The due slot is unchanged.
    await tick(panel, "disk", set(), NOW + timedelta(minutes=1), known_issues={"disk": copy.deepcopy(entry)})
    assert len(await events(panel)) == 1
    await tick(panel, "disk", set(), NOW + timedelta(seconds=known.REMIND_S + 120))
    assert len(await events(panel)) == 2
    assert len({event.occurrence for event in await events(panel)}) == 2


async def test_ingest_gap_recovers_after_process_state_was_lost(panel, monkeypatch):
    state = {"gap": True}
    monkeypatch.setattr(collector, "ingest_gap", lambda *_args: (3, 3) if state["gap"] else (0, 3))
    monkeypatch.setattr(collector, "_gap_said", {})
    await tick(panel, "ingest_gap", set(), NOW)
    assert [event.phase for event in await events(panel)] == ["fire"]
    collector._gap_said.clear()
    await tick(panel, "ingest_gap", set(), NOW + timedelta(minutes=1))
    assert len(await events(panel)) == 1
    state["gap"] = False
    await tick(panel, "ingest_gap", set(), NOW + timedelta(minutes=2))
    assert [event.phase for event in await events(panel)] == ["fire", "recovery"]
    state["gap"] = True
    await tick(panel, "ingest_gap", set(), NOW + timedelta(minutes=3))
    assert [event.phase for event in await events(panel)] == ["fire", "recovery", "fire"]
    assert (await events(panel))[-1].episode == 2


async def test_backup_jump_identity_uses_actual_run_timestamp(panel):
    def backup_report(at, run_at, size):
        rep = report("backup_growth", set(), at)
        rep["extras"] = {"backup-runs": {"v": 1, "ts": int(at.timestamp()), "runs": [
            {"ts": int(run_at.timestamp()) - i * 86400, "st": (size if i == 0 else 20) * 2**30}
            for i in range(6)]}}
        return rep

    run_at = NOW - timedelta(hours=1)
    await tick(panel, "backup_growth", set(), NOW, last_report=backup_report(NOW, run_at, 192))
    assert len(await events(panel)) == 1
    at = NOW + timedelta(minutes=1)
    await tick(panel, "backup_growth", set(), at, last_report=backup_report(at, run_at, 250))
    assert len(await events(panel)) == 1  # Changing sizes/helper snapshot is not a new run.
    at += timedelta(minutes=1)
    await tick(panel, "backup_growth", set(), at, last_report=backup_report(at, at, 250))
    actual = await events(panel)
    assert len(actual) == 2 and all(event.phase == "event" for event in actual)
    assert actual[0].occurrence != actual[1].occurrence
