"""Куда панель ходит по мониторам: адреса со встроенным IPv4, старые мониторы с NULL, проверка
через локацию и имена и адреса из отчета агента (app/net_guard.py)."""

import asyncio
import types

import httpx
import pytest
from sqlalchemy import select

from app import checks as checks_exec
from app import collector, domain_probe, net_guard
from app.api.checks import _norm_domain
from app.config import Settings
from app.models import AuditLog, Check


def _factory(client: httpx.AsyncClient):
    return client._transport.app.state.session_factory  # noqa: SLF001


@pytest.mark.parametrize("ip, cls", [
    # NAT64 общего префикса: класс самого IPv4
    ("64:ff9b::a00:1", net_guard.INTERNAL),
    ("64:ff9b::a9fe:a9fe", net_guard.FORBIDDEN),
    ("64:ff9b::808:808", net_guard.PUBLIC),
    # 6to4, Teredo (клиент инвертирован), IPv4-compatible
    ("2002:a00:1::", net_guard.INTERNAL),
    ("2002:a9fe:a9fe::1", net_guard.FORBIDDEN),
    ("2001:0:a9fe:a9fe::", net_guard.FORBIDDEN),
    ("2001:0:c000:201:8000:63bf:5601:5656", net_guard.FORBIDDEN),
    ("::a9fe:a9fe", net_guard.FORBIDDEN),
    ("::a00:1", net_guard.INTERNAL),
    # метаданные облаков вне link-local
    ("168.63.129.16", net_guard.FORBIDDEN),
    ("192.0.0.192", net_guard.FORBIDDEN),
    ("100.100.100.200", net_guard.FORBIDDEN),
    ("fd00:ec2::254", net_guard.FORBIDDEN),
    ("fd20:ce::254", net_guard.FORBIDDEN),
    ("64:ff9b::a83f:8110", net_guard.FORBIDDEN),
    # прежние ответы не меняются
    ("::ffff:169.254.169.254", net_guard.FORBIDDEN),
    ("::ffff:8.8.8.8", net_guard.PUBLIC),
    ("::1", net_guard.INTERNAL),
    ("2001:db8::1", net_guard.INTERNAL),
])
def test_embedded_ipv4_uses_ipv4_rules(ip, cls):
    assert net_guard.ip_class(ip) == cls


# Локальный NAT64 64:ff9b:1::/48 (RFC 8215) с префиксом оператора /48, /56, /64 и /96: IPv4 по
# раскладкам RFC 6052. Метаданные в каждой раскладке видны только ей самой (без нее адрес был
# бы внутренним), а 10.0.0.1 не должен стать "метаданными" из-за нулей чужой раскладки
@pytest.mark.parametrize("ip, cls", [
    ("64:ff9b:1:a83f:81:1000::", net_guard.FORBIDDEN),     # /48, 168.63.129.16
    ("64:ff9b:1:a8:3f:8110::", net_guard.FORBIDDEN),       # /56
    ("64:ff9b:1:0:a8:3f81:1000:0", net_guard.FORBIDDEN),   # /64
    ("64:ff9b:1::a83f:8110", net_guard.FORBIDDEN),         # /96
    ("64:ff9b:1:a9fe:a9:fe00::", net_guard.FORBIDDEN),     # /48, 169.254.169.254
    ("64:ff9b:1:a9:fe:a9fe::", net_guard.FORBIDDEN),       # /56
    ("64:ff9b:1:0:a9:fea9:fe00:0", net_guard.FORBIDDEN),   # /64
    ("64:ff9b:1::a9fe:a9fe", net_guard.FORBIDDEN),         # /96
    ("64:ff9b:1:a00:0:100::", net_guard.INTERNAL),         # /48, 10.0.0.1
    ("64:ff9b:1:a:0:1::", net_guard.INTERNAL),             # /56
    ("64:ff9b:1:0:a:0:100:0", net_guard.INTERNAL),         # /64
    ("64:ff9b:1::a00:1", net_guard.INTERNAL),              # /96
    # /96 с ненулевым байтом u: раскладки короче /96 тут невозможны, хотя дали бы 169.254.x.x
    ("64:ff9b:1:a9fe:ffa9:fe00:a00:1", net_guard.INTERNAL),
])
def test_local_nat64_layouts(ip, cls):
    assert net_guard.ip_class(ip) == cls


