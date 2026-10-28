"""Старый runner бэкапа: restic прямо в ExecStart юнита, а не скрипт панели (backup-setup 0.45).

Найденный случай: на ноде со старой раскладкой юнит бэкапа сам запускал restic с путями в ExecStart.
Карточка "Расписание и пути" показывала exclude и пустые списки (get-config читал скрипт панели, который
юнит не запускает), т.е. "бэкапится весь /", и предлагала менять пути, которые не применятся. Теперь
helper разбирает сам юнит в report.d/backup-unit.json, панель показывает эти пути как есть, правку путей
не дает (команда adopt), а аудит покрытия сверяется с путями юнита."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from app import backup_unit, gitlab
from app.api.servers import _backup_coverage
from app.models import Server

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "agent" / "backup-setup.sh").read_text(encoding="utf-8")


# ------------------------------- helper -------------------------------

def _helper_body() -> str:
    body = re.search(r"<<'HELPER_EOF'\n(.*?)\nHELPER_EOF\n", SRC, re.S).group(1)
    return body[:body.index('\ncase "${1:-}" in\n')]


# systemctl на файлах: "systemctl cat <юнит>" отдает $T/<юнит>, остального нет
_STUBS = r'''
systemctl() {
  case "$1" in
    cat) [ -f "$T/$2" ] && cat "$T/$2" ;;
    *) return 1 ;;
  esac
}
SCRIPT_CANDS=("$T/runner"); PROV_SCRIPT="$T/runner"; PROV_ENV="$T/resta.conf"
UNIT_JSON="$T/report.d/backup-unit.json"; CONF_JSON="$T/backup-config.json"
# @T@ в файлах - каталог теста так, как его видит bash (в Git Bash это /c/..., а не C:/...)
for f in "$T"/*; do [ -f "$f" ] && sed -i "s#@T@#$T#g" "$f"; done
'''


def _run(tmp_path: Path, files: dict[str, str], case: str) -> str:
    bash = shutil.which("bash")
    if not bash or not shutil.which("awk"):
        pytest.skip("нет bash или awk")
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    fd, path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(_helper_body() + '\nset +e\nT="$(cd "$1" && pwd)"\n' + _STUBS + case)
        out = subprocess.run([bash, path, tmp_path.as_posix()], capture_output=True, text=True, timeout=60,
                             encoding="utf-8")
    finally:
        os.unlink(path)
    return out.stdout + out.stderr


def _report(tmp_path: Path, files: dict[str, str]) -> dict:
    out = _run(tmp_path, files, "unit_report\n")
    return json.loads(out.strip().splitlines()[-1])


UNIT_INLINE = """[Unit]
Description=maintenance
[Service]
Type=oneshot
User=svcuser
EnvironmentFile=@T@/old.env
ExecStart=/opt/restic/restic -r rest:http://node-a:fakepass@192.0.2.10:64100/node-a/ \\
    --password-file /root/.repo-pass backup --verbose \\
    /etc /srv/app/ /home --exclude=/home/*/.cache -e /srv/app/tmp \\
    --tag nightly --exclude-caches
"""
OLD_ENV = "RESTIC_REPOSITORY=rest:http://node-a:fakepass@192.0.2.10:64100/node-a/\nRESTIC_PASSWORD=fakepass\n"


def test_inline_restic_with_continuation_lines(tmp_path):
    rep = _report(tmp_path, {"systemd-rest.service": UNIT_INLINE, "old.env": OLD_ENV})
    assert rep["runner"] == "unit" and rep["source"] == "unit" and rep["parsed"] is True
    assert rep["mode"] == "include" and rep["includes"] == ["/etc", "/srv/app", "/home"]
    assert rep["excludes"] == ["/home/*/.cache", "/srv/app/tmp"]
    assert rep["flags"] == ["exclude-caches"] and rep["adopt"] is True
    # ни адреса с паролем, ни файла пароля, ни тега - только пути
    raw = json.dumps(rep)
    assert "fakepass" not in raw and "192.0.2.10" not in raw and ".repo-pass" not in raw and "nightly" not in raw


