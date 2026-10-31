"""A damaged helper role archive must never produce a successful restore check."""

import gzip
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "ops/panel-restore-test.sh"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux Bash diagnostic")


@pytest.fixture
def restore_check(tmp_path):
    panel = tmp_path / "panel"
    panel.mkdir()
    (panel / ".env").write_text("KERVAX_DB_USER=fixture_admin\nKERVAX_DB_NAME=fixture_database\n")
    dump = tmp_path / "dump"
    dump.mkdir()
    (dump / "fixture_database.sql.gz").write_bytes(gzip.compress(b"-- database fixture\n"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "with open(os.environ['DOCKER_FIXTURE_CALLS'], 'a') as stream: stream.write(repr(args)+'\\n')\n"
        "if args[0] == 'inspect': print('fixture/postgres'); sys.exit(0)\n"
        "if args[0] in ('run','rm'): sys.exit(0)\n"
        "assert args.pop(0) == 'exec'\n"
        "interactive = args[0] == '-i'\n"
        "if interactive: args.pop(0)\n"
        "args.pop(0)\n"
        "if args[0] == 'pg_isready': sys.exit(0)\n"
        "assert args[0] == 'psql'\n"
        "if interactive and 'BROKEN_SQL' in sys.stdin.read():\n"
        "    print('fixture-role-diagnostic', file=sys.stderr)\n"
        "    sys.exit(3 if 'ON_ERROR_STOP=1' in args else 0)\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    sleep = bindir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)
    calls = tmp_path / "docker-calls"
    env = {**os.environ, "KERVAX_DIR": str(panel), "DOCKER_FIXTURE_CALLS": str(calls),
           "PATH": str(bindir) + os.pathsep + os.environ["PATH"]}
    return dump, env, calls


def test_corrupt_global_gzip_is_rejected_before_starting_a_container(restore_check):
    dump, env, calls = restore_check
    (dump / "globals.sql.gz").write_bytes(b"not gzip data")

    result = subprocess.run(["bash", str(SCRIPT), str(dump)], env=env,
                            capture_output=True, text=True, timeout=15)

    assert result.returncode != 0
    assert "corrupt gzip archive" in result.stdout
    assert not calls.exists()


def test_global_sql_error_is_fatal_and_role_diagnostics_are_not_printed(restore_check):
    dump, env, calls = restore_check
    (dump / "globals.sql.gz").write_bytes(gzip.compress(b"BROKEN_SQL; -- fixture-role-diagnostic\n"))

    result = subprocess.run(["bash", str(SCRIPT), str(dump)], env=env,
                            capture_output=True, text=True, timeout=15)

    assert result.returncode != 0
    assert "could not restore database roles" in result.stdout
    assert "fixture-role-diagnostic" not in result.stdout + result.stderr
    recorded = calls.read_text()
    assert "POSTGRES_USER=kv_restore_" in recorded
    assert "createdb" not in recorded
