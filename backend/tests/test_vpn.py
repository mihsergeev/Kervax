"""VPN на сервере: бейдж по отчету агента (образы контейнеров, интерфейсы) и клиенты от helper'а
vpn-setup. Разбор dump, конфигов и статус-файлов - python внутри helper'а, тест берет его оттуда же."""
import json
import re
from pathlib import Path

from app import setup_scripts, vpn

SRC = (Path(__file__).resolve().parents[2] / "agent" / "vpn-setup.sh").read_text(encoding="utf-8")
NOW = 1_800_000_000


def _helper_py():
    body = SRC.split("<<'PY'\n", 1)[1].split("\nPY\nthen", 1)[0]
    assert body.rstrip().endswith("main()")
    ns: dict = {}
    exec(compile(body.rstrip()[: -len("main()")], "vpn-setup", "exec"), ns)
    return ns


H = _helper_py()

PRIV = "aPrivateKeyOfTheServerThatMustNeverLeave000="
PSK = "aPresharedKeyThatMustNeverLeaveTheNode00000="
# интерфейс AmneziaWG: после fwmark идут параметры обфускации, у пиров 9 полей
DUMP = "\n".join([
    "\t".join(["awg0", PRIV, "srvpub=", "51820", "off", "4", "40", "70", "0", "0"]),
    "\t".join(["awg0", "alicepub=", PSK, "203.0.113.7:51000", "10.8.1.2/32", str(NOW - 30), "1000", "5000", "off"]),
    "\t".join(["awg0", "bobpub=", PSK, "(none)", "10.8.1.3/32", "0", "0", "0", "off"]),
    "\t".join(["awg0", "carolpub=", PSK, "[2001:db8::5]:4000", "10.8.1.4/32,fd00::4/128", str(NOW - 900), "7", "9", "25"]),
])


def test_dump_drops_keys():
    d = H["parse_dump"](DUMP)
    assert d["awg0"]["port"] == 51820
    peers = d["awg0"]["peers"]
    assert [p["ip"] for p in peers] == ["10.8.1.2", "10.8.1.3", "10.8.1.4"]
    assert [p["ep"] for p in peers] == ["203.0.113.7", "", "2001:db8::5"]
    sv = H["wg_service"]("awg", "amnezia-awg2", d, {"alicepub=": "alice"}, {}, 123)
    out = json.dumps(sv)
    assert PRIV not in out and PSK not in out and "alicepub=" not in out
    assert sv[0]["role"] == "server" and sv[0]["n"] == 3 and sv[0]["pid"] == 123
    assert sv[0]["clients"][0] == {"name": "alice", "ip": "10.8.1.2", "hs": NOW - 30, "rx": 1000, "tx": 5000,
                                   "ep": "203.0.113.7"}


def test_wg_host_role_by_config():
    """пир без Endpoint в конфиге подключается к нам - сервер; туннель к двум узлам с Endpoint - нет"""
    conf = """[Interface]
PrivateKey = x
ListenPort = 51820

# alice
[Peer]
PublicKey = alicepub=
AllowedIPs = 10.8.1.2/32

[Peer]
# Name = bob
PublicKey = bobpub=
AllowedIPs = 10.8.1.3/32
"""
    peers = H["conf_peers"](conf)
    assert peers == {"alicepub=": ("alice", False), "bobpub=": ("bob", False)}
    d = H["parse_dump"](DUMP)
    sv = H["wg_service"]("wg", "host", d, {}, peers)[0]
    assert sv["role"] == "server" and sv["clients"][0]["name"] == "alice"
    tunnel = """[Peer]
PublicKey = alicepub=
Endpoint = 198.51.100.1:51820
AllowedIPs = 10.9.0.0/24
[Peer]
PublicKey = bobpub=
Endpoint = 198.51.100.2:51820
AllowedIPs = 10.9.1.0/24
"""
    # пир с маршрутом по умолчанию - наш выход, а не клиент
    up = "\t".join(["awg0", "uppub=", PSK, "198.51.100.9:51820", "0.0.0.0/0", str(NOW - 5), "1", "2", "25"])
    sv = H["wg_service"]("wg", "host", H["parse_dump"](DUMP + "\n" + up), {}, peers)[0]
    assert sv["n"] == 3 and "198.51.100.9" not in json.dumps(sv)
    d2 = H["parse_dump"]("\n".join(DUMP.splitlines()[:3]))
    assert H["wg_service"]("wg", "host", d2, {}, H["conf_peers"](tunnel))[0]["role"] == "mesh"
    # без конфига: пара пиров на хосте - туннель
    assert H["wg_service"]("wg", "host", d2, {}, {})[0]["role"] == "mesh"