def test_sh_c_whole_root_with_exclude_file_and_redirect(tmp_path):
    unit = """[Service]
ExecStart=/bin/sh -c 'nice -n 10 /usr/bin/restic backup / --exclude-file=@T@/excl.txt --exclude=/proc >> /var/log/bk.log 2>&1'
"""
    rep = _report(tmp_path, {"restic-backup.service": unit, "excl.txt": "# system\n/sys\n\n/tmp/\n*.swp\n"})
    assert rep["service"] == "restic-backup.service" and rep["parsed"] is True
    assert rep["mode"] == "exclude" and rep["includes"] == []
    # список из --exclude-file раскрыт, лог перенаправления в пути не попал
    assert rep["excludes"] == ["/proc", "/sys", "/tmp/", "*.swp"]
    assert len(rep["exclude_files"]) == 1 and rep["exclude_files"][0].endswith("/excl.txt")
    assert "/var/log/bk.log" not in json.dumps(rep)
    # без EnvironmentFile adopt не сработает
    assert rep["adopt"] is False
    # лог перенаправления и в include-режиме не путь бэкапа (>> - два ">", имя после пробела)
    unit = """[Service]
ExecStart=/bin/sh -c '/usr/bin/restic backup /etc /srv >> /var/log/bk.log 2>&1; echo done > /tmp/x'
"""
    rep = _report(tmp_path, {"restic-backup.service": unit})
    assert rep["mode"] == "include" and rep["includes"] == ["/etc", "/srv"]


def test_dropin_reset_and_files_from(tmp_path):
    unit = """[Service]
ExecStart=/opt/restic/restic backup /old
# /etc/systemd/system/systemd-rest.service.d/override.conf
[Service]
ExecStart=
ExecStart=-/opt/restic/restic backup --files-from @T@/list.txt /var/lib/app
"""
    rep = _report(tmp_path, {"systemd-rest.service": unit, "list.txt": "/etc\n/opt/data\n"})
    assert rep["parsed"] is True and rep["mode"] == "include"
    assert rep["includes"] == ["/var/lib/app", "/etc", "/opt/data"]
    assert len(rep["files_from"]) == 1 and rep["files_from"][0].endswith("/list.txt")


def test_unknown_paths_are_not_claimed(tmp_path):
    # путь через переменную и список, которого нет: что бэкапится, неизвестно
    unit = "[Service]\nExecStart=/opt/restic/restic backup $DATA_DIR /etc\n"
    assert _report(tmp_path, {"systemd-rest.service": unit})["parsed"] is False
    unit = "[Service]\nExecStart=/opt/restic/restic backup --files-from /nonexistent/list\n"
    assert _report(tmp_path, {"systemd-rest.service": unit})["parsed"] is False
    unit = "[Service]\nExecStart=/usr/bin/true\n"
    assert _report(tmp_path, {"systemd-rest.service": unit})["parsed"] is False


def test_wrapper_scripts(tmp_path):
    # свой скрипт с restic в строке, продолжения строк и комментарии
    script = """#!/bin/bash
set -e
# restic backup /not-this
"$RESTIC" backup /etc \\
  /srv --exclude=/srv/cache
restic forget --keep-last 3
"""
    unit = "[Service]\nExecStart=@T@/bk.sh\n"
    rep = _report(tmp_path, {"systemd-rest.service": unit, "bk.sh": script})
    assert rep["source"] == "script" and rep["script"].endswith("/bk.sh")
    assert rep["parsed"] is True and rep["includes"] == ["/etc", "/srv"] and rep["excludes"] == ["/srv/cache"]
    # скрипт в раскладке ansible, но не тот, что читает get-config
    script = ('#!/usr/bin/env bash\nEXCLUDES_STR="--exclude=/proc --exclude=/tmp"\nINCLUDES_STR=""\n'
              'INCLUDE_MODE="exclude"\n')
    unit = "[Service]\nExecStart=/bin/bash @T@/other.sh\n"
    rep = _report(tmp_path, {"systemd-rest.service": unit, "other.sh": script})
    assert rep["parsed"] is True and rep["mode"] == "exclude" and rep["excludes"] == ["/proc", "/tmp"]


