"""Адрес клиента за своими прокси (app/client_ip.py): поддельный X-Forwarded-For не меняет ключ
входа, штатная цепочка caddy -> nginx фронтенда -> бэкенд дает настоящий адрес клиента, а когда
клиента не видно (чужой прокси, шлюз docker), ключ помечен как неопределенный."""

import ipaddress
import json
import re
import time
from pathlib import Path

import pytest

from app import client_ip, config
from app.client_ip import client_key

ROOT = Path(__file__).resolve().parents[2]
FRONT = "172.20.0.3"  # nginx фронтенда во внутренней docker-сети: соединение с бэкендом
GW = "172.20.0.1"  # шлюз внутренней сети: так в nginx приходит прокси на самом хосте
CADDY = "172.19.0.2"  # caddy в сети caddy
NEIGHBOR = "172.19.0.9"  # чужой контейнер в той же сети caddy
OTHER_PROXY = "172.17.0.5"  # прокси в другом контейнере, не вписанный в KERVAX_TRUSTED_PROXIES
LAN = "192.168.1.50"  # компьютер в офисной сети
CLIENT = "198.51.100.7"
FAKE = "192.0.2.50"  # адрес из белого списка входа, который клиент подставляет сам


@pytest.fixture
def trust(monkeypatch, tmp_path):
    """KERVAX_TRUSTED_PROXIES для теста; шлюз контейнера - синтетический, а не машины с тестами."""
    monkeypatch.setenv("KERVAX_DATA_DIR", tmp_path.as_posix())
    monkeypatch.setattr(client_ip, "_gateways", lambda: frozenset({ipaddress.ip_address(GW)}), raising=False)
    if hasattr(client_ip, "_snap"):
        client_ip._snap[0] = None

    def set_(value: str) -> None:
        monkeypatch.setenv("KERVAX_TRUSTED_PROXIES", value)
        monkeypatch.setenv("KERVAX_BIND", "127.0.0.1:8080")  # как в compose.yml
        config.get_settings.cache_clear()

    yield set_
    config.get_settings.cache_clear()


def nginx(xff_in: str, remote: str) -> str:
    """$proxy_add_x_forwarded_for из frontend/nginx.conf: пришедший заголовок + адрес соединения."""
    return f"{xff_in}, {remote}" if xff_in else remote


def caddy(client: str) -> str:
    """caddy 2.5+ (quickstart ставит 2.11) не верит X-Forwarded-For клиента и пишет его адрес."""
    return client


def who(xff: str, peer: str = FRONT):
    c = client_ip.resolve(peer, xff)
    return c.ip, c.known, c.exact