async def test_metadata_behind_nat64_is_refused_even_for_admin_monitor(monkeypatch):
    async def resolve(host, timeout):
        return ["64:ff9b::a9fe:a9fe"]

    sent = []

    async def send(self, request):
        sent.append(request.url.host)
        return httpx.Response(200)

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    check = types.SimpleNamespace(
        type="http", target="http://nat64.example.test/", timeout_ms=2000, degraded_ms=1000, retries=0,
        method="GET", keyword_up="", keyword_down="", expected_status="200-399", http_headers="",
        auth_method="", allow_internal_target=True,
    )
    out = await checks_exec.run_check(check)
    assert out.status == "down" and net_guard.FORBIDDEN_TEXT in out.message
    assert sent == []


@pytest.mark.pg
async def test_legacy_null_is_settled_and_public_monitor_stops_following_rebinding(client, monkeypatch):
    answers = {"legacy-public.example.test": ["8.8.8.8"], "legacy-inner.example.test": ["10.0.0.9"]}
    lookups = []

    async def resolve(host, timeout):
        lookups.append(host)
        return list(answers.get(host, []))

    async def fake_run(check):
        return checks_exec.CheckOutcome("up", latency_ms=5, message="HTTP 200")

    real_run = checks_exec.run_check
    monkeypatch.setattr(collector, "_legacy_retry", {})
    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(checks_exec, "run_check", fake_run)
    rows = {
        "public": Check(name="public", type="http", target="https://legacy-public.example.test/"),
        "inner": Check(name="inner", type="http", target="https://legacy-inner.example.test/"),
        "literal": Check(name="literal", type="tcp_port", target="192.0.2.10", port=22),
        "unresolved": Check(name="unresolved", type="http", target="https://gone.example.test/"),
        "resolver": Check(name="resolver", type="dns", target="example.test", dns_server="127.0.0.1"),
        "doh": Check(name="doh", type="dns", target="example.test", dns_server="https://dns.example.test/dns-query"),
    }
    async with _factory(client)() as s:
        s.add_all(rows.values())
        await s.commit()

    await collector.run_due_checks(_factory(client), Settings())

    async with _factory(client)() as s:
        got = {c.name: c.allow_internal_target for c in await s.scalars(select(Check))}
        audit = [(a.username, a.action, a.target, a.detail) for a in await s.scalars(
            select(AuditLog).where(AuditLog.action == "internal_target_auto"))]
    assert got == {"public": False, "inner": True, "literal": True, "unresolved": None,
                   "resolver": True, "doh": False}
    # одобрение имени по ответу DNS видно в журнале, адрес-литерал одобрен и так
    assert audit == [("system", "internal_target_auto", "inner", "legacy-inner.example.test -> 10.0.0.9")]

    # имя не разрешилось: на следующих тиках не резолвим его снова до DNS_RETRY
    async with _factory(client)() as s:
        due = list(await s.scalars(select(Check)))
    lookups.clear()
    await collector._settle_legacy_targets(_factory(client), due)
    assert lookups == []
    collector._legacy_retry.update(dict.fromkeys(collector._legacy_retry, 0))
    await collector._settle_legacy_targets(_factory(client), due)
    assert lookups == ["gone.example.test"]

    # имя внешнего монитора потом указало во внутреннюю сеть: панель туда не идет
    answers["legacy-public.example.test"] = ["127.0.0.1"]
    sent = []

    async def send(self, request):
        sent.append(request.url.host)
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    async with _factory(client)() as s:
        public = await s.scalar(select(Check).where(Check.name == "public"))
    public.retries = 0
    out = await real_run(public)
    assert out.status == "down" and net_guard.ADMIN_ONLY_TEXT in out.message
    assert sent == []


@pytest.mark.pg
async def test_settle_does_not_overwrite_a_concurrent_edit(client, monkeypatch):
    async def resolve(host, timeout):
        return ["10.0.0.9"]

    monkeypatch.setattr(collector, "_legacy_retry", {})
    monkeypatch.setattr(net_guard, "_resolve", resolve)
    async with _factory(client)() as s:
        moved = Check(name="moved", type="http", target="https://moved.example.test/")
        approved = Check(name="approved", type="http", target="https://approved.example.test/")
        s.add_all([moved, approved])
        await s.commit()
        stale = list(await s.scalars(select(Check).order_by(Check.id)))
    # пока тик решал, монитор перенацелили, а второй сохранил админ с явным решением
    async with _factory(client)() as s:
        (await s.get(Check, stale[0].id)).target = "https://elsewhere.example.test/"
        (await s.get(Check, stale[1].id)).allow_internal_target = False
        await s.commit()

    await collector._settle_legacy_targets(_factory(client), stale)

    async with _factory(client)() as s:
        assert (await s.get(Check, stale[0].id)).allow_internal_target is None
        assert (await s.get(Check, stale[1].id)).allow_internal_target is False
        assert not list(await s.scalars(select(AuditLog).where(AuditLog.action == "internal_target_auto")))


