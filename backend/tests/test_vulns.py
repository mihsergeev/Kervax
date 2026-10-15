"""Важные уязвимости в пакетах (app/vulns.py): сравнение версий как в dpkg, отбор из OVAL Canonical
только critical и уже эксплуатируемых (CISA KEV), сверка с пакетами и работающим ядром. Только
данные: ни алертов, ни пунктов "требует действий"."""
import bz2
import gzip
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import vulns
from app.models import VulnFeed

NS = ('xmlns="http://oval.mitre.org/XMLSchema/oval-definitions-5" '
      'xmlns:oval="http://oval.mitre.org/XMLSchema/oval-common-5" '
      'xmlns:ind-def="http://oval.mitre.org/XMLSchema/oval-definitions-5#independent" '
      'xmlns:unix-def="http://oval.mitre.org/XMLSchema/oval-definitions-5#unix" '
      'xmlns:linux-def="http://oval.mitre.org/XMLSchema/oval-definitions-5#linux"')
P = "oval:com.ubuntu.noble"


def _cve(cid, prio, tst, cvss="7.5"):
    return f'<cve priority="{prio}" cvss_score="{cvss}" test_ref="{P}:tst:{tst}">{cid}</cve>'


def _oval() -> bytes:
    """Как у Canonical: openssl (critical, high, medium из KEV, low), пакет без исправления, ядро
    linux и ядро riscv с тем же суффиксом -generic"""
    defs = f"""
    <definition id="{P}:def:100" class="inventory"><metadata><title>noble</title></metadata></definition>
    <definition id="{P}:def:1" class="vulnerability"><metadata><title>openssl</title><advisory>
      {_cve("CVE-2026-0001", "critical", 1, "9.8")}{_cve("CVE-2026-0002", "high", 1)}
      {_cve("CVE-2026-0003", "medium", 2)}{_cve("CVE-2026-0004", "low", 2)}
    </advisory></metadata><criteria><criteria operator="OR">
      <criterion test_ref="{P}:tst:1"/><criterion test_ref="{P}:tst:2"/></criteria></criteria></definition>
    <definition id="{P}:def:2" class="vulnerability"><metadata><title>sudo</title><advisory>
      {_cve("CVE-2026-0010", "critical", 3)}</advisory></metadata>
      <criteria><criterion test_ref="{P}:tst:3"/></criteria></definition>
    <definition id="{P}:def:3" class="vulnerability"><metadata><title>git</title><advisory>
      {_cve("CVE-2026-0020", "critical", 4)}</advisory></metadata>
      <criteria><criterion test_ref="{P}:tst:4"/></criteria></definition>
    <definition id="{P}:def:4" class="vulnerability"><metadata><title>linux</title><advisory>
      {_cve("CVE-2026-0100", "critical", 11, "9.4")}{_cve("CVE-2026-0101", "high", 11)}
      {_cve("CVE-2026-0102", "high", 12)}</advisory></metadata>
      <criteria><criteria operator="OR">
        <criteria operator="AND"><criterion test_ref="{P}:tst:10"/><criterion test_ref="{P}:tst:11"/></criteria>
        <criteria operator="AND"><criterion test_ref="{P}:tst:10"/><criterion test_ref="{P}:tst:12"/></criteria>
      </criteria></criteria></definition>
    <definition id="{P}:def:5" class="vulnerability"><metadata><title>linux-riscv</title><advisory>
      {_cve("CVE-2026-0200", "critical", 21)}</advisory></metadata>
      <criteria><criteria operator="AND"><criterion test_ref="{P}:tst:20"/><criterion test_ref="{P}:tst:21"/>
      </criteria></criteria></definition>"""
    tests = f"""
    <linux-def:dpkginfo_test id="{P}:tst:1"><linux-def:object object_ref="{P}:obj:1"/><linux-def:state state_ref="{P}:ste:1"/></linux-def:dpkginfo_test>
    <linux-def:dpkginfo_test id="{P}:tst:2"><linux-def:object object_ref="{P}:obj:1"/><linux-def:state state_ref="{P}:ste:2"/></linux-def:dpkginfo_test>
    <linux-def:dpkginfo_test id="{P}:tst:3"><linux-def:object object_ref="{P}:obj:3"/></linux-def:dpkginfo_test>
    <linux-def:dpkginfo_test id="{P}:tst:4"><linux-def:object object_ref="{P}:obj:4"/><linux-def:state state_ref="{P}:ste:4"/></linux-def:dpkginfo_test>
    <unix-def:uname_test id="{P}:tst:10"><unix-def:object object_ref="{P}:obj:200"/><unix-def:state state_ref="{P}:ste:10"/></unix-def:uname_test>
    <ind-def:variable_test id="{P}:tst:11"><ind-def:object object_ref="{P}:obj:210"/><ind-def:state state_ref="{P}:ste:11"/></ind-def:variable_test>
    <ind-def:variable_test id="{P}:tst:12"><ind-def:object object_ref="{P}:obj:210"/><ind-def:state state_ref="{P}:ste:12"/></ind-def:variable_test>
    <unix-def:uname_test id="{P}:tst:20"><unix-def:object object_ref="{P}:obj:200"/><unix-def:state state_ref="{P}:ste:20"/></unix-def:uname_test>
    <ind-def:variable_test id="{P}:tst:21"><ind-def:object object_ref="{P}:obj:210"/><ind-def:state state_ref="{P}:ste:21"/></ind-def:variable_test>"""
    objects = f"""
    <linux-def:dpkginfo_object id="{P}:obj:1"><linux-def:name var_ref="{P}:var:1"/></linux-def:dpkginfo_object>
    <linux-def:dpkginfo_object id="{P}:obj:3"><linux-def:name var_ref="{P}:var:3"/></linux-def:dpkginfo_object>
    <linux-def:dpkginfo_object id="{P}:obj:4"><linux-def:name var_ref="{P}:var:4"/></linux-def:dpkginfo_object>"""
    states = f"""
    <linux-def:dpkginfo_state id="{P}:ste:1"><linux-def:evr datatype="debian_evr_string" operation="less than">3.0.13-0ubuntu3.5</linux-def:evr></linux-def:dpkginfo_state>
    <linux-def:dpkginfo_state id="{P}:ste:2"><linux-def:evr datatype="debian_evr_string" operation="less than">3.0.13-0ubuntu3.10</linux-def:evr></linux-def:dpkginfo_state>
    <linux-def:dpkginfo_state id="{P}:ste:4"><linux-def:evr datatype="debian_evr_string" operation="less than">1:2.43.0-1ubuntu7.3+esm1</linux-def:evr></linux-def:dpkginfo_state>
    <unix-def:uname_state id="{P}:ste:10"><unix-def:os_release operation="pattern match">6\\.8\\.0\\-\\d+(\\-generic|\\-generic\\-64k)</unix-def:os_release></unix-def:uname_state>
    <ind-def:variable_state id="{P}:ste:11"><ind-def:value datatype="debian_evr_string" operation="less than">0:6.8.0-58</ind-def:value></ind-def:variable_state>
    <ind-def:variable_state id="{P}:ste:12"><ind-def:value datatype="debian_evr_string" operation="less than">0:6.8.0-147</ind-def:value></ind-def:variable_state>
    <unix-def:uname_state id="{P}:ste:20"><unix-def:os_release operation="pattern match">6\\.8\\.0\\-\\d+(\\-generic)</unix-def:os_release></unix-def:uname_state>
    <ind-def:variable_state id="{P}:ste:21"><ind-def:value datatype="debian_evr_string" operation="less than">0:6.8.0-1030</ind-def:value></ind-def:variable_state>"""
    variables = f"""
    <constant_variable id="{P}:var:1" datatype="string"><value>openssl</value><value>libssl3t64</value></constant_variable>
    <constant_variable id="{P}:var:3" datatype="string"><value>sudo</value></constant_variable>
    <constant_variable id="{P}:var:4" datatype="string"><value>git</value></constant_variable>"""
    xml = (f'<?xml version="1.0"?><oval_definitions {NS}><generator><oval:timestamp>2026-10-14T10:55:20'
           f'</oval:timestamp></generator><definitions>{defs}</definitions><tests>{tests}</tests>'
           f'<objects>{objects}</objects><states>{states}</states><variables>{variables}</variables></oval_definitions>')
    return bz2.compress(xml.encode())


