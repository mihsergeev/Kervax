"""Selected Windows service state, delivery and unknown telemetry regressions."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import collector, windows_services
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent, Server


def service(name="Spooler", state="Stopped", **kw):
    return {"name": name, "display_name": "Print Spooler", "state": state, "start_mode": "Auto", **kw}


def report(at, items=None, complete=True):
    return {"platform": "windows", "clock_unix": int(at.timestamp()), "uptime_seconds": 100000,
            "windows_services": {"collected_at": int(at.timestamp()), "complete": complete,
                                 "items": [service()] if items is None else items}}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'services.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Server(name="windows-node", token_hash="x", enabled=True, backup_not_required=True,
                           windows_service_names=["Spooler"], cpu_alert_percent=0, mem_alert_percent=0))
        await session.commit()
    sent, fail = [], []

    async def send(cfg, text, parse_mode=None):
        if fail:
            return ["Webhook: timeout"]
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(at, rep=None, **fields):
        async with factory() as session:
            srv = await session.scalar(select(Server))
            srv.last_seen = at
            srv.last_report = report(at) if rep is None else rep
            for key, value in fields.items():
                setattr(srv, key, value)
            await session.commit()
        await collector.evaluate_servers(factory, cfg, at)
        async with factory() as session:
            srv = await session.scalar(select(Server))
            events = list(await session.scalars(select(AlertEvent).where(AlertEvent.kind == "windows_service").order_by(AlertEvent.id)))
            return dict(srv.alert_state or {}), events

    yield tick, sent, fail
    await engine.dispose()


@pytest.mark.calm
async def test_one_alert_and_one_confirmed_recovery_without_text_resends(env):
    tick, sent, fail = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    fail.append(True)
    state, events = await tick(at)
    assert not state.get("windows_service") and not events
    fail.clear()
    at += timedelta(minutes=1)
    state, events = await tick(at)
    assert state["windows_service"] == 2 and len(events) == 1 and len(sent) == 1
    assert sent[0].splitlines()[1:] == ["<b>Служба Windows не работает</b>", "Spooler"]
    for minute in range(1, 4):
        state, events = await tick(at + timedelta(minutes=minute), report(at + timedelta(minutes=minute), [service(display_name=f"Changed {minute}", state="Paused")]))
        assert len(sent) == 1 and len(events) == 1
    at += timedelta(minutes=4)
    for minute in range(16):
        current = at + timedelta(minutes=minute)
        if minute == 15:
            fail.append(True)
        state, events = await tick(current, report(current, [service(state="Running")]))
        assert len(events) == 1
    fail.clear()
    at += timedelta(minutes=16)
    state, events = await tick(at, report(at, [service(state="Running")]))
    assert state["windows_service"] == 0 and [e.recovery for e in events] == [False, True]
    assert "выбранные службы Windows снова работают" in sent[-1]
    at += timedelta(minutes=1)
    state, events = await tick(at, report(at, [service(state="Running")]))
    assert len(sent) == 2 and len(events) == 2


@pytest.mark.calm
async def test_missing_partial_stale_replay_and_restart_never_confirm_recovery(env):
    tick, sent, _ = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    await tick(at)
    variants = ["absent", "empty", "partial", "missing-selected", "pending", "stale", "duplicate", "malformed", "frozen", "restart"]
    for variant in variants:
        at += timedelta(minutes=1)
        healthy = report(at, [service(state="Running")])
        state, _ = await tick(at, healthy)
        assert state.get("windows_service_calm")
        at += timedelta(minutes=3)
        rep = report(at, [service(state="Running")])
        if variant == "absent": rep.pop("windows_services")
        if variant == "empty": rep["windows_services"]["items"] = []
        if variant == "partial": rep["windows_services"]["complete"] = False
        if variant == "missing-selected": rep["windows_services"]["items"] = [service("Another", "Running")]
        if variant == "pending": rep["windows_services"]["items"][0]["state"] = "Start Pending"
        if variant == "stale": rep["windows_services"]["collected_at"] -= 300
        if variant == "duplicate": rep["windows_services"]["items"].append(service(state="Running"))
        if variant == "malformed": rep["windows_services"]["items"].append({})
        if variant == "frozen": rep = healthy
        if variant == "restart": rep["uptime_seconds"] = 30
        state, events = await tick(at, rep)
        assert state["windows_service"] == 2, variant
        if variant == "restart":
            assert state.get("windows_service_calm") == at.isoformat()
        else:
            assert not state.get("windows_service_calm"), variant
        assert len(events) == 1
    assert len(sent) == 1


async def test_multiple_selected_services_only_new_identity_alerts_and_all_recover(env):
    tick, sent, _ = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    await tick(at, report(at, [service(), service("W32Time", "Running")]), windows_service_names=["Spooler", "W32Time"])
    at += timedelta(minutes=1)
    state, events = await tick(at, report(at, [service(), service("W32Time")]))
    assert len(events) == 2 and "W32Time" in sent[-1]
    assert "Spooler" not in str(sent[-1])
    at += timedelta(minutes=1)
    state, events = await tick(at, report(at, [service(state="Running"), service("W32Time")]))
    assert len(events) == 2
    at += timedelta(minutes=1)
    state, events = await tick(at, report(at, [service(state="Running"), service("W32Time", "Running")]))
    assert [e.recovery for e in events] == [False, False, True]


@pytest.mark.parametrize("fields", [{"is_dev": True}, {"alert_mutes": ["windows_service"]}, {"alert_mutes": ["winsvc:spooler"]}, {"snooze_until": datetime.now(timezone.utc) + timedelta(days=3)}])
async def test_muted_dev_and_quiet_do_not_notify(env, fields):
    tick, sent, _ = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    await tick(at, **fields)
    assert not sent


async def test_deselecting_or_muting_announced_service_closes_silently(env):
    tick, sent, _ = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    await tick(at)
    at += timedelta(minutes=1)
    state, events = await tick(at, windows_service_names=[])
    assert not state.get("windows_service") and len(sent) == 1
    assert events[-1].recovery and "исключены из контроля" in events[-1].text


async def test_selection_api_roundtrip_and_windows_report(client, auth_headers):
    created = (await client.post("/api/servers", json={"name": "services-fixture"}, headers=auth_headers)).json()
    path = f"/api/servers/{created['server']['id']}"
    assert (await client.patch(path, json={"windows_service_names": ["Spooler"]}, headers=auth_headers)).status_code == 400
    at = datetime.now(timezone.utc)
    assert (await client.post("/api/agent/report", json=report(at), headers={"Authorization": f"Bearer {created['token']}"})).status_code == 200
    saved = await client.patch(path, json={"windows_service_names": ["Spooler", "spooler"]}, headers=auth_headers)
    assert saved.status_code == 200 and saved.json()["windows_service_names"] == ["spooler"]
    detail = (await client.get(path, headers=auth_headers)).json()
    assert detail["windows_services"]["complete"] and detail["windows_services"]["items"][0]["selected"]
    assert detail["problems"][0]["kind"] == "windows_service"
    for value in (None, [""], [" Spooler"], ["a\n"], ["x" * 257]):
        assert (await client.patch(path, json={"windows_service_names": value}, headers=auth_headers)).status_code == 422
    assert (await client.patch(path, json={"windows_service_names": []}, headers=auth_headers)).json()["windows_service_names"] == []


def test_linux_inventory_never_uses_windows_payload():
    at = datetime.now(timezone.utc)
    rep = report(at)
    rep["platform"] = "linux"
    assert windows_services.inventory(rep) == ({}, False)


def test_sanitized_inventory_contains_only_identity_and_state():
    at = datetime.now(timezone.utc)
    rep = report(at, [service(PathName="sensitive", StartName="account")])
    rows, complete = windows_services.inventory(rep)
    assert complete and set(rows["spooler"]) == {"name", "display_name", "state", "start_mode"}


@pytest.mark.calm
async def test_offline_breaks_service_recovery_hold(env):
    tick, sent, _ = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    await tick(at)
    at += timedelta(minutes=1)
    state, _ = await tick(at, report(at, [service(state="Running")]))
    assert state.get("windows_service_calm")
    at += timedelta(minutes=1)
    state, events = await tick(at, report(at, [service(state="Running")]), last_seen=at - timedelta(hours=1))
    assert state["windows_service"] == 2 and not state.get("windows_service_calm") and len(events) == 1


def test_unknown_services_never_release_a_mute_as_healthy():
    from app import mute_watch
    conds = {"windows_service": (0, {"_complete": False})}
    assert mute_watch.judge("windows_service", conds, [], [], {}, set()) is None
    assert mute_watch.judge("winsvc:spooler", conds, [], [], {}, set()) is None
    probs = [{"kind": "windows_service", "mute": "winsvc:spooler", "text": "Spooler stopped"}]
    assert mute_watch.judge("winsvc:spooler", conds, [], probs, {}, set())[0] is True


def test_service_selection_migration_roundtrip_preserves_existing_servers():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    import sqlalchemy as sa

    path = Path(__file__).parents[1] / "alembic/versions/0144_windows_services.py"
    spec = importlib.util.spec_from_file_location("windows_services_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE servers (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(sa.text("INSERT INTO servers VALUES (1, 'existing-server')"))
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            assert connection.execute(sa.text("SELECT windows_service_names FROM servers")).scalar() is None
            connection.execute(sa.text("UPDATE servers SET windows_service_names = '[\"Spooler\"]'"))
            assert connection.execute(sa.text("SELECT windows_service_names FROM servers")).scalar() == '["Spooler"]'
            module.downgrade()
            assert connection.execute(sa.text("SELECT name FROM servers")).scalar() == "existing-server"
            module.upgrade()
            assert connection.execute(sa.text("SELECT windows_service_names FROM servers")).scalar() is None
    engine.dispose()


def test_service_alert_detail_uses_utf8_russian_text():
    from types import SimpleNamespace
    at = datetime.now(timezone.utc)
    server = SimpleNamespace(last_report=report(at), last_seen=at, windows_service_names=["Spooler"], alert_mutes=[], alert_snoozes={})
    level, ctx = windows_services.conditions(server, at)
    assert level == 2
    assert ctx["detail"] == "Spooler: \u0441\u043b\u0443\u0436\u0431\u0430 \u043d\u0435 \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442"


@pytest.mark.parametrize("variant", ["stopped", "healthy", "stale", "partial", "missing", "duplicate", "absent", "offline"])
async def test_fixed_api_requires_confirmed_selected_service_health(client, auth_headers, variant):
    factory = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    rep = report(now, [service(state="Running")])
    if variant == "stopped": rep["windows_services"]["items"][0]["state"] = "Stopped"
    if variant == "stale": rep["windows_services"]["collected_at"] -= 300
    if variant == "partial": rep["windows_services"]["complete"] = False
    if variant == "missing": rep["windows_services"]["items"] = [service("Another", "Running")]
    if variant == "duplicate": rep["windows_services"]["items"].append(service(state="Running"))
    if variant == "absent": rep.pop("windows_services")
    async with factory() as session:
        server = Server(name="windows-fixed", token_hash="x", enabled=True, backup_not_required=True,
                        last_seen=now - timedelta(hours=1) if variant == "offline" else now,
                        last_report=rep, windows_service_names=["Spooler"], offline_after_seconds=120,
                        alert_state={"windows_service": 2, "windows_service_sig": ["spooler"]})
        session.add(server)
        await session.commit()
        sid = server.id
    response = await client.post(f"/api/servers/{sid}/fixed/windows_service", json={"text": "проверил"}, headers=auth_headers)
    assert response.status_code == (200 if variant == "healthy" else 409), response.text
    async with factory() as session:
        server = await session.get(Server, sid)
        assert bool((server.alert_fixed or {}).get("windows_service")) == (variant == "healthy")
        assert server.alert_state["windows_service"] == 2
    if variant == "stopped":
        assert "Spooler" in response.json()["detail"]
    elif variant != "healthy":
        assert "не получилось проверить" in response.json()["detail"]


@pytest.mark.calm
async def test_fixed_confirmed_service_closes_once_without_waiting_for_settle(env):
    from app import fixed
    tick, sent, _ = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    await tick(at)
    at += timedelta(minutes=1)
    state, _ = await tick(at, report(at, [service(state="Running")]))
    assert state.get("windows_service_calm") and len(sent) == 1
    marks = {"windows_service": fixed.new_entry("проверил", "operator", at)}
    state, events = await tick(at, report(at, [service(state="Running")]), alert_fixed=marks)
    assert state["windows_service"] == 0 and [e.recovery for e in events] == [False, True]
    assert "починили" in sent[-1] and "operator" in sent[-1]
    at += timedelta(minutes=1)
    _, events = await tick(at, report(at, [service(state="Running")]))
    assert len(sent) == 2 and len(events) == 2
