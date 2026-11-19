import asyncio
from collections import Counter
from functools import partial
from types import SimpleNamespace

import httpx

from app import check_dispatch, checks, parents
from app.config import Settings


async def test_scheduled_http_starts_are_spread(monkeypatch):
    monkeypatch.setattr(check_dispatch, "_MAX_SPREAD", 0.12)
    loop = asyncio.get_running_loop()

    async def timestamp():
        return loop.time()

    jobs = [check_dispatch.Probe(timestamp, check_dispatch.http_origin(f"https://site{i}.example"), 60)
            for i in range(74)]
    starts = await check_dispatch.gather(jobs, 50, 0)
    assert len(starts) == 74
    assert max(starts) - min(starts) >= 0.08
    assert check_dispatch.http_origin("HTTPS://ORIGIN.EXAMPLE.:443/path?q=1") == (
        check_dispatch.http_origin("origin.example/another-path"))


async def test_origin_queue_leaves_slots_for_other_sites(monkeypatch):
    monkeypatch.setattr(check_dispatch, "_ORIGIN_CONCURRENCY", 1)
    monkeypatch.setattr(check_dispatch, "_START_SPACING", 0)
    occupied = asyncio.Event()
    other_started = asyncio.Event()
    same = check_dispatch.http_origin("https://one.example")
    other = check_dispatch.http_origin("https://two.example")

    async def slow():
        occupied.set()
        await other_started.wait()
        return "slow"

    async def independent():
        await occupied.wait()
        other_started.set()
        return "independent"

    jobs = [check_dispatch.Probe(slow, same, 60) for _ in range(6)]
    jobs.append(check_dispatch.Probe(independent, other, 60))
    result = await asyncio.wait_for(check_dispatch.gather(jobs, 2, 0), timeout=3)
    assert result == ["slow"] * 6 + ["independent"]


async def test_known_backend_shares_budget_across_hostnames(monkeypatch):
    monkeypatch.setattr(check_dispatch, "_ORIGIN_CONCURRENCY", 1)
    monkeypatch.setattr(check_dispatch, "_START_SPACING", 0)
    index = parents.Index([
        SimpleNamespace(id=1, name="first", external_ip="2001:db8::10", last_report={"web_services": [
            {"sites": ["one.example", "two.example", "both.example", None]}, "bad report item"]}),
        SimpleNamespace(id=2, name="second", last_report={"web_services": [{"sites": ["both.example"]}]}),
    ])
    backends = check_dispatch.known_backends(index)
    assert backends == {b"one.example": 1, b"two.example": 1, b"2001:db8::10": 1}
    first_running = asyncio.Event()
    separate_running = asyncio.Event()
    released = False

    async def send(url, callback):
        gate = check_dispatch.request_gate()
        try:
            await gate.before_send(httpx.Request("GET", url))
            await callback()
        finally:
            gate.close()

    async def first():
        nonlocal released
        first_running.set()
        await separate_running.wait()
        released = True

    async def same():
        assert released

    async def separate():
        await first_running.wait()
        separate_running.set()

    # Different URLs reached after redirects share the backend budget. The ambiguous
    # name remains independent and can finish while that backend is occupied.
    jobs = [check_dispatch.Probe(partial(send, url, callback)) for url, callback in (
        ("https://one.example/", first), ("http://two.example/", same),
        ("https://both.example/", separate),
    )]
    assert await asyncio.wait_for(check_dispatch.gather(jobs, 3, 0, backends), timeout=3) == [None] * 3


async def test_requests_share_origin_slots_across_redirects_paths_and_retries(monkeypatch):
    monkeypatch.setattr(check_dispatch, "_ORIGIN_CONCURRENCY", 2)
    monkeypatch.setattr(check_dispatch, "_START_SPACING", 0.02)
    monkeypatch.setattr(checks, "get_settings", lambda: Settings(check_retry_delay_ms=0))

    async def dns(*_):
        return None, True

    monkeypatch.setattr(checks, "_dns_time", dns)
    active = 0
    peak = 0
    seen = Counter()

    async def respond(req):
        nonlocal active, peak
        if req.url.host != "shared.example":
            return httpx.Response(302, headers={"location": f"https://shared.example/{req.url.host}{req.url.path}"})
        path = req.url.path
        seen[path] += 1
        active += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(0.045)
            # A real error on one path still causes a retry of that monitor.
            return httpx.Response(503 if path == "/one.example/api" and seen[path] == 1 else 200)
        finally:
            active -= 1

    monkeypatch.setattr(checks.net_guard, "MonitorTransport", lambda *_: httpx.MockTransport(respond))
    jobs = []
    for host in ("one.example", "two.example", "three.example"):
        check = SimpleNamespace(type="http", target=f"https://{host}/", timeout_ms=3000, degraded_ms=160,
                                method="GET", keyword_up="", keyword_down="", expected_status="200-399",
                                extra_paths=["/api", "/status", "/health"], retries=1)
        jobs.append(check_dispatch.Probe(partial(checks.run_check, check),
                                         check_dispatch.http_origin(check.target), 60))
    results = await check_dispatch.gather(jobs, 50, 0)
    assert peak == 2
    assert [out.status for out in results] == ["up", "up", "up"]
    assert [[p["path"] for p in out.path_results] for out in results] == [["/api", "/status", "/health"]] * 3
    assert seen == Counter({f"/{host}{path}": 2 if host == "one.example" else 1
                            for host in ("one.example", "two.example", "three.example")
                            for path in ("/", "/api", "/status", "/health")})
    assert check_dispatch.request_gate() is None
