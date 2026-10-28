"""Повтор restic backup при сетевом обрыве (backup-setup 0.47).

Найденный случай: ночной бэкап ноды упал с "request timeout", в логе rest-server три "read: connection
timed out" - соединение до сервера бэкапов замерло по пути, сам сервер здоров, следующий запуск прошел.
Алерт краснел и сутки пропадали. Теперь сетевой сбой (код 1 и сетевая ошибка в выводе) повторяется в том
же запуске: до 3 попыток через 5 минут, метрики пишутся один раз по итогу. Фатальные коды (нет репо, лок,
пароль) выходят сразу. Раннер панели содержит цикл, старые скрипты получают его на refresh один раз."""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "agent" / "backup-setup.sh").read_text(encoding="utf-8")


def _helper_body() -> str:
    body = re.search(r"<<'HELPER_EOF'\n(.*?)\nHELPER_EOF\n", SRC, re.S).group(1)
    return body[:body.index('\ncase "${1:-}" in\n')]


# restic-заглушка: на каждый вызов backup берет следующую строку $T/plan ("код|текст"), считает вызовы
# в $T/calls; sleep только записывается (пауза между попытками - 5 минут)
_STUBS = r'''
SCRIPT_CANDS=("$T/runner"); PROV_SCRIPT="$T/runner"
cat > "$T/restic" <<'EOF'
#!/bin/bash
case "$1" in --help) echo "  --retry-lock duration"; exit 0 ;; esac
T="$(dirname "$0")"
n=$(( $(cat "$T/calls" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$T/calls"
echo "args: $*"
line="$(sed -n "${n}p" "$T/plan")"; [ -n "$line" ] || line="$(tail -n1 "$T/plan")"
echo "${line#*|}" >&2
exit "${line%%|*}"
EOF
chmod +x "$T/restic"
sleep() { echo "$*" >> "$T/slept"; }
export T; export -f sleep
for f in "$T"/*; do [ -f "$f" ] && sed -i "s#@T@#$T#g" "$f"; done
'''


def _run(tmp_path: Path, files: dict[str, str], case: str) -> str:
    bash = shutil.which("bash")
    if not bash or not shutil.which("awk"):
        pytest.skip("нет bash или awk")
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8", newline="\n")
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(_helper_body() + '\nset +e\nT="$(cd "$1" && pwd)"\n' + _STUBS + case)
        out = subprocess.run([bash, path, tmp_path.as_posix()], capture_output=True, text=True, timeout=60,
                             encoding="utf-8")
    finally:
        os.unlink(path)
    return out.stdout + out.stderr


# сам блок: BACKUP_CMD как в раннере, RC и число попыток наружу
_BLOCK = r'''
BACKUP_CMD="$T/restic backup / --exclude-caches"; KV_RETRY_LOCK=" --retry-lock 2h"
net_retry_block > "$T/blk"; source "$T/blk"
echo "RC=$RC calls=$(cat "$T/calls") slept=$(wc -l < "$T/slept" 2>/dev/null || echo 0)"
'''


def _block(tmp_path, plan: str) -> str:
    return _run(tmp_path, {"plan": plan}, _BLOCK)


def test_network_failure_is_retried_until_it_passes(tmp_path):
    out = _block(tmp_path, "1|Fatal: unable to save snapshot: request timeout\n"
                           "1|Fatal: unable to save snapshot: read tcp: connection timed out\n0|snapshot saved\n")
    assert "RC=0 calls=3 slept=2" in out
    assert "restic backup: attempt 3 of 3" in out
    assert (tmp_path / "slept").read_text().split() == ["300", "300"]
    # флаг лока на месте, сразу после backup
    assert "args: backup --retry-lock 2h / --exclude-caches" in out


def test_three_network_failures_end_with_the_last_code(tmp_path):
    out = _block(tmp_path, "1|Fatal: unable to save snapshot: 502 Bad Gateway\n")
    assert "RC=1 calls=3 slept=2" in out


@pytest.mark.parametrize("plan", [
    "12|Fatal: wrong password or no key found\n",
    "10|Fatal: repository does not exist: unable to open config file\n",
    "11|Fatal: unable to create lock in backend: repository is already locked\n",
    "3|Warning: at least one source file could not be read\n",
    "1|Fatal: unable to save snapshot: permission denied\n",
])
def test_fatal_or_not_network_is_not_retried(tmp_path, plan):
    out = _block(tmp_path, plan + "0|snapshot saved\n")
    code = plan.split("|")[0]
    assert f"RC={code} calls=1 slept=0" in out


