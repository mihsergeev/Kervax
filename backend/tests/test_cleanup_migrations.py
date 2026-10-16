"""Миграции 0113-0114: строки хронологии про UDP-сессии уходят, старые мьюты подов - на ворклоад."""
import importlib.util
import json
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration(name: str):
    p = Path(__file__).resolve().parents[1] / "alembic" / "versions" / name
    spec = importlib.util.spec_from_file_location(name[:4], p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_udp_session_events_are_dropped():
    eng = sa.create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE TABLE server_events (id INTEGER PRIMARY KEY, cat TEXT, kind TEXT, obj TEXT, text TEXT)"))
        rows = [(1, "ports", "open", "27584/udp6", "открыт порт 27584/udp6"),
                (2, "ports", "close", "27584/udp6", "закрыт порт 27584/udp6"),
                (3, "ports", "open", "123/udp", "открыт порт 123/udp"),
                (4, "ports", "open", "5353/udp", "открыт порт 5353/udp (avahi-daemon)"),
                (5, "ports", "open", "8080/tcp", "открыт порт 8080/tcp"),
                (6, "deploy", "new", "web", "новый контейнер web")]
        for r in rows:
            conn.execute(sa.text("INSERT INTO server_events VALUES (:i, :c, :k, :o, :t)"),
                         {"i": r[0], "c": r[1], "k": r[2], "o": r[3], "t": r[4]})
        with Operations.context(MigrationContext.configure(conn)):
            _migration("0113_drop_udp_session_events.py").upgrade()
        left = [r[0] for r in conn.execute(sa.text("SELECT id FROM server_events ORDER BY id"))]
    assert left == [3, 4, 5, 6]


def test_pod_mutes_move_to_workload():
    eng = sa.create_engine("sqlite://")
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE TABLE servers (id INTEGER PRIMARY KEY, alert_mutes JSON, alert_snoozes JSON)"))
        mutes = ["restarts:pod:default/mail-worker-6b8b56c769-f4k8m", "restarts:pod:default/mail-worker-6b8b56c769-s4d22",
                 "restarts:pod:db/postgres-0", "restarts:docker:web", "cpu"]
        snoozes = {"restarts:pod:default/api-5bfc9d5574-csqbv": "2026-10-20T00:00:00+00:00", "disk": "2026-10-18T00:00:00+00:00"}
        conn.execute(sa.text("INSERT INTO servers VALUES (1, :m, :s)"), {"m": json.dumps(mutes), "s": json.dumps(snoozes)})
        conn.execute(sa.text("INSERT INTO servers VALUES (2, :m, NULL)"), {"m": json.dumps(["cpu"])})
        with Operations.context(MigrationContext.configure(conn)):
            _migration("0114_pod_mutes_by_workload.py").upgrade()
        got = {r[0]: (json.loads(r[1]), json.loads(r[2]) if r[2] else None)
               for r in conn.execute(sa.text("SELECT * FROM servers ORDER BY id"))}
    assert got[1][0] == ["restarts:pod:default/mail-worker", "restarts:pod:db/postgres", "restarts:docker:web", "cpu"]
    assert got[1][1] == {"restarts:pod:default/api": "2026-10-20T00:00:00+00:00", "disk": "2026-10-18T00:00:00+00:00"}
    assert got[2] == (["cpu"], None)
