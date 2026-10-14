"""DNS-мониторы: запись в ожидаемое значение или без изменений с первого ответа."""
from datetime import datetime, timezone
from types import SimpleNamespace

import dns.asyncresolver
import dns.exception
import dns.resolver
import pytest

from app import checks, collector
from app.config import Settings
from app.models import Check

ANSWERS: dict[tuple[str, str], object] = {}


def _a(*ips):
    return [SimpleNamespace(address=ip) for ip in ips]


def _mx(*pairs):
    return [SimpleNamespace(preference=p, exchange=h + ".") for p, h in pairs]


def _txt(*texts):
    return [SimpleNamespace(strings=[t.encode()]) for t in texts]


@pytest.fixture(autouse=True)
def fake_resolver(monkeypatch):
    ANSWERS.clear()

    async def resolve(self, name, rtype, raise_on_no_answer=True):
        got = ANSWERS.get((name, rtype))
        if isinstance(got, Exception):
            raise got
        return SimpleNamespace(rrset=got)

    monkeypatch.setattr(dns.asyncresolver.Resolver, "resolve", resolve)


def _chk(**kw):
    base = dict(type="dns", target="example.com", dns_type="A", dns_expect="", dns_match="equal",
                dns_server="", dns_baseline=None, timeout_ms=3000, retries=0)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_expected_equal_and_contains():
    ANSWERS[("example.com", "A")] = _a("203.0.113.10", "203.0.113.11")
    out = await checks.run_check(_chk(dns_expect="203.0.113.11\n203.0.113.10"))
    assert out.status == "up" and out.message == "A: 203.0.113.10, 203.0.113.11"
    out = await checks.run_check(_chk(dns_expect="203.0.113.10"))
    assert out.status == "down" and "ждали 203.0.113.10" in out.message
    out = await checks.run_check(_chk(dns_expect="203.0.113.10", dns_match="contains"))
    assert out.status == "up"
    ANSWERS[("example.com", "TXT")] = _txt("v=spf1 include:_spf.example.net -all", "google-site-verification=x")
    out = await checks.run_check(_chk(dns_type="TXT", dns_match="contains",
                                      dns_expect="v=spf1 include:_spf.example.net -all"))
    assert out.status == "up"


async def test_mx_host_without_priority():
    ANSWERS[("example.com", "MX")] = _mx((10, "MX.example.com"), (20, "mx2.example.com"))
    out = await checks.run_check(_chk(dns_type="MX", dns_expect="mx.example.com\nmx2.example.com."))
    assert out.status == "up" and out.answers == ["10 mx.example.com", "20 mx2.example.com"]


async def test_baseline_remember_then_change():
    ANSWERS[("example.com", "A")] = _a("203.0.113.10")
    out = await checks.run_check(_chk())
    assert out.status == "up" and out.message.endswith("(запомнили)") and out.answers == ["203.0.113.10"]
    ANSWERS[("example.com", "A")] = _a("198.51.100.7")
    out = await checks.run_check(_chk(dns_baseline=["203.0.113.10"]))
    assert out.status == "down" and out.message == "ответ изменился: было 203.0.113.10, стало 198.51.100.7"


async def test_failures():
    ANSWERS[("gone.example", "A")] = dns.resolver.NXDOMAIN()
    out = await checks.run_check(_chk(target="gone.example"))
    assert out.status == "down" and "NXDOMAIN" in out.message
    ANSWERS[("slow.example", "A")] = dns.exception.Timeout()
    assert "не ответил" in (await checks.run_check(_chk(target="slow.example"))).message
    ANSWERS[("example.com", "MX")] = None
    out = await checks.run_check(_chk(dns_type="MX"))
    assert out.status == "down" and out.message == "записей MX нет"


async def test_api_create_normalizes_and_validates(client, auth_headers):
    r = await client.post("/api/checks", json={"name": "dns сайта", "type": "dns",
                                               "target": "https://Example.COM./path", "retries": 3,
                                               "check_ssl": True}, headers=auth_headers)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["target"] == "example.com" and j["dns_type"] == "A" and j["retries"] == 1
    assert j["check_ssl"] is False and j["check_locations"] is False
    r = await client.post("/api/checks", json={"name": "x", "type": "dns", "target": "example.com",
                                               "dns_server": "dns.google"}, headers=auth_headers)
    assert r.status_code == 400 and "IP" in r.json()["detail"]


async def test_baseline_stored_accept_and_reset(client, auth_headers):
    ANSWERS[("example.com", "A")] = _a("203.0.113.10")
    r = await client.post("/api/checks", json={"name": "dns", "type": "dns", "target": "example.com"},
                          headers=auth_headers)
    cid = r.json()["id"]
    run = (await client.post(f"/api/checks/{cid}/run", headers=auth_headers)).json()
    assert run["run_status"] == "up"
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        assert (await s.get(Check, cid)).dns_baseline == ["203.0.113.10"]
    # переехали: монитор красный, принимаем новый ответ - зеленеет сразу
    ANSWERS[("example.com", "A")] = _a("198.51.100.7")
    run = (await client.post(f"/api/checks/{cid}/run", headers=auth_headers)).json()
    assert run["run_status"] == "down" and run["dns_last"] == ["198.51.100.7"]
    r = await client.post(f"/api/checks/{cid}/dns-accept", headers=auth_headers)
    assert r.status_code == 200 and r.json()["run_status"] == "up" and r.json()["dns_baseline"] == ["198.51.100.7"]
    # спрашиваем другое - запомненное сбрасывается
    r = await client.patch(f"/api/checks/{cid}", json={"dns_type": "MX"}, headers=auth_headers)
    assert r.json()["dns_baseline"] is None


async def test_dns_alerts_are_not_folded(client, monkeypatch):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        a = Check(name="A example.com", type="dns", target="example.com", dns_type="A")
        b = Check(name="MX example.com", type="dns", target="example.com", dns_type="MX")
        s.add_all([a, b])
        await s.commit()
        ia, ib = a.id, b.id
    sent = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    pending = [("bad", "A example.com", "down", "ответ изменился", None, ia, None),
               ("bad", "MX example.com", "down", "записей MX нет", None, ib, None)]
    await collector._send_alerts(f, Settings(alert_webhook="http://hook"), pending, datetime.now(timezone.utc))
    assert len(sent) == 2
