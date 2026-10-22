"""Причина отказа пробного дампа доходит до интерфейса (helper backup-setup 0.38, app/dump_reason.py).

Живой случай: MySQL 8 на хосте пускает root только с паролем. Helper до 0.38 входил в базу только
как root и отправлял ошибку в /dev/null, панель получала "the dump probe failed, dumps were NOT
enabled:" без причины и показывала прочерк. С 0.38 helper входит через options-файлы ноды и всегда
пишет причину, а пустую причину от старого helper'а дописывает панель.
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app import dump_reason
from app.dump_reason import PROBE_PREFIX, fill_dump_reason

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "agent" / "backup-setup.sh").read_text(encoding="utf-8")
HELPER_TEXT = (ROOT / "frontend" / "src" / "helperText.ts").read_text(encoding="utf-8")


# ---------------------------------------------------------------- панель: пустая причина

def test_reason_left_alone_when_there_is_one():
    out = f"{PROBE_PREFIX} mysqldump: Got error: 1045: Access denied"
    assert fill_dump_reason("dump_setup", False, out, {"engine": "mysql"}, {"backup-setup": "0.37"}) == out
    # успех и чужие команды не трогаем, даже с пустым выводом
    assert fill_dump_reason("dump_setup", True, "", {"engine": "mysql"}, {}) == ""
    assert fill_dump_reason("set_paths", False, "", None, {}) == ""
    assert fill_dump_reason("run_now", False, PROBE_PREFIX, None, {}) == PROBE_PREFIX


def test_empty_reason_from_old_helper_on_host_mysql():
    """найденный случай: helper 0.37, MySQL на хосте, причина пустая"""
    for out in (PROBE_PREFIX, PROBE_PREFIX + " ", PROBE_PREFIX.rstrip(":")):
        got = fill_dump_reason("dump_setup", False, out, {"engine": "mysql", "container": ""},
                               {"backup-setup": "0.37"})
        assert got.startswith(PROBE_PREFIX + " no reason came from backup-setup 0.37 for mysql: ")
        assert "helpers before 0.38 lose the error of the check, update it" in got
        assert "(MySQL on the host: 0.38 also logs in through /etc/mysql/debian.cnf or /root/.my.cnf)" in got
        assert not got.rstrip().endswith(":")


def test_empty_reason_names_the_slot_and_version():
    got = fill_dump_reason("dump_setup", False, PROBE_PREFIX, {"engine": "pg", "container": "db-a"},
                           {"backup-setup": "0.30"})
    assert "backup-setup 0.30 for pg@db-a: helpers before 0.38" in got and "MySQL" not in got
    # версии сравниваются покомпонентно: 0.4 старше 0.38
    got = fill_dump_reason("dump_setup", False, PROBE_PREFIX, {"engine": "redis"}, {"backup-setup": "0.4"})
    assert "helpers before 0.38" in got
    # новый helper причину пишет сам; если все же пусто - без совета обновиться
    got = fill_dump_reason("dump_setup", False, PROBE_PREFIX, {"engine": "mysql"}, {"backup-setup": "0.38"})
    assert got == PROBE_PREFIX + " no reason came from backup-setup 0.38 for mysql"
    # агент не прислал версии
    got = fill_dump_reason("dump_setup", False, PROBE_PREFIX, {"engine": "mysql"}, None)
    assert "backup-setup unknown for mysql: helpers before 0.38" in got


def test_empty_output_gets_a_reason_too():
    got = fill_dump_reason("dump_remove", False, "  ", {"engine": "ch", "container": "k8s.default.sts.ch"},
                           {"backup-setup": "0.36"})
    assert got.startswith("no reason came from backup-setup 0.36 for ch@k8s.default.sts.ch: helpers before")
    assert fill_dump_reason("dump_setup", False, "", None, {"backup-setup": "0.38"}) == \
        "no reason came from backup-setup 0.38 for unknown"


async def test_agent_result_with_empty_reason_is_filled(client, auth_headers):
    """весь путь: кнопка -> агент -> пустая причина от helper'а 0.37 -> в статусе команды причина"""
    r = await client.post("/api/servers", json={"name": "db-a"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}
    rep = {"agent_version": "2.40", "mem_used": 1, "mem_total": 2, "disks": [{"mount": "/", "used": 1, "total": 2}],
           "setup_versions": {"backup-setup": "0.37"}}
    assert (await client.post("/api/agent/report", json=rep, headers=ah)).status_code == 200
    r = await client.post(f"/api/servers/{sid}/backup/command",
                          json={"action": "dump_setup", "engine": "mysql", "container": ""}, headers=auth_headers)
    cid = r.json()["id"]
    cmds = (await client.post("/api/agent/report", json=rep, headers=ah)).json()["backup_commands"]
    assert [c["engine"] for c in cmds] == ["mysql"]
    # агент обрезает хвостовой пробел: так ответ и пришел с ноды
    r = await client.post("/api/agent/backup-result", json={"id": cid, "ok": False, "output": PROBE_PREFIX},
                          headers=ah)
    assert r.status_code == 200
    st = (await client.get(f"/api/servers/{sid}/backup/command/{cid}", headers=auth_headers)).json()
    assert st["status"] == "error" and st["ok"] is False
    assert st["result"].startswith(PROBE_PREFIX + " no reason came from backup-setup 0.37 for mysql: ")
    assert "/etc/mysql/debian.cnf" in st["result"]


def test_frontend_knows_every_phrase():
    """фразы helper'а и панели, которые переводит helperText.ts: разойдутся - вместо понятного текста
    в интерфейсе покажется английский"""
    for phrase in ("no reason came from backup-setup", "helpers before", "lose the error of the check, update it",
                   "MySQL on the host", "MySQL did not let the helper in:", "root over the socket",
                   "no mysqldump or mariadb-dump on the host", "exited with code", "and printed nothing",
                   "docker does not know the container", "no neo4j-admin on the host",
                   "neither sqlite3 nor python3 with sqlite3 on the host"):
        assert phrase in HELPER_TEXT, phrase
    assert "grafana\\.db not found on the host or in volumes" in HELPER_TEXT
    for phrase in ("MySQL did not let the helper in:", "root over the socket", "no mysqldump or mariadb-dump on the host",
                   "exited with code $prc and printed nothing", "docker does not know the container",
                   "no neo4j-admin on the host", "grafana.db not found on the host or in volumes",
                   "neither sqlite3 nor python3 with sqlite3 on the host"):
        assert phrase in SRC, phrase
    assert f'REASON_SINCE = "{dump_reason.REASON_SINCE}"' in (ROOT / "backend" / "app" / "dump_reason.py").read_text(
        encoding="utf-8")
    # прочерка вместо причины больше нет
    assert "|| '\u2014'" not in HELPER_TEXT.split("the dump probe failed, dumps were NOT enabled")[1][:600]


# ---------------------------------------------------------------- helper на ноде

def test_helper_version_and_wiring():
    assert "KERVAX_SETUP_VERSION=0.40" in SRC and "\r" not in SRC
    probe = SRC[SRC.index("dump_probe() {"):SRC.index("# Compression of the dumps (0.30)")]
    # проверки дампа больше не выбрасывают свою ошибку
    for gone in ("else mysqldump --no-data --all-databases >/dev/null 2>&1; fi",
                 "redis-cli ping >/dev/null 2>&1", "rabbitmqctl list_vhosts >/dev/null 2>&1",
                 "sudo -u postgres pg_dumpall --schema-only >/dev/null 2>&1",
                 "grafana) [ -n \"$(grafana_db_path)\" ] && grafana_dumper >/dev/null ;;"):
        assert gone not in probe, gone
    assert "else my_host_probe; fi" in probe
    setup = SRC[SRC.index("cmd_dump_setup() {"):SRC.index("cmd_dump_remove() {")]
    # проверка идет до того, как что-то записано на ноде
    assert setup.index('dump_probe "$engine" "$container"') < setup.index("install -d -m 0700")
    assert setup.index('dump_probe "$engine" "$container"') < setup.index("write_run_dumps")
    assert 'printf \'else MYBIN=%q; MYDEF=%q\\n\' "${MY_BIN:-mysqldump}" "$MY_DEF"' in setup


def _helper_body() -> str:
    body = re.search(r"<<'HELPER_EOF'\n(.*?)\nHELPER_EOF\n", SRC, re.S).group(1)
    # без диспетчера команд в конце: функции подгружаются, ничего не запускается
    return body[:body.index('\ncase "${1:-}" in\n')]


# Окружение вместо ноды: каталоги во временной папке, systemd и chown - заглушки. Путь берем у
# самого bash (pwd), на Windows это /c/... без двоеточия, иначе helper не примет каталог дампов
STUBS = r'''
set +e
T="$(cd "$1" && pwd)"; export T
DUMPS_D="$T/dumps.d"; mkdir -p "$DUMPS_D"
export KV_DUMP_DIR="$T/backup" KV_DUMP_MINFREE=0
MY_CNFS=("$T/debian.cnf" "$T/my.cnf")
printf '[client]\nuser=maint\n' > "$T/debian.cnf"
write_run_dumps() { :; }
refresh_config() { :; }
find_script() { return 1; }
systemctl() { return 1; }
logger() { :; }
chown() { :; }
'''


def _run(tmp_path: Path, case: str) -> str:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("нет bash")
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        # файлом и с LF: на Windows длинная строка с кавычками доходит до bash испорченной
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(_helper_body() + "\n" + STUBS + case)
        out = subprocess.run([bash, path, tmp_path.as_posix()], capture_output=True, text=True, timeout=120,
                             encoding="utf-8")
    finally:
        os.unlink(path)
    return out.stdout + out.stderr


def test_helper_host_mysql_logs_in_through_debian_cnf(tmp_path):
    """найденный случай: root без пароля не пускают, а debian.cnf пускает - дамп включается и снимается"""
    got = _run(tmp_path, r'''
mysqldump() {
  if [ "${1:-}" = "--defaults-file=$T/debian.cnf" ]; then echo "-- MySQL dump of db-a"; return 0; fi
  echo "mysqldump: Got error: 1045: Access denied for user 'root'@'localhost' (using password: NO) when trying to connect" >&2
  return 2
}
export -f mysqldump
cmd_dump_setup mysql ""; echo "RC=$?"
grep -c "^else MYBIN=mysqldump; MYDEF=$T/debian.cnf\$" "$DUMPS_D/mysql.sh"
bash -n "$DUMPS_D/mysql.sh" && echo SYNTAX=ok
bash "$DUMPS_D/mysql.sh"; echo "DUMP_RC=$?"
for f in "$T"/backup/mysql/mysqldump-all-*.sql.gz; do gzip -dc "$f"; done
''')
    assert "OK mysql dumps enabled -> " in got and "RC=0" in got, got
    assert "\n1\n" in got and "SYNTAX=ok" in got and "DUMP_RC=0" in got, got
    assert "-- MySQL dump of db-a" in got, got


def test_helper_says_why_mysql_refused(tmp_path):
    """никакой вход не подошел: в причине каждый способ и его ошибка, на ноде ничего не осталось"""
    got = _run(tmp_path, r'''
mysqldump() {
  if [ "${1:-}" = "--defaults-file=$T/debian.cnf" ]; then
    echo "mysqldump: Got error: 1045: Access denied for user 'maint'@'localhost' (using password: YES) when trying to connect" >&2
  else
    echo "mysqldump: Got error: 1045: Access denied for user 'root'@'localhost' (using password: NO) when trying to connect" >&2
  fi
  return 2
}
cmd_dump_setup mysql ""; echo "RC=$?"
[ -e "$T/backup" ] && echo LEFT=dir
[ -e "$DUMPS_D/mysql.sh" ] && echo LEFT=script
''')
    line = next(x for x in got.splitlines() if x.startswith(PROBE_PREFIX))
    assert line.startswith(PROBE_PREFIX + " MySQL did not let the helper in: ")
    assert "/debian.cnf - mysqldump: Got error: 1045: Access denied for user 'maint'@'localhost' (using password: YES)" in line
    assert "when trying to connect; root over the socket - mysqldump: Got error: 1045: Access denied for user 'root'@'localhost' (using password: NO)" in line
    assert "RC=2" in got and "LEFT=" not in got, got


def test_helper_never_gives_an_empty_reason(tmp_path):
    """проверка упала молча - причина все равно есть: код выхода и что за проверка"""
    got = _run(tmp_path, r'''
dump_probe() { return 3; }
cmd_dump_setup pg ""; echo "RC=$?"
cmd_dump_setup pg db-a; echo "RC=$?"
''')
    assert PROBE_PREFIX + " the check of pg exited with code 3 and printed nothing" in got, got
    assert PROBE_PREFIX + " the check of pg@db-a exited with code 3 and printed nothing" in got, got
    assert got.count("RC=2") == 2, got


def test_helper_reports_redis_answer_and_missing_client(tmp_path):
    got = _run(tmp_path, r'''
redis-cli() { echo "NOAUTH Authentication required."; return 0; }
cmd_dump_setup redis ""; echo "RC=$?"
unset -f redis-cli
if command -v mysqldump >/dev/null 2>&1 || command -v mariadb-dump >/dev/null 2>&1; then echo NOCLIENT=skip
else cmd_dump_setup mysql ""; echo "RC=$?"; fi
''')
    # раньше redis-cli ping с выходом 0 считался ответом, хотя база просила пароль
    assert PROBE_PREFIX + " redis did not answer PONG: NOAUTH Authentication required." in got, got
    if "NOCLIENT=skip" not in got:
        assert PROBE_PREFIX + " no mysqldump or mariadb-dump on the host" in got, got
