"""Адрес ограничивает конкретный токен; прокси не дают подделать источник."""
import json
from pathlib import Path
import shlex
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import agent_auth, config
from app.models import Check, DockerCommand, Server

PRIMARY = "198.51.100.20"
OTHER = "198.51.100.21"
FRONTEND = "172.21.0.3"
CADDY = "172.22.0.2"
REPORT = {"hostname": "sample-node", "os": "Ubuntu", "agent_version": "2.56",
          "cpu_percent": 1, "mem_used": 1, "mem_total": 2, "disks": []}


def test_production_uvicorn_launchers_preserve_raw_socket_peer():
    root = Path(__file__).resolve().parents[2]
    docker = (root / "backend/Dockerfile").read_text(encoding="utf-8")
    command = next(json.loads(line[4:])[-1] for line in docker.splitlines() if line.startswith("CMD "))
    scale = (root / "compose.scale.yml").read_text(encoding="utf-8")
    commands = [command, *(line.strip()[2:] for line in scale.splitlines() if "exec uvicorn " in line)]
    assert len(commands) == 2, "Check the standalone and scaled production launchers"
    for value in commands:
        flags = shlex.split(value)
        assert "--no-proxy-headers" in flags
        assert "--proxy-headers" not in flags


def _request(ip, xff=None, **headers):
    items = [(name.lower().replace("_", "-").encode(), value.encode()) for name, value in headers.items()]
    if xff is not None:
        items.append((b"x-forwarded-for", xff.encode()))
    return Request({"type": "http", "client": (ip, 12345), "headers": items})


def _server(**values):
    return SimpleNamespace(**({"id": 1, "agent_ip": PRIMARY, "agent_source_ips": [],
                              "gre_enabled": False, "gre_net": None, "gre_peer": "",
                              "relay_via_id": None, "external_ip": OTHER, "last_report": {}} | values))


def _snapshot(path, **values):
    path.mkdir(parents=True, exist_ok=True)
    (path / agent_auth.PROXY_FILE).write_text(json.dumps({
        "v": 1, "generated_at": time.time(), "frontend_peer_ips": [FRONTEND],
        "forwarded_proxy_ips": [CADDY], **values,
    }), encoding="utf-8")


@pytest.fixture
def guard(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_auth, "get_settings", lambda: SimpleNamespace(
        agent_source_guard=True, data_dir=str(tmp_path)))
    _snapshot(tmp_path, frontend_peer_ips=[], forwarded_proxy_ips=[])
    return tmp_path


async def _denied(server, request, session=None):
    with pytest.raises(HTTPException) as caught:
        await agent_auth.require_agent_source(server, request, session)
    assert caught.value.status_code == 403
    assert caught.value.detail == "Источник запроса агента не разрешен"


async def test_direct_source_is_per_server_and_ignores_untrusted_headers(guard):
    server = _server()
    assert await agent_auth.require_agent_source(server, _request(PRIMARY, "malformed"), None) == PRIMARY
    await _denied(server, _request(OTHER, PRIMARY, x_real_ip=PRIMARY))
    # Адрес чужой ноды и прошлый наблюдаемый адрес не дают прав на этот токен.
    await _denied(server, _request(server.external_ip))


async def test_only_current_primary_and_admin_extras_are_authoritative(guard):
    server = _server(agent_source_ips=["203.0.113.40"], gre_peer=OTHER,
                     last_report={"proxy": "proxy.example:3128", "ips": [OTHER]})
    assert await agent_auth.require_agent_source(server, _request("203.0.113.40"), None) == "203.0.113.40"
    await _denied(server, _request(OTHER))
    server.agent_ip = OTHER
    await _denied(server, _request(PRIMARY))
    assert await agent_auth.require_agent_source(server, _request(OTHER), None) == OTHER


