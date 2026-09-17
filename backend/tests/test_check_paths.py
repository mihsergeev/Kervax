"""Additional paths of one monitor: the site and its API on one domain.

Live case (17.09.2026, corpsoft24): foundry-dev.corplab.ai is checked from inside the server,
and its /health (the API, another service in the cluster) also had to be watched. A second
monitor on the same domain would double the certificate and domain checks with their alerts,
so the paths live inside the monitor and are checked the same way as the main address.
"""

import types
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app import checks as checks_exec
from app import collector, manual_probe
from app.config import Settings
from app.db import create_engine_and_factory
from app.models import AgentPathProbe, Base, Check, ProbeRequest, Server

REPORT = {
    "hostname": "node", "os": "Ubuntu 24.04", "agent_version": "2.6",
    "cpu_percent": 1.0, "mem_used": 1, "mem_total": 2,
    "load": [0.1, 0.1, 0.1], "disks": [{"mount": "/", "used": 1, "total": 2}],
}


def _factory(client: httpx.AsyncClient):
    return client._transport.app.state.session_factory  # noqa: SLF001


async def test_paths_are_cleaned_and_saved(client, auth_headers):
    r = await client.post("/api/checks", headers=auth_headers, json={
        "name": "foundry", "type": "http", "target": "https://foundry.example",
        "extra_paths": [" /health ", "", "/health", "/api/status?full=1"],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["extra_paths"] == ["/health", "/api/status?full=1"]
    cid = body["id"]

    for bad in (["health"], ["/a b"], ["//other.example/x"], [f"/p{i}" for i in range(11)]):
        r = await client.patch(f"/api/checks/{cid}", headers=auth_headers, json={"extra_paths": bad})
        assert r.status_code == 422, bad

    # a removed path takes its agent result with it, the change time is kept for the warm-up
    async with _factory(client)() as s:
        s.add(AgentPathProbe(check_id=cid, path="/api/status?full=1", server_id=1,
                             ts=datetime.now(timezone.utc)))
        await s.commit()
    r = await client.patch(f"/api/checks/{cid}", headers=auth_headers, json={"extra_paths": ["/health"]})
    assert r.status_code == 200 and r.json()["extra_paths"] == ["/health"]
    async with _factory(client)() as s:
        assert (await s.scalars(select(AgentPathProbe))).all() == []
        assert (await s.get(Check, cid)).paths_changed_at is not None


def _fake_client(answers: dict, calls: list):
    """httpx.AsyncClient that answers by URL: a status code or an exception."""

    class FakeResp:
        def __init__(self, code):
            self.status_code = code
            self.text = ""

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            calls.append(url)
            got = answers[url]
            if isinstance(got, Exception):
                raise got
            return FakeResp(got)

    return FakeClient


def _check(target="https://foundry.example", paths=("/health",)):
    return types.SimpleNamespace(
        type="http", target=target, timeout_ms=3000, degraded_ms=2000, method="GET",
        keyword_up="", keyword_down="", expected_status="200-399", extra_paths=list(paths),
    )


async def test_panel_checks_the_paths(monkeypatch):
    calls: list[str] = []
    answers = {"https://foundry.example": 200, "https://foundry.example/health": 502}
    monkeypatch.setattr("app.checks.httpx.AsyncClient", _fake_client(answers, calls))

    out = await checks_exec._run_http(_check())
    assert out.status == "down" and out.message.startswith("/health: ") and "HTTP 502" in out.message
    assert calls == ["https://foundry.example", "https://foundry.example/health"]
    assert [(r["path"], r["status"]) for r in out.path_results] == [("/health", "down")]

    answers["https://foundry.example/health"] = 200
    out = await checks_exec._run_http(_check())
    assert out.status == "up" and out.message.startswith("HTTP 200")
    assert out.path_results[0]["status"] == "up"

    # the main address is down: the paths are not requested, the verdict is about the site
    calls.clear()
    answers["https://foundry.example"] = httpx.ConnectError("refused")
    out = await checks_exec._run_http(_check(target="https://foundry.example"))
    assert out.status == "down" and calls == ["https://foundry.example"]
    assert out.path_results[0]["status"] == "skipped"

    # https did not come up and the scheme was ours: the paths go over the same http
    calls.clear()
    answers.update({"https://foundry.example": httpx.ConnectError("refused"),
                    "http://foundry.example": 200, "http://foundry.example/health": 200})
    out = await checks_exec._run_http(_check(target="foundry.example"))
    assert calls[-1] == "http://foundry.example/health"


def test_combine_paths():
    main = checks_exec.CheckOutcome("up", 85, message="HTTP 200 · 85 мс")
    two_bad = checks_exec.combine_paths(main, [
        ("/health", checks_exec.CheckOutcome("down", message="HTTP 502")),
        ("/api", checks_exec.CheckOutcome("down", message="нет ответа")),
        ("/new", checks_exec.CheckOutcome("pending", message="ждём первый результат агента")),
    ])
    assert two_bad.status == "down" and two_bad.latency_ms == 85
    assert two_bad.message == "/health: HTTP 502 (и ещё путей: 1)"
    # a path that has no result yet does not count
    waiting = checks_exec.combine_paths(main, [
        ("/new", checks_exec.CheckOutcome("pending", message="ждём первый результат агента")),
    ])
    assert waiting.status == "up" and waiting.message == "HTTP 200 · 85 мс"
    slow = checks_exec.combine_paths(main, [
        ("/health", checks_exec.CheckOutcome("degraded", 2500, message="медленно: 2500 мс")),
    ])
    assert slow.status == "degraded" and slow.message == "/health: медленно: 2500 мс"


async def test_local_paths_go_to_the_agent(tmp_path):
    db = (tmp_path / "paths.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        srv = Server(name="node-a", token_hash="a", enabled=True, last_seen=now)
        s.add(srv)
        await s.flush()
        chk = Check(name="foundry", type="http", target="https://foundry.example", enabled=True,
                    probe_local=True, probe_server_id=srv.id, interval_seconds=60,
                    check_ssl=False, check_domain=False, extra_paths=["/health"],
                    probe_bound_at=now - timedelta(hours=1), created_at=now - timedelta(hours=1))
        s.add(chk)
        await s.commit()
        cid, sid = chk.id, srv.id

    from app.api.servers import _site_probe_tasks, _store_site_probes
    async with factory() as s:
        tasks = await _site_probe_tasks(s, sid)
        chk = await s.get(Check, cid)
        tid = manual_probe.path_task_ids(chk)["/health"]
    assert [t["id"] for t in tasks] == [cid, tid]
    assert tasks[1]["url"] == "https://foundry.example/health" and tasks[1]["keyword_up"] == ""
    assert manual_probe.path_task_check_id(tid) == cid

    async with factory() as s:
        await _store_site_probes(s, sid, [
            {"id": cid, "code": 200, "latency_ms": 3, "via": "cluster:default/frontend:80"},
            {"id": tid, "code": 502, "latency_ms": 1, "via": "cluster:default/api-gateway:8080"},
            # an answer about a path the monitor no longer has
            {"id": manual_probe.PATH_TASK_BASE + cid * (1 << 20) + 7, "code": 500},
        ], now)
        await s.commit()
    async with factory() as s:
        rows = (await s.scalars(select(AgentPathProbe))).all()
        assert [(r.path, r.code) for r in rows] == [("/health", 502)]

    assert await collector.run_due_checks(factory, Settings()) == 1
    async with factory() as s:
        chk = await s.get(Check, cid)
        assert chk.last_status == "down"
        assert chk.last_message.startswith("/health: ") and "HTTP 502" in chk.last_message
        assert "api-gateway" in chk.last_message
        assert [(r["path"], r["status"]) for r in chk.last_path_results] == [("/health", "down")]

    # a renamed path: the old answer does not count, the new one is waited for
    async with factory() as s:
        chk = await s.get(Check, cid)
        chk.extra_paths = ["/healthz"]
        chk.paths_changed_at = datetime.now(timezone.utc)
        chk.last_checked_at = None
        await s.commit()
    assert await collector.run_due_checks(factory, Settings()) == 1
    async with factory() as s:
        chk = await s.get(Check, cid)
        assert chk.last_status == "up"
        assert [(r["path"], r["status"]) for r in chk.last_path_results] == [("/healthz", "pending")]
    await engine.dispose()


async def test_manual_run_waits_for_the_paths(client, auth_headers, monkeypatch):
    r = await client.post("/api/servers", json={"name": "node-a"}, headers=auth_headers)
    sid, ah = r.json()["server"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}
    await client.post("/api/agent/report", json=REPORT, headers=ah)

    async def panel_must_not_check(check):
        raise AssertionError("the local site was checked from the panel")

    monkeypatch.setattr("app.checks.run_check", panel_must_not_check)
    r = await client.post("/api/checks", headers=auth_headers, json={
        "name": "foundry", "type": "http", "target": "https://foundry.example",
        "probe_local": True, "extra_paths": ["/health"],
    })
    cid = r.json()["id"]
    async with _factory(client)() as s:
        row = await s.get(Check, cid)
        row.probe_server_id = sid
        row.probe_bound_at = datetime.now(timezone.utc)
        await s.commit()

    rid = (await client.post(f"/api/checks/{cid}/run", headers=auth_headers)).json()["run_pending"]
    async with _factory(client)() as s:
        reqs = (await s.scalars(select(ProbeRequest).order_by(ProbeRequest.id))).all()
        assert [q.url for q in reqs] == ["", "/health"]
        path_rid = reqs[1].id

    cfg = (await client.post("/api/agent/report", json=REPORT, headers=ah)).json()
    manual = [t for t in cfg["site_probes"] if t["id"] < 0]
    assert [(t["id"], t["url"]) for t in manual] == [
        (-rid, "https://foundry.example"), (-path_rid, "https://foundry.example/health"),
    ]

    # the main address answered first: not a verdict yet
    await client.post("/api/agent/report", headers=ah, json=dict(REPORT, site_probes=[
        {"id": -rid, "code": 200, "latency_ms": 4},
    ]))
    body = (await client.get(f"/api/checks/{cid}/run/{rid}", headers=auth_headers)).json()
    assert body["run_pending"] == rid and not body["run_status"]

    await client.post("/api/agent/report", headers=ah, json=dict(REPORT, site_probes=[
        {"id": -path_rid, "code": 503, "latency_ms": 2},
    ]))
    body = (await client.get(f"/api/checks/{cid}/run/{rid}", headers=auth_headers)).json()
    assert body["run_status"] == "down" and body["run_message"].startswith("/health: ")
    assert "HTTP 503" in body["run_message"]
    assert body["last_path_results"][0]["status"] == "down"
    async with _factory(client)() as s:
        probe = await s.get(AgentPathProbe, (cid, "/health"))
        assert probe.code == 503 and probe.manual_until is not None
