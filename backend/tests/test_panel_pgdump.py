"""The panel dump check must accept a complete dump whatever the length of its listing.

With "printf | grep -q" under pipefail, grep left at the first match and the writer's SIGPIPE
failed the check: a complete dump with a long table of contents was rejected night after night."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "ops/panel-pgdump.sh"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Bash diagnostic")

TABLES = ("alembic_version", "servers", "checks", "users", "app_settings")


def _run(tmp_path, tables, filler_lines):
    panel = tmp_path / "panel"
    panel.mkdir()
    (panel / ".env").write_text("KERVAX_DB_USER=fixture_admin\nKERVAX_DB_NAME=fixture_database\n")
    out = tmp_path / "dumps"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # pg_restore --list: the needed entries first, then a long tail, as in a real listing
    toc = "".join(f"{n}; 0 1 TABLE DATA public {t} fixture_admin\n" for n, t in enumerate(tables))
    toc += "".join(f"{n}; 0 1 INDEX public fixture_index_{n} fixture_admin\n" for n in range(filler_lines))
    (tmp_path / "toc.txt").write_text(toc)
    docker = bindir / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if 'pg_dump' in args: sys.stdout.write('fixture dump\\n'); sys.exit(0)\n"
        "if 'pg_restore' in args:\n"
        "    sys.stdin.read()\n"
        "    sys.stdout.write(open(os.environ['FIXTURE_TOC']).read()); sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "KERVAX_DIR": str(panel),
        "KERVAX_DUMP_DIR": str(out),
        "KERVAX_DUMP_PUSH_URL": "",
        "KERVAX_DUMP_RESTIC_ENV": "",
        "FIXTURE_TOC": str(tmp_path / "toc.txt"),
    }
    run = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return run, out


@pytest.mark.parametrize("attempt", range(5))
def test_long_listing_of_a_complete_dump_is_accepted(tmp_path, attempt):
    run, out = _run(tmp_path, TABLES, filler_lines=20000)
    assert run.returncode == 0, run.stderr
    assert (out / "last.status").read_text().startswith("ok ")
    assert len(list(out.glob("kervax-*.dump"))) == 1


def test_missing_table_data_is_still_rejected(tmp_path):
    run, out = _run(tmp_path, [t for t in TABLES if t != "users"], filler_lines=20000)
    assert run.returncode == 1
    assert "no data of table users in the dump" in run.stderr
    assert not list(out.glob("kervax-*.dump*"))