async def test_gre_allows_only_own_enabled_tunnel_not_stale_public_peer(guard):
    server = _server(gre_enabled=True, gre_net="10.254.0.0/30", gre_peer=OTHER)
    assert await agent_auth.require_agent_source(server, _request("10.254.0.2"), None) == "10.254.0.2"
    await _denied(server, _request("10.254.0.6"))
    await _denied(server, _request(OTHER))
    server.gre_enabled = False
    await _denied(server, _request("10.254.0.2"))


async def test_relay_requires_configured_enabled_node_and_admin_addresses(guard):
    relay = _server(id=2, agent_ip="203.0.113.40", agent_source_ips=["203.0.113.41"],
                    external_ip="203.0.113.42", enabled=True, relay_enabled=True)
    class Session:
        async def get(self, model, sid):
            assert model is Server and sid == 2
            return relay
    session = Session()
    server = _server(relay_via_id=2)
    for address in (relay.agent_ip, *relay.agent_source_ips):
        assert await agent_auth.require_agent_source(server, _request(address), session) == address
    await _denied(server, _request(relay.external_ip), session)
    relay.relay_enabled = False
    await _denied(server, _request(relay.agent_ip), session)
    relay.relay_enabled, relay.enabled = True, False
    await _denied(server, _request(relay.agent_ip), session)


@pytest.mark.parametrize("address,primary", [
    ("2001:0db8:0000::20", "2001:db8::20"),
    ("::ffff:198.51.100.20", PRIMARY),
    (PRIMARY, "::ffff:198.51.100.20"),
])
async def test_numeric_ipv6_and_mapped_addresses_are_canonical(guard, address, primary):
    assert await agent_auth.require_agent_source(_server(agent_ip=primary), _request(address), None)


async def test_blank_legacy_and_deployment_opt_out(guard, monkeypatch):
    assert await agent_auth.require_agent_source(_server(agent_ip=""), _request(OTHER), None) is None
    await _denied(_server(agent_ip="", agent_source_ips=[PRIMARY]), _request(OTHER))
    monkeypatch.setattr(agent_auth, "get_settings", lambda: SimpleNamespace(agent_source_guard=False))
    assert await agent_auth.require_agent_source(_server(), _request(OTHER), None) is None


@pytest.mark.parametrize("primary", [
    "node.example", "198.51.100.0/24", "fe80::1%eth0", "::ffff:0.0.0.0", "::ffff:224.0.0.1",
])
async def test_invalid_configured_address_cannot_turn_binding_into_legacy(guard, primary):
    await _denied(_server(agent_ip=primary), _request(PRIMARY))


async def test_exact_proxy_roles_preserve_private_gre_source(guard):
    _snapshot(guard)
    request = _request(FRONTEND, f"{PRIMARY}, {CADDY}")
    assert await agent_auth.require_agent_source(_server(), request, None) == PRIMARY
    server = _server(gre_enabled=True, gre_net="10.254.0.0/30")
    assert await agent_auth.require_agent_source(server, _request(FRONTEND, f"10.254.0.2, {CADDY}"), None) == "10.254.0.2"
    # Строка слева от реального клиента не выбирается даже при совпадении с разрешенным IP.
    await _denied(server, _request(FRONTEND, f"{PRIMARY}, {OTHER}, {CADDY}"))


@pytest.mark.parametrize("peer,xff", [
    ("172.21.0.8", f"{PRIMARY}, {CADDY}"),  # сосед backend-сети
    (CADDY, PRIMARY),                        # Caddy не frontend peer
    (FRONTEND, f"{PRIMARY}, 172.22.0.8"),   # сосед edge-сети
    (FRONTEND, f"{PRIMARY}, 172.22.0.1"),   # Docker gateway не доверенный прокси
    (FRONTEND, f"{PRIMARY}, malformed, {CADDY}"),
    (FRONTEND, f"{PRIMARY}, , {CADDY}"),
    (FRONTEND, f"{PRIMARY}:123, {CADDY}"),
    (FRONTEND, f"fe80::1%eth0, {CADDY}"),
    (FRONTEND, CADDY),
    (FRONTEND, None),
])
async def test_spoofed_and_malformed_forwarding_is_denied(guard, peer, xff):
    _snapshot(guard)
    await _denied(_server(), _request(peer, xff, x_real_ip=PRIMARY))


