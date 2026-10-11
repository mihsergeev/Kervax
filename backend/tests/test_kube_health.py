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
