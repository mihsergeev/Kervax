"""Host-side proxy topology tests without Docker, credentials or network access."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("proxy_peers_sync", ROOT / "ops/agent-proxy-peers-sync.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
FRONT, BACK, PROXY, INTERNAL, EDGE, OTHER = (letter * 64 for letter in "abcdef")


def net(network_id, address, ipv6=""):
    return {"NetworkID": network_id, "IPAddress": address, "GlobalIPv6Address": ipv6}


@pytest.fixture
def topology(monkeypatch):
    env = {"KERVAX_PROXY_PEERS_PROJECT": "panel", "KERVAX_PROXY_PEERS_CADDY_PROJECT": "edge",
           "KERVAX_PROXY_PEERS_CADDY_SERVICE": "caddy", "KERVAX_PROXY_PEERS_EDGE_NETWORK": "edge-net"}
    items = {}
    for cid, project, service in ((FRONT, "panel", "frontend"), (BACK, "panel", "backend"), (PROXY, "edge", "caddy")):
        items[cid] = {"id": cid, "project": project, "service": service, "oneoff": "False",
                      "running": True, "status": "running", "health": None}
    items[FRONT]["networks"] = {"panel-internal": net(INTERNAL, "192.0.2.10", "2001:db8:1::10"),
                               "edge-net": net(EDGE, "198.51.100.10", "2001:db8:2::10")}
    items[BACK]["networks"] = {"panel-internal": net(INTERNAL, "192.0.2.20")}
    items[PROXY]["networks"] = {"edge-net": net(EDGE, "198.51.100.30", "2001:db8:2::30"),
                               "unrelated": net(OTHER, "203.0.113.40")}
    selected = {("panel", "frontend"): [FRONT], ("panel", "backend"): [BACK], ("edge", "caddy"): [PROXY]}
    calls = []

    def docker(*args):
        calls.append(args)
        if args[0] == "ps":
            project = next(arg.split("=", 2)[2] for arg in args if arg.startswith("label=com.docker.compose.project="))
            service = next(arg.split("=", 2)[2] for arg in args if arg.startswith("label=com.docker.compose.service="))
            return "\n".join(selected.get((project, service), []))
        if args[0] == "inspect":
            assert args[2] == sync.INSPECT
            return "\n".join(json.dumps(items[cid]) for cid in args[3:])
        if args[:2] == ("network", "inspect"):
            if args[-1] == "edge-net":
                return json.dumps({"id": EDGE, "name": "edge-net"})
            return json.dumps(args[-1])
        raise AssertionError("unexpected Docker command")

    monkeypatch.setattr(sync, "docker", docker)
    return env, items, selected, calls


def test_exact_roles_dual_stack_ignore_unrelated_network(topology):
    env, items, _, calls = topology
    result = sync.snapshot(env)
    assert result["v"] == 1 and isinstance(result["generated_at"], int)
    assert result["frontend_peer_ips"] == ["192.0.2.10", "2001:db8:1::10"]
    assert result["forwarded_proxy_ips"] == ["198.51.100.30", "2001:db8:2::30"]
    assert "203.0.113.40" not in json.dumps(result)
    assert all("Config.Env" not in str(call) and "Health.Log" not in str(call) for call in calls)


@pytest.mark.parametrize("role", [FRONT, BACK, PROXY])
def test_reject_unhealthy_or_wrong_label(topology, role):
    env, items, _, _ = topology
    items[role]["health"] = "unhealthy"
    with pytest.raises(ValueError):
        sync.snapshot(env)
    items[role]["health"] = None
    items[role]["project"] = "untrusted-project"
    with pytest.raises(ValueError):
        sync.snapshot(env)


def test_duplicate_role_and_container_role_confusion(topology):
    env, items, selected, _ = topology
    selected[("panel", "frontend")] = [FRONT, FRONT]
    with pytest.raises(ValueError):
        sync.snapshot(env)
    selected[("panel", "frontend")] = [FRONT]
    env["KERVAX_PROXY_PEERS_CADDY_PROJECT"] = "panel"
    env["KERVAX_PROXY_PEERS_CADDY_SERVICE"] = "frontend"
    with pytest.raises(ValueError, match="roles overlap"):
        sync.snapshot(env)


@pytest.mark.parametrize("bad", ["hostname.invalid", "192.0.2.1/24", "127.0.0.1", "0.0.0.0", "224.0.0.1", "255.255.255.255", 1, None, "2001:db8::1"])
def test_reject_bad_ipv4(topology, bad):
    env, items, _, _ = topology
    items[FRONT]["networks"]["panel-internal"]["IPAddress"] = bad
    with pytest.raises((ValueError, TypeError)):
        sync.snapshot(env)


@pytest.mark.parametrize("bad", ["::", "::1", "fe80::1", "ff02::1", "2001:db8::1%eth0", "::ffff:192.0.2.1"])
def test_reject_bad_ipv6(topology, bad):
    env, items, _, _ = topology
    items[PROXY]["networks"]["edge-net"]["GlobalIPv6Address"] = bad
    with pytest.raises(ValueError):
        sync.snapshot(env)


def test_network_id_mismatch_ambiguous_internal_and_address_roles(topology):
    env, items, _, _ = topology
    original = copy.deepcopy(items)
    items[PROXY]["networks"]["edge-net"]["NetworkID"] = OTHER
    with pytest.raises(ValueError):
        sync.snapshot(env)
    items.clear(); items.update(copy.deepcopy(original))
    items[BACK]["networks"]["edge-net"] = net(EDGE, "198.51.100.20")
    with pytest.raises(ValueError):
        sync.snapshot(env)
    items.clear(); items.update(copy.deepcopy(original))
    items[FRONT]["networks"]["panel-internal"]["NetworkID"] = "not-a-network-id"
    with pytest.raises(ValueError):
        sync.snapshot(env)
    items.clear(); items.update(copy.deepcopy(original))
    items[PROXY]["networks"]["edge-net"]["IPAddress"] = "192.0.2.10"
    with pytest.raises(ValueError, match="address roles overlap"):
        sync.snapshot(env)


def test_container_recreation_during_discovery_invalidates_result(topology, monkeypatch):
    env, _, _, _ = topology
    original = sync.docker

    def changed(*args):
        value = original(*args)
        if args[0] == "inspect" and len(args) == 6:
            items = [json.loads(line) for line in value.splitlines()]
            items[0]["networks"]["panel-internal"]["IPAddress"] = "192.0.2.99"
            return "\n".join(json.dumps(item) for item in items)
        return value

    monkeypatch.setattr(sync, "docker", changed)
    with pytest.raises(ValueError, match="changed during discovery"):
        sync.snapshot(env)


def test_global_docker_deadline_stops_before_systemd_timeout(monkeypatch):
    monkeypatch.setattr(sync, "READ_DEADLINE", 99)
    monkeypatch.setattr(sync, "time", SimpleNamespace(monotonic=lambda: 100))
    monkeypatch.setattr(sync.subprocess, "run", lambda *args, **kwargs: pytest.fail("Docker called after deadline"))
    with pytest.raises(TimeoutError):
        sync.docker("ps")


def test_atomic_publish_replaces_stale_addresses_and_clears_on_failure(topology, tmp_path, monkeypatch):
    env, items, _, _ = topology
    env["KERVAX_PROXY_PEERS_DATA_DIR"] = str(tmp_path)
    path = tmp_path / "agent_proxy_peers.json"
    sync.refresh(env)
    old = path.read_bytes()
    real_replace = sync.os.replace

    def observe_replace(source, destination):
        assert path.read_bytes() == old
        assert json.loads(Path(source).read_text())["frontend_peer_ips"][0] == "192.0.2.11"
        real_replace(source, destination)

    items[FRONT]["networks"]["panel-internal"]["IPAddress"] = "192.0.2.11"
    monkeypatch.setattr(sync.os, "replace", observe_replace)
    sync.refresh(env)
    monkeypatch.setattr(sync.os, "replace", real_replace)
    assert "192.0.2.10" not in path.read_text()
    items[PROXY]["health"] = "unhealthy"
    with pytest.raises(ValueError):
        sync.refresh(env)
    empty = json.loads(path.read_text())
    assert empty["v"] == 0 and empty["error"] == "topology_unavailable"
    assert empty["frontend_peer_ips"] == empty["forwarded_proxy_ips"] == []
    assert empty["generated_at"] >= json.loads(old)["generated_at"]
    assert not list(tmp_path.glob(".agent-proxy-peers-*"))
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o644


def test_failed_atomic_write_preserves_previous_complete_file_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "agent_proxy_peers.json"
    sync.publish(path, {"v": 1})
    monkeypatch.setattr(sync.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("fixture disk error")))
    with pytest.raises(OSError):
        sync.publish(path, {"v": 2})
    assert json.loads(path.read_text()) == {"v": 1}
    assert not list(tmp_path.glob(".agent-proxy-peers-*"))


def test_docker_reads_filtered_metadata_without_env(monkeypatch):
    calls = []
    monkeypatch.setattr(sync.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(stdout="{}"))
    sync.docker("inspect", "--format", sync.INSPECT, FRONT)
    argv, kwargs = calls[0]
    assert argv[:3] == ["docker", "inspect", "--format"]
    assert kwargs["check"] and 0 < kwargs["timeout"] <= 5
    assert "Config.Env" not in argv[3] and ".State.Health.Status" in argv[3]


def test_timer_and_deploy_refresh_contract():
    service = (ROOT / "ops/kervax-agent-proxy-peers.service").read_text()
    timer = (ROOT / "ops/kervax-agent-proxy-peers.timer").read_text()
    assert "User=root" in service and "EnvironmentFile=/etc/kervax/agent-proxy-peers.env" in service
    assert "ExecStart=/usr/bin/python3 /lib65/kervax/agent-proxy-peers-sync.py" in service
    assert "OnUnitActiveSec=30s" in timer and "Unit=kervax-agent-proxy-peers.service" in timer
    deploy = (ROOT / "ops/deploy-remote.sh").read_text()
    up = deploy.split("up() {", 1)[1].split("\n}", 1)[0]
    assert up.index("refresh_proxy_peers") < up.index("dc up") < up.rindex("refresh_proxy_peers")
    for name in ("quickstart.sh", "quickstart-ru.sh"):
        assert (ROOT / "ops" / name).read_text(encoding="utf-8").count('sh "$DIR/ops/agent-proxy-peers-setup.sh" "$DIR"') == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX setup executable mocks run in the Linux regression suite")
def test_disabled_update_has_no_helper_install(tmp_path):
    bash = Path(r"C:\Program Files\Git\usr\bin\bash.exe") if os.name == "nt" else Path("/bin/sh")
    (tmp_path / ".env").write_text("KERVAX_AGENT_SOURCE_GUARD=0\n")
    env = os.environ.copy()
    fake = tmp_path / "bin"
    fake.mkdir()
    docker = fake / "docker"
    docker.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"services":{"backend":{"environment":{"KERVAX_AGENT_SOURCE_GUARD":"0"}}}}\'\n')
    docker.chmod(0o755)
    env["PATH"] = str(fake) + os.pathsep + env["PATH"]
    if os.name == "nt":
        env["PATH"] = str(fake) + os.pathsep + str(bash.parent) + os.pathsep + env["PATH"]
    result = subprocess.run([str(bash), (ROOT / "ops/agent-proxy-peers-setup.sh").as_posix(), tmp_path.as_posix()], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
