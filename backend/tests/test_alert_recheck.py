"""Fresh manual recovery, failure, scope, retries and stale-answer boundaries."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import alert_recheck as rc, tamper
from app.models import AlertRecheck, Check, Server, User
from app.security import hash_agent_token


def factory(client):
    return client._transport.app.state.session_factory


async def server(client, **kw):
    async with factory(client)() as s:
        obj = Server(name="node-a", token_hash=hash_agent_token("neutral-test-token"), enabled=True,
                     last_seen=datetime.now(timezone.utc), last_report={
                         "caps": {"alert_recheck": True}, "setup_versions": {"recheck-setup": "0.1"},
                         "mem_used": 95, "mem_total": 100,
                     }, mem_alert_percent=80, alert_sustain_seconds=0, backup_not_required=True,
                     alert_state={"mem": 1})
        for k, v in kw.items():
            setattr(obj, k, v)
        s.add(obj)
        await s.commit()
        return obj.id


async def start(client, headers, oid, kind="mem", typ="server"):
    res = await client.post("/api/alert-rechecks", headers=headers,
                            json={"object_type": typ, "object_id": oid, "kind": kind})
    assert res.status_code == 200, res.text
    return res.json()


async def tasks(client):
    return (await client.get("/api/agent/commands", headers={"Authorization": "Bearer neutral-test-token"})).json()["alert_rechecks"]


async def answer(client, jid, snapshot, ok=True):
    return await client.post("/api/agent/alert-recheck-result", headers={"Authorization": "Bearer neutral-test-token"},
                             json={"id": jid, "ok": ok, "snapshot": snapshot})


async def result(client, headers, jid):
    rows = (await client.get("/api/alert-rechecks", headers=headers)).json()
    return next(x for x in rows if x["id"] == jid)


async def test_real_request_id_not_new_report_is_required(client, auth_headers):
    oid = await server(client)
    job = await start(client, auth_headers, oid)
    assert job["status"] == "checking"
    assert (await start(client, auth_headers, oid))["id"] == job["id"]
    # A fresh report arriving after the click is not an answer to this request.
    async with factory(client)() as s:
        obj = await s.get(Server, oid)
        obj.last_seen = datetime.now(timezone.utc)
        await s.commit()
    assert (await result(client, auth_headers, job["id"]))["status"] == "checking"
    assert await tasks(client) == [{"id": job["id"], "source": "mem", "db_stats": False}]
    assert await tasks(client) == []
    assert (await answer(client, job["id"], {"mem_used": 20, "mem_total": 100})).status_code == 200
    assert (await result(client, auth_headers, job["id"]))["status"] == "fixed"
    async with factory(client)() as s:
        obj = await s.get(Server, oid)
        assert obj.last_report["mem_used"] == 20
        assert obj.alert_mutes is None


async def test_remains_and_failure_keep_alert(client, auth_headers):
    oid = await server(client)
    job = await start(client, auth_headers, oid)
    await tasks(client)
    await answer(client, job["id"], {"mem_used": 95, "mem_total": 100})
    assert (await result(client, auth_headers, job["id"]))["status"] == "remains"
    async with factory(client)() as s:
        obj = await s.get(Server, oid)
        assert not obj.alert_fixed


@pytest.mark.parametrize("mode", ["no_receipt", "bad_block", "collector_failure", "changed_config", "new_report", "expired"])
async def test_no_false_recovery(client, auth_headers, mode):
    oid = await server(client)
    job = await start(client, auth_headers, oid)
    if mode != "no_receipt":
        await tasks(client)
    async with factory(client)() as s:
        obj = await s.get(Server, oid)
        row = await s.get(AlertRecheck, job["id"])
        if mode == "changed_config":
            obj.mem_alert_percent = 70
        elif mode == "new_report":
            obj.last_report = {**obj.last_report, "mem_used": 99}
        elif mode == "expired":
            row.deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await s.commit()
    payload = {} if mode == "bad_block" else {"mem_used": 20, "mem_total": 100}
    await answer(client, job["id"], payload, mode != "collector_failure")
    assert (await result(client, auth_headers, job["id"]))["status"] == "unavailable"
    async with factory(client)() as s:
        obj = await s.get(Server, oid)
        assert obj.last_report["mem_used"] == (99 if mode == "new_report" else 95)
        assert not obj.alert_fixed


async def test_late_duplicate_cannot_rewrite_result_or_new_alarm(client, auth_headers):
    oid = await server(client)
    job = await start(client, auth_headers, oid)
    await tasks(client)
    await answer(client, job["id"], {"mem_used": 20, "mem_total": 100})
    async with factory(client)() as s:
        obj = await s.get(Server, oid)
        obj.last_report = {**obj.last_report, "mem_used": 99}
        obj.alert_fixed = None  # ordinary collector has completed the recovery
        await s.commit()
    await answer(client, job["id"], {"mem_used": 20, "mem_total": 100})
    async with factory(client)() as s:
        obj = await s.get(Server, oid)
        assert obj.last_report["mem_used"] == 99
        assert not obj.alert_fixed


async def test_retry_does_not_move_the_baseline_of_an_older_inflight_collection(client, auth_headers):
    oid = await server(client)
    job = await start(client, auth_headers, oid)
    await tasks(client)
    async with factory(client)() as s:
        obj = await s.get(Server, oid)
        obj.last_report = {**obj.last_report, "mem_used": 99}
        row = await s.get(AlertRecheck, job["id"])
        row.taken_at = datetime.now(timezone.utc) - timedelta(seconds=rc.RETRY + 1)
        original = row.spec["baseline"]
        await s.commit()
    assert len(await tasks(client)) == 1
    async with factory(client)() as s:
        assert (await s.get(AlertRecheck, job["id"])).spec["baseline"] == original
    await answer(client, job["id"], {"mem_used": 20, "mem_total": 100})
    assert (await result(client, auth_headers, job["id"]))["status"] == "unavailable"
    async with factory(client)() as s:
        assert (await s.get(Server, oid)).last_report["mem_used"] == 99


async def test_verified_recovery_does_not_hide_new_bad_report_before_collector_tick(client, auth_headers):
    from app.collector import server_problems
    oid = await server(client)
    job = await start(client, auth_headers, oid)
    await tasks(client)
    await answer(client, job["id"], {"mem_used": 20, "mem_total": 100})
    async with factory(client)() as s:
        obj = await s.get(Server, oid)
        assert obj.alert_fixed["mem"]["done"] is None  # collector has not ticked
        obj.last_report = {**obj.last_report, "mem_used": 99}
        assert any(p["kind"] == "mem" for p in server_problems(obj, datetime.now(timezone.utc)))


async def test_offline_old_agent_and_unsupported_are_unknown(client, auth_headers):
    oid = await server(client, last_seen=datetime.now(timezone.utc) - timedelta(hours=1))
    assert (await start(client, auth_headers, oid))["status"] == "unavailable"
    oid2 = await server(client, name="node-b", token_hash="another-neutral-token", last_report={})
    assert "агент" in (await start(client, auth_headers, oid2))["message"]
    assert "принудительной" in (await start(client, auth_headers, oid2, "oom"))["message"]


async def test_scope_viewer_sections_and_client_command_rejected(client, auth_headers):
    oid = await server(client, group_name="private-group")
    job = await start(client, auth_headers, oid)
    async with factory(client)() as s:
        user = await s.scalar(select(User).where(User.username == "admin"))
        user.role, user.server_groups = "viewer", ["public-group"]
        await s.commit()
    assert (await client.post("/api/alert-rechecks", headers=auth_headers,
                             json={"object_type": "server", "object_id": oid, "kind": "mem"})).status_code == 403
    assert (await client.get("/api/alert-rechecks", headers=auth_headers)).json() == []
    async with factory(client)() as s:
        user = await s.scalar(select(User).where(User.username == "admin"))
        user.role, user.sections, user.server_groups = "editor", ["backups"], []
        await s.commit()
    assert (await client.post("/api/alert-rechecks", headers=auth_headers,
                             json={"object_type": "server", "object_id": oid, "kind": "mem"})).status_code == 403
    bad = await client.post("/api/alert-rechecks", headers=auth_headers,
                            json={"object_type": "server", "object_id": oid, "kind": "mem", "command": "repair"})
    assert bad.status_code == 422


async def check(client):
    now = datetime.now(timezone.utc)
    st = tamper.observe(None, {"size": 1000, "title": "Demo", "domains": {}, "bot": None, "engine": {"wp": "6.0.0"}},
                        now, set(), {"wp": {"6.0.0": "insecure", "6.0.1": "latest"}})
    async with factory(client)() as s:
        obj = Check(name="demo.example", type="http", target="https://demo.example", enabled=True,
                    last_status="up", tamper_state=st)
        s.add(obj)
        await s.commit()
        return obj.id


@pytest.mark.parametrize("mode", ["fixed", "remains", "failed", "hidden_version"])
async def test_cms_requires_a_fresh_observation(client, auth_headers, monkeypatch, mode):
    oid = await check(client)
    async def scan(obj):
        if mode == "failed":
            return {"err": "timeout"}
        eng = {} if mode == "hidden_version" else {"wp": "6.0.0" if mode == "remains" else "6.0.1"}
        return {"obs": {"size": 1000, "title": "Demo", "domains": {}, "bot": None, "engine": eng}}
    async def versions(*args, **kwargs):
        return {"wp": {"6.0.0": "insecure", "6.0.1": "latest"}}
    monkeypatch.setattr(tamper, "scan", scan)
    monkeypatch.setattr(tamper, "versions", versions)
    job = await start(client, auth_headers, oid, "tamper", "check")
    got = await result(client, auth_headers, job["id"])
    assert got["status"] == ("unavailable" if mode in {"failed", "hidden_version"} else mode)
    async with factory(client)() as s:
        obj = await s.get(Check, oid)
        assert (obj.tamper or {}).get("level") == ("none" if mode == "fixed" else "one")


@pytest.mark.parametrize("field,value", [("target", "https://changed.example"), ("enabled", False),
                                        ("http_headers", "X-Demo: changed"), ("extra_paths", ["/new"]), ("degraded_ms", 1)])
async def test_cms_old_result_after_edit_does_not_overwrite(client, auth_headers, monkeypatch, field, value):
    oid = await check(client)
    async def scan(obj):
        async with factory(client)() as s:
            row = await s.get(Check, oid)
            setattr(row, field, value)
            await s.commit()
        return {"obs": {"size": 1000, "title": "Demo", "domains": {}, "bot": None, "engine": {"wp": "6.0.1"}}}
    monkeypatch.setattr(tamper, "scan", scan)
    job = await start(client, auth_headers, oid, "tamper", "check")
    assert (await result(client, auth_headers, job["id"]))["status"] == "unavailable"
    async with factory(client)() as s:
        assert (await s.get(Check, oid)).tamper["level"] == "one"


@pytest.mark.parametrize("kind", ["tamper", "status", "ssl", "domain"])
async def test_new_automatic_evidence_wins_over_inflight_manual_result(client, auth_headers, monkeypatch, kind):
    from app import checks as checks_exec
    from app.checks import CheckOutcome, ExpiryInfo
    oid = await check(client)
    async def newer():
        async with factory(client)() as s:
            obj = await s.get(Check, oid)
            if kind == "tamper":
                obj.tamper_state = {**obj.tamper_state, "err": "new automatic evidence"}
            elif kind == "status":
                obj.last_checked_at, obj.last_status, obj.last_message = datetime.now(timezone.utc), "down", "new automatic evidence"
            else:
                setattr(obj, f"{kind}_days", -1)
                obj.expiry_checked_at = datetime.now(timezone.utc)
            await s.commit()
    async def scan(obj):
        await newer()
        return {"obs": {"size": 1000, "title": "Demo", "domains": {}, "bot": None, "engine": {"wp": "6.0.1"}}}
    async def status(obj):
        await newer()
        return CheckOutcome(status="up", latency_ms=1, message="fresh")
    async def expiry(obj):
        await newer()
        return ExpiryInfo(ssl_days=365, domain_days=365)
    monkeypatch.setattr(tamper, "scan", scan)
    monkeypatch.setattr(checks_exec, "run_check", status)
    monkeypatch.setattr(checks_exec, "probe_expiry", expiry)
    job = await start(client, auth_headers, oid, kind, "check")
    assert (await result(client, auth_headers, job["id"]))["status"] == "unavailable"
    async with factory(client)() as s:
        obj = await s.get(Check, oid)
        if kind == "tamper":
            assert obj.tamper_state["err"] == "new automatic evidence"
        elif kind == "status":
            assert obj.last_status == "down"
        else:
            assert getattr(obj, f"{kind}_days") == -1


async def test_kube_missing_cert_is_not_recovery(client, auth_headers):
    now = datetime.now(timezone.utc)
    cert = {"kind": "pki", "where": "/etc/kubernetes/pki/demo.crt", "expires": int(now.timestamp()) + 86400}
    rep = {"caps": {"alert_recheck": True}, "setup_versions": {"recheck-setup": "0.1"}, "kube_expiry": [cert], "flux": []}
    oid = await server(client, last_report=rep, alert_state={"kube_expiry": 1})
    job = await start(client, auth_headers, oid, "kube_expiry")
    await tasks(client)
    await answer(client, job["id"], {"kube_expiry": [], "flux": []})
    assert (await result(client, auth_headers, job["id"]))["status"] == "unavailable"


async def test_kube_renewed_cert_is_verified(client, auth_headers):
    now = datetime.now(timezone.utc)
    cert = {"kind": "pki", "where": "/etc/kubernetes/pki/demo.crt", "expires": int(now.timestamp()) + 86400}
    rep = {"caps": {"alert_recheck": True}, "setup_versions": {"recheck-setup": "0.1"}, "kube_expiry": [cert], "flux": []}
    oid = await server(client, last_report=rep, alert_state={"kube_expiry": 1})
    job = await start(client, auth_headers, oid, "kube_expiry")
    await tasks(client)
    await answer(client, job["id"], {"kube_expiry": [{**cert, "expires": int(now.timestamp()) + 365*86400}], "flux": []})
    assert (await result(client, auth_headers, job["id"]))["status"] == "fixed"


async def test_backup_never_clears_existing_database_inventory_without_fresh_data(client, auth_headers):
    rep = {"caps": {"alert_recheck": True}, "setup_versions": {"recheck-setup": "0.1", "backup-setup": "0.50"},
           "db_stats": [{"engine": "pg", "container": "demo-db", "dbs": [{"name": "demo"}]}],
           "db_engines": ["postgres"], "backup": {"present": True, "manageable": True, "dumps": []}}
    oid = await server(client, last_report=rep)
    job = await start(client, auth_headers, oid, "home:dbdump")
    task = (await tasks(client))[0]
    assert task["db_stats"] is True
    # Missing/incomplete inventory is unknown, never a clean coverage verdict.
    await answer(client, job["id"], {"backup": {"present": True}, "db_engines": ["postgres"], "extras": {}, "db_stats": []})
    assert (await result(client, auth_headers, job["id"]))["status"] == "unavailable"
    async with factory(client)() as s:
        assert (await s.get(Server, oid)).last_report["db_stats"] == rep["db_stats"]
