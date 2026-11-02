"""Acceptance requires reports accepted by the new backend, not just healthy HTTP."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("agent_source_live", ROOT / "ops/agent-source-live-check.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


def clock(monkeypatch, now=1000):
    monkeypatch.setattr(live, "time", SimpleNamespace(time=lambda: now, monotonic=lambda: now, sleep=lambda _: None))


def test_capture_only_prefresh_ids_root_private_file(tmp_path, monkeypatch):
    clock(monkeypatch)
    monkeypatch.setattr(live, "rows", lambda: {1: 999, 2: 950, 3: 800, 4: 1100})
    path = tmp_path / "baseline.json"
    live.capture(path)
    assert json.loads(path.read_text())["agents"] == {"1": 999, "2": 950}
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_no_prefresh_agents_prevents_unverified_deploy(tmp_path, monkeypatch):
    clock(monkeypatch)
    monkeypatch.setattr(live, "rows", lambda: {1: 800})
    with pytest.raises(ValueError, match="no recently reporting"):
        live.capture(tmp_path / "baseline.json")


def test_old_backend_progress_does_not_falsely_confirm_new_guard(tmp_path, monkeypatch):
    clock(monkeypatch)
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"v": 1, "agents": {"1": 990, "2": 991}}))
    start = datetime.fromtimestamp(1000, timezone.utc).isoformat()
    monkeypatch.setattr(live, "docker", lambda *args: "1" if args[0] == "exec" else start)
    monkeypatch.setattr(live, "rows", lambda: {1: 995, 2: 1001})
    assert live.verify(path, 1000, timeout=0) is False
    monkeypatch.setattr(live, "rows", lambda: {1: 1001, 2: 1001})
    assert live.verify(path, 1000, timeout=0) is True
    assert live.verify(path, 1002, timeout=0) is False
    monkeypatch.setattr(live, "rows", lambda: {1: 1002})
    assert live.verify(path, 1000, timeout=0) is False
    monkeypatch.setattr(live, "docker", lambda *args: "0" if args[0] == "exec" else start)
    with pytest.raises(ValueError, match="does not enforce"):
        live.verify(path, 1000, timeout=0)


@pytest.mark.parametrize("data", [{}, {"v": 1, "agents": {}}, {"v": 1, "agents": {"1": float("nan")}}, {"v": 1, "agents": {"0": 1000}}])
def test_missing_or_corrupt_baseline_never_confirms(tmp_path, data):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        live.verify(path, 1000, timeout=0)


def test_readonly_query_has_no_agent_tokens_or_command_queue_access(monkeypatch):
    assert "SELECT id,last_seen FROM servers WHERE enabled" in live.QUERY
    assert all(word not in live.QUERY for word in ("token_hash", "agent_token", "UPDATE ", "DELETE ", "INSERT ", "commands"))
    monkeypatch.setattr(live, "docker", lambda *args: '[[1,1000],[1,1001]]')
    with pytest.raises(ValueError):
        live.rows()
