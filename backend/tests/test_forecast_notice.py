import json
from datetime import datetime, timedelta, timezone

import pytest

from app import forecast_notice as notice

NOW = datetime(2026, 1, 15, 9, tzinfo=timezone.utc)
ROOT = {("/", "space")}


def item(eta=72, mount="/", kind="space"):
    return {"mount": mount, "kind": kind, "eta_h": eta, "pct": 78.0, "rate": 12.0}


def test_legacy_warning_cannot_skip_critical_confirmation():
    decision = evaluate(items=[item(20)], level=1)
    assert decision.level == 1
    decision = evaluate(decision.state, items=[item(20)], level=1, at=NOW + timedelta(minutes=30))
    assert decision.level == 1
    decision = evaluate(decision.state, items=[item(20)], level=1, at=NOW + timedelta(hours=1))
    assert decision.level == 2


def evaluate(previous=None, *, at=NOW, snapshot=None, items=None, level=0, observed=ROOT,
             growing=True, **kwargs):
    items = [item()] if items is None else items
    if growing:
        # Ordinary fixtures follow a measured continuing trend. Dedicated
        # plateau/jump tests below supply their exact observed percentages.
        elapsed_days = ((snapshot or at) - NOW).total_seconds() / 86400
        items = [{**value, "pct": value["pct"] + value["rate"] * elapsed_days} for value in items]
    return notice.evaluate(previous, items,
                           (snapshot or at).timestamp(), at, announced_level=level,
                           observed=observed, **kwargs)


def sustained(hours, *, eta=72, previous=None, start=NOW, level=0):
    result = evaluate(previous, at=start, items=[item(eta)], level=level)
    for half_hour in range(1, int(hours * 2) + 1):
        result = evaluate(result.state, at=start + timedelta(minutes=30 * half_hour),
                          items=[item(eta)], level=level)
    return result


def test_transient_49_minute_forecast_has_no_notice_or_recovery():
    first = evaluate()
    second = evaluate(first.state, at=NOW + timedelta(minutes=30))
    clear = evaluate(second.state, at=NOW + timedelta(minutes=49), items=[])
    assert (first.level, second.level, clear.level) == (0, 0, 0)
    assert clear.state["members"] == {}


def test_warning_needs_six_hours_of_distinct_snapshots_and_survives_json_reload():
    pending = sustained(5.5)
    assert pending.level == 0 and pending.items == []
    ready = evaluate(json.loads(json.dumps(pending.state)), at=NOW + timedelta(hours=6))
    assert ready.level == 1 and ready.items == [{**item(), "pct": 81.0}]
    assert ready.state["members"]["space:/"]["active"] == 0
    assert notice.mark_announced(ready.state)["members"]["space:/"]["active"] == 1
    assert ready.state["members"]["space:/"]["active"] == 0


def test_repeated_snapshot_does_not_satisfy_confirmation():
    pending = evaluate(items=[item(20)])
    replay = evaluate(pending.state, at=NOW + timedelta(hours=1), snapshot=NOW, items=[item(20)])
    assert replay.level == 0
    fresh = evaluate(replay.state, at=NOW + timedelta(hours=1), items=[item(20)])
    assert fresh.level == 0  # still only two actual growth observations
    third = evaluate(fresh.state, at=NOW + timedelta(hours=1.5), items=[item(20)])
    assert third.level == 2


def test_critical_requires_an_hour_but_imminent_needs_no_hold():
    assert sustained(0.5, eta=20).level == 0
    assert sustained(1, eta=20).level == 2
    assert evaluate(items=[item(6)]).level == 2
    assert evaluate(items=[item(0)]).level == 2
    assert evaluate(items=[item(6.1)]).level == 0


def test_warning_time_cannot_confirm_a_new_critical_forecast():
    warned = sustained(6)
    active = notice.mark_announced(warned.state)
    first_critical = evaluate(active, at=NOW + timedelta(hours=6.5), items=[item(20)], level=1)
    assert first_critical.level == 1
    half = evaluate(first_critical.state, at=NOW + timedelta(hours=7), items=[item(20)], level=1)
    assert half.level == 1
    critical = evaluate(half.state, at=NOW + timedelta(hours=7.5), items=[item(20)], level=1)
    assert critical.level == 2


@pytest.mark.parametrize("other", [item(mount="/data"), item(kind="inode")])
def test_other_mount_or_resource_cannot_inherit_confirmation(other):
    pending = sustained(5.5)
    moved = evaluate(pending.state, at=NOW + timedelta(hours=6), items=[other],
                     observed={(other["mount"], other["kind"])})
    assert moved.level == 0
    assert len(moved.state["members"]) == 1


