"""Здоровье баз (helper dbstat-setup 0.5): реплика, долгие транзакции, блокировки, wraparound,
таблицы без VACUUM."""
from datetime import datetime, timezone
from pathlib import Path

from app import collector, db_health
from app.models import Server

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())


def _pg(**kw):
    x = {"engine": "pg", "container": "pg", "role": "primary", "replicas": 1, "lag": 0, "receiver": "",
         "xact": 0, "xact_db": "", "xact_app": "", "idle_xact": 0, "idle_db": "", "idle_app": "",
         "lock_waits": 0, "lock_wait": 0, "xid": 5_000_000, "xid_db": "postgres", "dead": []}
    x.update(kw)
    return x


def _my(**kw):
    x = {"engine": "mysql", "container": "", "role": "replica", "io": "Yes", "sql": "Yes", "lag": 0, "err": "",
         "trx": 0, "xact": 0, "lock_waits": 0, "lock_wait": 0}
    x.update(kw)
    return x


def _rep(*items, ts=T):
    return {"extras": {"db-health": {"v": 1, "ts": ts, "items": list(items)}}}


def _srv(rep):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state={}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_healthy_databases_are_quiet():
    assert db_health.problems(_rep(_pg(), _my()), NOW) == []
    assert db_health.problems(_rep(_pg(xact=7200), ts=T - 7200), NOW) == []  # helper давно молчит - не судим
    # ошибка docker exec вместо ответа базы (postgres-exporter без psql) - не база
    junk = _pg(container="postgres-exporter", xact_app='OCI runtime exec failed: exec failed: "psql": '
               'executable file not found in $PATH: unknown')
    assert db_health.view(_rep(_pg(), junk), NOW)[0]["container"] == "pg" and len(db_health.view(_rep(_pg(), junk), NOW)) == 1


def test_replication():
    assert db_health.problems(_rep(_pg(role="replica", receiver="none", lag=40)), NOW) == [
        (2, "PostgreSQL pg: реплика не получает WAL от основной (статус none)", "pg:pg:stream")]
    assert db_health.problems(_rep(_pg(role="replica", receiver="streaming", lag=600)), NOW) == [
        (1, "PostgreSQL pg: реплика отстает на 10 мин", "pg:pg:lag")]
    assert db_health.problems(_rep(_pg(lag=2000)), NOW)[0][:2] == (
        2, "PostgreSQL pg: реплика отстает от основной на 33 мин")
    assert db_health.problems(_rep(_my(sql="No", err="Error 1062: Duplicate entry")), NOW) == [
        (2, "MySQL на хосте: репликация стоит (IO Yes, SQL No): Error 1062: Duplicate entry", "mysql::stream")]
    assert db_health.problems(_rep(_my(lag=400)), NOW)[0][:2] == (1, "MySQL на хосте: реплика отстает на 6 мин")


def test_transactions_locks_wraparound_vacuum():
    rep = _rep(_pg(xact=8000, xact_db="shop", xact_app="report-job", idle_xact=2400, idle_db="shop", idle_app="api",
                   lock_waits=3, lock_wait=420, xid=1_200_000_000, xid_db="shop",
                   dead=[{"db": "shop", "table": "events", "dead": 5_400_000, "live": 9_000_000, "vac": -1},
                         {"db": "shop", "table": "fresh", "dead": 2_000_000, "live": 3_000_000, "vac": 3600}]))
    assert [t for _l, t, _k in db_health.problems(rep, NOW)] == [
        "PostgreSQL pg: транзакция идет 2 ч 13 мин (база shop, report-job)",
        "PostgreSQL pg: транзакция открыта и простаивает 40 мин (база shop, api): держит блокировки",
        "PostgreSQL pg: ждут блокировку 3 запр., дольше всех 7 мин",
        "PostgreSQL pg: возраст транзакций базы shop 1.20 млрд, на 2.1 млрд PostgreSQL остановит запись - "
        "автоочистка не успевает замораживать строки",
        "PostgreSQL pg: в shop.events 5.4 млн мертвых строк, 60% от живых, VACUUM не было ни разу"]
    assert db_health.problems(_rep(_pg(xact=7 * 3600)), NOW)[0][0] == 2
    assert db_health.problems(_rep(_pg(xid=1_600_000_000)), NOW)[0][0] == 2


def test_alert_home_and_view():
    s = _srv(_rep(_pg(role="replica", receiver="none"), _my(xact=4000)))
    lvl, ctx = collector._server_conditions(s, NOW)["db_health"]
    assert lvl == 2 and ctx["sig"] == ["pg:pg:stream", "mysql::xact"]
    rows = [(p["level"], p.get("section")) for p in collector.server_problems(s, NOW) if p["kind"] == "db_health"]
    assert rows == [(2, "services"), (1, "services")]
    v = db_health.view(s.last_report, NOW)
    assert v[0]["role"] == "replica" and v[0]["problems"][0]["level"] == 2 and v[1]["problems"][0]["level"] == 1
    assert collector._server_conditions(_srv(_rep(_pg())), NOW)["db_health"][0] == 0
    assert "db_health" not in collector._server_conditions(_srv({}), NOW)


def test_helper_writes_health():
    src = (Path(__file__).resolve().parents[2] / "agent" / "dbstat-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.6" in src and "health_pg()" in src and "health_mysql()" in src
    assert "backend_xmin is not null" in src and "idle_xmin" in src
    assert "report.d/db-health.json" in src and "pid <> pg_backend_pid()" in src
    assert 'case "$v" in [0-9]*) ;; *) return 0 ;; esac' in src and 'case "$rec" in t|f)' in src
    assert "\r" not in src


def test_vacuum_only_for_a_transaction_holding_a_snapshot():
    """Две простаивающие: старшая держит advisory lock без снимка, младшая после SELECT держит
    снимок. VACUUM мешает только вторая"""
    rep = _rep(_pg(idle_xact=27600, idle_db="shop", idle_app="worker", idle_xmin=26880, idle_xmin_db="shop",
                   idle_xmin_app="worker"))
    assert [t for _l, t, _k in db_health.problems(rep, NOW)] == [
        "PostgreSQL pg: транзакция открыта и простаивает 7 ч 28 мин (база shop, worker): держит снимок, "
        "VACUUM не может чистить мертвые строки",
        "PostgreSQL pg: транзакция открыта и простаивает 7 ч 40 мин (база shop, worker): держит блокировки"]
    # одна и та же транзакция - одна строка, про VACUUM
    one = _rep(_pg(idle_xact=4000, idle_xmin=4000))
    assert [k for _l, _t, k in db_health.problems(one, NOW)] == ["pg:pg:idle"]