def test_amnezia_table_names():
    t = json.dumps([{"clientId": "alicepub=", "userData": {"clientName": "alice", "allowedIps": "10.8.1.2/32"}},
                    {"clientId": "x"}, "junk"])
    assert H["table_names"](t) == {"alicepub=": "alice", "x": ""}
    assert H["table_names"]("not json") == {}


def test_openvpn_status_v1_and_v2():
    v1 = """OpenVPN CLIENT LIST
Updated,Thu Oct 29 12:11:05 2026
Common Name,Real Address,Bytes Received,Bytes Sent,Connected Since
alice,203.0.113.9:40000,111,222,Thu Oct 29 10:00:00 2026
ROUTING TABLE
Virtual Address,Common Name,Real Address,Last Ref
10.8.0.6,alice,203.0.113.9:40000,Thu Oct 29 12:11:00 2026
GLOBAL STATS
Max bcast/mcast queue length,0
END"""
    r = H["parse_ovpn_status"](v1)
    assert len(r) == 1 and r[0]["cn"] == "alice" and r[0]["ip"] == "10.8.0.6" and r[0]["ep"] == "203.0.113.9"
    assert (r[0]["rx"], r[0]["tx"]) == (111, 222) and r[0]["since"] > 0
    v2 = ("TITLE,OpenVPN 2.6\nHEADER,CLIENT_LIST,Common Name,Real Address,Virtual Address,Virtual IPv6 Address,"
          "Bytes Received,Bytes Sent,Connected Since,Connected Since (time_t),Username\n"
          "CLIENT_LIST,bob,198.51.100.4:1194,10.8.0.10,,500,600,2026-10-29 10:00:00,1793170000,UNDEF\nEND")
    r = H["parse_ovpn_status"](v2)
    assert r == [{"cn": "bob", "ep": "198.51.100.4", "ip": "10.8.0.10", "rx": 500, "tx": 600, "since": 1793170000}]
    sv = H["ovpn_service"]("openvpn", v2, {"bob": "Боб"}, 8)
    assert sv["n"] == 8 and sv["clients"][0]["name"] == "Боб" and sv["clients"][0]["on"] is True


def test_status_paths_relative_to_dirs():
    assert H["status_paths"]("port 1194\nstatus openvpn-status.log\n", ["/", "/opt/amnezia/openvpn"]) == [
        "/openvpn-status.log", "/opt/amnezia/openvpn/openvpn-status.log"]
    assert H["status_paths"]("status /tmp/s.log 30", ["/x"]) == ["/tmp/s.log"]


def _ct(name, image, state="running", ports=()):
    return {"name": name, "image": image, "state": state,
            "ports": [{"pub": p, "priv": p, "proto": pr} for p, pr in ports]}


def _rep(cts=(), ifs=(), extras=None):
    r = {"docker": {"present": True, "access": True, "containers": list(cts)},
         "net_ifaces": [{"if": i, "rx": 0, "tx": 0} for i in ifs]}
    if extras is not None:
        r["extras"] = {"vpn": extras}
    return r


