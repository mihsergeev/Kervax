from datetime import datetime, timedelta, timezone

import pytest

from app import flux_notice as flux


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def resource(name="apps", *, ready=False, reason="HealthCheckFailed", kind="Kustomization"):
    return {"kind": kind, "where": f"demo/{name}", "ready": ready, "reason": reason}


def run(minutes, objects=None, previous=None, **kwargs):
    return flux.step(previous, [resource()] if objects is None else objects,
                     START + timedelta(minutes=minutes), **kwargs)


def held(until=60, objects=None, **kwargs):
    state = None
    for minute in range(until + 1):
        result = run(minute, objects, state, **kwargs)
        state = result["eps"]
    return result


def test_eighteen_minute_failure_never_qualifies_and_leaves_no_episode():
    problem = held(18)
    assert problem["items"] == [] and problem["sig"] == []
    assert problem["raw"] == 1 and not problem["_complete"]
    healthy = run(19, [resource(ready=True)], problem["eps"])
    assert healthy == {"eps": {}, "since": None, "sig": [], "items": [],
                       "raw": 0, "_complete": True}


def test_failure_qualifies_only_after_continuous_hour():
    assert held(59)["items"] == []
    result = held()
    assert result["items"] == [resource()]
    assert result["sig"] == ["Kustomization|demo/apps"]
    assert result["since"] == START.isoformat() and result["_complete"]


def test_another_object_cannot_inherit_elapsed_time():
    first = held(59)
    replacement = run(60, [resource("other")], first["eps"])
    assert replacement["items"] == []
    assert replacement["since"] == (START + timedelta(minutes=60)).isoformat()


def test_new_member_does_not_reset_old_member_or_qualify_early():
    first = held(59)
    result = run(60, [resource("new"), resource()], first["eps"])
    assert result["items"] == [resource()]
    assert set(result["eps"]) == {"Kustomization|demo/apps", "Kustomization|demo/new"}


@pytest.mark.parametrize("reason", ["Progressing", "ProgressingWithRetry", "Reconciling", "Unknown"])
def test_transient_reasons_remain_excluded_without_claiming_positive_recovery(reason):
    result = held(60, [resource(reason=reason)])
    assert result["items"] == [] and result["eps"] == {} and not result["_complete"]


@pytest.mark.parametrize("reason", ["HealthCheckFailed", "UpgradeFailed", "DependencyNotReady"])
def test_stalled_rollout_failure_still_qualifies(reason):
    assert held(60, [resource(reason=reason)])["items"] == [resource(reason=reason)]


def test_reason_and_message_changes_do_not_restart_same_failed_rollout():
    first = held(59, [resource(reason="HealthCheckFailed")])
    result = run(60, [dict(resource(reason="UpgradeFailed"), message="retry 7")], first["eps"])
    assert len(result["items"]) == 1


@pytest.mark.parametrize("kwargs", [{"observed": False}, {"sample_at": "invalid"}, {"sample_at": True},
                                    {"sample_at": START.timestamp() - 901}])
def test_unknown_observation_breaks_episode_without_confirming_recovery(kwargs):
    result = run(60, previous=held(59)["eps"], **kwargs)
    assert result["eps"] == {} and not result["_complete"] and result["items"] == []


@pytest.mark.parametrize("objects", [None, {}, [resource(reason="Unknown")],
                                      [resource(reason=[])], [{**resource(), "message": {}}],
                                      [{"kind": "Kustomization", "where": "demo/apps"}],
                                      [resource(), resource(ready=True)]])
def test_missing_malformed_or_conflicting_snapshot_cannot_confirm_recovery(objects):
    result = flux.step(held()["eps"], objects, START + timedelta(minutes=61))
    assert not result["_complete"] and result["items"] == []


def test_unknown_period_does_not_lend_time_to_next_failure():
    before = held(59)
    unknown = flux.step(before["eps"], None, START + timedelta(minutes=60))
    after = run(61, previous=unknown["eps"])
    assert after["items"] == [] and after["since"] == (START + timedelta(minutes=61)).isoformat()


@pytest.mark.parametrize("kwargs, minute", [({}, 63), ({"reset": True}, 60)])
def test_scheduler_gap_or_new_report_epoch_restarts_confirmation(kwargs, minute):
    result = run(minute, previous=held(59)["eps"], **kwargs)
    assert result["items"] == [] and result["since"] == (START + timedelta(minutes=minute)).isoformat()


def test_helm_repository_keeps_two_hour_confirmation():
    objects = [resource(kind="HelmRepository")]
    assert held(119, objects, hold_seconds=flux.STALE_HOLD)["items"] == []
    assert held(120, objects, hold_seconds=flux.STALE_HOLD)["items"] == objects


def test_disappeared_member_does_not_keep_episode_for_later_reappearance():
    first = held(59)
    absent = run(60, [], first["eps"])
    assert absent["eps"] == {}
    assert run(61, previous=absent["eps"])["items"] == []


def test_stale_helper_snapshot_cannot_accumulate_hour_from_fresh_transport():
    result = None
    for minute in range(61):
        result = run(minute, previous=result["eps"] if result else None, sample_at=START.timestamp())
        assert result["items"] == []
    assert not result["_complete"] and result["eps"] == {}


def test_regressing_helper_timestamp_restarts_confirmation():
    first = held(59)
    for ep in first["eps"].values():
        ep["sample"] = (START + timedelta(minutes=59)).timestamp()
    result = run(60, previous=first["eps"], sample_at=(START + timedelta(minutes=58)).timestamp())
    assert result["items"] == []


def test_corrupt_persisted_episode_cannot_inherit_old_since():
    prior = {"Kustomization|demo/apps": {"since": START.isoformat(), "last": "broken"}}
    assert run(60, previous=prior)["items"] == []
