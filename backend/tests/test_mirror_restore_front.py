import json
from pathlib import Path
import shutil
import subprocess

import pytest


def test_restore_front_never_marks_unknown_or_stale_ok(tmp_path):
    front = Path(__file__).resolve().parents[2] / "frontend"
    cli = front / "node_modules/rolldown/bin/cli.mjs"
    node = shutil.which("node")
    if not node or not cli.is_file():
        pytest.skip("needs frontend npm ci")
    output = tmp_path / "restore.mjs"
    subprocess.run([node, str(cli), "tests/mirrorRestore.check.ts", "--format", "esm", "--platform", "node",
                    "--banner", "globalThis.localStorage={getItem(){return null},setItem(){},removeItem(){}};",
                    "-o", str(output)], cwd=front, check=True, capture_output=True)
    run = subprocess.run([node, str(output)], check=True, capture_output=True)
    cases = json.loads(run.stdout.decode("utf-8"))
    assert cases["ok"]["tone"] == cases["ok"]["copy"] == "ok"
    assert "5 файлов" in cases["ok"]["text"]
    assert cases["failed"]["tone"] == cases["failed"]["copy"] == "bad"
    for name in ("unknown", "pending", "password", "empty", "no_snapshot", "stale"):
        assert cases[name]["tone"] == cases[name]["copy"] == "warn", name
    assert "Нужен пароль" in cases["password"]["text"]
