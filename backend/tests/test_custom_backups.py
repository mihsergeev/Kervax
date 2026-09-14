"""Свои бэкапы ноды: заданные без панели cron, systemd-таймеры и скрипты с метриками.

Данные — как их прислал helper с живой ноды ru-be-evpatij-prod (сентябрь 2026): ансибл-роль
pg-backup по крону с метриками, таймер дампа Neo4j и таймер бэкапа SQLite n8n, который
в ту ночь упал по таймауту. Панель их не видела и звала «нужен отдельный дамп».
"""

from datetime import datetime, timedelta, timezone

import httpx

from app import collector, custom_backups
from app.models import Server

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())


def _jobs():
    return [
        {"id": "systemd:n8n-sqlite-backup.timer", "kind": "systemd", "name": "n8n-sqlite-backup",
         "desc": "Consistent n8n SQLite backup", "schedule": "*-*-* 00:45:00", "engines": ["SQLite"],
         "files": False, "containers": [], "unit": "n8n-sqlite-backup.service",
         "ok": 0, "ok_ts": 0, "run_ts": T - 11 * 3600, "result": "timeout", "running": False,
         "next_ts": T + 12 * 3600, "prev_ts": 0, "enabled": True},
        {"id": "systemd:neo4j-backup.timer", "kind": "systemd", "name": "neo4j-backup",
         "schedule": "*-*-* 01:30:00", "engines": ["Neo4j"], "files": False, "containers": ["neo4j"],
         "ok": 1, "ok_ts": T - 10 * 3600, "run_ts": T - 10 * 3600, "result": "success",
         "running": False, "next_ts": T + 13 * 3600, "prev_ts": T - 11 * 3600, "enabled": True},
        {"id": "cron:0a1b2c3d4e5f", "kind": "cron", "name": "pg_backup_run.sh", "schedule": "0 4 * * *",
         "engines": ["PostgreSQL"], "files": False, "containers": ["postgresql"],
         "user": "root", "ok": 1, "ok_ts": T - 8 * 3600, "size_bytes": 1682026335,
         "dir": "/app/backups/postgres", "log": "/var/log/pg_backup/cron_postgresql.log",
         "metrics": "/var/lib/node_exporter/textfile_collector/pg_backup_postgresql.prom",
         "dbs": [{"name": "evpatij", "ok": 1, "ts": T - 8 * 3600, "size_bytes": 1682022719},
                 {"name": "postgres", "ok": 1, "ts": T - 8 * 3600, "size_bytes": 3616}]},
    ]


def _server(jobs=None, **kw):
    rep = {
        "db_engines": ["Neo4j", "PostgreSQL"],
        "docker": {"present": True, "access": True, "containers": [
            {"name": "postgresql", "image": "postgresql-postgresql", "state": "running"},
            {"name": "litellm-db", "image": "postgres:16-alpine", "state": "running"},
            {"name": "neo4j", "image": "neo4j:5.26-community", "state": "running"},
        ]},
        "extras": {"custom-backups": {"v": 1, "ts": T, "jobs": _jobs() if jobs is None else jobs}},
    }
    rep.update(kw.pop("report", {}))
    kw.setdefault("backup_not_required", True)
    return Server(name="ru-be-evpatij-prod", token_hash="x", enabled=True, last_seen=NOW,
                  offline_after_seconds=120, last_report=rep, **kw)