@pytest.mark.parametrize("bad", [
    {"generated_at_offset": -181}, {"generated_at_offset": 60},
    {"generated_at": float("nan")}, {"generated_at": True}, {"v": True}, {"v": 0},
    {"frontend_peer_ips": ["172.21.0.0/16"]}, {"forwarded_proxy_ips": ["unknown"]},
    {"forwarded_proxy_ips": [FRONTEND]},
])
async def test_invalid_or_stale_snapshot_never_authorizes_headers(guard, bad):
    bad = dict(bad)
    if "generated_at_offset" in bad:
        bad["generated_at"] = time.time() + bad.pop("generated_at_offset")
    _snapshot(guard, **bad)
    await _denied(_server(), _request(FRONTEND, f"{PRIMARY}, {CADDY}"))
    await _denied(_server(), _request(PRIMARY))
    await _denied(_server(agent_ip=FRONTEND), _request(FRONTEND))


async def test_missing_snapshot_and_duplicate_xff_fail_closed_for_proxy(guard):
    (guard / agent_auth.PROXY_FILE).unlink()
    await _denied(_server(), _request(FRONTEND, PRIMARY))
    await _denied(_server(agent_ip=FRONTEND), _request(FRONTEND))
    await _denied(_server(), _request(PRIMARY))
    _snapshot(guard)
    request = Request({"type": "http", "client": (FRONTEND, 123),
                       "headers": [(b"x-forwarded-for", PRIMARY.encode()),
                                   (b"x-forwarded-for", OTHER.encode())]})
    await _denied(_server(), request)


async def _new_server(client, auth_headers, primary=PRIMARY):
    r = await client.post("/api/servers", headers=auth_headers,
                          json={"name": "sample-node", "agent_ip": primary})
    assert r.status_code == 201, r.text
    data = r.json()
    return data["server"]["id"], data["token"]