@pytest.mark.parametrize("break_mode", ["timestamp_gap", "report_epoch", "missing_source"])
def test_gaps_reset_pending_confirmation(break_mode):
    pending = sustained(5.5)
    at = NOW + timedelta(hours=6)
    kwargs = {}
    if break_mode == "timestamp_gap":
        at += timedelta(hours=2)
    elif break_mode == "report_epoch":
        kwargs["restart"] = True
    else:
        pending = notice.evaluate(pending.state, None, at.timestamp(), at)
    reset = evaluate(pending.state, at=at, **kwargs)
    assert reset.level == 0
    assert reset.state["members"]["space:/"]["since"] == at.timestamp()


def test_reporting_gap_cannot_count_an_old_but_fresh_forecast_toward_new_hold():
    pending = evaluate(items=[item(20)])
    after_gap = evaluate(pending.state, at=NOW + timedelta(minutes=50), snapshot=NOW,
                         items=[item(20)], restart=True)
    next_snapshot = evaluate(after_gap.state, at=NOW + timedelta(hours=1), items=[item(20)])
    assert next_snapshot.level == 0
    assert next_snapshot.state["members"]["space:/"]["critical_since"] == (NOW + timedelta(minutes=50)).timestamp()


def test_confirmation_does_not_need_successful_delivery_and_does_not_assume_it():
    ready = sustained(6)
    not_sent = evaluate(ready.state, at=NOW + timedelta(hours=6.5), level=0)
    assert not_sent.level == 1
    assert not_sent.state["members"]["space:/"]["active"] == 0
    clear = evaluate(not_sent.state, at=NOW + timedelta(hours=7), items=[])
    assert clear.level == 0


@pytest.mark.parametrize("eta,expected", [(160, 1), (160.1, 0)])
def test_warning_hysteresis_only_for_a_delivered_member(eta, expected):
    ready = sustained(6)
    next_at = NOW + timedelta(hours=6.5)
    unsent = evaluate(ready.state, at=next_at, items=[item(eta)])
    active = evaluate(notice.mark_announced(ready.state), at=next_at, items=[item(eta)], level=1)
    assert unsent.level == 0
    assert active.level == expected


@pytest.mark.parametrize("eta,expected", [(30, 2), (30.1, 1)])
def test_critical_hysteresis(eta, expected):
    critical = sustained(1, eta=20)
    next_tick = evaluate(notice.mark_announced(critical.state), at=NOW + timedelta(hours=1.5),
                         items=[item(eta)], level=2)
    assert next_tick.level == expected


@pytest.mark.parametrize("observed", [None, set(), {("/other", "space")}, {("/", "inode")}])
def test_missing_mount_does_not_recover_delivered_notice(observed):
    critical = evaluate(items=[item(5)])
    unknown = evaluate(notice.mark_announced(critical.state), at=NOW + timedelta(minutes=30),
                       items=[], level=2, observed=observed)
    assert unknown.level is None and not unknown.complete
    recovered = evaluate(unknown.state, at=NOW + timedelta(hours=1), items=[], level=2)
    assert recovered.level == 0


def test_same_forecast_snapshot_cannot_recover_delivered_notice():
    critical = evaluate(items=[item(5)])
    repeated = evaluate(notice.mark_announced(critical.state), at=NOW + timedelta(minutes=30),
                         snapshot=NOW, items=[], level=2)
    assert repeated.level is None


def test_unresolved_announced_mount_is_retained_beside_new_imminent_mount():
    first = evaluate(items=[item(5)])
    changed = evaluate(notice.mark_announced(first.state), at=NOW + timedelta(minutes=30),
                       items=[item(4, mount="/data")], level=2, observed={("/data", "space")})
    assert changed.level == 2 and not changed.complete
    assert "space:/" in changed.state["members"]


def test_no_recovery_while_replacement_forecast_is_still_pending():
    first = evaluate(items=[item(5)])
    changed = evaluate(notice.mark_announced(first.state), at=NOW + timedelta(minutes=30),
                       items=[item(72, mount="/data")], level=2,
                       observed=ROOT | {("/data", "space")})
    assert changed.level is None and not changed.complete


@pytest.mark.parametrize("invalid", [None, {}, [item(float("nan"))], [item(-1)],
                                   [{**item(), "rate": 0}], [{**item(), "pct": float("inf")}],
                                   [item(), item()]])
def test_invalid_source_cannot_recover_and_resets_pending(invalid):
    first = evaluate(items=[item(5)])
    at = NOW + timedelta(minutes=30)
    unknown = notice.evaluate(notice.mark_announced(first.state), invalid, at.timestamp(), at,
                              announced_level=2, observed=ROOT)
    assert unknown.level is None and not unknown.complete
    assert unknown.state["members"]["space:/"]["active"] == 2
    assert "since" not in unknown.state["members"]["space:/"]


