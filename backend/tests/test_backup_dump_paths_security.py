"""Run the whole backup installer in a private root-owned scratch tree.

These scenarios need Linux root for real ownership/sticky-directory semantics.
No real unit, account, backup, or system configuration is changed: every writable
system path is relocated and systemctl/the database availability probe are stubs.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="real Linux root ownership is required for the isolated installer test",
)


@pytest.fixture
def installed():
    # /tmp and /var/lib are deliberately not valid dump destinations.
    scratch = Path(tempfile.mkdtemp(prefix="kv-dump-security-", dir="/srv"))
    scratch.chmod(0o755)
    try:
        for name in ("bin", "etc/cron.d", "etc/systemd/system", "etc/sudoers.d"):
            (scratch / name).mkdir(parents=True, exist_ok=True)
        ctl = scratch / "bin/systemctl"
        ctl.write_text(
            "#!/bin/sh\ncase \"$1\" in daemon-reload|enable|try-restart) exit 0;; *) exit 1;; esac\n",
            encoding="utf-8",
        )
        ctl.chmod(0o755)
        source = (ROOT / "agent/backup-setup.sh").read_text(encoding="utf-8")
        for old, new in (
            ("/var/lib/node_exporter/textfile_collector", "metrics"),
            ("/var/lib/kervax", "state"),
            ("/lib65/kervax", "lib65"),
            ("/usr/local/lib/.restic", "restic"),
            ("/etc/systemd-resta.conf", "etc/backup.env"),
            ("/etc/systemd-rest.conf", "etc/backup.sh"),
            ("/etc/systemd/system", "etc/systemd/system"),
            ("/etc/cron.d/kervax-backup", "etc/cron.d/kervax-backup"),
            ("/etc/sudoers.d/kervax-backup", "etc/sudoers.d/kervax-backup"),
            ("/etc/kervax", "etc/kervax"),
            ("/var/cache/bx231", "home"),
        ):
            source = source.replace(old, str(scratch / new))
        source = source.replace("AGENT_USER=kervax", "AGENT_USER=root")
        source = source.replace("PROV_USER=bx231", "PROV_USER=root")
        source = source.replace("DUMP_DIR_BASE=/backup", f"DUMP_DIR_BASE={scratch}/backup")
        # Use the complete installer and the helper it generates, with only the
        # external unit manager and the external database probe replaced.
        source = source.replace("systemctl ", f"{ctl} ")
        marker = 'case "${1:-}" in\n'
        assert source.count(marker) == 1
        source = source.replace(
            marker,
            'dump_probe() { return 0; }\n'
            'custom_scan_maybe() { return 0; }\n'
            'backup_runs_maybe() { return 0; }\n'
            'verify_maybe() { return 0; }\n' + marker,
        )
        installer = scratch / "install.sh"
        installer.write_text(source, encoding="utf-8")
        result = subprocess.run(["bash", str(installer)], capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        version = source.split("KERVAX_SETUP_VERSION=", 1)[1].split()[0]
        assert (scratch / "state/versions/backup-setup.ver").read_text().strip() == version
        yield scratch, scratch / "lib65/kervax-backup-helper"
    finally:
        shutil.rmtree(scratch)


def setup_dump(helper, directory, container="db-a", engine="redis"):
    return subprocess.run(
        ["bash", str(helper), "dump-setup", engine, container],
        env={**os.environ, "KV_DUMP_DIR": str(directory), "KV_DUMP_MINFREE": "0"},
        capture_output=True, text=True, timeout=30,
    )


@pytest.mark.parametrize("default", [False, True])
def test_dump_setup_does_not_follow_planted_directory_symlink(installed, default):
    scratch, helper = installed
    victim = scratch / "foreign"
    victim.mkdir(mode=0o751)
    (victim / "keep").write_text("keep", encoding="utf-8")
    base = scratch / ("backup" if default else "shared")
    base.mkdir(mode=0o755)
    base.chmod(0o1777)
    link = base / ("redis" if default else "kervax-dumps")
    link.symlink_to(victim, target_is_directory=True)
    os.lchown(link, 65534, 65534)  # a local unprivileged user planted it

    result = setup_dump(helper, base)

    assert result.returncode != 0, result.stdout + result.stderr
    assert stat.S_IMODE(victim.stat().st_mode) == 0o751
    assert list(p.name for p in victim.iterdir()) == ["keep"]
    assert not (scratch / "lib65/dumps.d/redis@db-a.sh").exists()


def test_dump_setup_rejects_foreign_existing_child(installed):
    scratch, helper = installed
    base = scratch / "data"
    child = base / "kervax-dumps"
    child.mkdir(parents=True)
    os.chown(child, 65534, 65534)
    child.chmod(0o777)

    result = setup_dump(helper, base)

    assert result.returncode != 0, result.stdout + result.stderr
    assert stat.S_IMODE(child.stat().st_mode) == 0o777
    assert not (child / "redis").exists()


def test_dump_remove_keeps_files_behind_unsafe_intermediate_directory(installed):
    scratch, helper = installed
    base = scratch / "backup"
    base.mkdir(mode=0o755)
    base.chmod(0o1777)
    victim = scratch / "foreign/db-a"
    victim.mkdir(parents=True)
    (victim / "keep").write_text("keep", encoding="utf-8")
    (base / "redis").symlink_to(victim.parent, target_is_directory=True)
    os.lchown(base / "redis", 65534, 65534)
    dumps = scratch / "lib65/dumps.d"
    dumps.mkdir()
    (dumps / "redis@db-a.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (dumps / "redis@db-a.meta").write_text(f"dir={base}/redis/db-a\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(helper), "dump-remove", "redis", "db-a"],
        capture_output=True, text=True, timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (victim / "keep").read_text() == "keep"
    assert "left in place" in result.stdout
    assert not (dumps / "redis@db-a.sh").exists()


def test_safe_default_and_custom_dump_directories_still_work(installed):
    scratch, helper = installed
    base = scratch / "data"
    base.mkdir(mode=0o751)
    result = setup_dump(helper, base)
    assert result.returncode == 0, result.stdout + result.stderr
    assert stat.S_IMODE(base.stat().st_mode) == 0o751
    assert (base / "kervax-dumps/redis/db-a").is_dir()

    # Losing exclusive root ownership after setup also blocks an already generated
    # dump, before it enters a database container or touches the dump files.
    out = base / "kervax-dumps/redis/db-a"
    out.chmod(0o777)
    result = subprocess.run(
        ["bash", str(scratch / "lib65/dumps.d/redis@db-a.sh")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert "unsafe dump directory" in result.stderr
    assert not list(out.iterdir())

    default = scratch / "backup"
    result = setup_dump(helper, default, container="db-b")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (default / "redis/db-b").is_dir()

    # A root-managed /backup symlink to a separate data disk keeps the default
    # layout, and the runner/removal metadata use its stable canonical path.
    data = scratch / "backup-data"
    default.rename(data)
    default.symlink_to(data, target_is_directory=True)
    result = setup_dump(helper, default, container="db-c")
    assert result.returncode == 0, result.stdout + result.stderr
    slot = data / "redis/db-c"
    assert slot.is_dir()
    meta = (scratch / "lib65/dumps.d/redis@db-c.meta").read_text()
    assert f"dir={slot}\n" in meta
    result = subprocess.run(
        ["bash", str(helper), "dump-remove", "redis", "db-c"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not slot.exists()
    assert (data / "redis/db-b").is_dir()


def test_refresh_adds_runtime_guard_to_legacy_dump_script(installed):
    scratch, helper = installed
    # Use a complete generated atomic runner, not a shortened stand-in: OUT is
    # assigned once to its destination and again to its temporary .partial directory.
    result = setup_dump(helper, scratch / "backup", container="", engine="mysql")
    assert result.returncode == 0, result.stdout + result.stderr
    dumps = scratch / "lib65/dumps.d"
    script = dumps / "mysql.sh"
    legacy = re.sub(
        r"# kervax-dump-path-guard: 1\n.*?\nfi\n", "", script.read_text(), flags=re.S,
    )
    assert '# kervax-dump-path-guard: 1' not in legacy
    assert 'OUT="$OUT/.partial"' in legacy
    script.write_text(legacy, encoding="utf-8")
    script.chmod(0o700)
    default = scratch / "backup"
    data = scratch / "backup-data"
    default.rename(data)
    default.symlink_to(data, target_is_directory=True)
    out = data / "mysql"
    out.chmod(0o777)
    safe_out = data / "safe/mysql"
    safe_out.mkdir(parents=True)
    safe_script = dumps / "mysql-safe.sh"
    safe_legacy = re.sub(r"^OUT=.*$", f"OUT={default}/safe/mysql", legacy, count=1, flags=re.M)
    safe_script.write_text(safe_legacy, encoding="utf-8")
    safe_script.chmod(0o700)
    manual = dumps / "manual.sh"
    manual_text = '#!/bin/sh\n# manually maintained\nOUT=/tmp\nexit 0\n'
    manual.write_text(manual_text, encoding="utf-8")
    manual.chmod(0o700)
    foreign = dumps / "foreign.sh"
    foreign_text = script.read_text()
    foreign.write_text(foreign_text, encoding="utf-8")
    os.chown(foreign, 65534, 65534)
    foreign.chmod(0o700)
    paused = dumps / "paused.sh"
    paused.write_text(safe_legacy, encoding="utf-8")
    paused.chmod(0o600)

    result = subprocess.run(
        ["bash", str(helper), "refresh"], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "# kervax-dump-path-guard: 1" in script.read_text()
    assert manual.read_text() == manual_text
    assert foreign.read_text() == foreign_text
    assert stat.S_IMODE(paused.stat().st_mode) == 0o600
    once = script.read_text()
    result = subprocess.run(
        ["bash", str(helper), "refresh"], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert script.read_text() == once
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "unsafe dump directory" in result.stderr
    assert not list(out.iterdir())
    result = subprocess.run(
        ["bash", "-c", 'mysqldump() { echo "-- MySQL dump of db-a"; }; '
         'export -f mysqldump; bash "$1"', "test", str(safe_script)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(safe_out.glob("mysqldump-all-*.sql.gz"))


def test_system_directory_validator_rejects_root_without_any_filesystem_writes():
    source = (ROOT / "agent/backup-setup.sh").read_text(encoding="utf-8")
    body = "dump_sys_dir() {" + source.split("dump_sys_dir() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    result = subprocess.run(
        ["bash", "-c", body + 'dump_sys_dir / && dump_sys_dir /etc/ && ! dump_sys_dir /backup'],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