async def test_slow_dns_does_not_hold_the_tick(client, monkeypatch):
    async def resolve(host, timeout):
        await asyncio.sleep(30)
        return ["10.0.0.9"]

    monkeypatch.setattr(collector, "_legacy_retry", {})
    monkeypatch.setattr(collector, "SETTLE_BUDGET", 0.2)
    monkeypatch.setattr(net_guard, "_resolve", resolve)
    async with _factory(client)() as s:
        s.add_all([Check(name=f"slow{i}", type="http", target=f"https://slow{i}.example.test/") for i in range(20)])
        await s.commit()
        due = list(await s.scalars(select(Check)))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await collector._settle_legacy_targets(_factory(client), due)
    assert loop.time() - t0 < 2
    assert set(collector._legacy_retry) == {c.id for c in due}


async def test_location_proxy_needs_explicit_internal_approval(monkeypatch):
    calls = []

    def hook(allow_internal=True):
        calls.append(allow_internal)

        async def noop(request):
            raise httpx.ConnectError("stop", request=request)

        return noop

    # NULL старого монитора одобрял внутренние адреса панели, а через локацию это сеть прокси
    monkeypatch.setattr(net_guard, "request_hook", hook)
    for value in (None, False, True):
        check = types.SimpleNamespace(
            type="http", target="http://10.0.0.5/", timeout_ms=1000, degraded_ms=500, method="GET",
            keyword_up="", keyword_down="", expected_status="200-399", http_headers="", auth_method="",
            allow_internal_target=value,
        )
        out = await checks_exec._run_http(check, proxy="http://192.0.2.1:3128")
        assert out.status == "down"
    assert calls == [False, False, True]


async def test_agent_reported_hosts_never_get_internal_access(client, auth_headers, monkeypatch):
    from app.models import Server

    reported = ["10.0.0.7.", "127.0.0.1.:443", "127.1./x", "0x7f.1.:80", "inner.example.test",
                "x.localhost", "outer.example.test"]
    async with _factory(client)() as s:
        s.add(Server(name="node-literal", token_hash="literal-fake", last_report={
            "web_services": [{"kind": "nginx", "sites": reported}],
        }))
        await s.commit()

    async def resolve(host, timeout):
        # как glibc: короткую форму IPv4 резолвер сам превращает в адрес
        if host in ("127.1", "0x7f.1", "inner.example.test", "x.localhost"):
            return ["127.0.0.1"]
        return ["8.8.8.8"] if host == "outer.example.test" else []

    sent = []

    async def send(self, request):
        sent.append(request.url.host)
        return httpx.Response(200)

    monkeypatch.setattr(net_guard, "_resolve", resolve)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    r = await client.post("/api/checks/discovered/probe", headers=auth_headers, json={"domains": reported})
    assert r.status_code == 200, r.text
    items = {i["domain"]: i for i in (await client.get(
        "/api/checks/discovered/probes", headers=auth_headers)).json()["items"]}
    # даже у админа проба найденного домена идет только наружу
    assert sent == ["8.8.8.8"] and items["outer.example.test"]["ext_status"] == "up"
    for literal in ("10.0.0.7", "127.0.0.1", "127.1", "0x7f.1"):
        assert items[literal]["ext_status"] == "down"
        assert items[literal]["ext_message"] == domain_probe.IP_INTERNAL_TEXT
    for name in ("inner.example.test", "x.localhost"):
        assert items[name]["ext_status"] == "down"
        assert items[name]["ext_message"] == domain_probe.NAME_INTERNAL_TEXT


def test_ip_literal_forms():
    for literal in ("10.0.0.7", "127.1", "0x7f.1", "127.0.0.1.", "0x7f.1.", "2001:db8::1"):
        assert domain_probe.ip_literal(literal), literal
    assert not domain_probe.ip_literal("inner.example.test")
    # точка на конце снимается после пути и порта
    assert [_norm_domain(x) for x in ("127.0.0.1./", "127.0.0.1.:443", "127.1./x", "0x7f.1.:80",
                                      "https://Example.test.:8443/a")] == [
        "127.0.0.1", "127.0.0.1", "127.1", "0x7f.1", "example.test"]
