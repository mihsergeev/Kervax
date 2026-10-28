"""Проверка восстановления бэкапа: файлы из снапшота сверяются с диском, дампы читаются, итог
недельной проверки - в алерт, "Что сломано" и карточку бэкапа."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
    assert "KERVAX_SETUP_VERSION=0.47" in src and "verify_bin()" in src
    # настройки бэкапа - из юнита (EnvironmentFile=, User=), файл переменных читается как данные:
    # на старых нодах /etc/systemd-resta.conf - скрипт уведомления с curl, "." слал ложное
    # "бэкап с ошибкой"; под cron PATH без /usr/sbin, и runuser не находился
    assert "verify_ctx()" in src and "verify_env()" in src and "EnvironmentFiles" in src
    body = src[src.index("verify_restic() {"):src.index("verify_json_str()")]
    assert '. "$PROV_ENV"' not in body and "source" not in body
    assert 'export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' in src
    assert "  verify)        cmd_verify ;;" in src and "verify_maybe || true" in src
    assert "\r" not in src


# ---------------- блокировки restic: проверка не оставляет их в репозитории (backup-setup 0.46) ----------------
# Найдено на живом клиенте: дамп из снапшота читался через head -c 8M, restic умирал от SIGPIPE, его блокировка
# оставалась в репозитории, и prune клиентского скрипта (без unlock) падал каждую неделю.

HSRC = (Path(__file__).resolve().parents[2] / "agent" / "backup-setup.sh").read_text(encoding="utf-8")

# restic-заглушка: пишет вызовы в calls, чтение без --no-lock отмечает в locked (настоящий restic
# положил бы блокировку), блокировки - файлы в locks/, unlock убирает старше 30 минут, как restic
FAKE_RESTIC = r'''#!/usr/bin/env bash
T=@T@
echo "$*" >> "$T/calls"
nl=0; a=()
for x in "$@"; do if [ "$x" = --no-lock ]; then nl=1; else a+=("$x"); fi; done
case "${a[0]}" in
  --help) echo "  --no-lock   --retry-lock duration"; exit 0 ;;
  snapshots|dump|ls|cat|list|find|stats) [ "$nl" = 1 ] || echo "${a[0]}" >> "$T/locked" ;;
esac
case "${a[0]}" in
  snapshots) printf '[{"time":"%s","id":"%s"}]\n' "$(date -u -d "@$(cat "$T/snapts")" +%Y-%m-%dT%H:%M:%SZ)" \
               "$(printf 'ab%.0s' $(seq 32))" ;;
  list) ls "$T/locks" ;;
  cat) cat "$T/locks/${a[2]}" || exit 1 ;;
  unlock) for f in "$T"/locks/*; do
            [ -f "$f" ] || continue
            t=$(sed -n 's/.*"time": *"\([^"]*\)".*/\1/p' "$f")
            [ $(( $(date +%s) - $(date -d "$t" +%s) )) -gt 1800 ] && rm -f "$f"
          done; echo unlocked >> "$T/unlocks" ;;
  dump) cat "${a[2]}" ;;
esac
'''

VERIFY_CASE = r'''
mklock() {  # id host user exclusive age_s
  printf '{\n  "time": "%s",\n  "exclusive": %s,\n  "hostname": "%s",\n  "username": "%s",\n  "pid": 4194305,\n  "uid": 0,\n  "gid": 0\n}\n' \
    "$(date -u -d "@$(( $(date +%s) - $5 ))" +%Y-%m-%dT%H:%M:%SZ)" "$4" "$2" "$3" > "$T/locks/$1"
}
id64() { printf "$1%.0s" $(seq 64); }
mkdir -p "$T/locks" "$T/data" "$T/dumps.d" "$T/dumpdir/pg@db-1"
now=$(date +%s); echo $((now - 3600)) > "$T/snapts"
for i in 1 2 3; do echo "file $i" > "$T/data/f$i.conf"; done
: > "$T/dumps.d/pg@db-1.sh"
# дамп больше 8 МБ: head обрывает чтение посередине
{ echo "-- PostgreSQL database dump"; head -c 9000000 /dev/urandom; } | gzip -1 > "$T/dumpdir/pg@db-1/all.sql.gz"
touch -d "@$((now - 7200))" "$T"/data/* "$T/dumpdir/pg@db-1/all.sql.gz"
# путь от каталога теста: в полном пути есть /tmp/, а такие каталоги проверка пропускает
printf 'INCLUDE_MODE="include"\nINCLUDES_STR="data"\nEXCLUDES_STR=""\n' > "$T/script"; cd "$T"
echo "RESTIC_REPOSITORY=rest:https://backup.example/n1/" > "$T/env"
RESTIC_BIN="$T/restic"; PROV_CACERT="$T/none.pem"; DUMPS_D="$T/dumps.d"; DUMP_DIR_BASE="$T/dumpdir"
verify_ctx() { VENV="$T/env"; VUSER=root; VHOME="$T"; VSCRIPT="$T/script"; }
sleep() { echo "sleep $*" >> "$T/sleeps"; }
'''


def _helper_body() -> str:
    body = re.search(r"<<'HELPER_EOF'\n(.*?)\nHELPER_EOF\n", HSRC, re.S).group(1)
    return body[:body.index('\ncase "${1:-}" in\n')]


def _verify(tmp_path: Path, locks: str = "") -> tuple[dict, dict]:
    bash = shutil.which("bash")
    if not bash or not shutil.which("gzip"):
        pytest.skip("нет bash или gzip")
    (tmp_path / "restic").write_text(FAKE_RESTIC.replace("@T@", tmp_path.as_posix()), encoding="utf-8", newline="\n")
    (tmp_path / "restic").chmod(0o755)
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(_helper_body() + '\nset +e\nT="$(cd "$1" && pwd)"\n' + VERIFY_CASE + locks + "\ncmd_verify\n")
        out = subprocess.run([bash, path, tmp_path.as_posix()], capture_output=True, text=True, timeout=120,
                             encoding="utf-8")
    finally:
        os.unlink(path)
    rd = lambda n: (tmp_path / n).read_text(encoding="utf-8") if (tmp_path / n).exists() else ""  # noqa: E731
    files = {n: rd(n) for n in ("calls", "locked", "unlocks", "sleeps")}
    files["locks"] = sorted(p.name[:4] for p in (tmp_path / "locks").iterdir())
    return json.loads(out.stdout.strip().splitlines()[-1]), files


def test_verify_reads_without_locks(tmp_path):
    """все чтения restic в проверке с --no-lock и без --retry-lock: блокировка не пишется, обрывать restic можно"""
    rep, f = _verify(tmp_path)
    assert rep["err"] == "" and rep["files"]["ok"] == 3 and rep["dumps"][0]["ok"] is True
    assert rep["locks_cleared"] == 0
    assert f["locked"] == "", f"restic без --no-lock: {f['locked']}"
    calls = f["calls"].splitlines()
    assert any(c.startswith("dump ") for c in calls) and any(c.startswith("snapshots ") for c in calls)
    assert all("--no-lock" in c for c in calls if not c.startswith("--help")), calls
    assert not any("--retry-lock" in c for c in calls) and f["unlocks"] == ""


def test_verify_clears_only_its_own_stale_lock(tmp_path):
    """своя брошенная блокировка (этот хост, пользователь бэкапа, общая, процесса нет) снимается"""
    rep, f = _verify(tmp_path, 'mklock "$(id64 a)" "$(hostname)" root false 86400\n')
    assert rep["locks_cleared"] == 1 and f["unlocks"] and f["locks"] == [] and rep["err"] == ""


def test_verify_keeps_locks_when_a_foreign_one_is_stale(tmp_path):
    """restic unlock снял бы и чужую старую блокировку - тогда не трогаем ничего, свою тоже"""
    rep, f = _verify(tmp_path, 'mklock "$(id64 a)" "$(hostname)" root false 86400\n'
                               'mklock "$(id64 b)" other-host root false 86400\n')
    assert rep["locks_cleared"] == 0 and f["unlocks"] == "" and f["locks"] == ["aaaa", "bbbb"]
    # чужой пользователь на этом же хосте и свежая своя (процесс мог только что выйти) - тоже не свои
    rep, f = _verify(tmp_path, 'rm -f "$T"/locks/* "$T/unlocks"\n'
                               'mklock "$(id64 c)" "$(hostname)" someone false 86400\n'
                               'mklock "$(id64 d)" "$(hostname)" root false 60\n')
    assert rep["locks_cleared"] == 0 and f["unlocks"] == "" and f["locks"] == ["cccc", "dddd"]


def test_verify_waits_out_an_exclusive_lock(tmp_path):
    """живая эксклюзивная блокировка (prune или check сервера бэкапов): проверка ждет, как раньше --retry-lock"""
    rep, f = _verify(tmp_path, 'mklock "$(id64 e)" backup-server root true 60\n')
    assert "locked exclusively" in rep["err"] and f["sleeps"].count("sleep 30") == 39
    assert not any(c.startswith(("dump ", "snapshots ")) for c in f["calls"].splitlines())
    assert f["locks"] == ["eeee"] and f["unlocks"] == ""


def test_verify_reruns_after_an_old_helper():
    """проверка старого helper'а (без locks_cleared) могла оставить блокировку: следующая идет сразу"""
    body = HSRC[HSRC.index("verify_maybe() {"):HSRC.index("\ncase \"${1:-}\" in\n")]
    assert """grep -q '"locks_cleared":' "$VERIFY_JSON" """ in body and "&& age=" in body
    # head в проверке только у restic с --no-lock
    for line in HSRC.splitlines():
        if "verify_restic" in line and "| head" in line:
            assert "$lock" in line, line
    assert 'lock="--no-lock"' in HSRC
