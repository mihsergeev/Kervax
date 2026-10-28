"""Счетчики ошибок SMART в helper'е diskhealth-setup: рост за сутки и значения, которые не счетчик.

Повод (10.2026): у Kingston SA400 в raw атрибута 199 лежит не число ошибок, а что-то упакованное -
720918, 786455, 851990 вперемешку, шаг 65536. Helper считал рост как последнее минус первое за
сутки, выходило "+65533 ошибок CRC", через полчаса значение падало - "диски снова в порядке", и так
по кругу несколько раз в день. Ряд ниже - настоящие значения с такого диска, раз в полчаса."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from app.collector import disk_health_problems

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "agent" / "diskhealth-setup.sh").read_text(encoding="utf-8")

SA400_199 = [786459, 786460, 786460, 851996, 851996, 720923, 786460, 786459, 786457, 851994, 786458,
             851994, 786457, 851993, 720922, 851994, 786457, 851993, 720921, 786457, 851993, 786457,
             786455, 720919, 786456, 851991, 786455, 720918, 851990, 851991, 786455, 720918, 851990]


def _body() -> str:
    body = re.search(r"<<'HELPER_EOF'\n(.*?)\nHELPER_EOF\n", SRC, re.S).group(1)
    body = re.sub(r"(?m)^OUT=.*$", 'OUT="$PWD/disk-health.json"', body)
    return re.sub(r"(?m)^STATE=.*$", 'STATE="$PWD/state"', body)


def _smart(attrs: dict[int, tuple[str, int]]) -> str:
    lines = ["SMART overall-health self-assessment test result: PASSED",
             "ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE"]
    for i, (name, raw) in sorted(attrs.items()):
        lines.append(f"{i:3d} {name:<23} 0x0032   100   100   000    Old_age   Always       -       {raw}")
    return "\n".join(lines) + "\n"


def _run(hist: list[tuple[int, ...]], now_attrs: dict[int, tuple[str, int]]) -> dict:
    """Тело helper'а на одном SATA-диске: история счетчиков (realloc, pending, uncorr, media, crc)
    раз в полчаса до текущего момента, сейчас smartctl отдает now_attrs."""
    bash = shutil.which("bash")
    if not bash or not shutil.which("awk"):
        pytest.skip("нет bash или awk")
    d = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(d, "bin"))
        os.makedirs(os.path.join(d, "state"))
        stubs = {
            "lsblk": 'echo \'NAME="sda" TYPE="disk" SIZE="480103981056" TRAN="sata" ROTA="0" RM="0" '
                     'SERIAL="SER-A" MODEL="SSD-A"\'\n',
            "smartctl": 'cat "$PWD/smart.txt"\n',
            "systemd-detect-virt": 'case "$*" in *-c*) exit 1 ;; esac\necho none\nexit 1\n',
            "journalctl": "exit 0\n",
        }
        for n, text in stubs.items():
            p = os.path.join(d, "bin", n)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write("#!/usr/bin/env bash\n" + text)
            os.chmod(p, 0o755)
        now = int(time.time())
        rows = [f"{now - (len(hist) - i) * 1800}|SER-A|" + "|".join("" if v is None else str(v) for v in r)
                for i, r in enumerate(hist)]
        files = {"smart.txt": _smart(now_attrs), "state/hist-sda": "".join(r + "\n" for r in rows),
                 "helper.sh": _body()}
        for n, text in files.items():
            with open(os.path.join(d, n), "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        out = subprocess.run([bash, "-c", 'export PATH="$PWD/bin:$PATH"; bash helper.sh'], cwd=d,
                             capture_output=True, text=True, timeout=120, encoding="utf-8")
        assert out.returncode == 0, out.stderr
        with open(os.path.join(d, "disk-health.json"), encoding="utf-8") as f:
            return json.load(f)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.parametrize("raw", [851990, 786454])  # сверху ряда был алерт +65531, снизу - отбой
def test_value_that_jumps_both_ways_is_not_a_crc_counter(raw):
    rep = _run([(None, None, None, None, v) for v in SA400_199],
               {9: ("Power_On_Hours", 9649), 199: ("SATA_CRC_Error_Count", raw)})
    disk = rep["disks"][0]
    assert disk["crc"] is None, "упакованное значение - не число ошибок, в плитку не идет"
    assert disk["grow"] == {}
    assert not [p for p in disk_health_problems(rep) if "CRC" in p[2]]


def test_real_crc_growth_is_still_reported():
    rep = _run([(0, None, None, None, 132)] * 10 + [(0, None, None, None, 132 + i) for i in range(1, 6)],
               {5: ("Reallocated_Sector_Ct", 0), 199: ("UDMA_CRC_Error_Count", 140)})
    disk = rep["disks"][0]
    assert disk["crc"] == 140
    assert disk["grow"] == {"crc": 8}
    assert [p[0] for p in disk_health_problems(rep) if "CRC" in p[2]] == [1]


def test_pending_sectors_may_go_down_and_still_grow():
    """197 законно уменьшается, когда сектор переписали или переназначили: значение остается,
    рост считается от минимума за сутки."""
    rep = _run([(0, 8, None, None, 0)] * 4 + [(0, 4, None, None, 0)] * 4,
               {5: ("Reallocated_Sector_Ct", 0), 197: ("Current_Pending_Sector", 6),
                199: ("UDMA_CRC_Error_Count", 0)})
    disk = rep["disks"][0]
    assert disk["pending"] == 6 and disk["crc"] == 0
    assert disk["grow"] == {"pending": 2}
    assert [p[0] for p in disk_health_problems(rep) if "pending" in p[1] or "выросли" in p[2]] == [2]


def test_only_the_jumping_counter_is_dropped():
    """Скачет только 199, рост переназначенных секторов на том же диске - настоящий."""
    hist = [(2, None, None, None, v) for v in SA400_199[:-4]] + [(3, None, None, None, v) for v in SA400_199[-4:]]
    rep = _run(hist, {5: ("Reallocated_Sector_Ct", 4), 199: ("SATA_CRC_Error_Count", 786454)})
    disk = rep["disks"][0]
    assert disk["crc"] is None and disk["realloc"] == 4
    assert disk["grow"] == {"realloc": 2}