@pytest.mark.parametrize("offset", [timedelta(hours=-3), timedelta(seconds=1)])
def test_stale_or_future_snapshot_is_unknown(offset):
    result = evaluate(snapshot=NOW + offset)
    assert result.level is None and not result.complete


def test_old_snapshot_after_newer_snapshot_is_unknown():
    first = evaluate()
    newer = evaluate(first.state, at=NOW + timedelta(minutes=30))
    older = evaluate(newer.state, at=NOW + timedelta(minutes=31), snapshot=NOW)
    assert older.level is None


def test_legacy_sent_notice_is_not_hidden_by_new_confirmation_policy():
    legacy = evaluate(items=[item(20)], level=2)
    assert legacy.level == 2 and legacy.state["members"]["space:/"]["active"] == 2
    assert evaluate(items=[], level=2).level == 0
    assert evaluate(items=[], level=2, observed=set()).level is None


def test_legacy_unknown_incident_survives_invalid_source_and_missing_inventory():
    unknown = notice.evaluate(None, None, NOW.timestamp(), NOW, announced_level=2)
    still_unknown = evaluate(unknown.state, at=NOW + timedelta(minutes=30),
                             items=[], level=2, observed=set())
    assert still_unknown.level is None
    recovered = evaluate(still_unknown.state, at=NOW + timedelta(hours=1), items=[], level=2)
    assert recovered.level == 0


def test_missing_mount_breaks_new_critical_confirmation_for_announced_warning():
    warned = sustained(6)
    critical = evaluate(notice.mark_announced(warned.state), at=NOW + timedelta(hours=6.5),
                        items=[item(20)], level=1)
    absent = evaluate(critical.state, at=NOW + timedelta(hours=7), items=[item(20)],
                      level=1, observed=set())
    assert absent.level is None
    returned = evaluate(absent.state, at=NOW + timedelta(hours=7.5), items=[item(20)], level=1)
    assert returned.level == 1


def measured_series(percent_at, *, hours=18, eta=72, rate=12):
    result, levels = None, []
    for half_hour in range(int(hours * 2) + 1):
        elapsed = half_hour / 2
        value = {**item(eta), "pct": percent_at(elapsed), "rate": rate}
        result = evaluate(result.state if result else None,
                          at=NOW + timedelta(hours=elapsed), items=[value], growing=False)
        levels.append(result.level)
    return result, levels


@pytest.mark.parametrize("eta", [72, 20])
def test_flat_percentage_never_confirms_even_if_eta_remains_critical_for_hours(eta):
    result, levels = measured_series(lambda hour: 78, eta=eta)
    assert set(levels) == {0}
    assert result.items == []
    assert len(result.state["members"]["space:/"]["points"]) <= 18


@pytest.mark.parametrize("eta", [72, 20])
def test_one_jump_followed_by_flat_usage_never_confirms(eta):
    _result, levels = measured_series(lambda hour: 78 if hour < 2.5 else 80, eta=eta)
    assert set(levels) == {0}


def test_repeated_cleanup_cycles_do_not_confirm_an_unchanged_warning_eta():
    _result, levels = measured_series(lambda hour: 78 + (hour % 3) / 2, hours=24)
    assert set(levels) == {0}


def test_previously_eligible_overnight_growth_is_not_announced_after_it_stops():
    result, levels = measured_series(lambda hour: 78 + min(hour, 6) / 2)
    assert levels[12] == 1  # would be eligible if daytime/delivery allowed it
    assert result.level == 0  # twelve more flat hours do not confirm a new notice
    assert result.state["members"]["space:/"]["active"] == 0


def test_already_delivered_forecast_is_not_auto_recovered_when_recent_growth_stops():
    warned = sustained(6)
    current = notice.mark_announced(warned.state)
    for half_hour in range(1, 25):
        result = evaluate(current, at=NOW + timedelta(hours=6 + half_hour / 2),
                          items=[{**item(), "pct": 81}], level=1, growing=False)
        current = result.state
        assert result.level == 1


def test_rounded_slow_growth_across_both_halves_qualifies_without_float_artifacts():
    result, levels = measured_series(lambda hour: round(78 + 0.9 * hour / 24, 1),
                                     hours=6, rate=0.9)
    assert set(levels[:-1]) == {0}
    assert result.level == 1
    assert result.items[0]["pct"] == 78.2


def test_a_single_rounding_increment_does_not_pretend_to_prove_two_growing_intervals():
    _result, levels = measured_series(lambda hour: 78 if hour < 3 else 78.1,
                                      hours=12, rate=0.3)
    assert set(levels) == {0}


