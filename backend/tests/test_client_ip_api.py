"""Адрес клиента за прокси в API: вход и отчет агента при каждой схеме установки, чужой прокси
впереди (адрес не определен), смена подтвержденного адреса сервера и настройки
KERVAX_TRUSTED_PROXIES / KERVAX_BIND."""

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app import client_ip, config, panel_errors, request_watch
from app.models import AuditLog, PanelError, RequestStat

ROOT = Path(__file__).resolve().parents[2]
FRONT, GW, CADDY = "172.20.0.3", "172.20.0.1", "172.19.0.2"
OTHER_PROXY = "172.17.0.5"  # прокси в другом контейнере, не вписанный в настройку
GOOD = {"username": "admin", "password": "testpass-2026"}
BAD = {"username": "admin", "password": "bad"}
REPORT = {"hostname": "k8s-a", "os": "U", "agent_version": "2.55", "cpu_percent": 5, "mem_used": 1, "mem_total": 2,
          "local_ip": "192.0.2.67", "ips": ["192.0.2.68"]}


@pytest.fixture
async def front(client, monkeypatch):
    """Запросы приходят в бэкенд от nginx фронтенда, как в docker-сети установки."""

    def mode(value: str, bind: str = "127.0.0.1:8080") -> None:
        monkeypatch.setenv("KERVAX_TRUSTED_PROXIES", value)
        monkeypatch.setenv("KERVAX_BIND", bind)
        config.get_settings.cache_clear()

    panel_errors.reset()
    app = client._transport.app  # type: ignore[attr-defined]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(FRONT, 40000)),
                                 base_url="http://test") as c:
        c.mode = mode  # type: ignore[attr-defined]
        c.factory = app.state.session_factory  # type: ignore[attr-defined]
        yield c


async def _auto(front, auth_headers) -> tuple[set, list]:
    d = (await front.get("/api/autoban/settings", headers=auth_headers)).json()
    return {x["net"] for x in d["auto"]}, d.get("skipped")