def test_panel_runner_keeps_set_paths(tmp_path):
    runner = '#!/usr/bin/env bash\nINCLUDE_MODE="exclude"\nEXCLUDES_STR="--exclude=/proc"\nINCLUDES_STR=""\n'
    unit = "[Service]\nExecStart=@T@/runner\n"
    out = _run(tmp_path, {"systemd-rest.service": unit, "runner": runner},
               "unit_report\ncmd_set_paths exclude /proc /tmp; echo rc=$?\n")
    assert json.loads(out.splitlines()[0]) == {"v": 1, "runner": "panel", "service": "systemd-rest.service"}
    assert "OK" in out and "rc=0" in out
    assert 'EXCLUDES_STR="--exclude=/proc --exclude=/tmp"' in (tmp_path / "runner").read_text(encoding="utf-8")


def test_unit_runner_refuses_set_paths(tmp_path):
    # скрипт панели остался лежать, но юнит его не запускает: правка не применится
    runner = '#!/usr/bin/env bash\nINCLUDE_MODE="exclude"\nEXCLUDES_STR=""\nINCLUDES_STR=""\n'
    out = _run(tmp_path, {"systemd-rest.service": UNIT_INLINE, "runner": runner, "old.env": OLD_ENV},
               "cmd_set_paths exclude /proc; echo rc=$?\n")
    assert "rc=2" in out and "kervax-backup-helper adopt" in out
    assert 'EXCLUDES_STR=""' in (tmp_path / "runner").read_text(encoding="utf-8")


def test_report_file_and_dump_coverage(tmp_path):
    out = _run(tmp_path, {"systemd-rest.service": UNIT_INLINE, "old.env": OLD_ENV}, r'''
unit_report_maybe; cat "$UNIT_JSON"
unit_covers /srv/app/dumps && echo covered1
unit_covers /backup || echo notcovered2
rm -f "$T/systemd-rest.service"; unit_report_maybe; [ -f "$UNIT_JSON" ] || echo gone
''')
    assert json.loads(out.splitlines()[0])["runner"] == "unit"
    assert "covered1" in out and "notcovered2" in out and "gone" in out


def test_adopt_takes_the_paths_of_the_unit(tmp_path):
    out = _run(tmp_path, {"systemd-rest.service": UNIT_INLINE, "old.env": OLD_ENV},
               # регэкспы {8,256} Git Bash на Windows не понимает, проверки пароля - не про этот тест
               "secret_ok() { :; }; repo_url_ok() { :; }\nKV_ADOPT_DRYRUN=1 cmd_adopt 0.19.1\n")
    assert "mode=include" in out and "DRY-RUN" in out
    assert re.findall(r"^  - (.+)$", out, re.M) == ["/etc", "/srv/app", "/home"]
    assert "fakepass" not in out


def test_helper_version_and_refresh():
    assert re.search(r"^KERVAX_SETUP_VERSION=0.47  # MAJOR", SRC, re.M)
    assert "refresh_config || true; unit_report_maybe || true;" in SRC
    assert backup_unit.HELPER == "0.45"


# ------------------------------- панель -------------------------------

UNIT_EXTRA = {"v": 1, "runner": "unit", "service": "systemd-rest.service", "source": "unit", "script": "",
              "parsed": True, "mode": "include", "includes": ["/etc", "/srv/app"], "excludes": ["/srv/app/tmp"],
              "exclude_files": [], "files_from": [], "flags": [], "adopt": True}


