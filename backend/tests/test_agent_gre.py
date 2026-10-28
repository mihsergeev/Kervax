"""GRE до панели (app/gre.py, api/gre.py).

Нода, чье соединение до панели режут по объему, ходит в нее через GRE-туннель до хоста панели.
Галочка в настройках - своя /30 без пересечений, список туннелей для хостового скрипта, ручки
helper'а gre-setup и настоящий адрес ноды в карточке вместо туннельного."""
import os
import time
from pathlib import Path

import httpx

from app import gre, home_mute
from app.models import Server
from app.setup_scripts import gaps, setup_needed

REPORT = {"hostname": "h", "os": "Ubuntu", "agent_version": "2.56", "cpu_percent": 1.0,
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


def _data() -> Path:
    return Path(os.environ["KERVAX_DATA_DIR"])


def _tunnels() -> list[str]:
    return (_data() / "gre_tunnels").read_text(encoding="utf-8").splitlines()


def _applied(*lines: str, age: int = 0) -> None:
    (_data() / "gre_tunnels.applied").write_text(
        f"# kervax-gre fw=ufw ts={int(time.time()) - age}\n" + "".join(x + "\n" for x in lines), encoding="utf-8")


async def _gre(client, auth_headers, name="dpi-a", xff="198.51.100.10") -> tuple[int, str]:
    sid, tok = await _server(client, auth_headers, name)
    await _report(client, tok, xff=xff)
    r = await client.put(f"/api/servers/{sid}/gre", json={"enabled": True}, headers=auth_headers)
    assert r.status_code == 200, r.text
    return sid, tok


async def test_switching_on_gives_each_server_its_own_net(client: httpx.AsyncClient, auth_headers):
    sid, tok = await _server(client, auth_headers, "dpi-a")
    # публичный адрес не известен - туннель вести некуда
    r = await client.put(f"/api/servers/{sid}/gre", json={"enabled": True}, headers=auth_headers)
    assert r.status_code == 400 and "публичный адрес" in r.json()["detail"]
    # агент за прокси: GRE ему не поможет
    await _report(client, tok, proxy="proxy.example:3128")
    r = await client.put(f"/api/servers/{sid}/gre", json={"enabled": True}, headers=auth_headers)
    assert r.status_code == 400 and "через прокси" in r.json()["detail"]
    await _report(client, tok)
    r = await client.put(f"/api/servers/{sid}/gre", json={"enabled": True}, headers=auth_headers)
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["gre_enabled"] is True
    assert (s["gre"]["state"], s["gre"]["net"], s["gre"]["panel_addr"], s["gre"]["node_addr"]) == (
        "no_helper", "10.254.0.0/30", "10.254.0.1", "10.254.0.2")

    b, _ = await _gre(client, auth_headers, "dpi-b", xff="198.51.100.11")
    assert (await _get(client, auth_headers, b))["gre"]["net"] == "10.254.0.4/30"
    assert _tunnels() == ["# kervax-gre v1", f"kvxg{sid} 198.51.100.10 10.254.0.1/30 10.254.0.2",
                          f"kvxg{b} 198.51.100.11 10.254.0.5/30 10.254.0.6"]

    # выключили - строка уходит, сеть остается за сервером: включили снова - те же адреса
    await client.put(f"/api/servers/{sid}/gre", json={"enabled": False}, headers=auth_headers)
    assert _tunnels() == ["# kervax-gre v1", f"kvxg{b} 198.51.100.11 10.254.0.5/30 10.254.0.6"]
    assert (await _get(client, auth_headers, sid))["gre"] is None
    c, _ = await _gre(client, auth_headers, "dpi-c", xff="198.51.100.12")
    assert (await _get(client, auth_headers, c))["gre"]["net"] == "10.254.0.8/30"
    r = await client.put(f"/api/servers/{sid}/gre", json={"enabled": True}, headers=auth_headers)
    assert r.json()["gre"]["net"] == "10.254.0.0/30"

    # удалили сервер - хост панели снимает его туннель
    assert (await client.delete(f"/api/servers/{b}", headers=auth_headers)).status_code in (200, 204)
    assert not any(x.startswith(f"kvxg{b} ") for x in _tunnels())


async def test_report_through_the_tunnel_shows_the_real_address(client: httpx.AsyncClient, auth_headers):
    sid, tok = await _gre(client, auth_headers)
    await _report(client, tok, xff="10.254.0.2", connect="10.254.0.1")
    s = await _get(client, auth_headers, sid)
    assert s["external_ip"] == "198.51.100.10"
    # чужой туннельный адрес - не адрес ноды, прежний остается
    await _report(client, tok, xff="10.254.0.6")
    assert (await _get(client, auth_headers, sid))["external_ip"] == "198.51.100.10"
    # ходит напрямую - как всегда, адрес запроса
    await _report(client, tok, xff="198.51.100.20")
    assert (await _get(client, auth_headers, sid))["external_ip"] == "198.51.100.20"


async def test_helper_gets_the_host_address_only_when_the_host_is_ready(client: httpx.AsyncClient, auth_headers):
    sid, tok = await _gre(client, auth_headers)
    hdr = {"Authorization": f"Bearer {tok}"}
    assert (await client.post("/api/agent/gre-wanted", content=tok)).text == "yes"
    ans = (await client.post("/api/agent/gre", json={"version": "0.1"}, headers=hdr)).json()
    assert ans == {"enabled": True, "remote": "", "local_addr": "10.254.0.2", "panel_addr": "10.254.0.1",
                   "prefix": 30, "mtu": 1476}
    # хостовый скрипт свою сторону не поднял: в карточке - что ждем хост панели
    assert (await _get(client, auth_headers, sid))["gre"]["state"] == "host"

    _applied(f"kvxg{sid} 198.51.100.10 203.0.113.5 ok")
    ans = (await client.post("/api/agent/gre", json={"version": "0.1", "up": True, "local_ip": "198.51.100.10",
                                                     "firewall": "ufw"}, headers=hdr)).json()
    assert ans["remote"] == "203.0.113.5"
    assert (await _get(client, auth_headers, sid))["gre"]["state"] == "up"
    await _report(client, tok, xff="10.254.0.2")
    g = (await _get(client, auth_headers, sid))["gre"]
    assert (g["state"], g["host"], g["firewall"]) == ("ok", True, "ufw")

    # нода шлет GRE с другого адреса - хост панели переделывает туннель на него
    await client.post("/api/agent/gre", json={"up": True, "local_ip": "198.51.100.30"}, headers=hdr)
    assert f"kvxg{sid} 198.51.100.30 10.254.0.1/30 10.254.0.2" in _tunnels()
    # за NAT GRE не пройдет: helper говорит, панель показывает
    await client.post("/api/agent/gre", json={"nat": True, "local_ip": "10.0.0.5",
                                              "error": "behind NAT"}, headers=hdr)
    g = (await _get(client, auth_headers, sid))["gre"]
    assert g["state"] == "nat" and g["peer"] == "198.51.100.30"

    # ответ хоста давно не обновлялся - скрипт на хосте не работает
    _applied(f"kvxg{sid} 198.51.100.30 203.0.113.5 ok", age=3600)
    await client.post("/api/agent/gre", json={"up": True, "local_ip": "198.51.100.30"}, headers=hdr)
    assert (await _get(client, auth_headers, sid))["gre"]["state"] == "host"

    await client.put(f"/api/servers/{sid}/gre", json={"enabled": False}, headers=auth_headers)
    assert (await client.post("/api/agent/gre-wanted", content=tok)).text == "no"
    assert (await client.post("/api/agent/gre", json={}, headers=hdr)).json() == {"enabled": False}
    # чужой токен - 401: helper по нему снимает туннель, условие установки ложно
    assert (await client.post("/api/agent/gre", json={}, headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await client.post("/api/agent/gre-wanted", content="nope")).status_code == 401


async def test_probe_gives_random_bytes_only_to_agents(client: httpx.AsyncClient, auth_headers):
    _, tok = await _server(client, auth_headers, "probe-a")
    hdr = {"Authorization": f"Bearer {tok}"}
    a = await client.get("/api/agent/probe?n=102400", headers=hdr)
    b = await client.get("/api/agent/probe?n=102400", headers=hdr)
    assert a.status_code == 200 and len(a.content) == 102400 and a.content != b.content
    assert (await client.get("/api/agent/probe?n=8192")).status_code == 401
    assert (await client.get("/api/agent/probe?n=999999", headers=hdr)).status_code == 422


def test_need_only_when_big_pieces_freeze_and_small_ones_pass():
    def p(small_ok, big_ok, n=6):
        return {"net_probe": {"small_ok": small_ok, "small_n": n, "big_ok": big_ok, "big_n": n, "big_kb": 100}}
    assert gre.need(p(6, 0)) == {"big_ok": 0, "big_n": 6, "small_ok": 6, "small_n": 6, "kb": 100}
    assert gre.need(p(5, 1))
    assert gre.need(p(6, 3)) is None      # большое проходит через раз - это не тот случай
    assert gre.need(p(2, 0)) is None      # и мелкое не проходит - канал просто плохой
    assert gre.need(p(3, 0, n=3)) is None  # замеров мало
    assert gre.need({}) is None


async def test_card_and_home_show_that_the_server_needs_gre(client: httpx.AsyncClient, auth_headers):
    sid, tok = await _server(client, auth_headers, "dpi-a")
    probe = {"small_ok": 6, "small_n": 6, "big_ok": 0, "big_n": 6, "big_kb": 100, "at": 1}
    await _report(client, tok, net_probe=probe)
    s = await _get(client, auth_headers, sid)
    assert s["gre_need"]["big_n"] == 6 and s["gre_enabled"] is False
    assert "gre-need" in s["home_mutes"]
    srv = Server(id=1, name="x", last_report={"net_probe": probe}, gre_enabled=True)
    assert home_mute.SIGS["gre-need"](srv, None, {}) == ""   # включили - пункта нет
    srv.last_report = {}
    assert home_mute.SIGS["gre-need"](srv, None, {}) is None


def test_helper_is_needed_only_where_gre_is_on():
    rep = {"agent_version": "2.56", "setup_versions": {}}
    cur = {"gre-setup": "0.1", "timesync-setup": "0.3"}
    assert setup_needed("gre-setup", rep) is False
    assert setup_needed("gre-setup", rep, gre=True) is True
    assert "helper:gre-setup" not in {k for k, _ in gaps(rep, cur)}
    assert "helper:gre-setup" in {k for k, _ in gaps(rep, cur, gre=True)}


def test_host_answer_is_parsed_and_only_public_addresses_count(tmp_path):
    (tmp_path / "gre_tunnels.applied").write_text(
        "# kervax-gre fw=firewalld ts=100\nkvxg3 198.51.100.10 203.0.113.5 ok\n"
        "kvxg4 198.51.100.11 - err no route to the node\njunk line\n", encoding="utf-8")
    st = gre.host_state(str(tmp_path))
    assert st["fw"] == "firewalld" and st["ts"] == 100
    assert st["ifaces"]["kvxg3"] == {"peer": "198.51.100.10", "local": "203.0.113.5", "ok": True, "error": ""}
    assert st["ifaces"]["kvxg4"]["error"] == "no route to the node" and not st["ifaces"]["kvxg4"]["ok"]
    assert gre.panel_ip(Server(id=3), st) == "203.0.113.5"
    assert gre.panel_ip(Server(id=4), st) == ""
    assert gre.is_public("203.0.113.5") is True  # документационные сети - не про NAT
    assert gre.is_public("10.0.0.5") is False and gre.is_public("100.64.1.1") is False
    assert gre.is_public("2001:db8::1") is False  # GRE только поверх IPv4
