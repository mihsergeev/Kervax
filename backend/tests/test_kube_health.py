"""Kubernetes шире: ноды NotReady и под давлением, упавшие разовые Job, почему под не встает в
расписание. Данные - агент 2.26."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector
from app.models import Server

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def _kube(**kw):
    base = {"present": True, "access": True,
            "nodes": [{"name": "cp1", "ready": True}, {"name": "w1", "ready": True}]}
    base.update(kw)
    return {"agent_version": "2.26", "kube": base}


def _srv(rep, state=None):
    return Server(id=1, name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state=state or {}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_node_problems():
    assert collector.kube_node_problems(_kube()) == []
    rep = _kube(nodes=[{"name": "cp1", "ready": True}, {"name": "w1", "ready": False},
                       {"name": "w2", "ready": True, "pressure": ["DiskPressure", "PIDPressure"]}])
    assert collector.kube_node_problems(rep) == [
        "нода w1 не готова (NotReady)",
        "нода w2: мало места на диске (DiskPressure), кончаются PID (PIDPressure)",
    ]
    rep["kube"]["access"] = False
    assert collector.kube_node_problems(rep) == []


def test_node_alert_holds_and_routes_to_nodes_tab():
    rep = _kube(nodes=[{"name": "w1", "ready": False}])
    s = _srv(rep)
    lvl, ctx = collector._server_conditions(s, NOW)["kube_node"]
    assert lvl == 0 and ctx["since"] == NOW.isoformat()  # перезагрузка ноды - минуты, ждем
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] == "kube_node"]
    s.alert_state = {"kube_node_since": (NOW - timedelta(minutes=16)).isoformat()}
    lvl, ctx = collector._server_conditions(s, NOW)["kube_node"]
    assert lvl == 2 and ctx["detail"] == "нода w1 не готова (NotReady)"
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "kube_node"]
    assert [(p["level"], p["section"], p["sec"]) for p in rows] == [(2, "kuber", "nodes")]
    s.last_report = _kube()
    assert collector._server_conditions(s, NOW)["kube_node"][0] == 0


def test_failed_jobs_on_home_for_a_day():
    ts = int(NOW.timestamp())
    rep = _kube(failed_jobs=[{"ns": "shop", "name": "migrate-42", "ts": ts - 3600, "reason": "BackoffLimitExceeded"},
                             {"ns": "ops", "name": "old", "ts": ts - 3 * 86400, "reason": "DeadlineExceeded"}])
    rows = [p for p in collector.server_problems(_srv(rep), NOW) if p["kind"] == "kube_job"]
    assert [(p["level"], p["section"], p["sec"], p["text"]) for p in rows] == [
        (1, "kuber", "cron", "упал Job: shop/migrate-42 (BackoffLimitExceeded)")]


def test_pending_pod_says_why():
    pods = [{"ns": "web", "name": "api-1", "phase": "Pending", "owner": "ReplicaSet",
             "msg": "0/3 nodes are available: 3 Insufficient memory."}]
    assert collector.bad_pods_text(pods) == "web/api-1 (Pending: 0/3 nodes are available: 3 Insufficient memory.)"


async def test_failed_job_event(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'k.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    ts = int(now.timestamp())
    old = {"ns": "ops", "name": "old", "ts": ts - 86400, "reason": "DeadlineExceeded"}
    async with factory() as s:
        s.add(Server(name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report=_kube(failed_jobs=[old])))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, cfg, now)
    assert not [m for m in sent if "Job" in m]  # упавшее до знакомства - точка отсчета
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.last_report = _kube(failed_jobs=[old, {"ns": "shop", "name": "migrate-42", "ts": ts,
                                                   "reason": "BackoffLimitExceeded"}])
        await s.commit()
    await collector.evaluate_servers(factory, cfg, now + timedelta(seconds=30))
    jobs = [m for m in sent if m.startswith("💥☸️")]
    assert len(jobs) == 1 and "упал Job: shop/migrate-42 (BackoffLimitExceeded)" in jobs[0], sent
    await collector.evaluate_servers(factory, cfg, now + timedelta(seconds=60))
    assert len([m for m in sent if m.startswith("💥☸️")]) == 1
    await engine.dispose()


def test_job_pod_and_log_tail():
    from app import job_logs

    rep = _kube(pods=[
        {"ns": "shop", "name": "migrate-42-x7k2p", "owner": "Job", "phase": "Failed", "reason": "Error"},
        {"ns": "shop", "name": "migrate-42-extra-ab3cd", "owner": "Job", "phase": "Failed"},  # другой Job
        {"ns": "ops", "name": "migrate-42-q1w2e", "owner": "Job", "phase": "Failed"},  # другой namespace
        {"ns": "shop", "name": "batch-0-k9m8n", "owner": "Job", "phase": "Succeeded"},  # индексный Job
    ])
    assert job_logs.find_pod(rep, {"ns": "shop", "name": "migrate-42"})["name"] == "migrate-42-x7k2p"
    assert job_logs.find_pod(rep, {"ns": "shop", "name": "batch"})["name"] == "batch-0-k9m8n"
    assert job_logs.find_pod(rep, {"ns": "shop", "name": "gone"}) is None
    raw = ("2026-10-16T11:15:58.123456789Z start\n\n"
           "2026-10-16T11:15:59.000000001Z \x1b[31mValueError: bad address\x1b[0m\n")
    assert job_logs.tail_text(raw) == "start\nValueError: bad address"
    long = "\n".join(f"line {i}" for i in range(100))
    assert job_logs.tail_text(long).splitlines() == [f"line {i}" for i in range(85, 100)]
    assert len(job_logs.tail_text(long, chars=30)) <= 30


async def test_failed_job_alert_carries_log(tmp_path, monkeypatch):
    """Алерт о Job ждет хвост лога его пода и приходит уже с ним: почему упал, видно в чате."""
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import KubeCommand

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'k.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    ts = int(now.timestamp())
    async with factory() as s:
        s.add(Server(name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report=_kube()))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None, **kw):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, cfg, now)
    pods = [{"ns": "shop", "name": "migrate-42-x7k2p", "owner": "Job", "phase": "Failed", "reason": "Error"}]
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.last_report = _kube(pods=pods, failed_jobs=[
            {"ns": "shop", "name": "migrate-42", "ts": ts, "reason": "BackoffLimitExceeded"}])
        await s.commit()
    t1 = now + timedelta(seconds=30)
    assert await collector.request_job_logs(factory, t1) == 1
    await collector.evaluate_servers(factory, cfg, t1)
    assert not [m for m in sent if m.startswith("💥☸️")]  # ждем ответ агента
    async with factory() as s:
        cmd = (await s.execute(select(KubeCommand))).scalars().one()
        assert (cmd.ns, cmd.name, cmd.action) == ("shop", "migrate-42-x7k2p", "logs")
        cmd.status, cmd.ok = "done", True
        cmd.result = "2026-10-16T11:15:58Z connecting\n2026-10-16T11:15:59Z ValueError: bad <address>\n"
        await s.commit()
    t2 = now + timedelta(seconds=60)
    assert await collector.request_job_logs(factory, t2) == 0  # повторно не спрашиваем
    await collector.evaluate_servers(factory, cfg, t2)
    jobs = [m for m in sent if m.startswith("💥☸️")]
    assert len(jobs) == 1, sent
    assert "упал Job: shop/migrate-42 (BackoffLimitExceeded)" in jobs[0]
    assert "↳ под migrate-42-x7k2p (Error), конец лога:\n<pre>connecting\nValueError: bad &lt;address&gt;</pre>" in jobs[0]
    await collector.evaluate_servers(factory, cfg, now + timedelta(seconds=90))
    assert len([m for m in sent if m.startswith("💥☸️")]) == 1
    await engine.dispose()


async def test_failed_job_alert_does_not_wait_forever(tmp_path, monkeypatch):
    """Агент не ответил - алерт уходит без лога после _WAIT, а не висит."""
    from app import job_logs
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'k.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    ts = int(now.timestamp())
    async with factory() as s:
        s.add(Server(name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report=_kube()))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None, **kw):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, cfg, now)
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.last_report = _kube(
            pods=[{"ns": "shop", "name": "migrate-42-x7k2p", "owner": "Job", "phase": "Failed"}],
            failed_jobs=[{"ns": "shop", "name": "migrate-42", "ts": ts, "reason": "BackoffLimitExceeded"}])
        await s.commit()
    for sec in (30, 60, 90):
        await collector.request_job_logs(factory, now + timedelta(seconds=sec))
        await collector.evaluate_servers(factory, cfg, now + timedelta(seconds=sec))
    assert not [m for m in sent if m.startswith("💥☸️")]
    late = now + timedelta(seconds=30 + job_logs._WAIT + 1)
    await collector.request_job_logs(factory, late)
    await collector.evaluate_servers(factory, cfg, late)
    jobs = [m for m in sent if m.startswith("💥☸️")]
    assert len(jobs) == 1 and "<pre>" not in jobs[0]
    await engine.dispose()


def test_digest_drops_job_logs():
    from app import alerts

    m = "💥☸️ k8s-a - упал Job: shop/migrate-42\n↳ под migrate-42-x7k2p, конец лога:\n<pre>boom</pre>"
    d = alerts._digest([m] * 6)
    assert "<pre>" not in d and "boom" not in d and d.count("упал Job: shop/migrate-42") == 6


def test_restarts_are_declined():
    """На проде в истории алертов было "CrashLoopBackOff, 7 рестарта"."""
    pods = [{"ns": "default", "name": f"api-{n}", "reason": "CrashLoopBackOff", "restarts": n} for n in (7, 21, 83)]
    assert collector.bad_pods_text(pods) == (
        "default/api-7 (CrashLoopBackOff, 7 рестартов), default/api-21 (CrashLoopBackOff, 21 рестарт), "
        "default/api-83 (CrashLoopBackOff, 83 рестарта)")
