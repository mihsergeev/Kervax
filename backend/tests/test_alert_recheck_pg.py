"""Run explicitly with KERVAX_TEST_PG against an isolated PostgreSQL database."""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app import alert_recheck as rc
from app.models import AlertRecheck, Server
from tests.test_alert_recheck import factory, server, start

pytestmark = pytest.mark.skipif(not os.getenv("KERVAX_TEST_PG"), reason="dedicated PostgreSQL concurrency test")


async def test_duplicate_clicks_workers_and_restart_share_one_job(client, auth_headers):
    oid = await server(client)
    from app.main import create_app
    app2 = create_app()  # independent engine/session pool, same persistent database
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://test") as other:
            body = {"object_type": "server", "object_id": oid, "kind": "mem"}
            got = await asyncio.gather(*(c.post("/api/alert-rechecks", headers=auth_headers, json=body)
                                        for c in [client, other] * 5))
            assert {r.status_code for r in got} == {200}
            assert len({r.json()["id"] for r in got}) == 1
            jid = got[0].json()["id"]
            # Two independent command-poll workers cannot both claim the lease.
            auth = {"Authorization": "Bearer neutral-test-token"}
            polls = await asyncio.gather(client.get("/api/agent/commands", headers=auth),
                                         other.get("/api/agent/commands", headers=auth))
            assert sum(len(r.json()["alert_rechecks"]) for r in polls) == 1
            # Lost agent result/panel process: SQL lease is the retry source.
            async with factory(client)() as s:
                row = await s.get(AlertRecheck, jid)
                row.taken_at = datetime.now(timezone.utc) - timedelta(seconds=rc.RETRY + 1)
                await s.commit()
            assert len((await other.get("/api/agent/commands", headers=auth)).json()["alert_rechecks"]) == 1
            answer = await other.post("/api/agent/alert-recheck-result", headers=auth,
                                      json={"id": jid, "ok": True, "snapshot": {"mem_used": 20, "mem_total": 100}})
            assert answer.status_code == 200
            rows = (await client.get("/api/alert-rechecks", headers=auth_headers)).json()
            assert rows[0]["id"] == jid and rows[0]["status"] == "fixed"
    finally:
        await app2.state.engine.dispose()


async def test_old_package_upload_cannot_commit_over_a_verified_inventory(client, auth_headers, monkeypatch):
    from app import packages
    from app.models import ServerPackages
    oid = await server(client, pkg_hash="aaa", sw_host=[{"p": "php", "v": "7.4.33", "pkg": "php7.4-fpm", "full": "7.4.33-1+sury"}],
                       last_report={"caps": {"alert_recheck": True}, "pkgs": {"h": "aaa"}, "docker": None, "db_stats": []})
    job = await start(client, auth_headers, oid, "home:sw-eol")
    auth = {"Authorization": "Bearer neutral-test-token"}
    await client.get("/api/agent/commands", headers=auth)
    entered, release = asyncio.Event(), asyncio.Event()
    original = packages.store_part
    async def paused(*args, **kwargs):
        if args[2] == "aaa":
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(packages, "store_part", paused)
    chunk = {"h": "aaa", "mgr": "dpkg", "part": 0, "parts": 1, "items": [["php7.4-fpm", "7.4.33-1+sury"]]}
    old = asyncio.create_task(client.post("/api/agent/packages", headers=auth, json=chunk))
    await asyncio.wait_for(entered.wait(), 3)
    fresh = asyncio.create_task(client.post("/api/agent/alert-recheck-result", headers=auth, json={
        "id": job["id"], "ok": True, "snapshot": {"docker": {"present": False, "access": False}, "db_stats": [],
        "packages": [["openssl", "3.0.0"]], "pkgs": {"h": "bbb", "mgr": "dpkg", "n": 1}}}))
    await asyncio.sleep(0.1)
    assert not fresh.done()  # the in-flight upload holds the same server row lock
    release.set()
    assert (await old).status_code == 200
    assert (await fresh).status_code == 200
    late = await client.post("/api/agent/packages", headers=auth, json=chunk)
    assert late.json()["done"] is False
    async with factory(client)() as s:
        row = await s.get(ServerPackages, oid)
        assert row.hash == "bbb" and row.items == [["openssl", "3.0.0"]]
        assert (await s.get(Server, oid)).pkg_hash == "bbb"