def test_statuses():
    views = {v.name: v for v in custom_backups.views(_server(), NOW)}
    n8n = views["n8n-sqlite-backup"]
    assert n8n.status == "failed" and "таймаут" in n8n.problem
    assert views["neo4j-backup"].status == "ok"
    pg = views["pg_backup_run.sh"]
    assert pg.status == "ok" and [d.name for d in pg.dbs] == ["evpatij", "postgres"]

    # суточный бэкап имеет право пропустить одну ночь, а второй пропуск — уже поломка
    old = _jobs()[2] | {"ok_ts": T - 30 * 3600}
    assert custom_backups.evaluate(old, NOW, set()).status == "ok"
    old["ok_ts"] = T - 50 * 3600
    stale = custom_backups.evaluate(old, NOW, set())
    assert stale.status == "stale" and "нет свежего" in stale.problem

    # выключенный таймер — бэкап не запускается вовсе
    off = _jobs()[1] | {"enabled": False}
    assert custom_backups.evaluate(off, NOW, set()).status == "disabled"
    # идёт прямо сейчас — не проблема, даже если прошлый упал
    run = _jobs()[0] | {"running": True}
    assert custom_backups.evaluate(run, NOW, set()).status == "running"
    # по крону без метрик и без файлов не видно, когда он отработал
    blind = {"id": "cron:x", "kind": "cron", "name": "x.sh", "schedule": "0 1 * * *", "ok": -1}
    assert custom_backups.evaluate(blind, NOW, set()).status == "unknown"
    # отмеченное «не отслеживать» не алертит
    assert custom_backups.evaluate(_jobs()[0], NOW, {"systemd:n8n-sqlite-backup.timer"}).status == "ignored"


def test_frozen_scan_raises_nothing():
    """helper перестал запускаться: застывший снимок не должен через двое суток превратиться
    в «нет свежего бэкапа» по заданиям, которые исправно работают."""
    s = _server()
    s.last_report["extras"]["custom-backups"]["ts"] = T - 5 * 3600
    views = custom_backups.views(s, NOW)
    assert {v.status for v in views} == {"unknown"} and all(v.scan_stale for v in views)
    assert collector._server_conditions(s, NOW)["backup_custom"][0] == 0
    # покрытие при этом держится: задания никуда не делись
    from app.api.servers import _backup_coverage
    audit = {(a.subject, a.instance): a for a in _backup_coverage(s)}
    assert audit[("PostgreSQL", "postgresql")].kind == "db_ok"


def test_intervals():
    assert custom_backups.cron_interval("0 4 * * *") == 86400
    assert custom_backups.cron_interval("*/15 * * * *") == 900
    assert custom_backups.cron_interval("0 */6 * * *") == 6 * 3600
    assert custom_backups.cron_interval("0 3 * * 0") == 7 * 86400
    assert custom_backups.cron_interval("@hourly") == 3600
    assert custom_backups.calendar_interval("*-*-* 01:30:00") == 86400
    assert custom_backups.calendar_interval("weekly") == 7 * 86400
    assert custom_backups.calendar_interval("Sun *-*-* 03:00:00") == 7 * 86400
    # ежечасный бэкап протухает быстрее ежедневного, но не раньше двух часов
    hourly = {"kind": "cron", "schedule": "0 * * * *"}
    assert custom_backups.stale_after(hourly) == 2 * 3600


def test_coverage_is_per_database():
    """Свой бэкап закрывает находку только по той базе, которую он действительно бэкапит:
    pg_backup снимает контейнер postgresql, а litellm-db на той же ноде остаётся без дампа."""
    from app.api.servers import _backup_coverage

    audit = {(a.subject, a.instance): a for a in _backup_coverage(_server())}
    assert audit[("PostgreSQL", "postgresql")].kind == "db_ok"
    assert "pg_backup_run.sh" in audit[("PostgreSQL", "postgresql")].detail
    assert audit[("PostgreSQL", "litellm-db")].kind == "db"
    assert audit[("Neo4j", "neo4j")].kind == "db_ok"

    # задание без ссылок на контейнеры закрывает базу, только если она на ноде одна
    jobs = _jobs()
    jobs[2]["containers"] = []
    audit = {(a.subject, a.instance): a for a in _backup_coverage(_server(jobs))}
    assert audit[("PostgreSQL", "postgresql")].kind == "db"
    assert audit[("PostgreSQL", "litellm-db")].kind == "db"

    # нативная postgres (без контейнера) и дамп без контейнера — покрыта
    native = _server(jobs, report={"docker": {"present": False}, "db_engines": ["PostgreSQL"]})
    audit = {(a.subject, a.instance): a for a in _backup_coverage(native)}
    assert audit[("PostgreSQL", "")].kind == "db_ok"

    # «не отслеживать» — и покрытие не закрывает
    ign = _server()
    ign.custom_backup_ignored = ["cron:0a1b2c3d4e5f"]
    audit = {(a.subject, a.instance): a for a in _backup_coverage(ign)}
    assert audit[("PostgreSQL", "postgresql")].kind == "db"