@pytest.mark.parametrize("mode,good,unknown,verified", [
    ("none", "192.0.2.99, 203.0.113.30", "203.0.113.31, 172.20.0.1", True),
    ("gateway", "192.0.2.99, 203.0.113.30, 172.20.0.1", f"203.0.113.31, {OTHER_PROXY}", True),
    (CADDY, "192.0.2.99, 203.0.113.30, 172.19.0.2", "203.0.113.31, 172.19.0.9", True),
    # догадка private: соседний контейнер в сети caddy подставит любой адрес - не подтвержден
    ("private", "192.0.2.99, 203.0.113.30, 172.19.0.2", "garbage, 172.19.0.2", False),
])
async def test_agent_report_address_per_install_scheme(front, auth_headers, mode, good, unknown, verified):
    front.mode(mode)
    r = await front.post("/api/servers", json={"name": "k8s-a", "agent_ip": "198.51.100.20"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    hdr = {"Authorization": f"Bearer {token}"}
    assert (await front.post("/api/agent/report", json=REPORT, headers={**hdr, "X-Forwarded-For": good})).status_code == 200
    assert (await front.get(f"/api/servers/{sid}", headers=auth_headers)).json()["external_ip"] == "203.0.113.30"
    # в белый список автобана - только подтвержденный адрес; IP, заданный руками, и отчет - нет
    want = {"203.0.113.30/32"} if verified else set()
    auto, skipped = await _auto(front, auth_headers)
    assert auto == want and skipped == ([] if verified else [{"name": "k8s-a", "why": "unverified"}])
    # адрес не определен (чужой прокси, шлюз, мусор): прежний адрес ноды не затирается
    assert (await front.post("/api/agent/report", json=REPORT, headers={**hdr, "X-Forwarded-For": unknown})).status_code == 200
    assert (await front.get(f"/api/servers/{sid}", headers=auth_headers)).json()["external_ip"] == "203.0.113.30"
    assert (await _auto(front, auth_headers))[0] == want


async def test_verified_address_change_alerts_admin_once_a_day(front, auth_headers, monkeypatch):
    """Подтвержденные адреса хранятся по семействам: смена адреса семейства или первый адрес
    другого семейства - алерт; одна и та же пара "старый>новый" - не чаще раза в сутки."""
    sent: list[str] = []

    async def fake(session, settings, text):
        sent.append(text)

    monkeypatch.setattr("app.alerts.security_alert", fake)
    front.mode("gateway")
    r = await front.post("/api/servers", json={"name": "k8s-a"}, headers=auth_headers)
    sid = r.json()["server"]["id"]
    hdr = {"Authorization": f"Bearer {r.json()['token']}"}

    async def report(ip: str, **extra) -> None:
        rr = await front.post("/api/agent/report", json={**REPORT, **extra}, headers={**hdr, "X-Forwarded-For": f"{ip}, {GW}"})
        assert rr.status_code == 200

    await report("10.20.0.5")  # первый подтвержденный адрес (частный) - не смена
    assert sent == []
    await report("203.0.113.30")  # частный -> публичный: адрес впервые уходит в белый список
    assert len(sent) == 1 and "10.20.0.5 -> 203.0.113.30" in sent[0]
    sent.clear()
    await report("203.0.113.30")
    assert sent == []
    await report("2001:db8::30")  # v4 уже подтвержден, новый адрес v6 - алерт
    assert len(sent) == 1 and "203.0.113.30 -> 2001:db8::30" in sent[0]
    await report("203.0.113.30")  # нода с двумя стеками: свои адреса по семействам - не смена
    await report("2001:db8::30")
    await report("192.0.2.200", proxy="proxy.example:3128")  # через прокси: не подтвержден
    assert len(sent) == 1
    await report("203.0.113.40")  # смена после отчета через прокси не прячется
    assert len(sent) == 2 and "Сменился адрес сервера k8s-a" in sent[1] and "203.0.113.30 -> 203.0.113.40" in sent[1]
    await report("203.0.113.30")  # другая пара - свой алерт
    await report("203.0.113.40")  # та же пара в те же сутки - без повтора (отметка в базе)
    assert len(sent) == 3 and "203.0.113.40 -> 203.0.113.30" in sent[2]
    from app.models import Server

    async with front.factory() as s:
        srv = await s.get(Server, sid)
        assert (srv.confirmed_ip4, srv.confirmed_ip6) == ("203.0.113.40", "2001:db8::30")
        srv.ip_alerts = {k: t - 86401 for k, t in srv.ip_alerts.items()}
        await s.commit()
    await report("203.0.113.30")
    await report("203.0.113.40")  # сутки прошли - пара снова алертит
    assert len(sent) == 5
    # неподтвержденный адрес (догадка private) алерта не дает и подтвержденный не меняет
    front.mode("private")
    await front.post("/api/agent/report", json=REPORT, headers={**hdr, "X-Forwarded-For": f"203.0.113.70, {CADDY}"})
    assert len(sent) == 5
    async with front.factory() as s:
        srv = await s.get(Server, sid)
        assert (srv.external_ip, srv.external_ip_ok, srv.confirmed_ip4) == ("203.0.113.70", False, "203.0.113.40")


async def test_proxy_report_without_server_ip_drops_the_verified_address(front, auth_headers):
    front.mode("gateway")
    r = await front.post("/api/servers", json={"name": "k8s-a"}, headers=auth_headers)
    hdr = {"Authorization": f"Bearer {r.json()['token']}"}
    await front.post("/api/agent/report", json=REPORT, headers={**hdr, "X-Forwarded-For": f"203.0.113.30, {GW}"})
    assert (await _auto(front, auth_headers))[0] == {"203.0.113.30/32"}
    await front.post("/api/agent/report", json={**REPORT, "proxy": "proxy.example:3128"},
                     headers={**hdr, "X-Forwarded-For": f"192.0.2.200, {GW}"})
    auto, skipped = await _auto(front, auth_headers)
    assert auto == set() and skipped == [{"name": "k8s-a", "why": "proxy"}]


async def test_untrusted_private_proxy_in_front(client, front, auth_headers):
    """Прокси в другом контейнере, не вписанный в KERVAX_TRUSTED_PROXIES: адрес клиента не
    определен, ключ входа - адрес прокси. Все за ним делят счет неудач и блокировку, белый список
    не действует (даже если прокси вписан туда сам), адрес ноды не затирается, админ видит
    подсказки в "Ошибках панели". Клиент с определенным адресом входит как обычно."""
    front.mode("gateway")
    r = await client.put("/api/login-guard", headers=auth_headers, json={
        "max_failures": 5, "ban_minutes": 60, "allow": [{"net": OTHER_PROXY}, {"net": "198.51.100.0/24"}]})
    assert r.status_code == 200
    via = lambda ip: {"X-Forwarded-For": f"{ip}, {OTHER_PROXY}"}  # noqa: E731
    for i in range(5):
        assert (await front.post("/api/auth/login", json=BAD, headers=via(f"198.51.100.{60 + i}"))).status_code == 401
    r = await front.post("/api/auth/login", json=GOOD, headers=via("198.51.100.70"))
    assert r.status_code == 429 and r.json()["detail"].startswith("locked:")
    assert (await front.post("/api/auth/login", json=GOOD, headers={"X-Forwarded-For": f"203.0.113.9, {GW}"})).status_code == 200
    g = (await client.get("/api/login-guard", headers={**auth_headers})).json()
    assert [b["key"] for b in g["bans"]] == [OTHER_PROXY]
    g = (await front.get("/api/login-guard", headers={**auth_headers, **via("198.51.100.70")})).json()
    assert (g["my_ip"], g["my_ip_known"], g["allow_inactive"], g["my_allowed"]) == (OTHER_PROXY, False, True, None)
    assert all(x["allowed"] is None for x in g["recent"])

    r = await client.post("/api/servers", json={"name": "k8s-b"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    hdr = {"Authorization": f"Bearer {token}"}
    assert (await front.post("/api/agent/report", json=REPORT, headers={**hdr, "X-Forwarded-For": f"203.0.113.30, {GW}"})).status_code == 200
    assert (await front.post("/api/agent/report", json=REPORT, headers={**hdr, **via("203.0.113.31")})).status_code == 200
    assert (await front.get(f"/api/servers/{sid}", headers=auth_headers)).json()["external_ip"] == "203.0.113.30"

    await panel_errors.drain()
    async with front.factory() as s:
        fails = [(a.target, a.detail) for a in await s.scalars(select(AuditLog).where(AuditLog.action == "login_fail"))]
        errs = {e.action: e.message for e in await s.scalars(select(PanelError))}
    assert len(fails) == 5 and all(t == OTHER_PROXY and "KERVAX_TRUSTED_PROXIES" in d for t, d in fails)
    assert OTHER_PROXY in errs["адрес клиента"] and "KERVAX_TRUSTED_PROXIES" in errs["адрес клиента"]
    assert "Белый список входа не действует" in errs["белый список входа"]


async def test_known_addresses_keep_their_own_counters(front):
    """Определенный адрес - как до ветки: свой счет и блокировка, ротация адреса - новые счета."""
    front.mode("gateway")
    hdr = {"X-Forwarded-For": f"203.0.113.20, {GW}"}
    for _ in range(5):
        assert (await front.post("/api/auth/login", json=BAD, headers=hdr)).status_code == 401
    r = await front.post("/api/auth/login", json=GOOD, headers=hdr)
    assert r.status_code == 429 and r.json()["detail"].startswith("locked:")
    assert (await front.post("/api/auth/login", json=GOOD, headers={"X-Forwarded-For": f"203.0.113.21, {GW}"})).status_code == 200


async def test_request_watch_does_not_count_unknown_sources(front, monkeypatch):
    """За чужим прокси все агенты одним адресом: всплеск по нему был бы ложным алертом."""
    from types import SimpleNamespace

    monkeypatch.setattr(request_watch, "time", SimpleNamespace(time=lambda: 1_800_000_000))
    request_watch.reset()
    front.mode("gateway")
    for _ in range(60):
        r = await front.get("/api/agent/commands", headers={"X-Forwarded-For": f"203.0.113.80, {OTHER_PROXY}",
                                                              "Authorization": "Bearer nope"})
        assert r.status_code == 401
        await front.get("/api/agent/commands", headers={"X-Forwarded-For": f"203.0.113.81, {GW}",
                                                         "Authorization": "Bearer nope"})
    await request_watch.flush(front.factory)
    async with front.factory() as s:
        keys = {r.key for r in await s.scalars(select(RequestStat))}
    assert keys == {"203.0.113.81"}
    request_watch.reset()


async def test_caddy_gateway_is_not_the_agent_or_admin_address(front, auth_headers, monkeypatch):
    """IPv6 через docker-proxy в IPv4-сети caddy: шлюз этой сети не становится ни адресом ноды
    (external_ip, подтвержденный адрес, алерт), ни "вашим адресом" в защите входа."""
    edge_gw = "172.19.0.1"
    monkeypatch.setenv("KERVAX_PROXY_GATEWAY", edge_gw)
    front.mode(CADDY)
    r = await front.post("/api/servers", json={"name": "k8s-a"}, headers=auth_headers)
    sid = r.json()["server"]["id"]
    hdr = {"Authorization": f"Bearer {r.json()['token']}"}
    await front.post("/api/agent/report", json=REPORT, headers={**hdr, "X-Forwarded-For": f"203.0.113.30, {CADDY}"})
    await front.post("/api/agent/report", json=REPORT, headers={**hdr, "X-Forwarded-For": f"{edge_gw}, {CADDY}"})
    from app.models import Server

    async with front.factory() as s:
        srv = await s.get(Server, sid)
        assert (srv.external_ip, srv.external_ip_ok, srv.confirmed_ip4) == ("203.0.113.30", True, "203.0.113.30")
    g = (await front.get("/api/login-guard", headers={**auth_headers, "X-Forwarded-For": f"{edge_gw}, {CADDY}"})).json()
    assert (g["my_ip"], g["my_ip_known"]) == (edge_gw, False)


async def test_private_published_beyond_loopback_is_reported(front):
    front.mode("private", "0.0.0.0:8080")
    assert await client_ip.check_config(front.factory) == ["private"]
    front.mode("private", "unpublished")
    assert await client_ip.check_config(front.factory) == []


def test_bind_local():
    for bind in ("unpublished", "127.0.0.1:8080", "127.0.0.2:80", "[::1]:8080", "localhost:8080"):
        assert client_ip.bind_local(bind), bind
    # пустой - запуск не через compose панели: где опубликован фронтенд, неизвестно
    for bind in ("", "0.0.0.0:8080", "8080", "192.168.1.10:8080", "[::]:8080", "panel.example.test:8080"):
        assert not client_ip.bind_local(bind), bind


async def test_gateway_is_ignored_when_published_beyond_loopback(front):
    front.mode("gateway", "0.0.0.0:8080")
    who = client_ip.resolve(FRONT, f"192.0.2.50, 198.51.100.7, {GW}")
    assert (who.ip, who.known) == (GW, False)
    assert await client_ip.check_config(front.factory) == ["gateway"]
    front.mode("gateway", "127.0.0.1:8080")
    assert client_ip.resolve(FRONT, f"192.0.2.50, 198.51.100.7, {GW}").ip == "198.51.100.7"
    assert await client_ip.check_config(front.factory) == []
    front.mode("gateway", "")
    assert client_ip.resolve(FRONT, f"192.0.2.50, 198.51.100.7, {GW}").ip == GW
    assert await client_ip.check_config(front.factory) == ["gateway"]
    async with front.factory() as s:
        rows = [e.message for e in await s.scalars(select(PanelError)) if e.action == "KERVAX_TRUSTED_PROXIES"]
    assert len(rows) == 1 and "KERVAX_BIND" in rows[0]


async def test_trusted_proxies_typo_is_reported_at_startup(front):
    front.mode("gateway privat")
    assert await client_ip.check_config(front.factory) == ["privat"]
    async with front.factory() as s:
        rows = [e for e in await s.scalars(select(PanelError)) if e.action == "KERVAX_TRUSTED_PROXIES"]
    assert len(rows) == 1 and "privat" in rows[0].message
    front.mode("gateway 203.0.113.0/24")
    assert await client_ip.check_config(front.factory) == []


def test_empty_value_keeps_caddy_trusted():
    """Пустое KERVAX_TRUSTED_PROXIES в схеме caddy не должно превращать caddy в адрес каждого
    клиента: в оверлее ":-" (пустое = умолчание private), и туда же передан KERVAX_BIND."""
    edge = (ROOT / "compose.caddy.yml").read_text(encoding="utf-8")
    assert "KERVAX_TRUSTED_PROXIES: ${KERVAX_TRUSTED_PROXIES:-private}" in edge
    assert "KERVAX_BIND: unpublished" in edge
    base = (ROOT / "compose.yml").read_text(encoding="utf-8")
    assert "KERVAX_BIND: ${KERVAX_BIND:-127.0.0.1:8080}" in base
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "empty value means" not in env


async def test_recreated_frontend_with_snapshot_is_not_a_false_alarm(front, monkeypatch):
    """Схема со снимком: фронтенд после выкладки получил новый адрес, снимок обновится сам за
    <=30 с. Пока адрес свежий (2 срока жизни снимка), подсказок "адрес не определен" нет."""
    import json as _json
    import time as _time

    from app.agent_auth import PROXY_MAX_AGE

    front.mode("private")
    data = Path(config.get_settings().data_dir)
    data.mkdir(parents=True, exist_ok=True)
    (data / "agent_proxy_peers.json").write_text(_json.dumps({
        "v": 1, "generated_at": int(_time.time()), "frontend_peer_ips": ["172.20.0.9"],
        "forwarded_proxy_ips": [CADDY]}), encoding="utf-8")
    who = client_ip.resolve(FRONT, f"198.51.100.7, {CADDY}")
    assert (who.known, who.quiet) == (False, True)
    assert (await front.post("/api/auth/login", json=BAD, headers={"X-Forwarded-For": f"198.51.100.7, {CADDY}"})).status_code == 401
    await panel_errors.drain()
    async with front.factory() as s:
        assert not [e for e in await s.scalars(select(PanelError)) if e.action == "адрес клиента"]
    # адрес так и не попал в снимок: дальше это уже не выкладка, а поломка - подсказка есть
    client_ip._peer_first[FRONT] -= 2 * PROXY_MAX_AGE + 1
    assert client_ip.resolve(FRONT, f"198.51.100.7, {CADDY}").quiet is False
    assert (await front.post("/api/auth/login", json=BAD, headers={"X-Forwarded-For": f"198.51.100.8, {CADDY}"})).status_code == 401
    await panel_errors.drain()
    async with front.factory() as s:
        assert [e for e in await s.scalars(select(PanelError)) if e.action == "адрес клиента"]
