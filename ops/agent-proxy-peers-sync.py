#!/usr/bin/env python3
"""Publish exact trusted proxy peers from explicitly selected Docker topology."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


INSPECT = ('{"id":{{json .Id}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},'
           '"service":{{json (index .Config.Labels "com.docker.compose.service")}},'
           '"oneoff":{{json (index .Config.Labels "com.docker.compose.oneoff")}},'
           '"running":{{json .State.Running}},"status":{{json .State.Status}},'
           '"health":{{with index .State "Health"}}{{json .Status}}{{else}}null{{end}},'
           '"networks":{{json .NetworkSettings.Networks}}}')
ID = re.compile(r"[a-f0-9]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
READ_DEADLINE = None


def docker(*args):
    remaining = 5 if READ_DEADLINE is None else min(5, READ_DEADLINE - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("proxy topology read deadline exceeded")
    result = subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=remaining)
    return result.stdout.strip()


def setting(env, name, default=None):
    value = env.get("KERVAX_PROXY_PEERS_" + name, default)
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("invalid topology selector: " + name)
    return value


def container(project, service):
    ids = docker("ps", "--no-trunc", "-q", "--filter", "label=com.docker.compose.project=" + project,
                 "--filter", "label=com.docker.compose.service=" + service).split()
    if len(ids) != 1 or not ID.fullmatch(ids[0]):
        raise ValueError("expected exactly one running container per selected role")
    item = json.loads(docker("inspect", "--format", INSPECT, ids[0]))
    if (item.get("id") != ids[0] or item.get("project") != project or item.get("service") != service
            or item.get("oneoff", "False").lower() != "false" or item.get("running") is not True
            or item.get("status") != "running" or item.get("health") not in (None, "healthy")):
        raise ValueError("selected container identity or health is invalid")
    return item


def networks(item):
    entries = item.get("networks")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("selected role has no networks")
    result = {}
    for name, entry in entries.items():
        network_id = entry.get("NetworkID") if isinstance(entry, dict) else None
        if not NAME.fullmatch(name) or not isinstance(network_id, str) or not ID.fullmatch(network_id) or network_id in result:
            raise ValueError("invalid or duplicate network identity")
        result[network_id] = entry
    return result


def addresses(entry):
    values = []
    for key, version in (("IPAddress", 4), ("GlobalIPv6Address", 6)):
        raw = entry.get(key, "")
        if raw == "":
            continue
        if not isinstance(raw, str) or "%" in raw:
            raise ValueError("proxy peer must be a numeric address without a zone")
        ip = ipaddress.ip_address(raw)
        if ip.version != version or ip.is_unspecified or ip.is_loopback or ip.is_multicast or ip.is_link_local or getattr(ip, "ipv4_mapped", None) or str(ip) == "255.255.255.255":
            raise ValueError("invalid proxy peer address")
        values.append(str(ip))
    if not values:
        raise ValueError("selected network has no usable peer address")
    return sorted(set(values))


def snapshot(env):
    project = setting(env, "PROJECT")
    frontend = container(project, setting(env, "FRONTEND_SERVICE", "frontend"))
    backend = container(project, setting(env, "BACKEND_SERVICE", "backend"))
    caddy = container(setting(env, "CADDY_PROJECT"), setting(env, "CADDY_SERVICE"))
    if len({frontend["id"], backend["id"], caddy["id"]}) != 3:
        raise ValueError("container roles overlap")
    front, back, edge = networks(frontend), networks(backend), networks(caddy)
    edge_name = setting(env, "EDGE_NETWORK")
    edge_info = json.loads(docker("network", "inspect", "--format", '{"id":{{json .Id}},"name":{{json .Name}}}', edge_name))
    edge_id = edge_info.get("id")
    if (edge_info.get("name") != edge_name or not isinstance(edge_id, str) or not ID.fullmatch(edge_id)
            or edge_id not in front or edge_id not in edge or edge_id in back):
        raise ValueError("edge network does not join exactly the selected proxy and frontend roles")
    shared = set(front) & set(back)
    if len(shared) != 1:
        raise ValueError("frontend/backend internal network is ambiguous")
    internal_id = shared.pop()
    if internal_id in edge:
        raise ValueError("edge proxy must not be a backend internal peer")
    actual_id = json.loads(docker("network", "inspect", "--format", "{{json .Id}}", internal_id))
    if actual_id != internal_id:
        raise ValueError("internal network identity changed")
    frontend_ips, proxy_ips = addresses(front[internal_id]), addresses(edge[edge_id])
    if set(frontend_ips) & set(proxy_ips):
        raise ValueError("proxy address roles overlap")
    selected = {item["id"]: item for item in (frontend, backend, caddy)}
    final = [json.loads(line) for line in docker("inspect", "--format", INSPECT, *selected).splitlines()]
    if len(final) != 3 or {item["id"]: item for item in final} != selected:
        raise ValueError("selected container topology changed during discovery")
    return {"v": 1, "generated_at": int(time.time()), "frontend_peer_ips": frontend_ips,
            "forwarded_proxy_ips": proxy_ips}


def publish(path, data):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".agent-proxy-peers-", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o644)
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def refresh(env):
    global READ_DEADLINE
    directory = Path(env["KERVAX_PROXY_PEERS_DATA_DIR"])
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("data directory must already exist and be absolute")
    path = directory / "agent_proxy_peers.json"
    READ_DEADLINE = time.monotonic() + 75
    try:
        data = snapshot(env)
    except Exception:
        publish(path, {"v": 0, "generated_at": int(time.time()), "frontend_peer_ips": [],
                       "forwarded_proxy_ips": [], "error": "topology_unavailable"})
        raise
    publish(path, data)


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("must run as root")
    try:
        refresh(os.environ)
    except Exception:
        raise SystemExit("proxy peer topology unavailable; trusted peer snapshot cleared")