def _rep(extra=None, **bk):
    b = {"present": True, "configured": True, "manageable": True, "mode": "exclude", "includes": [], "excludes": []}
    b.update(bk)
    rep = {"backup": b, "disks": [{"mount": "/", "used": 50 << 30}, {"mount": "/data", "used": 40 << 30}],
           "docker": {"containers": [{"name": "app", "binds": ["/srv/app/files", "/var/lib/other"]}]}}
    if extra is not None:
        rep["extras"] = {"backup-unit": extra}
    return rep


def test_apply_takes_the_paths_from_the_unit():
    rep = _rep(UNIT_EXTRA)
    backup_unit.apply(rep)
    bk = rep["backup"]
    assert bk["runner"] == "unit" and bk["mode"] == "include"
    assert bk["includes"] == ["/etc", "/srv/app"] and bk["excludes"] == []
    assert bk["unit_paths"]["parsed"] is True and bk["unit_paths"]["adopt"] is True
    assert not backup_unit.paths_editable(bk) and backup_unit.paths_known(bk)
    # не разобран: путей нет, покрытие неизвестно
    rep = _rep(UNIT_EXTRA | {"parsed": False})
    backup_unit.apply(rep)
    assert rep["backup"]["mode"] == "" and rep["backup"]["includes"] == []
    assert not backup_unit.paths_known(rep["backup"])
    # runner панели и старый helper: блок как был
    rep = _rep({"v": 1, "runner": "panel", "service": "systemd-rest.service"}, excludes=["/proc"])
    backup_unit.apply(rep)
    assert rep["backup"]["runner"] == "panel" and rep["backup"]["excludes"] == ["/proc"]
    assert backup_unit.paths_editable(rep["backup"])
    rep = _rep(excludes=["/proc"])
    backup_unit.apply(rep)
    assert "runner" not in rep["backup"] and backup_unit.paths_known(rep["backup"])


def _gaps(rep) -> set[str]:
    s = Server(name="node-a", token_hash="x", enabled=True, last_report=rep, backup_not_required=False)
    return {a.subject for a in _backup_coverage(s) if a.gap}


def test_coverage_follows_the_unit():
    # без разбора юнита панель считала бэкапом весь / и молчала про /data и чужой bind
    assert _gaps(_rep()) == set()
    rep = _rep(UNIT_EXTRA)
    backup_unit.apply(rep)
    assert _gaps(rep) == {"/data", "/var/lib/other"}
    # юнит не разобран - дыр не выдумываем
    rep = _rep(UNIT_EXTRA | {"parsed": False})
    backup_unit.apply(rep)
    assert _gaps(rep) == set()
    assert gitlab.restic_covers(rep, "/etc/gitlab") is True


def test_ingest_applies_the_unit():
    src = (ROOT / "backend" / "app" / "api" / "servers.py").read_text(encoding="utf-8")
    i = src.index("backup_target.clean_report(report_dict)")
    assert "backup_unit.apply(report_dict)" in src[i:i + 300]


async def test_set_paths_refused_for_the_unit_runner(client, auth_headers):
    from app.models import BackupCommand

    sid = (await client.post("/api/servers", json={"name": "node-u"}, headers=auth_headers)).json()["server"]["id"]
    f = client._transport.app.state.session_factory
    rep = _rep(UNIT_EXTRA)
    backup_unit.apply(rep)
    async with f() as s:
        (await s.get(Server, sid)).last_report = rep
        await s.commit()
    url = f"/api/servers/{sid}/backup/command"
    r = await client.post(url, json={"action": "set_paths", "mode": "include", "paths": ["/etc"]},
                          headers=auth_headers)
    assert r.status_code == 400 and "kervax-backup-helper adopt" in r.json()["detail"]
    # расписание - таймер, оно применяется и у старого runner'а
    r = await client.post(url, json={"action": "set_schedule", "schedule": "02:30"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    async with f() as s:
        assert (await s.get(BackupCommand, r.json()["id"])).action == "set_schedule"