def test_panel_runner_has_the_loop(tmp_path):
    out = _run(tmp_path, {}, r'''
RESTIC_BIN="$T/restic"; PROV_CACERT="$T/none.pem"; PROV_ENV="$T/env"; PROV_HOME="$T"
PROV_METRICS_DIR="$T/m"; PROV_METRICS_FILE=rk.prom; PROV_USER=nobody
write_runner exclude /proc
bash -n "$T/runner" && echo SYNTAX-OK
''')
    assert "SYNTAX-OK" in out
    runner = (tmp_path / "runner").read_text()
    assert runner.count("# kervax-net-retry: 1") == 1
    # длительность считается по всем попыткам: ts_start до цикла, dur после, метрики один раз
    i_start, i_loop = runner.index("ts_start=$(date +%s); set +e"), runner.index("while :; do")
    i_dur, i_metrics = runner.index("set -e; dur="), runner.index("write_metrics 1 0")
    assert i_start < i_loop < i_dur < i_metrics
    assert runner.count('eval "${BACKUP_CMD/ backup / backup$KV_RETRY_LOCK }"') == 1


# ansible-роль до повтора: eval и RC=$? на отдельных строках
ANSIBLE_OLD = r'''#!/usr/bin/env bash
set -euo pipefail
BACKUP_CMD="@T@/restic backup /"
# kervax-retry-lock: 1
KV_RETRY_LOCK=""
case "$(${BACKUP_CMD%% *} --help 2>/dev/null)" in *--retry-lock*) KV_RETRY_LOCK=" --retry-lock 2h" ;; esac

ts_start=$(date +%s)
set +e
eval "${BACKUP_CMD/ backup / backup$KV_RETRY_LOCK }"
RC=$?
set -e
ts_end=$(date +%s)
duration=$((ts_end - ts_start))
echo "FINAL RC=$RC"
exit $RC
'''

# раннер старого helper'а: все в одну строку
PANEL_OLD = r'''#!/usr/bin/env bash
set -euo pipefail
BACKUP_CMD="@T@/restic backup / --exclude-caches"
# kervax-retry-lock: 1
KV_RETRY_LOCK=""
case "$(${BACKUP_CMD%% *} --help 2>/dev/null)" in *--retry-lock*) KV_RETRY_LOCK=" --retry-lock 2h" ;; esac
ts_start=$(date +%s); set +e; eval "${BACKUP_CMD/ backup / backup$KV_RETRY_LOCK }"; RC=$?; set -e; dur=$(( $(date +%s) - ts_start ))
echo "FINAL RC=$RC dur=$dur"
exit $RC
'''

_PATCH_TWICE = r'''
net_retry_maybe; cp "$T/runner" "$T/first"
net_retry_maybe; cmp -s "$T/first" "$T/runner" && echo SAME
ls "$T"/runner.kervax-pre-netretry.bak 2>/dev/null | wc -l | sed 's/^/BAK=/'
bash "$T/runner"; echo "EXIT=$?"
'''


@pytest.mark.parametrize("old", [ANSIBLE_OLD, PANEL_OLD], ids=["ansible", "panel"])
def test_old_script_gets_the_loop_once(tmp_path, old):
    out = _run(tmp_path, {"runner": old, "plan": "1|Fatal: request timeout\n0|snapshot saved\n"}, _PATCH_TWICE)
    assert "SAME" in out and "BAK=1" in out
    text = (tmp_path / "runner").read_text()
    assert text.count("# kervax-net-retry: 1") == 1 and "RC=$?" not in text
    # что стояло на строке до и после eval, осталось
    assert "set +e" in text and "set -e" in text and ("dur=$((" in text or "duration=" in text)
    assert "FINAL RC=0" in out and "EXIT=0" in out
    assert (tmp_path / "calls").read_text().strip() == "2"
    bak = (tmp_path / "runner.kervax-pre-netretry.bak").read_text()
    assert "kervax-net-retry" not in bak and "RC=$?" in bak


def test_hand_made_retry_is_left_alone(tmp_path):
    # блок, вставленный руками (по образцу роли): пометки нет, есть "попытка"
    hand = ANSIBLE_OLD.replace(
        'eval "${BACKUP_CMD/ backup / backup$KV_RETRY_LOCK }"\nRC=$?\n',
        'BK_TRY=1\nwhile :; do\n  echo "restic backup: попытка $BK_TRY"\n'
        '  eval "${BACKUP_CMD/ backup / backup$KV_RETRY_LOCK }" 2>&1 | tee /dev/null\n'
        '  RC=${PIPESTATUS[0]}\n  break\ndone\n')
    out = _run(tmp_path, {"runner": hand, "plan": "0|ok\n"}, 'net_retry_maybe; echo DONE\n')
    assert "DONE" in out
    assert "kervax-net-retry" not in (tmp_path / "runner").read_text()
    assert not (tmp_path / "runner.kervax-pre-netretry.bak").exists()


def test_unknown_layout_is_left_alone(tmp_path):
    # eval без RC=$? рядом - не наш вид, не трогаем
    odd = ANSIBLE_OLD.replace("RC=$?\n", "echo done\nRC=$?\n")
    out = _run(tmp_path, {"runner": odd, "plan": "0|ok\n"}, 'net_retry_maybe; echo DONE\n')
    assert "DONE" in out
    assert (tmp_path / "runner").read_text().count("kervax-net-retry") == 0
    assert not (tmp_path / "runner.kervax-pre-netretry.bak").exists()
