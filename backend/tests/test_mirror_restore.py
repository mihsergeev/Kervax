"""Destination-only restore probe: selection, isolation and real restic failures."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def probe():
    source = (ROOT / "agent/mirror-setup.sh").read_text(encoding="utf-8")
    source = source.split("<<'VERIFY_EOF'\n", 1)[1].split("\nVERIFY_EOF\n", 1)[0]
    module = types.ModuleType("mirror_verify")
    exec(compile(source, "mirror-verify.py", "exec"), module.__dict__)
    return module


@pytest.mark.parametrize("path", ["/etc/passwd", "/app/data/file with spaces.txt", "/a/b-1_2+3"])
def test_safe_literal_paths(probe, path):
    assert probe.safe_path(path)


@pytest.mark.parametrize("path", ["/../escape", "/a/../escape", "/a/./b", "/a//b", "//a",
                                  "relative", "/a/*", "/a/[x]", "/a/?", "/a/\nsecret", "/"])
def test_unsafe_paths_rejected(probe, path):
    assert not probe.safe_path(path)


def test_selection_is_bounded_regular_nonempty_and_unique(probe):
    import json
    entries = [{"path": "/a/link", "type": "symlink", "size": 1},
               {"path": "/a/empty", "type": "file", "size": 0},
               {"path": "/a/big", "type": "file", "size": probe.MAX_FILE + 1}]
    entries += [{"path": f"/a/file{i}", "type": "file", "size": probe.MAX_FILE} for i in range(10)]
    entries.insert(4, entries[3])
    selected = probe.select_files(json.dumps(node) for node in entries)
    assert len(selected) == 4
    assert sum(size for _, size in selected) == probe.MAX_BYTES
    assert len({path for path, _ in selected}) == 4


@pytest.mark.skipif(sys.platform != "linux", reason="descriptor-relative Linux path checks")
def test_restored_symlink_cannot_escape_target(probe, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    (target / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        probe.file_hash(target, "/link/outside", 6)
    (target / "file").symlink_to(outside)
    with pytest.raises(OSError):
        probe.file_hash(target, "/file", 6)


def test_helper_has_no_secret_delivery_or_write_access_for_probe():
    setup = (ROOT / "agent/mirror-setup.sh").read_text(encoding="utf-8")
    worker = setup.split("verify_one() {", 1)[1].split("\n}\n", 1)[0]
    assert "BindReadOnlyPaths=$copy:/repository" in worker
    assert "LoadCredential=password:$pw" in worker
    assert "PrivateNetwork=yes" in worker and "DynamicUser=yes" in worker
    assert "ProtectSystem=strict" in worker and "CapabilityBoundingSet=" in worker
    assert 'InaccessiblePaths="$DATA $HOME_D /etc/kervax"' in worker
    assert "RuntimeMaxSec=200" in worker and "MemoryMax=1G" in worker
    spool = setup.split("cmd_process_spool() {", 1)[1].split("\n}\n", 1)[0]
    assert "password" not in spool and "verify" not in spool
    assert 'read -r -s -p' in setup


def test_worker_failure_never_logs_exception_or_secrets(probe, monkeypatch, capsys):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/fixture/credentials")
    monkeypatch.setattr(sys, "argv", ["worker", "restic", "/repository"])

    def fail(*_):
        raise ValueError("fixture-private-password-and-path")

    monkeypatch.setattr(probe, "probe", fail)
    probe.main()
    captured = capsys.readouterr()
    assert "status=unknown" in captured.out
    assert "fixture-private" not in captured.out + captured.err


@pytest.mark.skipif(sys.platform != "linux", reason="resource limits are Linux-only")
def test_worker_total_deadline_is_enforced(probe):
    probe.TIMEOUT = 0
    with pytest.raises(probe.ProbeError, match="^timeout$"):
        probe.probe("restic", "/repository", "/password")


@pytest.fixture
def destination(probe, tmp_path):
    return make_destination(tmp_path)


def make_destination(tmp_path):
    if sys.platform != "linux" or not shutil.which("restic"):
        pytest.skip("real restore needs Linux and restic")
    primary = tmp_path / "primary"
    mirror = tmp_path / "destination"
    files = tmp_path / "files"
    files.mkdir()
    password = tmp_path / "password"
    password.write_text("fixture-recovery-password\n", encoding="utf-8")
    password.chmod(0o600)
    expected = {}
    for n in range(5):
        path = files / f"fixture{n}.txt"
        path.write_bytes((f"destination-restore-fixture-{n}\n" * 100).encode())
        expected[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    binary = shutil.which("restic")
    base = [binary, "-r", str(primary), "--password-file", str(password)]
    for args in (["init"], ["backup", str(files), "--quiet"]):
        subprocess.run(base + args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shutil.copytree(primary, mirror)
    # Nothing can fall back to reading the primary repository or original files.
    shutil.rmtree(primary)
    shutil.rmtree(files)
    return binary, mirror, password, expected


def test_real_restore_from_destination_only_with_sha256(probe, destination, monkeypatch):
    binary, mirror, password, expected = destination
    observed = {}
    targets = set()
    original = probe.file_hash

    def capture(target, path, size):
        observed[path] = original(target, path, size)
        targets.add(target.parent)
        return observed[path]

    monkeypatch.setattr(probe, "file_hash", capture)
    before = {str(p.relative_to(mirror)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in mirror.rglob("*") if p.is_file()}
    result = probe.probe(binary, str(mirror), str(password))
    assert result["status"] == "ok" and result["files"] == 5 and result["bytes"] > 0
    assert observed == expected
    assert targets and all(not t.exists() for t in targets)
    after = {str(p.relative_to(mirror)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in mirror.rglob("*") if p.is_file()}
    assert before == after  # No repository lock, cache or repair write.


def test_real_wrong_password_is_unknown(probe, destination):
    binary, mirror, password, _ = destination
    password.write_text("wrong-fixture-password\n", encoding="utf-8")
    with pytest.raises(probe.ProbeError, match="^open$"):
        probe.probe(binary, str(mirror), str(password))


def test_real_corrupt_pack_cannot_report_ok(probe, destination):
    binary, mirror, password, _ = destination
    for pack in (mirror / "data").glob("*/*"):
        pack.chmod(0o600)
        pack.write_bytes(b"damaged-pack")
    with pytest.raises(probe.ProbeError, match="read|listing|restore"):
        probe.probe(binary, str(mirror), str(password))


def test_real_restore_mismatch_fails_and_cleans_temp(probe, destination, monkeypatch):
    binary, mirror, password, _ = destination
    targets = set()

    def mismatch(target, path, size):
        targets.add(target.parent)
        return "0" * 64

    monkeypatch.setattr(probe, "file_hash", mismatch)
    with pytest.raises(probe.ProbeError, match="^mismatch$"):
        probe.probe(binary, str(mirror), str(password))
    assert targets and all(not t.exists() for t in targets)


def test_root_helper_real_systemd_sandbox_and_report():
    if (sys.platform != "linux" or os.geteuid() != 0 or not Path("/run/systemd/system").is_dir()
            or not shutil.which("setfacl")):
        pytest.skip("root/systemd/ACL integration lab required")
    # PrivateTmp hides pytest's /tmp. Use a disposable /var/lib tree, like the
    # production layout, with actual root-only ciphertext and credentials.
    with tempfile.TemporaryDirectory(prefix="restore-probe-", dir="/var/lib") as directory:
        root = Path(directory)
        root.chmod(0o755)
        _, mirror, password, _ = make_destination(root)
        copy = root / "data/primary/repo"
        copy.parent.mkdir(parents=True)
        mirror.rename(copy)
        secrets = root / "etc/mirror-passwords/primary"
        secrets.mkdir(parents=True, mode=0o700)
        password.rename(secrets / "repo")
        password = secrets / "repo"
        for path in [copy, *copy.rglob("*")]:
            path.chmod(0o700 if path.is_dir() else 0o600)
        before = {str(p.relative_to(copy)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in copy.rglob("*") if p.is_file()}
        source = (ROOT / "agent/mirror-setup.sh").read_text(encoding="utf-8")
        worker = root / "worker.py"
        worker.write_text(source.split("<<'VERIFY_EOF'\n", 1)[1].split("\nVERIFY_EOF\n", 1)[0])
        worker.chmod(0o755)
        bash = source.split("<<'HELPER_EOF'\n", 1)[1].split("\nHELPER_EOF\n", 1)[0]
        for old, new in [("/lib65/kervax/kervax-mirror-verify.py", str(worker)),
                         ("/var/lib/kervax-mirror", str(root / "home")),
                         ("/etc/kervax", str(root / "etc")), ("/var/lib/kervax", str(root / "state")),
                         ("/srv/kervax-mirror", str(root / "data")),
                         ("/run/kervax-mirror", str(root / "run")),
                         ("kervax-mirror-read", "nogroup")]:
            bash = bash.replace(old, new)
        helper = root / "helper.sh"
        helper.write_text(bash)
        helper.chmod(0o755)
        for rel in ("data/.state/primary/repo", "home/sources", "state/report.d", "state/versions"):
            (root / rel).mkdir(parents=True, exist_ok=True)
        (root / "state/versions/mirror-setup.ver").write_text("0.3\n")
        (root / "home/sources/primary").write_text("host=example.invalid\nport=22\nrepos=repo\n")

        def run():
            command = subprocess.run([str(helper), "verify", "primary", "repo"],
                                     capture_output=True, timeout=300)
            report = json.loads((root / "state/report.d/backup-mirror.json").read_text())
            result = report["copies"][0]
            assert (command.returncode == 0) == (result["verify_status"] == "ok")
            return result

        password.chmod(0o644)
        assert run()["verify_code"] == "password_permissions"
        password.chmod(0o600)
        # A repository symlink must never expose any host file to the worker.
        link = copy / "foreign-link"
        link.symlink_to("/etc/shadow")
        assert run()["verify_code"] == "unsafe_path"
        link.unlink()
        result = run()
        assert result["verify_status"] == "ok" and result["verify_files"] == 5
        assert result["verify_ts"] > 0 and len(result["verify_snapshot"]) == 64
        assert result["verify_bytes"] > 0
        after = {str(p.relative_to(copy)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in copy.rglob("*") if p.is_file()}
        assert before == after
