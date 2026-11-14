"""Notification identities survive changing presentation and durable queue handoff."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import alerts, autoban, checks, collector, request_watch, settings_store
from app.config import Settings
from app.models import AppSetting, AutobanBan, BackupCommand, Check, CheckIncident, Location, MaintenanceWindow, RequestStat, Server

NOW = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)
SETTINGS = Settings(alert_webhook="https://fixture.invalid", panel_url="https://panel.example.com")


@pytest.fixture
def admission(monkeypatch):
    """Commit producer state without claiming that a transport confirmed anything."""
    captured = []

    async def dispatch(_cfg, _can, messages, _threshold, **kwargs):
        captured.extend(messages)
        if callback := kwargs.get("on_accept"):
            async with kwargs["session_factory"]() as session:
                await callback(session)
                await session.commit()
            kwargs["accepted"].update(range(len(messages)))
        return False

    monkeypatch.setattr(alerts, "dispatch", dispatch)
    return captured


@pytest.mark.parametrize("kind,flag,icon", [
    ("bad", None, ""), ("ssl", ("ssl_alerted_days", 7), "⚠️"),
    ("domain", ("domain_alerted_days", 7), "⚠️"),
    ("locpart", ("loc_notified", [3, 5]), ""),
])
def test_site_identity_uses_source_and_facts_not_current_text(kind, flag, icon):
    first = collector.Pending(kind, "before", "down", "count 12", 41, 7, flag, icon)
    changed = first._replace(name="renamed", msg="count 900 and another template")
    a = collector._site_notice(first, NOW)
    b = collector._site_notice(changed, NOW + timedelta(minutes=1))
    assert {key: val for key, val in a.items() if key != "observed_at"} == {
        key: val for key, val in b.items() if key != "observed_at"
    }
    assert "before" not in a["stream"] and "count" not in json.dumps(a)


@pytest.mark.parametrize("kind", ["ssl", "domain"])
def test_updated_expiry_closes_same_stream(kind):
    fire = collector._site_notice(collector.Pending(kind, "site", "", "7 days", None, 3,
                                                    (f"{kind}_alerted_days", 7), "⚠️"), NOW)
    recovery = collector._site_notice(collector.Pending(kind, "site", "", "renewed", None, 3,
                                                        None, "✅"), NOW)
    assert fire["stream"] == recovery["stream"]
    assert fire["phase"] == "fire" and recovery["phase"] == "recovery"
    assert recovery["members"] == [] and recovery["level"] == 0


def test_next_site_incident_has_new_occurrence_but_same_source():
    pending = collector.Pending("bad", "site", "down", "HTTP 503", 11, 3, None)
    first = collector._site_notice(pending, NOW)
    later = collector._site_notice(pending._replace(inc_id=12), NOW + timedelta(hours=1))
    assert first["stream"] == later["stream"]
    assert first["occurrence"] != later["occurrence"]


async def test_queued_site_fire_can_recover_without_transport_confirmation(client):
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        check = Check(name="site", type="http", target="https://example.com", alert_after_failures=1)
        session.add(check)
        await session.flush()
        pending = []
        await collector.record_outcome(session, check, checks.CheckOutcome("down", message="HTTP 503"), NOW, pending)
        incident = await session.scalar(select(CheckIncident))
        incident.notice_queued = True
        assert not incident.notified
        check.flap_state = pending[0].flag[1]
        pending.clear()
        await collector.record_outcome(session, check, checks.CheckOutcome("down", message="changed"),
                                       NOW + timedelta(seconds=1), pending)
        assert pending == []
        await collector.record_outcome(session, check, checks.CheckOutcome("up", message="HTTP 200"),
                                       NOW + timedelta(seconds=2), pending)
        assert pending[0].inc_id == incident.id
        assert check.flap_state["recovery"]["incident_id"] == incident.id
        await session.commit()
    async with factory() as session:
        recovered, stale = await collector._site_recoveries(session, NOW + timedelta(seconds=3))
        notice = collector._site_notice(recovered[0], NOW)
        assert not stale and notice["incident_id"] == incident.id
        assert notice["phase"] == "recovery" and notice["occurrence"] == f"incident:{incident.id}:recovery"


async def test_domain_fold_keeps_each_source_even_with_same_display_name(client, admission, monkeypatch):
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        rows = [Check(name="same-name", type="http", target=f"https://{host}.example.com") for host in ("a", "b")]
        session.add_all(rows)
        await session.commit()
        ids = [row.id for row in rows]
    pending = [collector.Pending("domain", "same-name", "", "7 days", None, cid,
                                 ("domain_alerted_days", 7), "⚠️") for cid in ids]
    await collector._send_alerts(factory, SETTINGS, pending, NOW)
    assert len(admission) == 1
    assert {p.notice["check_id"] for p in admission[0].parts} == set(ids)
    assert all(p is not admission[0] for p in admission[0].parts)


async def test_group_recovery_retains_each_incident_with_recovery_direction(client):
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        rows = [Check(name=f"site-{index}", type="http", target=f"https://{index}.example.com", last_status="up")
                for index in range(2)]
        session.add_all(rows)
        await session.commit()
        group = collector.site_group.new_group("fixture-place", "down", "place", None, NOW - timedelta(hours=1))
        group.update(kind="down", grp="", group_scoped=True, solo=False, first=rows[0].name)
        for row in rows:
            member = collector._grp_member(row.name, row.name, "", "HTTP 503")
            member["notice"] = collector._site_notice(
                collector.Pending("bad", row.name, "down", "HTTP 503", row.id + 10, row.id, None), NOW)
            group["members"][str(row.id)] = member
        await collector.site_group.save(session, {group["id"]: group})
    messages, _after = await collector._site_groups(factory, {}, [], {}, {}, {}, [], NOW, SETTINGS.panel_url)
    assert len(messages) == 1 and messages[0].recovery
    for part in messages[0].parts:
        assert part.notice["phase"] == "recovery"
        assert part.notice["occurrence"] == f"incident:{part.notice['incident_id']}:recovery"


async def test_proxy_admission_saves_episode_before_transport_and_closes_same_source(client, admission):
    factory = client._transport.app.state.session_factory
    proxy = Location(id=4, name="proxy", url="https://proxy.example.com", enabled=True)
    await collector._proxy_alerts(factory, SETTINGS, [proxy], [], {}, {4}, NOW)
    await collector._proxy_alerts(factory, SETTINGS, [proxy], [], {}, {4}, NOW + timedelta(hours=1))
    assert len(admission) == 1
    fire = admission[0].notice
    assert fire["stream"] == "location:4:proxy" and fire["since"] == NOW.isoformat()
    assert "occurrence" not in fire
    await collector._proxy_alerts(factory, SETTINGS, [proxy], [], {}, set(), NOW + timedelta(hours=2))
    assert len(admission) == 2 and admission[-1].notice["phase"] == "recovery"
    assert admission[-1].notice["since"] == fire["since"]
    async with factory() as session:
        assert json.loads(await settings_store.get_raw(session, settings_store.LOC_BROKEN_KEY)) == {}


async def test_request_watch_admission_retains_episode_and_recovers(client, admission):
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        session.add(RequestStat(minute=NOW, key="203.0.113.9", total=100, bad=100,
                                codes={"401": 100}, paths={"/api/agent/commands": 100}))
        await session.commit()
    await request_watch.check(factory, SETTINGS, NOW)
    assert len(admission) == 1
    fire = admission[0].notice
    async with factory() as session:
        assert (await request_watch.load_state(session))["203.0.113.9"]["notified"]
    await request_watch.check(factory, SETTINGS, NOW + timedelta(minutes=1))
    assert len(admission) == 1
    await request_watch.check(factory, SETTINGS, NOW + timedelta(hours=1))
    assert len(admission) == 2 and admission[-1].notice["phase"] == "recovery"
    assert admission[-1].notice["since"] == fire["since"]
    assert "occurrence" not in fire
    assert admission[-1].notice["stream"] == fire["stream"]


async def test_maintenance_occurrence_is_admitted_once(client, admission):
    factory = client._transport.app.state.session_factory
    end = NOW - timedelta(minutes=10)
    async with factory() as session:
        window = MaintenanceWindow(title="window", server_ids=[], check_ids=[], starts_at=end - timedelta(hours=1),
                                   ends_at=end, repeat="", verify=True)
        session.add(window)
        await session.commit()
        wid = window.id
    await collector.maintenance_tick(factory, SETTINGS, NOW)
    await collector.maintenance_tick(factory, SETTINGS, NOW + timedelta(minutes=1))
    assert len(admission) == 1
    assert admission[0].notice["stream"] == f"maintenance:{wid}:verify"
    assert admission[0].notice["occurrence"] == end.isoformat()
    async with factory() as session:
        assert collector._aware((await session.get(MaintenanceWindow, wid)).verified_end) == end


async def test_autofix_notice_uses_command_id_and_finished_time(client, admission, monkeypatch):
    factory = client._transport.app.state.session_factory
    monkeypatch.setattr(collector, "get_settings", lambda: SETTINGS)
    async with factory() as session:
        server = Server(name="node", token_hash="fixture")
        session.add(server)
        await session.flush()
        command = BackupCommand(server_id=server.id, action="disk_cleanup", ok=True, result='{"bytes":1024}',
                                payload={"name": "apt"}, created_at=NOW - timedelta(minutes=1), done_at=NOW)
        session.add(command)
        await session.commit()
        sid, cid = server.id, command.id
    await collector.send_autofix_note(factory, sid, cid)
    assert admission[0].notice["occurrence"] == f"command:{cid}"
    assert admission[0].notice["stream"] == f"server:{sid}:autofix"
    assert admission[0].notice["observed_at"] == NOW.isoformat()


async def test_autoban_admission_records_period_and_ban_ids_without_confirmation(client, admission):
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        server = Server(name="node", token_hash="fixture")
        session.add(server)
        await session.flush()
        ban = AutobanBan(server_id=server.id, ip="203.0.113.10", since=NOW - timedelta(minutes=10),
                         until=NOW + timedelta(days=1), watch=False, digested=False)
        session.add(ban)
        await session.commit()
        bid = ban.id
    assert not await autoban.send_digest(factory, SETTINGS, NOW)
    assert len(admission) == 1
    assert admission[0].notice["members"] == [f"ban:{bid}"]
    assert admission[0].notice["occurrence"].endswith("2026-11-15")
    async with factory() as session:
        assert (await session.get(AutobanBan, bid)).digested
        assert await session.get(AppSetting, autoban.DIGEST_KEY) is not None
    await autoban.send_digest(factory, SETTINGS, NOW + timedelta(minutes=1))
    assert len(admission) == 1


async def test_coverage_admission_records_local_day_and_stable_gap_keys(client, admission, monkeypatch):
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)

    async def rules(_session):
        return {"uncovered": {"enabled": True, "text": settings_store.SERVER_ALERT_KINDS["uncovered"][1]}}

    monkeypatch.setattr(settings_store, "get_server_alert_rules", rules)
    monkeypatch.setattr(collector, "gaps", lambda *_args: [("helper:dbstat", "database inventory missing")])
    async with factory() as session:
        server = Server(name="node", token_hash="fixture", last_seen=now,
                        last_report={"uptime_seconds": 100000, "clock_unix": now.timestamp()},
                        alert_state={"uncov": {"helper:dbstat": (now - timedelta(days=1)).isoformat()}})
        session.add(server)
        await session.commit()
        sid = server.id
    await collector._daily_uncovered(factory, SETTINGS)
    await collector._daily_uncovered(factory, SETTINGS)
    assert len(admission) == 1
    notice = admission[0].notice
    assert notice["members"] == [f"server:{sid}:helper:dbstat"]
    assert notice["occurrence"] == now.astimezone(collector.tg_buttons.zone("Europe/Moscow")).date().isoformat()
    async with factory() as session:
        assert await settings_store.get_uncovered_sent(session) is not None
