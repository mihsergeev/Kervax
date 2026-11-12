"""Missing local observations retain incident state and durable server identity."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app import checks, collector, site_flap
from app.models import AgentProbe, AuditLog, Check, CheckIncident, CheckSample, Server


async def test_missing_observation_neither_opens_nor_resolves_incident(client):
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    async with factory() as session:
        check = Check(name="app", type="http", target="https://app.example.com", probe_local=True,
                      alert_after_failures=1, interval_seconds=60)
        session.add(check)
        await session.flush()
        cid = check.id
        pending = []
        await collector.record_outcome(session, check, checks.CheckOutcome("unknown", message="нет результата"), now, pending)
        assert pending == []
        assert await session.scalar(select(func.count()).select_from(CheckIncident)) == 0
        assert check.consecutive_fails == 0

        await collector.record_outcome(session, check, checks.CheckOutcome("down", message="TLS failure"), now + timedelta(minutes=1), pending)
        assert [item.kind for item in pending] == ["bad"]
        incident = await session.scalar(select(CheckIncident).where(CheckIncident.check_id == cid))
        incident.notified = True
        check.flap_state = site_flap.opened("down", now)
        pending.clear()
        for minute in (2, 40, 60):
            await collector.record_outcome(session, check, checks.CheckOutcome("unknown", message="нет результата"), now + timedelta(minutes=minute), pending)
            assert incident.ended_at is None
            assert incident.notified
            assert incident.last_message == "TLS failure"
            assert site_flap.level(check.flap_state) == "down"
            assert pending == []

        await collector.record_outcome(session, check, checks.CheckOutcome("down", message="TLS failure"), now + timedelta(minutes=61), pending)
        assert pending == []
        await collector.record_outcome(session, check, checks.CheckOutcome("up", message="HTTP 200"), now + timedelta(minutes=62), pending)
        assert incident.ended_at is not None
        assert [item.kind for item in pending] == ["recovery"]
        assert await session.scalar(select(func.count()).select_from(CheckIncident)) == 1
        assert await session.scalar(select(func.count()).select_from(CheckSample).where(CheckSample.status == "unknown")) == 4
        await session.commit()


@pytest.mark.parametrize("disabled,scope,edited", [(False, None, False), (True, None, False), (False, ["other"], False), (False, None, True)])
async def test_recover_previous_binding_respects_access_and_request_edits(client, disabled, scope, edited):
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    async with factory() as session:
        server = Server(name="former-node", token_hash="fixture", enabled=not disabled,
                        token_site_groups=scope, last_report={})
        check = Check(name="app", type="http", target="https://app.example.com", group_name="app",
                      probe_local=True, probe_server_id=None, probe_bound_at=now)
        session.add_all([server, check])
        await session.flush()
        session.add(AgentProbe(check_id=check.id, server_id=server.id, code=200,
                               ts=now - timedelta(minutes=30)))
        if edited:
            session.add(AuditLog(action="check_update", target="app", ts=now))
        await session.commit()
        cid, sid = check.id, server.id
    restore = not disabled and scope is None and not edited
    assert await collector.rebind_local_probes(factory) == int(restore)
    assert await collector.rebind_local_probes(factory) == 0
    async with factory() as session:
        check = await session.get(Check, cid)
        assert check.probe_server_id == (sid if restore else None)


@pytest.mark.parametrize("main,path,expected", [("up", "unknown", "unknown"), ("unknown", "up", "unknown"), ("unknown", "down", "down"), ("down", "unknown", "down")])
def test_incomplete_path_observations_cannot_confirm_recovery(main, path, expected):
    result = checks.combine_paths(checks.CheckOutcome(main, message="main"), [
        ("/health", checks.CheckOutcome(path, message="health")),
    ])
    assert result.status == expected


def test_missing_observations_do_not_count_as_flapping_or_stable_recovery():
    now = datetime.now(timezone.utc)
    history = [(now - timedelta(minutes=50), "down"),
               (now - timedelta(minutes=40), "up"),
               (now - timedelta(minutes=10), "unknown")]
    history.extend((now - timedelta(minutes=n), "up") for n in (3, 2, 1))
    assert site_flap.runs(history, now - timedelta(hours=1)) == 1
    assert not site_flap.stable(history, now)
    assert site_flap.stable(history, now + timedelta(minutes=20))


async def test_undelivered_recovery_waits_for_real_success_after_unknown(client):
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    async with factory() as session:
        check = Check(name="app", type="http", target="https://app.example.com", enabled=True,
                      last_status="unknown")
        session.add(check)
        await session.flush()
        collector._queue_site_recovery(check, [], "recovery", "снова отвечает", now)
        await session.commit()
        assert await collector._site_recoveries(session, now + timedelta(minutes=1)) == ([], {})
        check.last_status = "up"
        await session.commit()
        pending, stale = await collector._site_recoveries(session, now + timedelta(minutes=2))
        assert len(pending) == 1 and not stale
