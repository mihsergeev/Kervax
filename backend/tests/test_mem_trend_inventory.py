"""Membership, evidence gaps and real-worker budgets for retained memory history."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app import collector, known, mem_trend
from app.config import Settings
from app.models import Server
from tests.test_known import _panel, _set
from tests.test_mem_trend import _hist as old_hist

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
MB = 2**20
def _hist(vals):
    h=old_hist(vals,NOW.date()-timedelta(days=1));h["d"]=NOW.date().isoformat();return h


OLD = "pod:sample/worker-old"
NEW = "pod:sample/worker-new"


def pod(name="worker-old", node="worker-a", **kw):
    return {"ns":"sample", "name":name, "owner":"ReplicaSet", "phase":"Running", "node":node,
            "use":{"mem":1300*MB,"ts":NOW.timestamp()}, **kw}


def report(pods=None, complete=True, **kw):
    return {"agent_version":"2.62", "hostname":"controller-a", "mem_total":32*2**30, "mem_used":2**30,
            "kube":{"access":True, "pods_complete":complete, "pods":[pod()] if pods is None else pods,
                    "nodes":[{"name":"worker-a", "cap":{"mem":8*2**30},
                              "use":{"mem":8*2**30-500*MB,"ts":NOW.timestamp()}}]}, **kw}


def state(active=True):
    return {"mem_trend":{OLD:_hist([500,700,900,1100,1300])},
            "mem_leak":2 if active else 0,"mem_leak_sig":[OLD] if active else []}


def server(rep=None, st=None, entry=None):
    return Server(id=1,name="controller-a",token_hash="x",enabled=True,last_seen=NOW,
                  offline_after_seconds=120,backup_not_required=True,alert_sustain_seconds=0,
                  last_report=report() if rep is None else rep,alert_state=state() if st is None else st,
                  known_issues={"mem_leak":entry} if entry else None)


def test_deleted_pod_stops_being_active_but_history_is_retained():
    rep=report([pod("worker-new")])
    st=state()
    updated=mem_trend.update(st["mem_trend"],rep,NOW)
    assert OLD in updated and updated[NEW]["h"]==[]
    st["mem_trend"]=updated
    s=server(rep,st)
    assert mem_trend.problems(s,NOW)==[]
    assert collector._server_conditions(s,NOW)["mem_leak"][0]==0
    assert mem_trend.recovery_confirmed(s,NOW,[OLD])


@pytest.mark.parametrize("count,expected",[(1,True),(399,True),(400,False),(401,False)])
def test_legacy_261_nonempty_inventory_is_proof_only_below_local_cap(count,expected):
    rep=report([pod(f"new-{i}") for i in range(count)])
    rep["agent_version"]="2.61";rep["kube"].pop("pods_complete")
    assert mem_trend.inventory_complete(rep,"pod") is expected
    assert mem_trend.recovery_confirmed(server(rep),NOW,[OLD]) is expected


@pytest.mark.parametrize("patch",[{}, {"pods":[]}, {"access":False,"pods_complete":True},
    {"access":True,"pods_complete":False,"pods":[pod("worker-new")]},
    {"access":True,"pods_complete":True,"pods":"invalid"}])
def test_failed_partial_or_missing_inventory_cannot_resolve_active_history(patch):
    rep=report();rep["kube"]=patch
    s=server(rep)
    result=mem_trend.problems(s,NOW)
    assert len(result)==1 and result[0]["key"]==OLD and result[0]["level"]==2
    assert result[0]["eta_days"] is None
    assert not mem_trend.recovery_confirmed(s,NOW,[OLD])
    assert OLD in mem_trend.update(s.alert_state["mem_trend"],rep,NOW+timedelta(days=3))
    # An unannounced old series must not open an alert while its existence is unknown.
    assert mem_trend.problems(server(rep,state(active=False)),NOW)[0]["level"]==0


def test_live_growth_uses_worker_not_controller_and_alerts_even_in_partial_inventory():
    s=server(report(complete=False),state(active=False))
    row=mem_trend.problems(s,NOW)[0]
    assert row["level"]==2 and row["eta_days"]==2.5
    assert "worker-a" in row["text"] and "500 МБ" in row["text"]
    assert collector._server_conditions(s,NOW)["mem_leak"][0]==2


@pytest.mark.parametrize("change",["node","cap","use","timestamp","stale","invalid","missing_mem","cap_shape"])
def test_insufficient_worker_data_never_falls_back_to_controller_memory(change):
    rep=report()
    node=rep["kube"]["nodes"][0]
    if change=="node":node["name"]="other-worker"
    elif change=="timestamp":node["use"].pop("ts")
    elif change=="stale":node["use"]["ts"]-=600
    elif change=="invalid":node["cap"]["mem"]="invalid"
    elif change=="missing_mem":node["use"].pop("mem")
    elif change=="cap_shape":node["cap"]="invalid"
    else:node.pop(change)
    row=mem_trend.problems(server(rep,state(active=False)),NOW)[0]
    assert row["eta_days"] is None and row["level"]==0
    assert "в сутки" in row["text"] and "31" not in row["text"]
    assert not mem_trend.recovery_confirmed(server(rep),NOW,[OLD])


def test_zero_worker_headroom_is_critical_and_unrelated_workers_do_not_share_budget():
    rep=report([pod(),pod("second",node="worker-b")])
    rep["kube"]["nodes"][0]["use"]["mem"]=8*2**30
    rep["kube"]["nodes"].append({"name":"worker-b","cap":{"mem":16*2**30},
        "use":{"mem":8*2**30,"ts":NOW.timestamp()}})
    st=state(active=False);st["mem_trend"]["pod:sample/second"]=_hist([500,700,900,1100,1300])
    rows={r["key"]:r for r in mem_trend.problems(server(rep,st),NOW)}
    assert rows[OLD]["eta_days"]==0 and rows[OLD]["level"]==2
    assert rows["pod:sample/second"]["eta_days"]==41


def test_new_pod_name_and_reused_name_with_new_uid_have_their_own_series():
    st=state()["mem_trend"]
    st[OLD]["u"]="old-uid"
    rep=report([pod(u="new-uid"),pod("worker-new",u="different-uid")])
    updated=mem_trend.update(st,rep,NOW)
    assert updated[OLD]["h"]==updated[NEW]["h"]==[]
    assert updated[OLD]["u"]=="new-uid"
    assert mem_trend.leak(updated[OLD]) is None


def test_unrelated_retained_top_process_does_not_block_announced_pod_recovery():
    st=state();st["mem_trend"]["proc:helper@service"]=_hist([10,10,10,10,10])
    s=server(report([pod("worker-new")]),st)
    assert mem_trend.recovery_confirmed(s,NOW)
    assert collector._server_conditions(s,NOW)["mem_leak"][0]==0


def test_incomplete_report_resets_known_confirmation_timer():
    entry={"at":NOW.isoformat(),"level":2,"sig":[OLD],"gone_at":(NOW-timedelta(seconds=601)).isoformat()}
    kept,action=known.step(entry,0,[],NOW,confirmed=False)
    assert "gone_at" not in kept and not action
    kept,_=known.step(kept,0,[],NOW+timedelta(seconds=1))
    assert known.step(kept,0,[],NOW+timedelta(seconds=600))[0] is not None
    assert known.step(kept,0,[],NOW+timedelta(seconds=601))==(None,"closed")


async def test_known_mem_leak_closes_after_600_confirmed_seconds_without_admin_ttl(tmp_path,monkeypatch):
    engine,factory=await _panel(tmp_path)
    entry={"at":NOW.isoformat(),"by":"operator","text":"investigating","level":2,"sig":[OLD]}
    s=server(report([pod("worker-new")]),entry=entry)
    s.alert_state["mem_trend"]["proc:helper@service"]=_hist([10,10,10,10,10])
    async with factory() as session:
        session.add(s);await session.commit()
    async def send(*args,**kw):return []
    monkeypatch.setattr("app.alerts.send_alert",send)
    async def tick(seconds,rep):
        t=NOW+timedelta(seconds=seconds)
        await _set(factory,last_seen=t,last_report=rep)
        await collector.evaluate_servers(factory,Settings(),t)
        async with factory() as session:
            return (await session.get(Server,1)).known_issues
    full=report([pod("worker-new")])
    assert (await tick(0,full))["mem_leak"].get("gone_at")
    incomplete=deepcopy(full);incomplete["kube"]["pods_complete"]=False
    assert "gone_at" not in (await tick(300,incomplete))["mem_leak"]
    for seconds in range(700,1300,60):
        assert (await tick(seconds,full))["mem_leak"].get("gone_at")
    assert await tick(1299,full)
    assert await tick(1300,full) is None
    await engine.dispose()


def test_local_docker_and_pod_growth_share_the_same_worker_budget():
    rep=report(hostname="worker-a")
    rep["docker"]={"access":True,"containers_complete":True,"containers":[
        {"name":"job-runner","image":"sample/app:1","state":"running","mem":1300*MB}]}
    rep["kube"]["nodes"][0]["use"]["mem"]=8*2**30-1024*MB
    st=state(active=False)
    st["mem_trend"]["docker:job-runner"]=_hist([500,700,900,1100,1300])
    rows=mem_trend.problems(server(rep,st),NOW)
    assert len(rows)==2
    assert all(r["eta_days"]==2.6 and r["level"]==2 for r in rows)
