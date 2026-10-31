"""Exercise the complete generated PG runner, including partial-run cleanup."""

import gzip
import json
import os
import re
import stat
import subprocess
import sys

import pytest

from tests.test_backup_dump_paths_security import installed, setup_dump


pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="the full isolated installer needs real Linux root ownership",
)


def pg_runner(installed, names):
    scratch, helper = installed
    result = setup_dump(helper, scratch / "backup", container="db-a", engine="pg")
    assert result.returncode == 0, result.stdout + result.stderr
    names_file = scratch / "database-names.json"
    names_file.write_text(json.dumps(names), encoding="utf-8")
    docker = scratch / "bin/docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[0] == 'inspect':\n"
        "    print('POSTGRES_USER=owner_without_named_database'); sys.exit(0)\n"
        "assert args.pop(0) == 'exec'\n"
        "if args[0] == '-u': del args[:2]\n"
        "args.pop(0)\n"
        "command = args.pop(0)\n"
        "failure = os.environ.get('PG_FIXTURE_FAIL', '')\n"
        "if command == 'pg_dumpall':\n"
        "    if failure == 'globals': sys.exit(2)\n"
        "    print('-- roles fixture'); sys.exit(0)\n"
        "if command == 'psql':\n"
        "    if '-d' not in args or args[args.index('-d')+1] != 'postgres': sys.exit(2)\n"
        "    if failure == 'list': sys.exit(2)\n"
        "    names = json.load(open(os.environ['PG_FIXTURE_NAMES']))\n"
        "    separator = '\\0' if any('0' in x for x in args if x.startswith('-')) else '\\n'\n"
        "    sys.stdout.write(separator.join(names) + separator); sys.exit(0)\n"
        "assert command == 'pg_dump'\n"
        "name = args[-1]\n"
        "if failure == name: sys.exit(2)\n"
        "print('-- database fixture ' + repr(name))\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(scratch / "bin") + os.pathsep + os.environ["PATH"],
        "PG_FIXTURE_NAMES": str(names_file),
    }
    return scratch, scratch / "lib65/dumps.d/pg@db-a.sh", env


@pytest.mark.parametrize("failure", ["globals", "list", "alpha"])
def test_failed_pg_stage_never_publishes_or_rotates_previous_dump(installed, failure):
    scratch, runner, env = pg_runner(installed, ["alpha", "omega"])
    destination = scratch / "backup/pg/db-a"
    previous = destination / "20260101-000000"
    previous.mkdir()
    old = gzip.compress(b"-- previous known good database")
    (previous / "alpha.sql.gz").write_bytes(old)

    result = subprocess.run(
        ["bash", str(runner)], env={**env, "PG_FIXTURE_FAIL": failure},
        capture_output=True, text=True, timeout=30,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "pg dump failed" in result.stderr
    assert (previous / "alpha.sql.gz").read_bytes() == old
    assert list(destination.iterdir()) == [previous]


def test_pg_names_with_spaces_quotes_newlines_and_non_ascii_are_dumped_whole(installed):
    names = ["app data", 'quote"db', "line\ndb", "кириллица", "-option"]
    scratch, runner, env = pg_runner(installed, names)

    result = subprocess.run(
        ["bash", str(runner)], env=env, capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    runs = list((scratch / "backup/pg/db-a").iterdir())
    assert len(runs) == 1
    assert not runs[0].name.startswith(".")
    assert len(list(runs[0].glob("*.sql.gz"))) == len(names) + 1
    for name in names:
        filename = re.sub(rb"[^A-Za-z0-9._-]", b"_", name.encode()).decode()
        assert gzip.decompress((runs[0] / f"{filename}.sql.gz").read_bytes()) == (
            "-- database fixture " + repr(name) + "\n"
        ).encode()


def test_colliding_pg_filenames_do_not_silently_overwrite_a_database(installed):
    scratch, runner, env = pg_runner(installed, ["app data", "app_data"])

    result = subprocess.run(
        ["bash", str(runner)], env=env, capture_output=True, text=True, timeout=30,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "filenames collide" in result.stderr
    assert not list((scratch / "backup/pg/db-a").iterdir())


def test_refresh_replaces_exact_legacy_body_and_preserves_paused_or_manual_scripts(installed):
    scratch, helper = installed
    source = helper.read_text()
    old = source.split("<<'PG_OLD_BODY'\n", 1)[1].split("\nPG_OLD_BODY\n", 1)[0] + "\n"
    scratch, runner, env = pg_runner(installed, ["application"])
    generated = runner.read_text()
    start = generated.index("MULTI=1\n")
    end = generated.index("rc=$?\n", start)
    legacy = generated[:start] + old + generated[end:]
    runner.write_text(legacy)
    runner.chmod(0o600)
    manual = runner.with_name("pg@manual.sh")
    manual.write_text(legacy.replace("# generated by kervax", "# hand-maintained from kervax"))
    manual.chmod(0o700)
    manual_before = manual.read_text()
    changed = runner.with_name("pg@customized.sh")
    changed.write_text(legacy.replace('for db in $DBS; do', 'for db in $DBS; do # custom'))
    changed.chmod(0o700)
    changed_before = changed.read_text()

    for _ in range(2):
        result = subprocess.run(
            ["bash", str(helper), "refresh"], env=env,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert runner.read_text() == generated
        assert stat.S_IMODE(runner.stat().st_mode) == 0o600
        assert manual.read_text() == manual_before
        assert changed.read_text() == changed_before
