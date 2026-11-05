"""Окно бэкапа при переходе через полночь и расхождении меток restic и systemd."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"
CLI = FRONT / "node_modules" / "rolldown" / "bin" / "cli.mjs"


@pytest.mark.parametrize("tz", ["Europe/Moscow", "America/New_York"])
def test_backup_window(tmp_path, tz):
    node = shutil.which("node")
    if not node or not CLI.exists():
        pytest.skip("нет node или node_modules фронта (npm ci в frontend)")
    out = tmp_path / "check.mjs"
    subprocess.run(
        [node, str(CLI), "tests/backupWindow.check.ts", "--format", "esm", "--platform", "node",
         "--banner", "globalThis.localStorage={getItem(){return null},setItem(){},removeItem(){}};",
         "-o", str(out)],
        cwd=FRONT, check=True, capture_output=True,
    )
    got = json.loads(subprocess.run(
        [node, str(out)], check=True, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "TZ": tz},
    ).stdout)
    assert got["just_started"] is None
    assert got["past_midnight"] is None
    assert "23:30" in got["past_deadline"]
    assert "идёт с 02:00" in got["early_start_past_deadline"]
    assert got["early_start_in_window"] is None
    assert got["finished_in_window_with_skew"] is None
    assert got["finished_in_window_with_old_late_metric"] is None
    assert "закончился в 08:00" in got["finished_past_deadline"]
    assert "закончился в 08:05" in got["finished_past_deadline_with_old_early_metric"]
    assert got["finished_after_midnight_in_window"] is None
    assert "закончился в 08:05" in got["finished_after_midnight_past_deadline"]
