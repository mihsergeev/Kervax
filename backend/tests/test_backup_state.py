"""Настроен ли бэкап: одно правило для главной, раздела "Бэкапы", алертов и формы настройки.

Найденный случай: на свежей ноде лежала метрика restic от бэкапа, которого давно нет (ни restic,
ни таймера). Главная писала "бэкап завершился с ошибкой", раздел клал ноду в "Без бэкапа", а форма
"Настроить бэкап" отказывала - "уже настроен". Настроить бэкап было нельзя никак."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import backup_state, collector
from app.models import Server

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
T = NOW.timestamp()
ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "agent" / "backup-setup.sh").read_text(encoding="utf-8")

# следы старого бэкапа: метрика от февраля, restic и таймера нет, helper панели стоит
ORPHAN = {"present": True, "restic_found": False, "configured": False, "metric_present": True,
          "success": 0, "last_backup_ts": 1771236654, "manageable": True}
ALIVE_FAILED = {"present": True, "restic_found": True, "configured": True, "timer_enabled": True,
                "metric_present": True, "success": 0, "last_backup_ts": T - 3600}


def _srv(bk, extras=None, **kw):
    rep = {"clock_unix": T, "backup": bk}
    if extras:
        rep["extras"] = extras
    base = dict(id=1, name="n1", token_hash="x", enabled=True, last_seen=NOW, offline_after_seconds=120,
                alert_sustain_seconds=900, last_report=rep, alert_state={}, alert_mutes=[],
                created_at=NOW - timedelta(days=5), backup_not_required=False)
    base.update(kw)
    return Server(**base)


def test_orphan_metric_is_no_backup_with_traces():
    assert not backup_state.backup_alive(ORPHAN) and not backup_state.backup_failed(ORPHAN)
    v = backup_state.view(_srv(ORPHAN))
    assert v["state"] == "none" and v["reason"] == ""
    assert v["traces"] == "осталась метрика старого бэкапа от 16.02.2026, restic и таймера бэкапа нет"
    # бэкап есть: таймер, либо restic вместе с метрикой (бэкап по крону)
    assert backup_state.backup_alive({"present": True, "configured": True})
    assert backup_state.backup_alive({"present": True, "restic_found": True, "metric_present": True})
    assert not backup_state.backup_alive({"present": True, "restic_found": True})
    assert not backup_state.backup_alive(None)


def test_failed_setup_is_partial_until_backup_appears():
    err = {"ts": (NOW - timedelta(hours=2)).isoformat(), "msg": "не удалось настроить бэкап на клиенте", "job": 7}
    v = backup_state.view(_srv(ORPHAN, backup_setup_err=err))
    assert (v["state"], v["reason"], v["since"]) == ("partial", err["msg"], err["ts"])
    assert v["traces"]  # и следы старого видны
    # бэкап появился (настроили руками) - старая ошибка настройки не в счет
    assert backup_state.view(_srv(ALIVE_FAILED, backup_setup_err=err))["state"] == "ok"
    assert backup_state.setup_error(_srv(ALIVE_FAILED, backup_setup_err=err)) is None


def test_fail_reason_from_helper_belongs_to_the_last_run():
    fail = {"ts": T - 3500, "msg": "Fatal: unable to open config file: connection refused"}
    rep_ex = {"backup-runs": {"v": 1, "ts": T, "runs": [], "fail": fail}}
    s = _srv(ALIVE_FAILED, extras=rep_ex)
    assert backup_state.view(s) == {"state": "ok", "reason": "сервер бэкапов не принимает соединения",
                                    "reason_raw": fail["msg"], "since": None, "traces": ""}
    # ошибка прошлого прогона, последний упал позже и по-другому - не выдаем ее за причину
    s = _srv(ALIVE_FAILED | {"last_backup_ts": T}, extras={"backup-runs": {"fail": fail | {"ts": T - 86400}}})
    assert backup_state.fail_reason(s.last_report) == ""
    # прогон успешный - причины нет, даже если в файле осталась
    s = _srv(ALIVE_FAILED | {"success": 1}, extras=rep_ex)
    assert backup_state.fail_reason(s.last_report) == ""



# helper до 0.42 брал в причину первые строки журнала - повторы restic перед отказом
RETRY_MSG = ('Stat(<config/>) returned error, retrying after 1.301661681s: Head "https://***@203.0.113.7:64101/'
             'backup-a/config": tls: failed to verify certificate: x509: certificate signed by unknown authority '
             'Stat(<config/>) returned error, retrying after 2.381765077s: Head "https://***@203.0.113.7:64101')


def test_fail_short_says_what_broke():
    assert backup_state.fail_short(RETRY_MSG) == "сервер бэкапов: сертификат не доверен (x509)"
    assert backup_state.fail_line(RETRY_MSG).startswith('Head "https://***@203.0.113.7:64101/backup-a/config": tls:')
    assert "retrying" not in backup_state.fail_line(RETRY_MSG)
    fatal = ('Fatal: unable to open config file: Head "https://***@203.0.113.7:64101/backup-a/config": '
             'x509: certificate is valid for 198.51.100.9, not 203.0.113.7')
    assert backup_state.fail_short(fatal) == "сервер бэкапов: сертификат выписан на другой адрес (x509)"
    assert backup_state.fail_short("Fatal: wrong password or no key found") == "не подходит пароль репозитория"
    locked = "Fatal: unable to create lock in backend: repository is already locked by PID 7"
    assert backup_state.fail_short(locked) == "репозиторий занят чужим локом"
    # незнакомое - первая осмысленная строка, без "Fatal:"
    assert backup_state.fail_short("Fatal: something new happened") == "something new happened"
    assert len(backup_state.fail_short("Fatal: " + "x" * 400)) <= 122
    assert backup_state.fail_short("") == ""


def test_alert_and_home_get_the_short_reason():
    s = _srv(ALIVE_FAILED, extras={"backup-runs": {"fail": {"ts": T - 3500, "msg": RETRY_MSG}}})
    v = backup_state.view(s)
    assert v["reason"] == "сервер бэкапов: сертификат не доверен (x509)" and v["reason_raw"] == RETRY_MSG
    why = collector._server_conditions(s, NOW)["backup_failed"][1]["why"]
    assert why == ": сервер бэкапов: сертификат не доверен (x509)"

def test_alerts_follow_the_same_rule():
    # следы старого бэкапа: не "бэкап упал", а "не настроен" (после отсрочки суток)
    s = _srv(ORPHAN, alert_state={"backup_missing_since": (NOW - timedelta(days=2)).isoformat()})
    cond = collector._server_conditions(s, NOW)
    assert "backup_failed" not in cond and "backup_stale" not in cond
    assert cond["backup_missing"][0] == 1 and cond["backup_missing"][1]["why"] == ""
    # висящий по метрике алерт закрывается явным нулем, иначе висел бы вечно
    s.alert_state |= {"backup_failed": 1, "backup_stale": 1}
    cond = collector._server_conditions(s, NOW)
    assert cond["backup_failed"][0] == 0 and cond["backup_stale"][0] == 0
    # упавшая настройка - причиной в "не настроен"
    s.backup_setup_err = {"ts": NOW.isoformat(), "msg": "не удалось создать репозиторий", "job": 3}
    why = collector._server_conditions(s, NOW)["backup_missing"][1]["why"]
    assert why == ". Настройка из панели не прошла: не удалось создать репозиторий"
    # живой упавший бэкап - "упал" со строкой ошибки restic
    fail = {"ts": T - 3500, "msg": "Fatal: wrong password or no key found"}
    s = _srv(ALIVE_FAILED, extras={"backup-runs": {"fail": fail}})
    cond = collector._server_conditions(s, NOW)
    assert cond["backup_failed"] == (1, {"why": ": не подходит пароль репозитория"})
    assert cond["backup_missing"][0] == 0


def test_alert_text_carries_the_reason():
    from app import settings_store

    tpl = settings_store.SERVER_ALERT_KINDS["backup_failed"][1]
    s = _srv(ALIVE_FAILED)
    assert collector._fmt_rule(tpl, s, {"why": ": Fatal: x"}) == "последний бэкап завершился с ошибкой: Fatal: x"
    tpl = settings_store.SERVER_ALERT_KINDS["backup_missing"][1]
    assert collector._fmt_rule(tpl, s, {"since": "", "why": ""}).startswith("на сервере не настроен бэкап (")
    # старые тексты по умолчанию - в легаси: у кого они сохранены, получат причину
    assert "последний бэкап завершился с ошибкой" in settings_store.LEGACY_SERVER_DEFAULTS


def test_dumps_beside_orphan_metric_are_judged_by_the_clock():
    bk = ORPHAN | {"dumps": [{"engine": "postgres", "files": 2, "last_ts": T - 3600}]}
    broken, reason, _skipped, _free = collector.dump_issues({"backup": bk}, NOW)
    # раньше метрика "упал" глушила дампы целиком; теперь их сверяют по часам, как без бэкапа
    assert broken == [] and reason == collector._DUMP_REASON_LOCAL


# ------------------------------- API -------------------------------

def _app(client):
    return client._transport.app


async def _two_servers(client, auth_headers, client_bk, n=1):
    a = (await client.post("/api/servers", json={"name": f"cl-{n}"}, headers=auth_headers)).json()["server"]["id"]
    b = (await client.post("/api/servers", json={"name": f"bs-{n}"}, headers=auth_headers)).json()["server"]["id"]
    async with _app(client).state.session_factory() as s:
        ca, cb = await s.get(Server, a), await s.get(Server, b)
        ca.last_report = {"backup": client_bk}
        ca.agent_ip = "10.0.0.5"
        cb.last_report = {"backup_server": {"present": True, "running": True, "port": 64100, "tls_port": 64101}}
        cb.agent_ip = "10.0.0.6"
        await s.commit()
    return a, b


async def test_setup_form_allows_orphan_and_asks_before_overwriting(client, auth_headers, monkeypatch):
    from app.api import servers as srv_api

    started = []

    async def fake(_f, job_id, params):
        started.append(params.get("_user"))

    monkeypatch.setattr(srv_api, "_orchestrate_setup", fake)
    body = {"backup_server_id": 0, "mode": "exclude", "paths": ["/proc"], "schedule": "12:00"}
    a, b = await _two_servers(client, auth_headers, ORPHAN)
    r = await client.post(f"/api/servers/{a}/backup/setup", json=body | {"backup_server_id": b}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert len(started) == 1
    # живой бэкап: без согласия отказ, с согласием - поверх
    a2, b2 = await _two_servers(client, auth_headers, ALIVE_FAILED, 2)
    r = await client.post(f"/api/servers/{a2}/backup/setup", json=body | {"backup_server_id": b2}, headers=auth_headers)
    assert r.status_code == 409 and "переустановить поверх" in r.json()["detail"]
    r = await client.post(f"/api/servers/{a2}/backup/setup", json=body | {"backup_server_id": b2, "reinstall": True},
                          headers=auth_headers)
    assert r.status_code == 200 and len(started) == 2
    lst = {x["id"]: x for x in (await client.get("/api/servers", headers=auth_headers)).json()}
    assert lst[a]["backup_state"]["state"] == "none" and lst[a]["backup_state"]["traces"]
    assert lst[a2]["backup_state"]["state"] == "ok"


async def test_setup_result_and_deprovision_mark_the_server(client, auth_headers):
    from app.api.servers import _job_runner
    from app.models import BackupCommand, BackupSetupJob

    a, b = await _two_servers(client, auth_headers, ORPHAN)
    f = _app(client).state.session_factory
    async with f() as s:
        job = BackupSetupJob(server_id=a, backup_server_id=b, status="running", steps=[])
        s.add(job)
        await s.commit()
        jid = job.id
    _steps, save, _step = _job_runner(f, jid, "admin", "setup")
    await save("error", "не удалось настроить бэкап на клиенте")
    lst = {x["id"]: x for x in (await client.get("/api/servers", headers=auth_headers)).json()}
    st = lst[a]["backup_state"]
    assert st["state"] == "partial" and st["reason"] == "не удалось настроить бэкап на клиенте" and st["since"]

    # "удалить настройку": команда агенту, успех снимает отметку
    r = await client.post(f"/api/servers/{a}/backup/command", json={"action": "deprovision"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    token = "tok-" + "x" * 30
    from app.security import hash_agent_token

    async with f() as s:
        srv = await s.get(Server, a)
        srv.token_hash = hash_agent_token(token)
        assert (await s.get(BackupCommand, cid)).action == "deprovision"
        await s.commit()
    r = await client.post("/api/agent/backup-result", json={"id": cid, "ok": True, "output": "OK removed"},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    async with f() as s:
        assert (await s.get(Server, a)).backup_setup_err is None

    # удачная настройка тоже снимает
    async with f() as s:
        (await s.get(Server, a)).backup_setup_err = {"ts": NOW.isoformat(), "msg": "x", "job": jid}
        await s.commit()
    await save("done", "бэкап настроен")
    async with f() as s:
        assert (await s.get(Server, a)).backup_setup_err is None


async def test_deprovision_refused_for_a_working_backup(client, auth_headers):
    a, _b = await _two_servers(client, auth_headers, ALIVE_FAILED)
    r = await client.post(f"/api/servers/{a}/backup/command", json={"action": "deprovision"}, headers=auth_headers)
    assert r.status_code == 400 and "работает бэкап" in r.json()["detail"]


# ------------------------------- helper -------------------------------

def _helper_body() -> str:
    body = re.search(r"<<'HELPER_EOF'\n(.*?)\nHELPER_EOF\n", SRC, re.S).group(1)
    return body[:body.index('\ncase "${1:-}" in\n')]


def _run(tmp_path: Path, case: str) -> str:
    bash = shutil.which("bash")
    if not bash or not shutil.which("awk"):
        pytest.skip("нет bash или awk")
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(_helper_body() + '\nset +e\nT="$(cd "$1" && pwd)"\n' + case)
        out = subprocess.run([bash, path, tmp_path.as_posix()], capture_output=True, text=True, timeout=60,
                             encoding="utf-8")
    finally:
        os.unlink(path)
    return out.stdout + out.stderr


def test_helper_version_and_spool_know_deprovision():
    assert re.search(r"^KERVAX_SETUP_VERSION=0\.42 ", SRC, re.M)
    assert backup_state.FAIL_REASON_HELPER == "0.39"
    assert '      deprovision)  if out="$(cmd_deprovision 2>&1)"; then ok=true; fi ;;' in SRC


def test_agent_passes_deprovision_to_the_helper():
    """без агента 2.45 кнопка "Удалить настройку" неактивна: старый агент отбивает действие сам"""
    go = (ROOT / "agent" / "main.go").read_text(encoding="utf-8")
    run = go[go.index("func runBackupCommand("):]
    assert 'case "deprovision":' in run[:run.index("\n}\n")]
    assert 'lines = []string{"action=deprovision"}' in run
    m = re.search(r'^const version = "2\.(\d+)"$', go, re.M)
    assert m and int(m.group(1)) >= 45


def test_helper_writes_why_the_last_run_failed(tmp_path):
    """строка ошибки restic последнего прогона - в backup-runs.json, пароль из адреса скрыт"""
    out = _run(tmp_path, r'''
systemctl() {
  case "$*" in
    *"-p Result"*) echo exit-code ;;
    *"-p InvocationID"*) echo inv1 ;;
    *"-p ExecMainExitTimestamp"*) echo "" ;;
  esac
}
journalctl() {
  case "$*" in
    *_SYSTEMD_INVOCATION_ID=inv1*)
      printf 'repository opened\n'
      printf 'Fatal: unable to open config file: Get "https://cl-1:s3cr3t@10.0.0.6:64101/cl-1/config": dial tcp: connection refused\n'
      printf 'Is there a "repository" at the \\following location?\n' ;;
  esac
}
backup_runs_parse systemd-rest.service "$(backup_last_fail systemd-rest.service)" </dev/null
''')
    data = json.loads(out.strip().splitlines()[-1])
    assert data["runs"] == [] and data["unit"] == "systemd-rest.service"
    msg = data["fail"]["msg"]
    assert msg.startswith("Fatal: unable to open config file:") and "s3cr3t" not in msg and "//***@10.0.0.6" in msg
    assert data["fail"]["ts"] > 0


def test_helper_runs_json_without_failure_is_unchanged(tmp_path):
    out = _run(tmp_path, r'''
systemctl() { case "$*" in *"-p Result"*) echo success ;; esac; }
backup_runs_parse systemd-rest.service "$(backup_last_fail systemd-rest.service)" </dev/null
''')
    data = json.loads(out.strip().splitlines()[-1])
    assert "fail" not in data and data["v"] == 1


def test_helper_deprovision_removes_only_its_own_files(tmp_path):
    out = _run(tmp_path, r'''
systemctl() { :; }
refresh_config() { :; }
PROV_ENV="$T/resta.conf"; PROV_SCRIPT="$T/rest.conf"; RESTIC_BIN_DIR="$T/.restic"; PROV_HOME="$T/bx"
PROV_METRICS_DIR="$T/tc"; RUNS_JSON="$T/runs.json"
mkdir -p "$RESTIC_BIN_DIR" "$PROV_HOME" "$PROV_METRICS_DIR"
echo pw > "$PROV_ENV"; echo run > "$PROV_SCRIPT"; echo bin > "$RESTIC_BIN_DIR/restic"
echo 'restic_last_backup_success{host="n1"} 0' > "$PROV_METRICS_DIR/rk.prom"
echo 'restic_last_backup_success{host="n1"} 1' > "$PROV_METRICS_DIR/restic-backup.prom"
echo 'node_custom 1' > "$PROV_METRICS_DIR/other.prom"
echo 'restic_alert_silence 1' > "$PROV_METRICS_DIR/rk2.prom"
cmd_deprovision
ls -A "$T" "$PROV_METRICS_DIR"
''')
    assert "OK the backup setup is removed from the node:" in out and "repository on the backup server is kept" in out
    left = out.split("\n", 1)[1]
    for gone in ("resta.conf", "rest.conf", ".restic", "bx", "rk.prom", "restic-backup.prom"):
        assert not re.search(rf"^{re.escape(gone)}$", left, re.M), (gone, out)
    # чужие файлы метрик не трогаем
    assert "other.prom" in left and "rk2.prom" in left


def test_helper_reason_is_the_fatal_line_not_the_retries(tmp_path):
    """restic минутами повторяет запрос перед отказом: в причину идет строка Fatal, а не повторы"""
    out = _run(tmp_path, r'''
systemctl() {
  case "$*" in
    *"-p Result"*) echo exit-code ;;
    *"-p InvocationID"*) echo inv1 ;;
    *"-p ExecMainExitTimestamp"*) echo "" ;;
  esac
}
journalctl() {
  case "$*" in
    *_SYSTEMD_INVOCATION_ID=inv1*)
      for i in 1 2 3; do
        printf 'Stat(<config/>) returned error, retrying after 1.%ss: Head "https://backup-a:fakepass@203.0.113.7:64101/backup-a/config": tls: failed to verify certificate: x509: certificate signed by unknown authority\n' "$i"
      done
      printf 'Fatal: unable to open config file: Head "https://backup-a:fakepass@203.0.113.7:64101/backup-a/config": tls: failed to verify certificate: x509: certificate signed by unknown authority\n' ;;
  esac
}
backup_runs_parse systemd-rest.service "$(backup_last_fail systemd-rest.service)" </dev/null
''')
    msg = json.loads(out.strip().splitlines()[-1])["fail"]["msg"]
    assert msg.startswith("Fatal: unable to open config file:") and "retrying" not in msg
    assert "fakepass" not in msg and "x509: certificate signed by unknown authority" in msg


def test_helper_gives_the_certificate_to_nodes_switched_by_040(tmp_path):
    """0.40 перевел ансибл-runner без --cacert: refresh дописывает RESTIC_CACERT в env один раз и
    только там, где переводил to-https (рядом .http-bak)"""
    out = _run(tmp_path, r'''
chown() { :; }
PROV_ENV="$T/env"; PROV_CACERT="$T/backup-ca.pem"
printf -- '-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n' > "$PROV_CACERT"
printf 'RESTIC_PASSWORD=fakepass\nRESTIC_REPOSITORY=rest:https://backup-a:fakepass@203.0.113.7:64101/backup-a/\n' > "$PROV_ENV"
https_cacert_maybe; echo "no-bak: $(grep -c '^RESTIC_CACERT=' "$PROV_ENV")"
printf 'RESTIC_REPOSITORY=rest:http://x/\n' > "$PROV_ENV.http-bak"
https_cacert_maybe; https_cacert_maybe
echo "switched: $(grep -c '^RESTIC_CACERT=' "$PROV_ENV") $(stat -c %a "$PROV_ENV")"
grep '^RESTIC_CACERT=' "$PROV_ENV"; grep -c '^RESTIC_PASSWORD=fakepass$' "$PROV_ENV"
printf 'RESTIC_REPOSITORY=rest:http://backup-a:fakepass@203.0.113.7:64100/backup-a/\n' > "$PROV_ENV"
https_cacert_maybe; echo "http: $(grep -c '^RESTIC_CACERT=' "$PROV_ENV")"
''')
    lines = out.strip().splitlines()
    assert "no-bak: 0" in lines and "http: 0" in lines
    assert any(x.startswith("switched: 1 ") for x in lines)  # права 0600 проверяет сценарий в WSL
    assert any(x.startswith("RESTIC_CACERT=") and x.endswith("/backup-ca.pem") for x in lines)
    assert "1" in lines  # пароль на месте


def test_helper_job_check_runs_restic_as_the_backup_service(tmp_path):
    """проверка после перевода на https идет как ночной прогон: User и EnvironmentFile сервиса бэкапа,
    без своего --cacert; у юнита без EnvironmentFile - env панели"""
    out = _run(tmp_path, r'''
find_timer() { echo systemd-rest.timer; }
timeout() { shift; "$@"; }
systemd-run() { echo "RUN $*" >&2; }
systemctl() {
  case "$*" in
    "show -p User --value systemd-rest.service") echo bx231 ;;
    "show -p EnvironmentFiles --value systemd-rest.service")
      printf '/etc/systemd-resta.conf (ignore_errors=no)\n/etc/backup-extra.env (ignore_errors=yes)\n' ;;
  esac
}
unit_restic_check
systemctl() { :; }
unit_restic_check
''')
    runs = [x for x in out.splitlines() if x.startswith("RUN ")]
    assert len(runs) == 2
    assert "-p User=bx231 -p EnvironmentFile=/etc/systemd-resta.conf -p EnvironmentFile=/etc/backup-extra.env" in runs[0]
    assert runs[0].endswith("-- /usr/local/lib/.restic/restic --no-cache --no-lock cat config")
    assert "--cacert" not in runs[0] and "RuntimeMaxSec=150" in runs[0]
    assert "User=" not in runs[1] and "-p EnvironmentFile=/etc/systemd-resta.conf --" in runs[1]