def test_images_and_companions():
    """по образу, а не по имени: 3x-ui-caddy-1 рядом с 3x-ui - не VPN, панель Amnezia Control тоже"""
    rep = _rep([
        _ct("3x-ui", "ghcr.io/mhsanaei/3x-ui:v2.8.11"),
        _ct("3x-ui-caddy-1", "lucaslorentz/caddy-docker-proxy:2.13.1"),
        _ct("3x-ui-docker-api-1", "wollomatic/socket-proxy:1.13.1"),
        _ct("amnezia-awg2", "amnezia-awg2", ports=[(30943, "udp")]),
        _ct("amnezia-awg3", "acontrol-awg3"),
        _ct("acontrol-backend-1", "ghcr.io/mihsergeev/amnezia-control-backend:latest"),
        _ct("openvpn", "kylemanna/openvpn"),
        _ct("old", "amnezia-xray", state="exited"),
        _ct("mtg", "nineseconds/mtg:2"),
        _ct("squid_proxy", "thelebster/docker-squid-simple-proxy"),
    ], ifs=["eth0", "amn0", "gre0", "gretap0", "docker0"])
    svc = vpn.services(rep, NOW)
    got = {(m["k"], m["role"]): m["src"] for m in svc}
    assert got == {("3x-ui", "server"): ["3x-ui"], ("awg", "server"): ["amnezia-awg2", "amnezia-awg3"],
                   ("openvpn", "server"): ["openvpn"], ("mtproto", "server"): ["mtg"],
                   ("squid", "server"): ["squid_proxy"]}
    awg = next(m for m in svc if m["k"] == "awg")
    assert awg["ports"] == ["30943/udp"] and awg["n"] is None and awg["short"] == "AmneziaWG"
    assert vpn.needs_helper(rep)


def test_mesh_client_is_not_a_vpn_server():
    """Tailscale на обычной ноде - клиент меша: тише и без просьбы ставить helper"""
    rep = _rep(ifs=["eth0", "tailscale0"])
    svc = vpn.services(rep, NOW)
    assert [(m["k"], m["role"]) for m in svc] == [("tailscale", "mesh")]
    assert not vpn.needs_helper(rep)
    # голый wg0 без helper'а - роль неизвестна: тише, но helper нужен, чтобы ее узнать
    rep = _rep(ifs=["wg0"])
    m = vpn.services(rep, NOW)[0]
    assert (m["k"], m["role"], m.get("guess")) == ("wg", "mesh", True)
    assert vpn.needs_helper(rep)


def test_helper_merges_clients_and_role():
    ex = {"v": 1, "ts": NOW - 60, "svc": [
        {"k": "awg", "src": "amnezia-awg2", "if": "awg0", "port": 30943, "role": "server", "n": 3, "clients": [
            {"name": "alice", "ip": "10.8.1.2", "hs": NOW - 90, "rx": 1, "tx": 2, "ep": "203.0.113.7"},
            {"name": "bob", "ip": "10.8.1.3", "hs": NOW - 4000, "rx": 3, "tx": 4, "ep": ""},
        ]},
        {"k": "awg", "src": "amnezia-awg3", "if": "awg0", "role": "server", "n": 1, "clients": [
            {"name": "carol", "ip": "10.8.3.2", "hs": NOW - 61, "rx": 5, "tx": 6, "ep": "198.51.100.3"}]},
        {"k": "wg", "src": "host", "if": "wg0", "role": "server", "n": 0, "clients": []},
        {"k": "tailscale", "src": "host", "if": "tailscale0", "role": "server", "exit": True, "routes": ["0.0.0.0/0"]},
        {"k": "3x-ui", "src": "3x-ui", "role": "server", "protos": ["vless", "bogus"], "n": 1, "clients": [
            {"name": "dave", "ip": "", "hs": NOW - 10, "rx": 7, "tx": 8, "ep": "203.0.113.20"}]},
        {"k": "host_wg", "src": "host", "err": "OSError"},
    ]}
    rep = _rep([_ct("amnezia-awg2", "amnezia-awg2"), _ct("amnezia-awg3", "acontrol-awg3"),
                _ct("3x-ui", "ghcr.io/mhsanaei/3x-ui:v2.2.6")], ifs=["wg0", "tailscale0"], extras=ex)
    v = vpn.view(rep, NOW)
    by = {m["k"]: m for m in v["svc"]}
    assert v["helper"] and not v["stale"]
    awg = by["awg"]
    assert awg["n"] == 4 and awg["on"] == 2
    # на связи сначала, потом по свежести handshake
    assert [c["name"] for c in awg["clients"]] == ["carol", "alice", "bob"]
    assert awg["clients"][0]["src"] == "amnezia-awg3"
    assert by["wg"]["role"] == "server" and "guess" not in by["wg"] and by["wg"]["src"] == ["wg0"]
    assert by["tailscale"]["role"] == "server" and by["tailscale"]["exit"]
    assert by["3x-ui"]["short"] == "VLESS" and by["3x-ui"]["protos"] == ["vless"]
    assert "host_wg" not in by
    flat = vpn.clients(rep, NOW)
    assert {(c["k"], c["name"], c["ip"]) for c in flat} == {
        ("awg", "alice", "10.8.1.2"), ("awg", "bob", "10.8.1.3"), ("awg", "carol", "10.8.3.2"),
        ("3x-ui", "dave", "")}