def test_observed_growth_must_be_meaningful_against_the_forecast_model():
    _result, levels = measured_series(lambda hour: round(78 + hour / 30, 1),
                                      hours=12, rate=12)
    assert set(levels) == {0}


def test_small_intermediate_dips_do_not_disqualify_continuing_growth():
    result, _levels = measured_series(
        lambda hour: round(78 + hour / 4 - (0.1 if hour % 2 == 0.5 else 0), 1),
        hours=6, rate=6)
    assert result.level == 1


def test_imminent_forecast_bypasses_recent_growth_confirmation():
    result, levels = measured_series(lambda hour: 78, hours=2, eta=6)
    assert set(levels) == {2} and result.level == 2


@pytest.mark.parametrize("healthy_items", [[], [item(200)]])
def test_same_fresh_healthy_snapshot_can_complete_shared_fifteen_minute_recovery(healthy_items):
    first = evaluate(items=[item(5)])
    state = notice.mark_announced(first.state)
    healthy_at = NOW + timedelta(minutes=30)
    calm = None
    for minute in range(16):
        at = healthy_at + timedelta(minutes=minute)
        result = evaluate(state, at=at, snapshot=healthy_at, items=healthy_items, level=2)
        state = result.state
        assert result.level == 0 and result.complete
        member = state["members"]["space:/"]
        assert member["active"] == 2 and member["ready"] == 0
        assert member["clear_ts"] == healthy_at.timestamp()
        if calm is None:
            calm = at
    assert (at - calm).total_seconds() == 900
    closed = evaluate(state, at=at + timedelta(minutes=1), snapshot=healthy_at, items=[], level=0)
    assert closed.level == 0 and closed.state["members"] == {}


def test_missing_mount_during_recovery_calm_breaks_confirmation_even_on_same_snapshot():
    first = evaluate(items=[item(5)])
    healthy_at = NOW + timedelta(minutes=30)
    healthy = evaluate(notice.mark_announced(first.state), at=healthy_at, items=[], level=2)
    missing = evaluate(healthy.state, at=healthy_at + timedelta(minutes=8), snapshot=healthy_at,
                       items=[], level=2, observed=set())
    assert missing.level is None and not missing.complete
    assert "clear_ts" not in missing.state["members"]["space:/"]
    returned = evaluate(missing.state, at=healthy_at + timedelta(minutes=9), snapshot=healthy_at,
                        items=[], level=2)
    assert returned.level is None
    fresh = evaluate(returned.state, at=NOW + timedelta(hours=1), items=[], level=2)
    assert fresh.level == 0 and fresh.complete


@pytest.mark.parametrize("break_mode", ["stale", "report_epoch", "replayed"])
def test_unknown_evidence_breaks_established_recovery_candidate(break_mode):
    first = evaluate(items=[item(5)])
    healthy_at = NOW + timedelta(minutes=30)
    healthy = evaluate(notice.mark_announced(first.state), at=healthy_at, items=[], level=2)
    at = healthy_at + timedelta(minutes=1)
    snapshot = healthy_at
    kwargs = {}
    if break_mode == "stale":
        at += timedelta(hours=3)
    elif break_mode == "report_epoch":
        kwargs["restart"] = True
    else:
        snapshot = NOW
    broken = evaluate(healthy.state, at=at, snapshot=snapshot, items=[], level=2, **kwargs)
    assert broken.level is None and not broken.complete
    assert "clear_ts" not in broken.state["members"]["space:/"]


def test_stale_empty_snapshot_cannot_first_establish_recovery():
    first = evaluate(items=[item(5)])
    stale = evaluate(notice.mark_announced(first.state), at=NOW + timedelta(hours=3),
                     snapshot=NOW + timedelta(minutes=1), items=[], level=2)
    assert stale.level is None and not stale.complete
    assert stale.state["members"]["space:/"]["active"] == 2
    assert "clear_ts" not in stale.state["members"]["space:/"]


def test_legacy_recovery_binds_inventory_and_requires_it_throughout_calm():
    first = evaluate(items=[], level=1)
    assert first.level == 0 and first.complete
    assert first.state["members"]["space:/"]["active"] == 1
    repeated = evaluate(first.state, at=NOW + timedelta(minutes=15), snapshot=NOW,
                        items=[], level=1)
    assert repeated.level == 0 and repeated.complete
    replaced = evaluate(repeated.state, at=NOW + timedelta(minutes=16), snapshot=NOW,
                        items=[], level=1, observed={("/other", "space")})
    assert replaced.level is None and not replaced.complete
    assert replaced.state["members"]["space:/"]["active"] == 1
    assert "space:/other" not in replaced.state["members"]
