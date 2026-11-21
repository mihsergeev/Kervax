"""Тег latest образов двигает только старшая стабильная версия (ops/release-latest.sh)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/release-latest.sh"
TAGS = ["v1.8.149", "v1.8.150", "v1.8.151", "v1.9.9", "v1.10.0", "v2.0.0-rc1", "v1.10.0-beta", "agent-2.56"]

pytestmark = pytest.mark.skipif(shutil.which("sh") is None, reason="нужен POSIX sh")


def _latest(tag, tags):
    """Скрипт получает вывод git ls-remote --tags --refs, как в release.yml."""
    lines = "".join(f"{i:040x}\trefs/tags/{t}\n" for i, t in enumerate(tags, 1))
    return subprocess.run(["sh", str(SCRIPT), tag], input=lines, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("tag, tags, want", [
    ("v1.8.151", TAGS[:3], "true"),       # старшая из выпущенных
    ("v1.8.150", TAGS[:3], "false"),      # бэкпорт после v1.8.151
    ("v1.10.0", TAGS, "true"),            # числа, а не строки: 1.10 > 1.9
    ("v1.9.9", TAGS, "false"),
    ("v2.0.0-rc1", TAGS, "false"),        # пре-релиз не latest, даже старший
    ("v1.10.0-beta", TAGS, "false"),
    ("v1.8.152", TAGS[:3] + ["v1.8.152"], "true"),
    # имя - все после первого refs/tags/: это тег vx/refs/tags/v9.9.9, а не v9.9.9
    ("v1.8.151", TAGS[:3] + ["vx/refs/tags/v9.9.9"], "true"),
])
def test_only_highest_stable_tag_moves_latest(tag, tags, want):
    res = _latest(tag, tags)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == want


def test_tag_missing_from_the_list_stops_the_release():
    for tags in ([], ["v1.8.150"], ["vx/refs/tags/v1.8.151"]):
        res = _latest("v1.8.151", tags)
        assert res.returncode != 0 and res.stdout.strip() == ""


def test_release_workflow_tags_latest_only_through_the_check():
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    # решение в meta и повтор прямо перед push :latest
    assert release.count("| sh ops/release-latest.sh \"$GITHUB_REF_NAME\"") == 2
    assert release.count(":latest") == 2  # docker tag и docker push под if, в сборке latest нет
    assert 'if [ "$LATEST" = true ]; then' in release
    assert "group: release" in release and "cancel-in-progress: false" in release
