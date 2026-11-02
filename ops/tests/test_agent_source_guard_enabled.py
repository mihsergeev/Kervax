"""Use effective Compose values and never expose the rest of its secrets."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("guard_enabled", ROOT / "ops/agent-source-guard-enabled.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize("value,want", [(value, True) for value in ("1", "TRUE", " true ", "ON", "t", "y", "Yes", True)] + [(value, False) for value in ("0", "false", "OFF", "f", "n", "no", False, None)])
def test_effective_compose_boolean_and_exact_files(value, want, monkeypatch, capsys):
    calls = []
    payload = {"services": {"backend": {"environment": {"KERVAX_AGENT_SOURCE_GUARD": value, "KERVAX_JWT_SECRET": "fixture-secret-not-for-output"}}}}
    monkeypatch.setattr(guard.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(stdout=json.dumps(payload)))
    assert guard.enabled("/srv/panel", ["compose.yml", "guard.yml"]) is want
    argv, kwargs = calls[0]
    assert argv == ["docker", "compose", "-f", "compose.yml", "-f", "guard.yml", "config", "--format", "json"]
    assert kwargs["capture_output"] and kwargs["cwd"] == "/srv/panel"
    assert capsys.readouterr().out == ""


def test_invalid_effective_value_is_not_silently_disabled(monkeypatch):
    monkeypatch.setattr(guard.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps({"services": {"backend": {"environment": {"KERVAX_AGENT_SOURCE_GUARD": "unexpected"}}}})))
    with pytest.raises(ValueError):
        guard.enabled("/srv/panel")