def test_alert_conditions():
    cond = collector._server_conditions(_server(), NOW)
    lvl, ctx = cond["backup_custom"]
    assert lvl == 1 and "n8n-sqlite-backup" in ctx["jobs"] and ctx["n"] == 1

    ok_jobs = _jobs()[1:]
    cond = collector._server_conditions(_server(ok_jobs), NOW)
    assert cond["backup_custom"][0] == 0

    # старый helper без блока: условие есть и равно нулю — прошлый алерт закроется
    s = _server([])
    s.last_report.pop("extras")
    assert collector._server_conditions(s, NOW)["backup_custom"][0] == 0


def test_own_file_backup_counts_as_configured():
    """restic/borg, настроенный без панели, — это бэкап: «не настроен» было бы враньём.
    Дамп одной базы бэкапом всей ноды не считается."""
    borg = {"id": "cron:b", "kind": "cron", "name": "borg-backup.sh", "schedule": "0 2 * * *",
            "engines": [], "files": True, "containers": [], "ok": -1, "ok_ts": T - 3600}
    s = _server([borg], backup_not_required=False)
    s.created_at = NOW - timedelta(days=30)
    s.alert_state = {"backup_missing_since": (NOW - timedelta(days=3)).isoformat()}
    assert collector._server_conditions(s, NOW)["backup_missing"][0] == 0

    s = _server(_jobs()[2:], backup_not_required=False)
    s.created_at = NOW - timedelta(days=30)
    s.alert_state = {"backup_missing_since": (NOW - timedelta(days=3)).isoformat()}
    assert collector._server_conditions(s, NOW)["backup_missing"][0] == 1


async def test_report_extras_and_ignore_api(client: httpx.AsyncClient, auth_headers):
    r = await client.post("/api/servers", json={"name": "node"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    report = {
        "hostname": "node", "os": "Ubuntu 24.04", "agent_version": "2.8",
        "cpu_percent": 1.0, "mem_used": 1, "mem_total": 2,
        "load": [0.1, 0.1, 0.1], "disks": [{"mount": "/", "used": 1, "total": 2}],
        # снимок свежий по НАСТОЯЩИМ часам: API судит по текущему времени, а не по NOW
        "extras": {"custom-backups": {"v": 1, "ts": int(datetime.now(timezone.utc).timestamp()),
                                      "jobs": _jobs()}},
    }
    r = await client.post("/api/agent/report", json=report,
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text

    srv = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    names = {j["name"]: j for j in srv["custom_backups"]}
    assert set(names) == {"n8n-sqlite-backup", "neo4j-backup", "pg_backup_run.sh"}
    assert names["n8n-sqlite-backup"]["status"] == "failed"

    r = await client.post(f"/api/servers/{sid}/backup/custom-ignore", headers=auth_headers,
                          json={"id": "systemd:n8n-sqlite-backup.timer", "ignored": True})
    assert r.status_code == 200, r.text
    names = {j["name"]: j for j in r.json()["custom_backups"]}
    assert names["n8n-sqlite-backup"]["status"] == "ignored"
    r = await client.post(f"/api/servers/{sid}/backup/custom-ignore", headers=auth_headers,
                          json={"id": "systemd:n8n-sqlite-backup.timer", "ignored": False})
    names = {j["name"]: j for j in r.json()["custom_backups"]}
    assert names["n8n-sqlite-backup"]["status"] == "failed"