def test_stale_helper_has_nobody_online():
    ex = {"v": 1, "ts": NOW - 3600, "svc": [
        {"k": "awg", "src": "amnezia-awg2", "role": "server", "n": 1, "clients": [
            {"name": "alice", "ip": "10.8.1.2", "hs": NOW - 3610, "rx": 1, "tx": 2, "ep": ""}]}]}
    rep = _rep([_ct("amnezia-awg2", "amnezia-awg2")], extras=ex)
    v = vpn.view(rep, NOW)
    assert v["stale"] and v["svc"][0]["on"] == 0 and v["svc"][0]["clients"][0]["name"] == "alice"
    # адреса могли уйти другим: для учета трафика клиентов нет
    assert vpn.clients(rep, NOW) == []


def test_junk_from_helper():
    ex = {"ts": "x", "svc": ["a", {"k": "awg", "src": {"x": 1}, "clients": [None, {"name": ["x"]}, {"ip": "10.0.0.2"}]}]}
    v = vpn.view(_rep(extras=ex), NOW)
    assert v["svc"][0]["clients"] == [{"name": "", "ip": "10.0.0.2", "hs": None, "rx": 0, "tx": 0, "ep": "",
                                       "on": False, "src": "awg"}]
    assert vpn.view(_rep(), NOW) is None


def test_setup_needed_and_when():
    rep = _rep([_ct("amnezia-awg2", "amnezia-awg2")])
    assert setup_scripts.setup_needed("vpn-setup", rep)
    assert not setup_scripts.setup_needed("vpn-setup", _rep(ifs=["tailscale0"]))
    assert not setup_scripts.setup_needed("vpn-setup", _rep([_ct("web", "nginx:1.27")]))
    when = re.search(r'^KERVAX_SETUP_WHEN="(.*)"$', SRC, re.M).group(1)
    assert not re.search(r"[{}'$]", when)
    # что панель видит по образу, то условие на ноде находит в docker ps: иначе панель просит
    # helper, а плейбук его молча не ставит
    words = re.findall(r"-e ([^\s)]+)", when.split("docker ps", 1)[1])
    assert set(vpn.WHEN_WORDS) <= set(words)
    for img in ("amnezia-awg2", "acontrol-awg3", "ghcr.io/xtls/xray-core:26.3.27", "ghcr.io/mhsanaei/3x-ui:v2.8.11",
                "kylemanna/openvpn", "nineseconds/mtg:2", "headscale/headscale:0.29.2", "3proxy/3proxy:latest",
                "ghcr.io/sagernet/sing-box@sha256:da0e", "thelebster/docker-squid-simple-proxy",
                "ghcr.io/wg-easy/wg-easy:15", "quay.io/outline/shadowbox:stable", "gozargah/marzban:latest",
                "hwdsl2/ipsec-vpn-server", "teddysun/v2ray", "ginuerzh/gost:2.11", "telegrammessenger/proxy"):
        assert vpn.kind_of_image(img), img
        assert any(w in img for w in words), img
    # каталог helper'а - те же образы, что у панели, для тех, у кого он читает клиентов
    for k, rx in H["CATALOG"]:
        assert any(k == pk and rx == prx.pattern for pk, _l, _r, prx in vpn.CATALOG), k
