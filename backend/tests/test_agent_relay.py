"""Ретранслятор для агентов (app/relay.py, api/relay.py).

Сервер, который сам достает до панели, держит прокси до нее для агентов, чей адрес режут на пути
к панели. Галочка в настройках - helper relay-setup спрашивает панель, что держать, и пускает
только серверы, выбравшие этот ретранслятор в окне команды установки."""
import httpx

from app.setup_scripts import gaps, setup_needed

REPORT = {"hostname": "h", "os": "Ubuntu", "agent_version": "2.44", "cpu_percent": 1.0,
          "mem_used": 1, "mem_total": 2, "disks": []}


async def _server(client, auth_headers, name, **extra) -> tuple[int, str]:
    r = await client.post("/api/servers", json={"name": name, **extra}, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()["server"]["id"], r.json()["token"]


async def _report(client, token, xff="198.51.100.10", **extra):
    r = await client.post("/api/agent/report", json={**REPORT, **extra},
                          headers={"Authorization": f"Bearer {token}", "X-Forwarded-For": f"{xff}, 172.18.0.1"})
    assert r.status_code == 200, r.text


async def _get(client, auth_headers, sid) -> dict:
    return next(s for s in (await client.get("/api/servers", headers=auth_headers)).json() if s["id"] == sid)


async def _relay(client, auth_headers) -> tuple[int, str]:
    rid, rtok = await _server(client, auth_headers, "relay-a")
    await _report(client, rtok)
    r = await client.put(f"/api/servers/{rid}/relay", json={"enabled": True}, headers=auth_headers)
    assert r.status_code == 200, r.text
    return rid, rtok


async def test_relay_only_on_a_server_that_reaches_the_panel_directly(client: httpx.AsyncClient, auth_headers):
    sid, tok = await _server(client, auth_headers, "relay-a")
    r = await client.put(f"/api/servers/{sid}/relay", json={"enabled": True}, headers=auth_headers)
    assert r.status_code == 400 and "не выходил на связь" in r.json()["detail"]
    # агент сам ходит через прокси - напрямую сервер до панели не достает
    await _report(client, tok, proxy="proxy.example:3128")
    r = await client.put(f"/api/servers/{sid}/relay", json={"enabled": True}, headers=auth_headers)
    assert r.status_code == 400 and "через прокси" in r.json()["detail"]
    await _report(client, tok)
    r = await client.put(f"/api/servers/{sid}/relay", json={"enabled": True, "port": 9443}, headers=auth_headers)
    assert r.status_code == 200
    s = r.json()
    assert s["relay_enabled"] is True and s["relay_port"] == 9443
    assert s["relay"]["state"] == "no_helper" and s["relay"]["port"] == 9443
    r = await client.put(f"/api/servers/{sid}/relay", json={"enabled": True, "port": 80}, headers=auth_headers)
    assert r.status_code == 422


async def test_password_never_leaves_in_the_server_list(client: httpx.AsyncClient, auth_headers):
    rid, rtok = await _relay(client, auth_headers)
    cfg = (await client.post("/api/agent/relay", json={}, headers={"Authorization": f"Bearer {rtok}"})).json()
    assert cfg["password"] and cfg["user"]
    listing = (await client.get("/api/servers", headers=auth_headers)).text
    assert cfg["password"] not in listing
    one = (await client.get(f"/api/servers/{rid}", headers=auth_headers)).text
    assert cfg["password"] not in one


async def test_choosing_a_relay_lets_the_server_in(client: httpx.AsyncClient, auth_headers):
    rid, rtok = await _relay(client, auth_headers)
    cid, _ = await _server(client, auth_headers, "blocked-a")
    # без внешнего адреса ретранслятор пускать некого
    r = await client.post(f"/api/servers/{cid}/install-via", json={"mode": "relay", "relay_id": rid},
                          headers=auth_headers)
    assert r.status_code == 400
    r = await client.post(f"/api/servers/{cid}/install-via",
                          json={"mode": "relay", "relay_id": rid, "ip": "203.0.113.40"}, headers=auth_headers)
    assert r.status_code == 200, r.text
    proxy = r.json()["proxy"]
    assert proxy.startswith("http://kervax:") and proxy.endswith("@198.51.100.10:8790")

    cfg = (await client.post("/api/agent/relay", json={}, headers={"Authorization": f"Bearer {rtok}"})).json()
    assert cfg == {"enabled": True, "port": 8790, "user": "kervax", "password": cfg["password"],
                   "clients": ["203.0.113.40"]}
    assert f"kervax:{cfg['password']}@" in proxy
    s = await _get(client, auth_headers, cid)
    assert s["relay_via_id"] == rid and s["agent_ip"] == "203.0.113.40"

    # напрямую - адрес из списка ретранслятора уходит
    r = await client.post(f"/api/servers/{cid}/install-via", json={"mode": "direct"}, headers=auth_headers)
    assert r.json()["proxy"] == ""
    cfg = (await client.post("/api/agent/relay", json={}, headers={"Authorization": f"Bearer {rtok}"})).json()
    assert cfg["clients"] == []


async def test_location_and_bad_choices(client: httpx.AsyncClient, auth_headers):
    cid, _ = await _server(client, auth_headers, "blocked-a")
    r = await client.post("/api/locations", json={"name": "loc-a", "url": "http://user:secret@proxy.example:3128"},
                          headers=auth_headers)
    assert r.status_code in (200, 201), r.text
    lid = r.json()["id"]
    r = await client.post(f"/api/servers/{cid}/install-via", json={"mode": "location", "location_id": lid},
                          headers=auth_headers)
    assert r.json()["proxy"] == "http://user:secret@proxy.example:3128"
    # ретранслятор выключен или не выбран, неверный IP
    off, _ = await _server(client, auth_headers, "plain-a")
    for body in ({"mode": "relay", "relay_id": off, "ip": "203.0.113.40"}, {"mode": "relay", "ip": "203.0.113.40"},
                 {"mode": "custom", "ip": "not-an-ip"}, {"mode": "location"}):
        r = await client.post(f"/api/servers/{cid}/install-via", json=body, headers=auth_headers)
        assert r.status_code == 400, body


async def test_helper_reports_and_switching_off(client: httpx.AsyncClient, auth_headers):
    rid, rtok = await _relay(client, auth_headers)
    hdr = {"Authorization": f"Bearer {rtok}"}
    await client.post("/api/agent/relay", headers=hdr, json={
        "version": "0.1", "listening": True, "firewall": "ufw", "error": "", "conns": 7, "denied": "x", "clients": 2})
    v = (await _get(client, auth_headers, rid))["relay"]
    assert (v["state"], v["firewall"], v["conns"], v["denied"], v["clients"]) == ("ok", "ufw", 7, 0, 2)
    await client.post("/api/agent/relay", headers=hdr, json={"error": "port 8790: Address already in use"})
    assert (await _get(client, auth_headers, rid))["relay"]["state"] == "error"

    r = await client.post("/api/agent/relay-wanted", content=rtok, headers={"Content-Type": "text/plain"})
    assert r.text == "yes"
    await client.put(f"/api/servers/{rid}/relay", json={"enabled": False}, headers=auth_headers)
    assert (await client.post("/api/agent/relay-wanted", content=rtok)).text == "no"
    assert (await client.post("/api/agent/relay", json={}, headers=hdr)).json() == {"enabled": False}
    assert (await _get(client, auth_headers, rid))["relay"] is None
    # чужой токен - 401: helper по нему снимает ретранслятор, условие установки ложно
    assert (await client.post("/api/agent/relay", json={}, headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await client.post("/api/agent/relay-wanted", content="nope")).status_code == 401


async def test_deleting_the_relay_frees_its_clients(client: httpx.AsyncClient, auth_headers):
    rid, _ = await _relay(client, auth_headers)
    cid, _ = await _server(client, auth_headers, "blocked-a")
    await client.post(f"/api/servers/{cid}/install-via", json={"mode": "relay", "relay_id": rid, "ip": "203.0.113.40"},
                      headers=auth_headers)
    assert (await client.delete(f"/api/servers/{rid}", headers=auth_headers)).status_code in (200, 204)
    assert (await _get(client, auth_headers, cid))["relay_via_id"] is None


def test_helper_is_needed_only_where_the_relay_is_on():
    rep = {"agent_version": "2.44", "setup_versions": {}}
    cur = {"relay-setup": "0.1", "timesync-setup": "0.3"}
    assert setup_needed("relay-setup", rep) is False
    assert setup_needed("relay-setup", rep, relay=True) is True
    assert "helper:relay-setup" not in {k for k, _ in gaps(rep, cur)}
    assert "helper:relay-setup" in {k for k, _ in gaps(rep, cur, relay=True)}
