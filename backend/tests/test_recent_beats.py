"""Recent status strips: bounded PostgreSQL reads and the same SQLite results."""

from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text

from app.api import checks as checks_api
from app.models import Check, CheckSample, User
from app.security import hash_password


async def test_recent_beats_preserves_window_order_disabled_empty_and_ties(client, monkeypatch):
    now = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(checks_api, "datetime", FrozenDateTime)
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        for cid in range(1, 7):
            session.add(Check(id=cid, name=f"monitor-{cid}", type="http",
                target="https://example.invalid", parent_mode="off", enabled=cid != 2,
                interval_seconds=3600 if cid == 2 else 60))
        # n=3 and the disabled hourly monitor still set the common cutoff to eight hours.
        cutoff = now - timedelta(hours=8)
        samples = [
            (1, now - timedelta(minutes=i), status)
            for i, status in enumerate(("up", "degraded", "down", "up", "up"), 1)
        ] + [
            (2, now - timedelta(hours=2), "down"),
            (2, now - timedelta(hours=1), "up"),
            (4, cutoff - timedelta(microseconds=1), "down"),
            (5, cutoff, "degraded"),
            (5, cutoff - timedelta(microseconds=1), "down"),
        ] + [(6, now, status) for status in ("up", "down", "degraded", "unknown")]
        session.add_all(CheckSample(check_id=cid, ts=ts, status=status)
                        for cid, ts, status in samples)
        await session.commit()
        beats = await checks_api._recent_beats(session, n=3)
    assert beats[1] == ["down", "degraded", "up"]
    assert beats[2] == ["down", "up"]
    assert 3 not in beats and 4 not in beats
    assert beats[5] == ["degraded"]
    # Timestamp ties never defined which sample wins; retain exactly n valid samples.
    assert len(beats[6]) == 3
    assert Counter(beats[6]) <= Counter(("up", "down", "degraded", "unknown"))


async def test_overview_beats_only_reach_visible_check_ids(client):
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(User(username="scoped-viewer", password_hash=hash_password("test-passphrase-2026"),
                         role="viewer", site_groups=["visible"], sections=["sites"]))
        for cid, group in ((1, "visible"), (2, "hidden")):
            session.add(Check(id=cid, name=f"monitor-{cid}", type="http",
                target="https://example.invalid", parent_mode="off", group_name=group))
            session.add(CheckSample(check_id=cid, ts=now, status="up" if cid == 1 else "down"))
        await session.commit()
    login = await client.post("/api/auth/login", json={
        "username": "scoped-viewer", "password": "test-passphrase-2026"})
    assert login.status_code == 200
    response = await client.get("/api/checks/overview", headers={
        "Authorization": "Bearer " + login.json()["access_token"]})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert [(c["id"], c["beats"]) for c in response.json()["checks"]] == [(1, ["up"])]


async def test_postgresql_recent_beats_reads_only_requested_tail(client):
    engine = client._transport.app.state.engine
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL query-plan regression")
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        session.add_all(Check(id=cid, name=f"monitor-{cid}", type="http",
            target="https://example.invalid", parent_mode="off", interval_seconds=3600)
            for cid in range(1, 5))
        await session.commit()
        await session.execute(text("""
            INSERT INTO check_samples (check_id, ts, status, message)
            SELECT c.id, now() - n * interval '1 second', 'up', ''
            FROM checks c CROSS JOIN generate_series(1, 1500) n
        """))
        await session.commit()
        await session.execute(text("ANALYZE check_samples"))
        statements = []

        def capture(conn, cursor, statement, parameters, context, many):
            statements.append((statement, parameters))

        event.listen(engine.sync_engine, "before_cursor_execute", capture)
        try:
            beats = await checks_api._recent_beats(session, n=5)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", capture)
        assert beats == {cid: ["up"] * 5 for cid in range(1, 5)}
        statement, parameters = statements[-1]
        conn = await session.connection()
        plan = (await conn.exec_driver_sql(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, parameters)).scalar_one()[0]["Plan"]

        def sample_reads(node):
            own = (node["Actual Rows"] * node["Actual Loops"]
                   if node.get("Relation Name") == "check_samples" else 0)
            return own + sum(sample_reads(child) for child in node.get("Plans", []))

        # A small status strip must not scan/sort thousands of other samples in its window.
        assert sample_reads(plan) <= 40
