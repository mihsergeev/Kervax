"""Root scripts on the panel host against a hostile panel data directory.

agent-firewall-sync.sh and gre-sync.sh run as root and exchange files with the panel through its
data directory, which the backend container can write to. Whatever the container puts there - a
symlink at the temporary or the result name, a symlink instead of the list - must not make root
write, chmod or read a host file outside it. Synthetic data only, firewall and ip are stubs."""
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
FW_SYNC = ROOT / "ops/agent-firewall-sync.sh"
GRE_SYNC = ROOT / "ops/gre-sync.sh"

pytestmark = pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None or not Path("/proc/self/fd").is_dir(),
                                reason="POSIX sh, symlinks and file modes")

UFW = """#!/bin/sh
case "$1" in
  status) echo "Status: active"; cat {rules} ;;
  allow) echo "80,443/tcp                 ALLOW       $5            # kervax-agent" >> {rules} ;;
esac
exit 0
"""

IP = """#!/bin/sh
case "$*" in
  "-o route get "*) echo "$4 via 192.0.2.1 dev eth0 src 203.0.113.5 uid 0" ;;
esac
exit 0
"""


def stubs(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rules = tmp_path / "ufw.rules"
    rules.write_text("")
    for name, body in (("ufw", UFW.format(rules=rules)), ("ip", IP), ("logger", "#!/bin/sh\n")):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return bin_dir, rules


def victim(tmp_path):
    host_file = tmp_path / "host" / "victim"
    host_file.parent.mkdir()
    host_file.write_text("root:synthetic-host-secret:0:0\n")
    host_file.chmod(0o600)
    return host_file


def untouched(host_file):
    assert host_file.read_text() == "root:synthetic-host-secret:0:0\n"
    assert stat.S_IMODE(host_file.stat().st_mode) == 0o600


def run_fw(tmp_path, data, bin_dir):
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               KERVAX_AGENT_IPS=str(data / "agent_allow_ips"), KERVAX_FW_STATE=str(tmp_path / "state" / "fw"),
               KERVAX_FW_LOCK=str(tmp_path / "fw.lock"))
    result = subprocess.run(["sh", str(FW_SYNC)], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def run_gre(tmp_path, data, bin_dir):
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               KERVAX_GRE_LIST=str(data / "gre_tunnels"), KERVAX_GRE_STATE=str(tmp_path / "state" / "gre"),
               KERVAX_GRE_LOCK=str(tmp_path / "gre.lock"))
    result = subprocess.run(["sh", str(GRE_SYNC)], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def data_dir(tmp_path, name, text):
    data = tmp_path / "data"
    data.mkdir()
    (data / name).write_text(text)
    return data


def regular(path):
    assert path.is_file() and not path.is_symlink()
    return path.read_text().splitlines()


def test_firewall_sync_never_writes_through_a_planted_temporary_symlink(tmp_path):
    bin_dir, _ = stubs(tmp_path)
    host_file = victim(tmp_path)
    data = data_dir(tmp_path, "agent_allow_ips", "203.0.113.30\n")
    (data / "agent_allow_ips.applied.tmp").symlink_to(host_file)

    run_fw(tmp_path, data, bin_dir)

    untouched(host_file)
    applied = regular(data / "agent_allow_ips.applied")
    assert applied[0].startswith("# kervax-agent-fw fw=ufw ts=") and applied[1:] == ["203.0.113.30"]
    assert stat.S_IMODE((data / "agent_allow_ips.applied").stat().st_mode) == 0o644
    # nothing of ours is left behind next to the planted link
    assert sorted(p.name for p in data.iterdir()) == ["agent_allow_ips", "agent_allow_ips.applied",
                                                      "agent_allow_ips.applied.tmp"]


def test_firewall_sync_replaces_a_planted_result_symlink_instead_of_following_it(tmp_path):
    bin_dir, _ = stubs(tmp_path)
    host_file = victim(tmp_path)
    data = data_dir(tmp_path, "agent_allow_ips", "203.0.113.30\n")
    (data / "agent_allow_ips.applied").symlink_to(host_file.parent, target_is_directory=True)

    run_fw(tmp_path, data, bin_dir)

    untouched(host_file)
    assert [p.name for p in host_file.parent.iterdir()] == ["victim"]
    assert regular(data / "agent_allow_ips.applied")[1:] == ["203.0.113.30"]


def test_firewall_sync_does_not_read_a_host_file_through_a_list_symlink(tmp_path):
    bin_dir, rules = stubs(tmp_path)
    host_file = tmp_path / "host-allow-list"
    host_file.write_text("198.51.100.77\n")
    host_file.chmod(0o600)
    data = tmp_path / "data"
    data.mkdir()
    (data / "agent_allow_ips").symlink_to(host_file)

    run_fw(tmp_path, data, bin_dir)

    assert "198.51.100.77" not in rules.read_text()
    applied = data / "agent_allow_ips.applied"
    assert not applied.exists() or "198.51.100.77" not in applied.read_text()


def test_gre_sync_never_writes_through_a_planted_temporary_symlink(tmp_path):
    bin_dir, _ = stubs(tmp_path)
    host_file = victim(tmp_path)
    data = data_dir(tmp_path, "gre_tunnels", "# kervax-gre v1\nkvxg3 198.51.100.10 10.254.0.1/30 10.254.0.2\n")
    (data / "gre_tunnels.applied.tmp").symlink_to(host_file)

    run_gre(tmp_path, data, bin_dir)

    untouched(host_file)
    applied = regular(data / "gre_tunnels.applied")
    assert applied[0].startswith("# kervax-gre fw=ufw ts=") and applied[1:] == ["kvxg3 198.51.100.10 203.0.113.5 ok"]
    assert stat.S_IMODE((data / "gre_tunnels.applied").stat().st_mode) == 0o644
    assert sorted(p.name for p in data.iterdir()) == ["gre_tunnels", "gre_tunnels.applied", "gre_tunnels.applied.tmp"]


def test_gre_sync_replaces_a_planted_result_symlink_instead_of_following_it(tmp_path):
    bin_dir, _ = stubs(tmp_path)
    host_file = victim(tmp_path)
    data = data_dir(tmp_path, "gre_tunnels", "# kervax-gre v1\n")
    (data / "gre_tunnels.applied").symlink_to(host_file.parent, target_is_directory=True)

    run_gre(tmp_path, data, bin_dir)

    untouched(host_file)
    assert [p.name for p in host_file.parent.iterdir()] == ["victim"]
    assert regular(data / "gre_tunnels.applied")[0].startswith("# kervax-gre fw=ufw ts=")


def test_gre_sync_does_not_read_a_host_file_through_a_list_symlink(tmp_path):
    bin_dir, rules = stubs(tmp_path)
    host_file = tmp_path / "host-tunnels"
    host_file.write_text("# kervax-gre v1\nkvxg7 198.51.100.77 10.254.0.1/30 10.254.0.2\n")
    host_file.chmod(0o600)
    data = tmp_path / "data"
    data.mkdir()
    (data / "gre_tunnels").symlink_to(host_file)

    run_gre(tmp_path, data, bin_dir)

    applied = data / "gre_tunnels.applied"
    assert not applied.exists() or "198.51.100.77" not in applied.read_text()


FIREWALLD = """#!/bin/sh
case "$1" in
  --state) exit 0 ;;
  --query-rich-rule=*) exit 1 ;;
esac
echo "$*" >> {log}
exit 0
"""

FAILING_DD = """#!/bin/sh
# a partial read that then fails: nothing of it may be taken as the list
printf '203.0.113.9\n'
exit 1
"""


def firewalld(tmp_path, failing_dd=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "firewalld.log"
    log.write_text("")
    stubs = {"ufw": "#!/bin/sh\necho 'Status: inactive'\n", "firewall-cmd": FIREWALLD.format(log=log),
             "logger": "#!/bin/sh\n"}
    if failing_dd:
        stubs["dd"] = FAILING_DD
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "fw").write_text("198.51.100.5\n198.51.100.6\n")
    return bin_dir, log


def test_firewall_sync_changes_nothing_when_the_list_cannot_be_read(tmp_path):
    bin_dir, log = firewalld(tmp_path, failing_dd=True)
    data = data_dir(tmp_path, "agent_allow_ips", "198.51.100.5\n")
    (data / "agent_allow_ips.applied").write_text("# kervax-agent-fw fw=firewalld ts=1\n198.51.100.5\n")

    run_fw(tmp_path, data, bin_dir)

    assert log.read_text() == ""
    assert (tmp_path / "state" / "fw").read_text() == "198.51.100.5\n198.51.100.6\n"
    assert (data / "agent_allow_ips.applied").read_text() == "# kervax-agent-fw fw=firewalld ts=1\n198.51.100.5\n"


def test_firewall_sync_on_firewalld_keeps_its_rules_in_the_state_on_an_empty_list(tmp_path):
    bin_dir, log = firewalld(tmp_path)
    data = data_dir(tmp_path, "agent_allow_ips", "")

    run_fw(tmp_path, data, bin_dir)

    assert "remove-rich-rule" not in log.read_text()
    assert (tmp_path / "state" / "fw").read_text().split() == ["198.51.100.5", "198.51.100.6"]
    # the list is back with one server less: only that one is closed and leaves the state
    (data / "agent_allow_ips").write_text("198.51.100.5\n")
    run_fw(tmp_path, data, bin_dir)
    removed = [line for line in log.read_text().splitlines() if "remove-rich-rule" in line]
    assert removed and all("198.51.100.6" in line for line in removed)
    assert (tmp_path / "state" / "fw").read_text().split() == ["198.51.100.5"]


def test_gre_sync_changes_nothing_when_the_list_cannot_be_read(tmp_path):
    bin_dir, _ = stubs(tmp_path)
    (bin_dir / "dd").write_text(FAILING_DD)
    (bin_dir / "dd").chmod(0o755)
    data = data_dir(tmp_path, "gre_tunnels", "# kervax-gre v1\n")
    (data / "gre_tunnels.applied").write_text("# kervax-gre fw=ufw ts=1\n")

    run_gre(tmp_path, data, bin_dir)

    assert (data / "gre_tunnels.applied").read_text() == "# kervax-gre fw=ufw ts=1\n"
    assert not (tmp_path / "state").exists()