KEV = {"CVE-2026-0003", "CVE-2026-0102", "CVE-1999-0001"}


def test_vercmp_like_dpkg():
    lt = [("1.0~rc1", "1.0"), ("1.0", "1.0-1"), ("2.0", "1:1.0"), ("3.0.13-0ubuntu3.5", "3.0.13-0ubuntu3.10"),
          ("1:2.25.1-1ubuntu3.14", "1:2.25.1-1ubuntu3.14+esm1"), ("6.8.0-91", "6.8.0-147"), ("1.0", "1.0a"),
          ("2:11.3.0-2ubuntu0~ubuntu20.04.8", "2:11.3.0-2ubuntu0~ubuntu20.04.8+esm1"), ("1.0~~", "1.0~")]
    for a, b in lt:
        assert vulns.vercmp(a, b) < 0, (a, b)
        assert vulns.vercmp(b, a) > 0, (b, a)
    assert vulns.vercmp("1.0-1", "1.0-1") == 0
    assert vulns.vercmp("0:1.0-1", "1.0-1") == 0
    assert vulns.vercmp("1.01", "1.1") == 0


def test_release_of():
    assert vulns.release_of("Ubuntu 24.04.3 LTS") == "noble"
    assert vulns.release_of("Ubuntu 20.04.6 LTS") == "focal"
    assert vulns.release_of("Ubuntu 26.04 LTS") == "resolute"
    assert vulns.release_of("Debian GNU/Linux 12 (bookworm)") == ""
    assert vulns.release_of("") == ""


