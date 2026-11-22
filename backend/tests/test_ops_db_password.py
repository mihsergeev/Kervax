"""Пароль базы панели в скриптах ops не уходит в аргументы процессов: `docker exec -e
PGPASSWORD=<пароль>` на хосте видел в ps любой пользователь."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux shell")
SECRET = "fixture-only-placeholder"

DOCKER = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
a = sys.argv[1:]
with open(os.environ['FIXTURE_CALLS'], 'a') as f: f.write(json.dumps(a) + '\n')
assert a[0] == 'exec'
env, i = {'PATH': os.environ['PATH']}, 1
while a[i].startswith('-'):
    if a[i] == '-e':
        name, eq, value = a[i + 1].partition('=')
        env[name] = value if eq else os.environ.get(name, '')
        i += 2
    else:
        i += 1
sys.exit(subprocess.call(a[i + 1:], env=env))
'''


def test_no_password_value_in_docker_exec_arguments():
    for path in sorted((ROOT / "ops").glob("*.sh")):
        assert not re.search(r"-e\s+PGPASSWORD=", path.read_text(encoding="utf-8")), path.name


@pytest.mark.parametrize("shell", ["bash", "sh"])
def test_deploy_reads_the_database_with_password_over_stdin(tmp_path, shell):
    line = next(x for x in (ROOT / "ops/deploy.sh").read_text().splitlines() if "PSQL() {" in x)
    # строка живет внутри ssh "...": раскрываем ее так же, как локальный bash перед отправкой
    remote = subprocess.run(["bash", "-c", 'printf %s "' + line.strip() + '"'],
                            capture_output=True, text=True, check=True).stdout
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in {"docker": DOCKER, "sudo": '#!/bin/sh\nexec "$@"\n',
                       "psql": '#!/bin/sh\nprintf "pw=%s sql=%s\\n" "${PGPASSWORD:-}" "$*"\n'}.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    calls = tmp_path / "calls"
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "FIXTURE_CALLS": str(calls)}
    script = f"DBPW='{SECRET}'\n{remote}\nPSQL 'select version_num from alembic_version'\n"
    out = subprocess.run([shell, "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert out.stdout == f"pw={SECRET} sql=-U kervax -d kervax -tAc select version_num from alembic_version\n"
    argv = [json.loads(x) for x in calls.read_text().splitlines()]
    assert argv and not any(SECRET in arg for call in argv for arg in call)
