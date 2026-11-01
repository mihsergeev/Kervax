"""Learning a pod UID after upgrade must not erase its already active trend."""
from copy import deepcopy
from datetime import timedelta

import pytest

from app import collector, mem_trend
from app.config import Settings
from app.models import Server
from tests.test_known import _panel, _set
from tests.test_mem_trend_inventory import MB, NOW, OLD, pod, report, server, state

UID = "aaaaaaaa"
OTHER_UID = "bbbbbbbb"


@pytest.mark.parametrize("complete", [True, False])
def test_first_uid_preserves_legacy_history_and_critical_condition(complete):
    rep = report([pod(u=UID)], complete=complete)
    st = state()
    before = deepcopy(st["mem_trend"][OLD])
    s = server(rep, st)
    # Evaluation happens before update in the collector too.
    row = mem_trend.problems(s, NOW)[0]
    assert row["key"] == OLD and row["level"] == 2 and row["eta_days"] == 2.5
    updated = mem_trend.update(st["mem_trend"], rep, NOW)
    assert updated[OLD] == {**before, "u": UID}
    s.alert_state = {**st, "mem_trend": updated}
    assert collector._server_conditions(s, NOW)["mem_leak"][0] == 2
    assert mem_trend.problems(s, NOW)[0]["rate"] == 200 * MB


@pytest.mark.parametrize("old_uid", [None, "", UID])
@pytest.mark.parametrize("current_uid", [None, ""])
def test_missing_uid_neither_resets_history_nor_erases_known_identity(old_uid, current_uid):
    st = state()["mem_trend"]
    if old_uid is not None:
        st[OLD]["u"] = old_uid
    before = deepcopy(st[OLD])
    rep = report([pod(u=current_uid)])
    updated = mem_trend.update(st, rep, NOW)
    assert updated[OLD]["h"] == before["h"]
    assert updated[OLD]["m"] == before["m"]
    assert updated[OLD].get("u") == (old_uid or None)
    assert collector._server_conditions(server(rep, {**state(), "mem_trend": updated}), NOW)["mem_leak"][0] == 2


def test_bootstrap_uid_waits_for_a_successful_memory_sample():
    st = state()["mem_trend"]
    rep = report([pod(u=UID)])
    rep["kube"]["pods"][0]["use"]["ts"] -= 600
    assert mem_trend.update(st, rep, NOW) == st
    assert "u" not in mem_trend.update(st, rep, NOW)[OLD]


@pytest.mark.parametrize("complete", [True, False])
def test_two_known_different_uids_keep_the_existing_replacement_rules(complete):
    st = state()["mem_trend"]
    st[OLD]["u"] = UID
    rep = report([pod(u=OTHER_UID)], complete=complete)
    updated = mem_trend.update(st, rep, NOW)
    if complete:
        assert updated[OLD]["u"] == OTHER_UID and updated[OLD]["h"] == []
        assert mem_trend.leak(updated[OLD]) is None
    else:
        assert updated == st
        assert collector._server_conditions(server(rep, {**state(), "mem_trend": updated}), NOW)["mem_leak"][0] == 2


@pytest.mark.parametrize("complete", [True, False])
async def test_upgrade_uid_bootstrap_keeps_known_after_full_collector_updates(tmp_path, monkeypatch, complete):
    engine, factory = await _panel(tmp_path)
    entry = {"at": NOW.isoformat(), "by": "operator", "text": "investigating", "level": 2, "sig": [OLD]}
    st = state()
    original_history = deepcopy(st["mem_trend"][OLD]["h"])
    s = server(report([pod(u=UID)], complete=complete), st, entry)
    async with factory() as session:
        session.add(s)
        await session.commit()
    sent = []
    async def send(*args, **kwargs):
        sent.append(args)
        return []
    monkeypatch.setattr("app.alerts.send_alert", send)
    try:
        # Cross the 600s Known auto-close boundary with fresh critical worker samples.
        for seconds in (0, 60, 600, 660):
            t = NOW + timedelta(seconds=seconds)
            rep = report([pod(u=UID)], complete=complete)
            rep["kube"]["pods"][0]["use"]["ts"] = t.timestamp()
            rep["kube"]["nodes"][0]["use"]["ts"] = t.timestamp()
            await _set(factory, last_seen=t, last_report=rep)
            await collector.evaluate_servers(factory, Settings(), t)
            async with factory() as session:
                live = await session.get(Server, 1)
                assert live.known_issues and "mem_leak" in live.known_issues
                assert "gone_at" not in live.known_issues["mem_leak"]
                trend = live.alert_state["mem_trend"][OLD]
                assert trend["u"] == UID and trend["h"] == original_history
                assert collector._server_conditions(live, t)["mem_leak"][0] == 2
    finally:
        await engine.dispose()
    assert not sent