def test_repo_proxies_build_the_chain_the_backend_expects():
    conf = (ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    assert re.search(r"proxy_set_header\s+X-Forwarded-For\s+\$proxy_add_x_forwarded_for;", conf)
    base = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert "KERVAX_TRUSTED_PROXIES: ${KERVAX_TRUSTED_PROXIES-gateway}" in base
    edge = (ROOT / "compose.caddy.yml").read_text(encoding="utf-8")
    assert "KERVAX_TRUSTED_PROXIES: ${KERVAX_TRUSTED_PROXIES:-private}" in edge
    # в режиме caddy порт фронтенда не опубликован: в nginx приходят только из docker-сетей
    assert "ports: !reset []" in edge
    # бэкенд наружу не опубликован: к нему ходит только фронтенд
    backend = base.split("\n  backend:\n", 1)[1].split("\n  frontend:\n", 1)[0]
    assert "ports:" not in backend


def test_gateways_from_proc_route_fixtures(tmp_path):
    r4 = tmp_path / "route"
    r4.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t010014AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        "eth0\t000014AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        "eth1\t00000000\tzz\t0003\t0\t0\t0\t00000000\t0\t0\t0\n", encoding="ascii")
    r6 = tmp_path / "ipv6_route"
    r6.write_text(
        "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
        "fd123456000000000000000000000001 00000400 00000001 00000000 00000003 eth0\n"
        "fd123456000000000000000000000000 40 00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00000100 00000001 00000000 00000001 eth0\n"
        "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 ffffffff 00000001 00000000 00200200 lo\n", encoding="ascii")
    assert client_ip.read_gateways(str(r4), str(r6)) == frozenset(
        {ipaddress.ip_address(GW), ipaddress.ip_address("fd12:3456::1")})
    assert client_ip.read_gateways(str(tmp_path / "nope"), str(tmp_path / "nope6")) == frozenset()


def test_direct_client_with_forged_header_keeps_its_address(trust):
    for mode in ("", "gateway", "private", CADDY):
        trust(mode)
        assert client_key("203.0.113.9", FAKE) == "203.0.113.9"
        assert who(f"{FAKE}, 10.0.0.1", "2001:db8::9") == ("2001:db8::9", True, True)


def test_office_install_without_proxy_ignores_forged_header(trust):
    """KERVAX_BIND=0.0.0.0:8080 и none: клиент из LAN приходит в nginx сам, левее - его выдумка."""
    for mode in ("none", ""):
        trust(mode)
        assert who(nginx(FAKE, LAN)) == (LAN, True, True)
        assert who(nginx(f"10.0.0.5, {FAKE}", LAN)) == (LAN, True, True)
        assert who(nginx("", LAN)) == (LAN, True, True)
        # ротация подставного адреса ключ не меняет
        assert {client_key(FRONT, nginx(f"192.0.2.{i}", LAN)) for i in range(1, 20)} == {LAN}
    trust("gateway")
    # при ожидаемом прокси частный адрес, сам дописавший заголовок, - чужой прокси: адрес не
    # определен, но и выдумка левее в ключ не идет
    assert who(nginx(FAKE, LAN)) == (LAN, False, False)
    assert who(nginx("", LAN)) == (LAN, True, True)


def test_standard_caddy_chain_gives_the_real_client(trust):
    trust("private")
    # private - догадка (соседний контейнер тоже частный): адрес известен, но не точно
    assert who(nginx(caddy(CLIENT), CADDY)) == (CLIENT, True, False)
    # клиент из LAN через caddy (quickstart --local-ca): его частный адрес и есть клиент
    assert who(nginx(caddy(LAN), CADDY)) == (LAN, True, False)
    # локальная проверка с самого хоста приходит в caddy со шлюза его сети
    assert who(nginx(caddy("172.19.0.1"), CADDY)) == ("172.19.0.1", True, False)
    # соседний контейнер подставил адрес: известен, но не точно - белый список не действует
    assert who(nginx(FAKE, NEIGHBOR)) == (FAKE, True, False)
    # прокси, который дописывает, а не заменяет: левее клиента - выдумка, ее не берем
    assert who(nginx(f"{FAKE}, {CLIENT}", CADDY))[0] == CLIENT


def test_host_proxy_through_docker_gateway(trust):
    """compose.yml: свой nginx хоста ходит на 127.0.0.1:8080, nginx фронтенда видит шлюз."""
    trust("gateway")
    assert who(nginx(f"{FAKE}, {CLIENT}", GW)) == (CLIENT, True, True)
    assert who(nginx(CLIENT, GW)) == (CLIENT, True, True)
    # прокси хоста без X-Forwarded-For или IPv6 через docker-proxy: кто за шлюзом - не видно
    assert who(nginx("", GW)) == (FRONT, False, False)
    trust("none")
    assert who(nginx(f"{FAKE}, {CLIENT}", GW)) == (GW, False, False)


def test_untrusted_proxy_in_front_is_not_a_client(trust):
    """Прокси в другом контейнере или на машине в LAN, не вписанный в настройку: все клиенты
    пришли бы одним его адресом - ключ помечен неопределенным, а не банится за всех."""
    trust("gateway")
    assert who(nginx(CLIENT, OTHER_PROXY)) == (OTHER_PROXY, False, False)
    assert who(nginx("garbage", OTHER_PROXY)) == (OTHER_PROXY, False, False)
    trust(f"gateway {OTHER_PROXY}")
    assert who(nginx(CLIENT, OTHER_PROXY)) == (CLIENT, True, True)
    # слова и сети складываются
    trust("private, 203.0.113.0/24")
    assert who(nginx(CLIENT, "203.0.113.4")) == (CLIENT, True, True)
    assert who(nginx(caddy(CLIENT), CADDY)) == (CLIENT, True, False)


def test_exact_proxy_stops_a_neighbouring_container(trust):
    trust(CADDY)
    assert who(nginx(caddy(CLIENT), CADDY)) == (CLIENT, True, True)
    assert who(nginx(FAKE, NEIGHBOR)) == (NEIGHBOR, False, False)
    trust(f"{CADDY}/32, 2001:db8:ff::/48")
    assert who(nginx(CLIENT, "2001:db8:ff::2")) == (CLIENT, True, True)


def test_fresh_proxy_snapshot_narrows_private_to_exact_caddy(trust, tmp_path):
    trust("private")
    snap = {"v": 1, "generated_at": int(time.time()), "frontend_peer_ips": [FRONT], "forwarded_proxy_ips": [CADDY]}
    (tmp_path / "agent_proxy_peers.json").write_text(json.dumps(snap), encoding="utf-8")
    assert who(nginx(caddy(CLIENT), CADDY)) == (CLIENT, True, True)
    assert who(nginx(FAKE, NEIGHBOR)) == (NEIGHBOR, False, False)
    # фронтенд пересоздан, снимок еще старый: адрес фронтенда - не клиент
    assert who(nginx(FAKE, CADDY), "172.20.0.9") == ("172.20.0.9", False, False)
    # none не отменяет свежий снимок
    trust("none")
    assert who(nginx(caddy(CLIENT), CADDY)) == (CLIENT, True, True)


def test_ipv6_chain(trust):
    trust("private")
    front6, caddy6 = "fd12:3456::3", "fd12:3456::2"
    assert who(nginx("2001:db8::7", caddy6), front6)[:2] == ("2001:db8::7", True)
    assert who(nginx("2001:db8::66, 2001:db8::7", caddy6), front6)[0] == "2001:db8::7"
    assert who(nginx("::ffff:198.51.100.7", CADDY))[0] == CLIENT
    trust("none")
    assert who(nginx("2001:db8::66", "2001:db8::7"), front6) == ("2001:db8::7", True, True)


def test_caddy_network_gateway_is_never_a_client(trust, monkeypatch, tmp_path):
    """IPv6-клиент в IPv4-сети caddy приходит через docker-proxy с адреса шлюза этой сети: это не
    клиент (все IPv6-клиенты делили бы ключ). Шлюз - из KERVAX_PROXY_GATEWAY (compose.caddy.yml)."""
    edge_gw = "172.19.0.1"
    monkeypatch.setenv("KERVAX_PROXY_GATEWAY", f"{edge_gw} fd12:3456::1")
    trust("private")
    assert who(nginx(caddy(edge_gw), CADDY)) == (edge_gw, False, False)
    # хост сам пришел в nginx через шлюз сети caddy: не догадка "это caddy", а неизвестно кто
    assert who(nginx(FAKE, edge_gw)) == (edge_gw, False, False)
    snap = {"v": 1, "generated_at": int(time.time()), "frontend_peer_ips": [FRONT], "forwarded_proxy_ips": [CADDY]}
    (tmp_path / "agent_proxy_peers.json").write_text(json.dumps(snap), encoding="utf-8")
    assert who(nginx(caddy(edge_gw), CADDY)) == (edge_gw, False, False)
    assert who(nginx(caddy(CLIENT), CADDY)) == (CLIENT, True, True)


def test_port_and_brackets_written_by_a_trusted_proxy(trust):
    trust(CADDY)
    assert who(nginx(f"{CLIENT}:5555", CADDY)) == (CLIENT, True, True)
    assert who(nginx("[2001:db8::1]:443", CADDY)) == ("2001:db8::1", True, True)
    assert who(nginx("[2001:db8::1]", CADDY)) == ("2001:db8::1", True, True)
    assert who(nginx("2001:db8::1", CADDY)) == ("2001:db8::1", True, True)
    assert who(nginx(f"{FAKE}, {CLIENT}:443", f"{CADDY}:8443"))[0] == CLIENT
    for bad in ("[2001:db8::1]:x", f"[{CLIENT}]:443", f"{CLIENT}:", "[2001:db8::1", "2001:db8::1]:443"):
        assert who(nginx(bad, CADDY)) == (FRONT, False, False), bad


def test_garbage_or_only_own_proxies_fall_back_to_the_connection(trust):
    trust("private")
    for xff in ("garbage", f"garbage, {CADDY}", f"{CLIENT}, , {CADDY}", f"{CLIENT}%eth0, {CADDY}", CADDY):
        assert who(xff) == (FRONT, False, False), xff
    # мусор левее клиента не мешает: до него разбор не доходит
    assert client_key(FRONT, nginx(f"garbage, {'x' * 5000}", CLIENT)) == CLIENT
    # длинная выдуманная цепочка: берется ближний к прокси адрес, а не самый левый
    many = ", ".join(f"203.0.113.{i % 250}" for i in range(500))
    assert client_key(FRONT, nginx(many, CLIENT)) == CLIENT
    assert client_key(None, CLIENT) == "unknown"
    assert client_key("testclient", CLIENT) == "testclient"
    # процесс на той же машине без прокси (healthcheck): это и есть клиент
    assert who("", "127.0.0.1") == ("127.0.0.1", True, True)


def test_trusted_proxies_typo_is_reported():
    assert client_ip.config_problems("gateway privat 10.0.0.0/33 none 198.51.100.0/24, NONE") == ["privat", "10.0.0.0/33"]
    assert client_ip.config_problems("") == []
