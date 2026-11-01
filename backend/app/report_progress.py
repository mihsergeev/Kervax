"""Persisted evidence that the agent collected a new report, rather than replayed one.

This is an ordering guard, not an authentication mechanism. Agents without a clock
retain the legacy contract. Uptime distinguishes clock correction from old data:
an ordinary reboot advances wall time while uptime falls; NTP rollback advances
uptime while wall time falls. A regression of both needs three subsequent ordered
observations; a single old report or two alternating replays cannot establish it.
"""
from datetime import datetime, timezone
from math import isfinite

KEY = "report_progress"
MAX_SAME_SECONDS = 120  # independent of a custom offline timeout or recovery hold
NEW_EPOCH_POINTS = 3


def _number(value) -> int | float | None:
    try:
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and isfinite(value) and 0 <= value <= 2**63 - 1):
            return value
    except (OverflowError, TypeError, ValueError):
        pass
    return None


def _age(at, now):
    try:
        value = datetime.fromisoformat(str(at or ""))
        value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return (now - value).total_seconds()
    except ValueError:
        return None


def _candidate(old, clock, uptime, now):
    previous = old.get("candidate")
    previous = previous if isinstance(previous, dict) else {}
    age = _age(previous.get("at"), now)
    old_clock, old_up = _number(previous.get("clock")) or 0, _number(previous.get("up")) or 0
    if age is not None and 0 <= age <= MAX_SAME_SECONDS:
        if clock == old_clock and uptime == old_up:
            return old, False, False
        ordered = clock > old_clock and uptime > old_up
    else:
        ordered = False
    count = min(int(_number(previous.get("n")) or 0) + 1, NEW_EPOCH_POINTS) if ordered else 1
    point = {"clock": clock, "up": uptime, "at": now.isoformat()}
    if count >= NEW_EPOCH_POINTS:
        return point, True, True
    return {**old, "candidate": {**point, "n": count}}, False, False


def step(previous, report: dict, now: datetime) -> tuple[dict | None, bool, bool]:
    """(stored progress, usable observation, continuity break).

    A new report is usable after a scheduler gap, but the gap cannot satisfy a
    recovery hold. The first ordered observation also starts a new confirmation.
    """
    old = previous if isinstance(previous, dict) else {}
    if not isinstance(report, dict):
        return old or None, False, False
    clock = _number(report.get("clock_unix", 0))
    if clock is None:
        return old or None, False, False
    if not clock:
        return None, True, False
    uptime = _number(report.get("uptime_seconds", 0))
    if uptime is None:
        return old or None, False, False
    old_clock, old_up = _number(old.get("clock", 0)), _number(old.get("up", 0))
    epoch = not old_clock or old_clock is None or old_up is None
    old_clock, old_up = old_clock or 0, old_up or 0
    age = _age(old.get("at"), now)
    if age is None or not 0 <= age <= MAX_SAME_SECONDS:
        epoch = True
    if old_clock:
        if clock < old_clock:
            if uptime and old_up and uptime < old_up:
                return _candidate(old, clock, uptime, now)
            if not (uptime and old_up and uptime > old_up):
                return old, False, False
            epoch = True  # advancing uptime proves progress across a clock correction
        elif uptime and old_up and uptime < old_up:
            if clock <= old_clock:
                return old, False, False
            epoch = True  # ordinary reboot, not an older pre-failure report
        elif clock == old_clock and uptime == old_up:
            if age is None:
                # Missing/corrupt legacy progress cannot confirm an elapsed hold.
                return {"clock": clock, "up": uptime, "at": now.isoformat()}, True, True
            return {k: v for k, v in old.items() if k != "candidate"}, 0 <= age <= MAX_SAME_SECONDS, False
    return {"clock": clock, "up": uptime, "at": now.isoformat()}, True, epoch
