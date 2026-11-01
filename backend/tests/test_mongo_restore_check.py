"""A successful process is evidence only after complete restore and DB validation."""
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess

import pytest


SPEC = importlib.util.spec_from_file_location("mongo_restore_test", Path(__file__).parents[2] / "ops/mongo-restore-test.py")
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)
COUNTS = {"databases": 2, "collections": 1, "documents": 3, "indexes": 2,
          "users": 1, "roles": 1, "sha256": "d" * 64}


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    archive = tmp_path / "dump.archive.gz"
    archive.write_bytes(gzip.compress(b"complete archive fixture"))
    calls = []
    state = {"restore_rc": 0, "validation_rc": 0, "closed": False}

    def run(self, args, **kwargs):
        calls.append((args, kwargs))
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, b"sha256:" + b"a" * 64 + b"\n")
        if args[:1] == ["create"]:
            return subprocess.CompletedProcess(args, 0, b"b" * 64)
        if "mongorestore" in args:
            if "--version" in args:
                return subprocess.CompletedProcess(args, 0, b"mongorestore version: 100.20.0\n")
            assert gzip.decompress(kwargs["stdin"].read()) == b"complete archive fixture"
            if state.get("mutate"):
                archive.write_bytes(gzip.compress(b"replacement archive"))
            return subprocess.CompletedProcess(args, state["restore_rc"], b"secret document diagnostic")
        if "mongosh" in args:
            script = kwargs["data"].decode()
            if script == restore.PING_JS:
                return subprocess.CompletedProcess(args, 0, b"")
            assert script == restore.INVENTORY_JS
            return subprocess.CompletedProcess(args, state["validation_rc"],
                                               json.dumps({"inventory": COUNTS, "version": "7.0.45"}).encode())
        return subprocess.CompletedProcess(args, 0, b"")

    def close(self):
        state["closed"] = True

    monkeypatch.setattr(restore.Sandbox, "run", run)
    monkeypatch.setattr(restore.Sandbox, "close", close)
    args = [str(archive), "--image", "mongo:7.0", "--mode", "replica"]
    return archive, args, calls, state


def result(capsys):
    captured = capsys.readouterr()
    assert "secret document diagnostic" not in captured.out + captured.err
    return json.loads(captured.out)


def test_complete_restore_validates_all_data_and_binds_report_to_input(sandbox, capsys):
    archive, args, calls, state = sandbox
    assert restore.main(args) == 0
    report = result(capsys)
    assert report["restore_ok"] and report["validation_ok"] and report["ok"]
    assert report["expectations_match"] is None  # restore success is not a baseline comparison
    assert report["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert report["image_id"] == "sha256:" + "a" * 64
    assert report["mongo_version"] == "7.0.45" and report["tools_version"] == "100.20.0"
    assert report["inventory"] == COUNTS and state["closed"]
    command = next(a for a, _ in calls if "--archive" in a)
    assert "--oplogReplay" in command and "--stopOnError" in command
    create = next(a for a, _ in calls if a[0] == "create")
    assert create[create.index("--network") + 1] == "none"
    assert "--read-only" in create and "--cap-drop" in create and "--pids-limit" in create
    assert not set(create) & {"-v", "--volume", "--mount", "-p", "--publish", "--privileged"}


@pytest.mark.parametrize("failure", ["restore_rc", "validation_rc"])
def test_failure_never_reports_verified_and_always_cleans(sandbox, capsys, failure):
    _, args, calls, state = sandbox
    state[failure] = 3
    assert restore.main(args) == 1
    report = result(capsys)
    assert not report["ok"] and not report["validation_ok"] and state["closed"]
    assert report["restore_ok"] == (failure == "validation_rc")
    if failure == "restore_rc":
        assert not any(kw.get("data", b"").decode() == restore.INVENTORY_JS for _, kw in calls)


@pytest.mark.parametrize("damage", ["raw", "truncate", "bad_crc"])
def test_entire_gzip_is_verified_before_creating_sandbox(sandbox, capsys, damage):
    archive, args, calls, _ = sandbox
    contents = gzip.compress(b"x" * (3 * 1024 * 1024))
    if damage == "raw": contents = b"not gzip"
    elif damage == "truncate": contents = contents[:-7]
    else: contents = contents[:-8] + b"bad CRC!"
    archive.write_bytes(contents)
    assert restore.main(args) == 1
    assert result(capsys)["error"] == "corrupt gzip archive"
    assert not calls


def test_unchanged_input_is_required_after_restore(sandbox, capsys):
    _, args, _, state = sandbox
    state["mutate"] = True
    assert restore.main(args) == 1
    report = result(capsys)
    assert not report["ok"] and not report["validation_ok"] and state["closed"]
    assert report["error"] == "archive changed during verification"


@pytest.mark.parametrize("match", [False, True])
def test_expectations_compare_full_content_and_metadata_hash(sandbox, capsys, tmp_path, match):
    _, args, _, _ = sandbox
    expected = dict(COUNTS)
    if not match: expected["sha256"] = "c" * 64
    manifest = tmp_path / "expected.json"
    manifest.write_text(json.dumps({"inventory": expected}))
    assert restore.main([*args, "--expect", str(manifest)]) == (0 if match else 1)
    report = result(capsys)
    assert report["restore_ok"] and report["validation_ok"]
    assert report["expectations_match"] == match
    assert report["ok"] == match


def test_manifest_data_is_never_executed_or_passed_to_mongo(sandbox, capsys, tmp_path):
    _, args, calls, _ = sandbox
    manifest = tmp_path / "expected.json"
    manifest.write_text(json.dumps({**COUNTS, "sha256": "process.exit(0); secret"}))
    assert restore.main([*args, "--expect", str(manifest)]) == 1
    assert result(capsys)["error"] == "invalid expectations" and not calls


def test_standalone_requires_explicit_quiescence_and_never_replays_oplog(sandbox, capsys):
    _, args, calls, _ = sandbox
    args[-1] = "standalone"
    assert restore.main(args) == 1
    assert not calls and not result(capsys)["restore_ok"]
    assert restore.main([*args, "--quiesced"]) == 0
    result(capsys)
    assert "--oplogReplay" not in next(a for a, _ in calls if "--archive" in a)


def test_report_never_overwrites_input_or_existing_file(sandbox, capsys):
    archive, args, _, _ = sandbox
    before = archive.read_bytes()
    assert restore.main([*args, "--report", str(archive)]) == 1
    assert result(capsys)["error"] == "cannot create new private report"
    assert archive.read_bytes() == before


def test_new_report_is_private_and_contains_no_names_or_raw_documents(sandbox, capsys, tmp_path):
    _, args, _, _ = sandbox
    report_file = tmp_path / "result.json"
    assert restore.main([*args, "--report", str(report_file)]) == 0
    report = result(capsys)
    assert json.loads(report_file.read_text()) == report
    if os.name == "posix":
        assert report_file.stat().st_mode & 0o777 == 0o600
    assert "secret document" not in report_file.read_text()
    assert set(report["inventory"]) == restore.INVENTORY_KEYS


def test_time_limit_failure_still_cleans_owned_sandbox(sandbox, capsys, monkeypatch):
    _, args, _, state = sandbox
    original = restore.Sandbox.run
    def run(self, command, **kwargs):
        if "--archive" in command:
            raise restore.Failure("Docker command failed or timed out")
        return original(self, command, **kwargs)
    monkeypatch.setattr(restore.Sandbox, "run", run)
    assert restore.main(args) == 1
    assert not result(capsys)["restore_ok"] and state["closed"]


def test_cleanup_failure_cannot_report_success(sandbox, capsys, monkeypatch):
    _, args, _, _ = sandbox
    def fail(self):
        raise restore.Failure("sandbox cleanup could not be confirmed")
    monkeypatch.setattr(restore.Sandbox, "close", fail)
    assert restore.main(args) == 1
    report = result(capsys)
    assert report["restore_ok"] and report["validation_ok"] and not report["ok"]


def test_symlink_input_is_rejected_before_docker(sandbox, capsys, tmp_path):
    archive, args, calls, _ = sandbox
    link = tmp_path / "archive-link"
    try:
        link.symlink_to(archive)
    except OSError:
        pytest.skip("symlink privileges unavailable")
    args[0] = str(link)
    assert restore.main(args) == 1
    assert not result(capsys)["restore_ok"] and not calls


def test_cleanup_uses_owned_immutable_id_and_rejects_reused_name(monkeypatch):
    box = restore.Sandbox(10**12)
    box.created = True
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, ("b" * 64 + " another-token\n").encode())
    monkeypatch.setattr(restore.subprocess, "run", run)
    with pytest.raises(restore.Failure, match="ownership"):
        box.close()
    assert len(calls) == 1 and "rm" not in calls[0]