def test_feed_keeps_only_important_with_a_fix():
    feed = vulns.load_oval(_oval(), KEV)
    assert feed["gen"] == "2026-10-14T10:55:20"
    # openssl: critical и medium из KEV остаются, high и low - нет; sudo без исправления - нет
    ids = sorted(c[0] for e in feed["pkgs"]["libssl3t64"] for c in e[2])
    assert ids == ["CVE-2026-0001", "CVE-2026-0003"]
    assert "sudo" not in feed["pkgs"]
    assert feed["pkgs"]["git"][0][1] == "1:2.43.0-1ubuntu7.3+esm1"
    kern = {(k[0], k[2]): [c[0] for c in k[3]] for k in feed["kernel"]}
    assert kern == {("linux", "6.8.0-58"): ["CVE-2026-0100"], ("linux", "6.8.0-147"): ["CVE-2026-0102"],
                    ("linux-riscv", "6.8.0-1030"): ["CVE-2026-0200"]}
    assert feed["kev"] == ["CVE-2026-0003", "CVE-2026-0102"]


def test_match_packages_and_running_kernel():
    feed = vulns.load_oval(_oval(), KEV)
    items = [["openssl", "3.0.13-0ubuntu3.1"], ["libssl3t64", "3.0.13-0ubuntu3.1"], ["git", "1:2.43.0-1ubuntu7.2"],
             ["sudo", "1.9.15p5-3ubuntu5"], ["curl", "8.5.0-2ubuntu10.6"]]
    found = vulns.match(feed, items, "6.8.0-110-generic")
    by = {f["src"]: f for f in found}
    assert set(by) == {"openssl", "git", "linux"}
    ossl = by["openssl"]
    assert ossl["pkgs"] == [["openssl", "3.0.13-0ubuntu3.1"], ["libssl3t64", "3.0.13-0ubuntu3.1"]]
    assert ossl["fixed"] == "3.0.13-0ubuntu3.10" and not ossl["pro"] and not ossl["kernel"]
    # эксплуатируемая - первой, хоть у нее и medium
    assert [(c["id"], c["kev"]) for c in ossl["cves"]] == [("CVE-2026-0003", True), ("CVE-2026-0001", False)]
    assert by["git"]["pro"] is True
    # ядро: работает 6.8.0-110, первое исправление (58) уже стоит, второе (147) - нет; riscv с тем же
    # суффиксом -generic не в счет
    k = by["linux"]
    assert k["kernel"] and k["fixed"] == "6.8.0-147" and [c["id"] for c in k["cves"]] == ["CVE-2026-0102"]
    assert found[0]["src"] in ("openssl", "linux")  # эксплуатируемые выше
    sm = vulns.summary(found)
    assert sm == {"pkgs": 3, "cves": 4, "critical": 2, "kev": 2, "pro": 1, "kernel": True}
    # все обновлено и ядро новое - пусто
    fresh = [["openssl", "3.0.13-0ubuntu3.10"], ["libssl3t64", "3.0.13-0ubuntu3.10"], ["git", "1:2.43.0-1ubuntu7.3+esm1"]]
    assert vulns.match(feed, fresh, "6.8.0-147-generic") == []
    assert vulns.pro_attached(fresh) and not vulns.pro_attached(items)
    assert vulns.match(None, items, "") == []


def test_kev_parse():
    raw = json.dumps({"vulnerabilities": [{"cveID": "CVE-2026-0003"}, {"cveID": ""}, "x"]}).encode()
    assert vulns.parse_kev(raw) == {"CVE-2026-0003"}
    assert vulns.parse_kev(b"not json") == set()


