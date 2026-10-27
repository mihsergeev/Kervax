"""Список репозиториев сервера бэкапов и плитка раздела "Бэкапы" считают беды одной функцией
(frontend/src/bsrvRepos.ts, bsrvRepoProblems). 27.10 они разошлись: опоздавший бэкап, у
которого шел новый прогон, плитка и бейдж считали, а список ставил в "В порядке" с пометкой
"идет бэкап". Тест собирает frontend/tests/bsrvRepos.check.ts (rolldown из node_modules
фронта) и сверяет: что считает плитка, то в списке в "Требуют внимания", и наоборот."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "frontend"
CLI = FRONT / "node_modules" / "rolldown" / "bin" / "cli.mjs"


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    node = shutil.which("node")
    if not node or not CLI.exists():
        pytest.skip("нет node или node_modules фронта (npm ci в frontend)")
    out = tmp_path_factory.mktemp("bsrv") / "check.mjs"
    # i18n читает язык из localStorage при загрузке модуля, в node его нет
    subprocess.run(
        [node, str(CLI), "tests/bsrvRepos.check.ts", "--format", "esm", "--platform", "node",
         "--banner", "globalThis.localStorage={getItem(){return null},setItem(){},removeItem(){}};",
         "-o", str(out)],
        cwd=FRONT, check=True, capture_output=True,
    )
    run = subprocess.run([node, str(out)], check=True, capture_output=True)
    return json.loads(run.stdout.decode("utf-8"))


def test_late_with_running_backup_is_shown(result):
    """Опоздал и прямо сейчас идет новый прогон: снапшота нет, это беда и в плитке, и в списке."""
    r = result["late_running"]
    assert r["tiles"] and "db1" in r["tiles"][0]
    assert r["bad"] == ["db1"]
    assert r["tones"] == {"db1": "bad", "ok1": "ok"}
    assert r["states"]["db1"] == "stale"


def test_running_backup_alone_is_fine(result):
    """Свежий лок без опоздания - просто идет бэкап: нигде не беда."""
    r = result["running"]
    assert r["tiles"] == [] and r["bad"] == []
    assert r["tones"] == {"db1": "ok"} and r["states"] == {"db1": "running"}


def test_list_and_tile_agree(result):
    """По каждому случаю: плитка считает беду репозиториев тогда и только тогда, когда список
    ставит репозиторий в "Требуют внимания". Заглушенный алерт и приглушенный репозиторий - нигде."""
    for name, r in result.items():
        assert bool(r["tiles"]) == bool(r["bad"]), name
        assert sorted(r["bad"]) == sorted(k for k, v in r["tones"].items() if v == "bad"), name
    assert result["late_alert_muted"]["tones"] == {"db1": "mute"}
    assert result["repo_muted"]["tones"] == {"db1": "mute"}
    assert result["lock_long"]["bad"] == ["db1"] and result["prune"]["bad"] == ["db1"]
