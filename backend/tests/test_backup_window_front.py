"""Уведомление "бэкап идёт и не уложился в окно" для прогона, начатого до полуночи.

Ночной бэкап стартует в 23:30, окно до 8:00. Проверка смотрела только на час "сейчас": в 23:35
час 23 >= 8, и только что начатый прогон уже "не уложился в окно (до 8:00)". Дедлайн теперь -
ближайшие 8:00 после старта.

Тест собирает frontend/tests/backupWindow.check.ts (rolldown из node_modules фронта)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"
CLI = FRONT / "node_modules" / "rolldown" / "bin" / "cli.mjs"


def test_running_backup_window_crosses_midnight(tmp_path):
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
    got = json.loads(subprocess.run([node, str(out)], check=True, capture_output=True, text=True).stdout)
    assert got["just_started"] is None
    assert got["past_midnight"] is None
    assert "23:30" in got["past_deadline"]
    assert got["early_start_past_deadline"]
    assert got["early_start_in_window"] is None