def test_cleanup_removes_only_owned_id(monkeypatch):
    box = restore.Sandbox(10**12)
    box.created = True
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, ("b" * 64 + " " + box.token + "\n").encode())
    monkeypatch.setattr(restore.subprocess, "run", run)
    box.close()
    assert calls[-1] == ["docker", "rm", "--force", "--volumes", "b" * 64]


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
def test_interrupt_during_cleanup_is_deferred_until_owned_container_removed(monkeypatch, signum):
    box = restore.Sandbox(10**12)
    box.created = True
    calls = []
    before = signal.getsignal(signum)
    def run(args, **kwargs):
        calls.append(args)
        if "inspect" in args:
            signal.getsignal(signum)(signum, None)
        return subprocess.CompletedProcess(args, 0, ("b" * 64 + " " + box.token + "\n").encode())
    monkeypatch.setattr(restore.subprocess, "run", run)
    with pytest.raises(restore.Failure, match="interrupted during cleanup"):
        box.close()
    assert calls[-1] == ["docker", "rm", "--force", "--volumes", "b" * 64]
    assert signal.getsignal(signum) is before


def test_unexpected_keyboard_interrupt_can_never_return_success(sandbox, capsys, monkeypatch):
    _, args, _, _ = sandbox
    def interrupt(self):
        raise KeyboardInterrupt()
    monkeypatch.setattr(restore.Sandbox, "close", interrupt)
    assert restore.main(args) == 1
    report = result(capsys)
    assert not report["ok"] and report["error"] == "verification interrupted"


def test_hash_honors_deadline_between_chunks(monkeypatch, tmp_path):
    archive = tmp_path / "input"
    archive.write_bytes(b"x" * (3 * 1024 * 1024))
    monkeypatch.setattr(restore.time, "monotonic", lambda: 2)
    with archive.open("rb") as stream, pytest.raises(restore.Failure, match="time limit exceeded"):
        restore.archive_hash(stream, 1)
    assert stream.closed


def test_cleanup_daemon_failure_is_not_mistaken_for_absent_container(monkeypatch):
    box = restore.Sandbox(10**12)
    box.created = True
    def unavailable(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, b"")
    monkeypatch.setattr(restore.subprocess, "run", unavailable)
    with pytest.raises(restore.Failure, match="cleanup could not be confirmed"):
        box.close()