async def _call(client, source, token, endpoint="report", payload=None, method="POST", headers=None):
    transport = httpx.ASGITransport(app=client._transport.app, client=(source, 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        kwargs = {"headers": {"Authorization": f"Bearer {token}", **(headers or {})}}
        if endpoint.endswith("-wanted"):
            kwargs["content"] = token
        elif method != "GET":
            kwargs["json"] = REPORT if payload is None else payload
        return await c.request(method, "/api/agent/" + endpoint, **kwargs)


def _enable_guard(monkeypatch):
    monkeypatch.setenv("KERVAX_AGENT_SOURCE_GUARD", "true")
    config.get_settings.cache_clear()
    _snapshot(Path(config.get_settings().data_dir), frontend_peer_ips=[], forwarded_proxy_ips=[])


async def test_ip_change_immediate_across_requests_preserves_queue_and_last_seen(client, auth_headers, monkeypatch):
    sid, token = await _new_server(client, auth_headers)
    _enable_guard(monkeypatch)
    assert (await _call(client, PRIMARY, token)).status_code == 200
    factory = client._transport.app.state.session_factory
    async with factory() as session:
        before = await session.get(Server, sid)
        last_seen = before.last_seen
        command = DockerCommand(server_id=sid, container="sample", action="logs")
        session.add(command)
        await session.commit()
        command_id = command.id
    r = await client.patch(f"/api/servers/{sid}", headers=auth_headers, json={"agent_ip": OTHER})
    assert r.status_code == 200, r.text
    r = await _call(client, PRIMARY, token, headers={"X-Forwarded-For": OTHER})
    assert r.status_code == 403
    r = await _call(client, PRIMARY, token, endpoint="commands", method="GET")
    assert r.status_code == 403
    async with factory() as session:
        server = await session.get(Server, sid)
        assert server.last_seen == last_seen
        assert (await session.get(DockerCommand, command_id)).status == "pending"
    # Новый процесс/запрос видит новое значение из БД без кеша старого IP.
    assert (await _call(client, OTHER, token)).status_code == 200


ENDPOINTS = [
    ("report", "POST", REPORT), ("packages", "POST", {"h": "hash", "part": 0, "parts": 1}),
    ("commands", "GET", None), ("site-probe-result", "POST", {"site_probes": []}),
    ("going-down", "POST", {}), ("docker-result", "POST", {"id": 1, "ok": True}),
    ("kube-result", "POST", {"id": 1, "ok": True}), ("backup-result", "POST", {"id": 1, "ok": True}),
    ("alert-recheck-result", "POST", {"id": "a" * 32, "ok": True}),
    ("relay", "POST", {}), ("relay-wanted", "POST", None), ("gre", "POST", {}),
    ("gre-wanted", "POST", None), ("probe", "GET", None), ("mirror-wanted", "POST", None),
]


@pytest.mark.parametrize("endpoint,method,payload", ENDPOINTS)
async def test_every_authenticated_agent_route_rejects_wrong_source_after_token_auth(
    client, auth_headers, monkeypatch, endpoint, method, payload,
):
    sid, token = await _new_server(client, auth_headers)
    _enable_guard(monkeypatch)
    r = await _call(client, OTHER, token, endpoint, payload, method)
    assert r.status_code == 403, (endpoint, r.text)
    assert r.json() == {"detail": "Источник запроса агента не разрешен"}
    r = await _call(client, OTHER, "invalid-token", endpoint, payload, method)
    assert r.status_code == 401, (endpoint, r.text)
    async with client._transport.app.state.session_factory() as session:
        server = await session.get(Server, sid)
        assert server.last_seen is None and server.last_report is None
        assert server.relay_seen_at is None and server.gre_seen_at is None


async def test_report_uses_validated_source_not_leftmost_untrusted_xff(client, auth_headers, monkeypatch):
    sid, token = await _new_server(client, auth_headers)
    _enable_guard(monkeypatch)
    _snapshot(Path(config.get_settings().data_dir))
    r = await _call(client, FRONTEND, token, headers={"X-Forwarded-For": f"{OTHER}, {PRIMARY}, {CADDY}"})
    assert r.status_code == 200, r.text
    async with client._transport.app.state.session_factory() as session:
        assert (await session.get(Server, sid)).external_ip == PRIMARY


async def test_agent_cannot_claim_proxy_or_extra_source_permissions_in_report(client, auth_headers, monkeypatch):
    sid, token = await _new_server(client, auth_headers)
    _enable_guard(monkeypatch)
    claimed = {**REPORT, "proxy": "proxy.example:3128", "agent_source_ips": [OTHER], "ips": [OTHER]}
    assert (await _call(client, OTHER, token, payload=claimed)).status_code == 403
    assert (await _call(client, PRIMARY, token, payload=claimed)).status_code == 200
    assert (await _call(client, OTHER, token, endpoint="commands", method="GET")).status_code == 403
    async with client._transport.app.state.session_factory() as session:
        assert (await session.get(Server, sid)).agent_source_ips == []


async def test_public_install_resources_and_push_keep_separate_access(client, auth_headers, monkeypatch, tmp_path):
    await _new_server(client, auth_headers)
    _enable_guard(monkeypatch)
    monkeypatch.setenv("KERVAX_AGENT_DIST_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    (tmp_path / "install.sh").write_text("#!/bin/sh\nprintf safe", encoding="utf-8")
    (tmp_path / "manifest.sig").write_text("public-test-signature", encoding="utf-8")
    for path in ("install.sh", "manifest.sig"):
        assert (await client.get("/api/agent/" + path)).status_code == 200
    push_token = "example-push-token-1234"
    async with client._transport.app.state.session_factory() as session:
        check = Check(name="sample-cron", type="push", target="", push_token=push_token)
        session.add(check)
        await session.commit()
        check_id = check.id
    assert (await client.post("/api/agent/push/" + push_token)).status_code == 200
    async with client._transport.app.state.session_factory() as session:
        assert (await session.get(Check, check_id)).push_at is not None