async def _enroll(client, auth_headers, name):
    r = await client.post("/api/servers", json={"name": name}, headers=auth_headers)
    assert r.status_code == 201
    return r.json()["server"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}


async def test_server_and_fleet_api(client, auth_headers):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    feed = vulns.load_oval(_oval(), KEV)
    async with f() as s:
        s.add(VulnFeed(release="noble", data=feed, gen=feed["gen"], ts=datetime.now(timezone.utc)))
        await s.commit()
    sid, h = await _enroll(client, auth_headers, "web-a")
    sid_b, h_b = await _enroll(client, auth_headers, "db-a")
    rep = {"hostname": "h", "agent_version": "2.32", "os": "Ubuntu 24.04.3 LTS",
           "os_upd": {"kernel": "6.8.0-110-generic", "updates": 3, "security": 1, "ts": 1},
           "pkgs": {"mgr": "dpkg", "n": 2, "h": "aaa"}}
    assert (await client.post("/api/agent/report", json=rep, headers=h)).status_code == 200
    body = json.dumps({"h": "aaa", "mgr": "dpkg", "part": 0, "parts": 1,
                       "items": [["libssl3t64", "3.0.13-0ubuntu3.1"], ["git", "1:2.43.0-1ubuntu7.2"]]}).encode()
    r = await client.post("/api/agent/packages", content=gzip.compress(body),
                          headers={**h, "Content-Type": "application/json", "Content-Encoding": "gzip"})
    assert r.status_code == 200 and r.json()["done"] is True
    j = (await client.get(f"/api/servers/{sid}/vulns", headers=auth_headers)).json()
    assert j["release"] == "noble" and j["feed_gen"] == "2026-10-14T10:55:20" and j["eol"] == ""
    assert {x["src"] for x in j["found"]} == {"openssl", "git", "linux"}
    assert j["summary"]["kev"] == 2 and j["pro_on"] is False
    # сервер без пакетов и с другой ОС - пусто, без ошибок
    rep_b = {**rep, "os": "Debian GNU/Linux 12 (bookworm)", "pkgs": None}
    assert (await client.post("/api/agent/report", json=rep_b, headers=h_b)).status_code == 200
    jb = (await client.get(f"/api/servers/{sid_b}/vulns", headers=auth_headers)).json()
    assert jb["release"] == "" and jb["found"] == []
    fl = (await client.get("/api/vulns", headers=auth_headers)).json()
    assert [x["name"] for x in fl["servers"]] == ["web-a", "db-a"]
    assert fl["feeds"][0]["release"] == "noble"


async def test_refresh_downloads_and_keeps_old_on_error(client, monkeypatch):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    oval = _oval()
    kev = json.dumps({"vulnerabilities": [{"cveID": c} for c in KEV]}).encode()
    calls = []

    async def fake_get(_client, url):
        calls.append(url)
        if "kev" in url or "known_exploited" in url:
            return kev
        if "noble" in url:
            return oval
        raise RuntimeError("нет сети")

    monkeypatch.setattr(vulns, "_get", fake_get)
    errs = await vulns.refresh(f, ["noble", "jammy"], now)
    assert list(errs) == ["jammy"]
    async with f() as s:
        noble = await s.get(VulnFeed, "noble")
        jammy = await s.get(VulnFeed, "jammy")
        assert noble.data["kev"] == ["CVE-2026-0003", "CVE-2026-0102"] and noble.err == ""
        assert jammy.data is None and "нет сети" in jammy.err
    # следующая загрузка noble упала - вчерашние данные остались
    async def broken(_client, url):
        raise RuntimeError("таймаут")
    monkeypatch.setattr(vulns, "_get", broken)
    await vulns.refresh(f, ["noble"], now + timedelta(days=1))
    async with f() as s:
        noble = await s.get(VulnFeed, "noble")
        assert noble.data["kev"] == ["CVE-2026-0003", "CVE-2026-0102"] and "таймаут" in noble.err


@pytest.mark.parametrize("age_h,err,tried_h,due", [
    (None, "", None, True), (2, "", None, False), (25, "", None, True),
    (25, "boom", 0.5, False), (25, "boom", 2, True)])
async def test_due_releases(client, age_h, err, tried_h, due):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    from app.models import Server
    async with f() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, os="Ubuntu 24.04.3 LTS"))
        if age_h is not None:
            s.add(VulnFeed(release="noble", data={}, ts=now - timedelta(hours=age_h), err=err,
                           tried=now - timedelta(hours=tried_h) if tried_h is not None else None))
        await s.commit()
        assert (await vulns.due_releases(s, now) == ["noble"]) is due
