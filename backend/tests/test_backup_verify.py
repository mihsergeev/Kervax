"""Проверка восстановления бэкапа: файлы из снапшота сверяются с диском, дампы читаются, итог
недельной проверки - в алерт, "Что сломано" и карточку бэкапа."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import backup_verify, collector
from app.models import Server

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())


def _rep(**kw):
    b = {"v": 1, "ts": T - 3600, "secs": 14, "snap": "5e288fd8", "snap_ts": T - 7200,
         "files": {"ok": 8, "skipped": 1, "bad": []},
         "dumps": [{"engine": "pg@db-1", "file": "/backup/pg@db-1/all.sql.gz", "ok": True}], "err": ""}
    b.update(kw)
    return {"extras": {"backup-verify": b}}


def _srv(rep):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state={}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_passing_check():
    assert backup_verify.problems(_rep(), NOW) == []
    v = backup_verify.view(_rep(), NOW)
    assert v["files_ok"] == 8 and v["dumps_ok"] == ["pg@db-1"] and v["level"] == 0
    assert backup_verify.problems({"extras": {"backup-verify": {"v": 1, "ts": T, "skip": "no backup"}}}, NOW) == []
    assert backup_verify.view({}, NOW) is None


def test_failures():
    rep = _rep(files={"ok": 6, "skipped": 0, "bad": ["/etc/a.conf", "/etc/b.conf"]},
               dumps=[{"engine": "pg@db-1", "file": "/backup/pg@db-1/x.gz", "ok": False}])
    assert backup_verify.problems(rep, NOW) == [
        (2, "проверка восстановления: 2 файла из снапшота не совпали с диском (/etc/a.conf, /etc/b.conf)"),
        (2, "дамп pg@db-1 из снапшота не читается (/backup/pg@db-1/x.gz)"),
    ]
    assert backup_verify.problems(_rep(err="snapshots: Fatal: unable to open repository"), NOW) == [
        (1, "проверка восстановления не выполнилась: snapshots: Fatal: unable to open repository")]
    old = _rep(ts=T - 12 * 86400)
    assert backup_verify.problems(old, NOW) == [(1, "восстановление не проверялось 12 дней")]


def test_alert_and_home_row():
    s = _srv(_rep(files={"ok": 7, "bad": ["/etc/a.conf"]}))
    lvl, ctx = collector._server_conditions(s, NOW)["backup_restore"]
    assert lvl == 2 and ctx["detail"] == "проверка восстановления: 1 файл из снапшота не совпал с диском (/etc/a.conf)"
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "backup_restore"]
    assert [(p["level"], p["section"], p["srv"]) for p in rows] == [(2, "backups", False)]
    s.last_report = _rep()
    assert collector._server_conditions(s, NOW)["backup_restore"][0] == 0
    s.last_report = {}
    assert "backup_restore" not in collector._server_conditions(s, NOW)


def test_helper_has_verify():
    """helper отдает проверку командой verify и запускает её из ежеминутного refresh"""
    src = (Path(__file__).resolve().parents[2] / "agent" / "backup-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.36" in src and "verify_bin()" in src
    # настройки бэкапа - из юнита (EnvironmentFile=, User=), файл переменных читается как данные:
    # на старых нодах /etc/systemd-resta.conf - скрипт уведомления с curl, "." слал ложное
    # "бэкап с ошибкой"; под cron PATH без /usr/sbin, и runuser не находился
    assert "verify_ctx()" in src and "verify_env()" in src and "EnvironmentFiles" in src
    body = src[src.index("verify_restic() {"):src.index("verify_json_str()")]
    assert '. "$PROV_ENV"' not in body and "source" not in body
    assert 'export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' in src
    assert "  verify)        cmd_verify ;;" in src and "verify_maybe || true" in src
    assert "\r" not in src
