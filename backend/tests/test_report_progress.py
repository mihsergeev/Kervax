"""Clock correction, reboot and replay have different observation semantics."""
from datetime import datetime, timedelta, timezone

import pytest

from app import report_progress

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def rep(clock=10000, uptime=5000):
    return {"clock_unix": clock, "uptime_seconds": uptime}


def test_duplicate_clock_does_not_extend_first_observation():
    old, fresh, epoch = report_progress.step(None, rep(), NOW)
    assert fresh and epoch
    later = NOW + timedelta(seconds=121)
    updated, fresh, epoch = report_progress.step(old, rep(), later)
    assert updated["clock"] == old["clock"] and updated["up"] == old["up"] and not fresh and not epoch


@pytest.mark.parametrize("clock,uptime", [(9000, 4000), (9000, 5000), (10000, 4000)])
def test_old_report_cannot_replace_high_water_mark(clock, uptime):
    old, _, _ = report_progress.step(None, rep(), NOW)
    updated, fresh, epoch = report_progress.step(old, rep(clock, uptime), NOW + timedelta(minutes=20))
    assert all(updated[key] == old[key] for key in ("clock", "up", "at")) and not fresh and not epoch


def test_alternating_old_reports_do_not_create_epochs():
    old, _, _ = report_progress.step(None, rep(), NOW)
    for i in range(20):
        old, fresh, epoch = report_progress.step(old, rep(9000 + i % 2, 4000 + i % 2),
                                                 NOW + timedelta(minutes=i + 1))
        assert not fresh and not epoch and old["clock"] == 10000


@pytest.mark.parametrize("clock,uptime", [(10060, 10), (9000, 5060)])
def test_reboot_and_ntp_correction_allow_new_evidence(clock, uptime):
    old, _, _ = report_progress.step(None, rep(), NOW)
    new, fresh, epoch = report_progress.step(old, rep(clock, uptime), NOW + timedelta(minutes=1))
    assert fresh and epoch and new["clock"] == clock and new["up"] == uptime
    followup, fresh, epoch = report_progress.step(new, rep(clock + 60, uptime + 60), NOW + timedelta(minutes=2))
    assert fresh and not epoch and followup["up"] == uptime + 60


def test_old_agent_without_clock_keeps_legacy_contract():
    assert report_progress.step(None, {"uptime_seconds": 42}, NOW) == (None, True, False)


def test_new_report_after_scheduler_gap_is_usable_but_breaks_continuity():
    old, _, _ = report_progress.step(None, rep(), NOW)
    new, fresh, reset = report_progress.step(old, rep(11200, 6200), NOW + timedelta(minutes=20))
    assert fresh and reset and new["clock"] == 11200


def test_combined_reboot_and_clock_correction_requires_ordered_candidate_epoch():
    old, _, _ = report_progress.step(None, rep(100000, 50000), NOW)
    for minute in (1, 2, 3):
        old, fresh, epoch = report_progress.step(old, rep(10000 + minute * 60, minute * 60),
                                                 NOW + timedelta(minutes=minute))
        assert fresh == (minute == 3) and epoch == (minute == 3)
    assert old["clock"] == 10180 and "candidate" not in old


def test_candidate_timeout_and_duplicate_do_not_confirm_epoch():
    old, _, _ = report_progress.step(None, rep(100000, 50000), NOW)
    old, _, _ = report_progress.step(old, rep(10060, 60), NOW + timedelta(minutes=1))
    duplicate, fresh, epoch = report_progress.step(old, rep(10060, 60), NOW + timedelta(minutes=2))
    assert not fresh and not epoch and duplicate["candidate"]["n"] == 1
    old, fresh, epoch = report_progress.step(duplicate, rep(10300, 300), NOW + timedelta(minutes=5))
    assert not fresh and not epoch and old["candidate"]["n"] == 1


@pytest.mark.parametrize("field", ["clock_unix", "uptime_seconds"])
@pytest.mark.parametrize("value", [10**1000, float("nan"), float("inf"), -1, True, "broken", {}])
def test_malformed_number_cannot_confirm_recovery(field, value):
    old, _, _ = report_progress.step(None, rep(), NOW)
    sample = {**rep(), field: value}
    saved, fresh, epoch = report_progress.step(old, sample, NOW + timedelta(minutes=20))
    assert saved == old and not fresh and not epoch


@pytest.mark.parametrize("previous", ["broken", [], {"clock": 10**1000}, {"up": False}, {"clock": {}}])
def test_malformed_progress_starts_a_new_confirmation_epoch(previous):
    saved, fresh, epoch = report_progress.step(previous, rep(), NOW)
    assert saved == {"clock": 10000, "up": 5000, "at": NOW.isoformat()} and fresh and epoch
