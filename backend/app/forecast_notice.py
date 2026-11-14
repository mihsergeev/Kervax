"""Confirm forecast notifications against successive fresh filesystem evidence.

The forecast itself remains visible while a notice is pending. Confirmation is
per filesystem and resource: a different mount or inode forecast cannot inherit
another problem's elapsed time. This module does not change disk/full alerts.
"""
from dataclasses import dataclass
from datetime import datetime
from math import isfinite

STATE = "disk_forecast_notice"
WARN_H = 5 * 24
CRIT_H = 24
WARN_CONFIRM = 6 * 3600
CRIT_CONFIRM = 3600
IMMINENT_H = 6
MAX_AGE = 2 * 3600
# Forecast pct is rounded to 0.1 percentage points. A single rounded increment
# cannot distinguish a continuing trend from a completed one-off copy.
PCT_STEP = 0.1
GROWTH_SHARE = 0.25


@dataclass(frozen=True)
class Decision:
    level: int | None
    items: list[dict]
    state: dict | None
    complete: bool = True


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            if isfinite(value):
                return float(value)
        except OverflowError:
            pass
    return None


def _identity(item):
    mount, kind = item.get("mount"), item.get("kind", "space")
    if isinstance(mount, str) and mount and kind in ("space", "inode"):
        return kind + ":" + mount
    return None


def _valid_item(item):
    if not isinstance(item, dict) or _identity(item) is None:
        return False
    eta, pct, rate = (_number(item.get(k)) for k in ("eta_h", "pct", "rate"))
    return eta is not None and eta >= 0 and pct is not None and 0 <= pct <= 100 and rate is not None and rate > 0


def _level(eta, active):
    if eta <= CRIT_H or (active >= 2 and eta <= CRIT_H * 1.25):
        return 2
    if eta <= WARN_H or (active and eta <= WARN_H * 4 / 3):
        return 1
    return 0


def _severity(value):
    return value if type(value) is int and value in (1, 2) else 0


def _growth_confirmed(points, seconds, rate):
    """Require measured growth in both halves of a recent confirmation window.

    The model's daily minimum can lag a stopped trend for hours. Persistent ETA
    alone therefore does not qualify a notice. Two growing intervals reject a
    single jump followed by a plateau; net differences tolerate minor rounding
    or fluctuations inside an otherwise continuing trend.
    """
    if len(points) < 3:
        return False
    end = points[-1]
    starts = [point for point in points[:-2] if point[0] <= end[0] - seconds]
    if not starts:
        return False
    start = starts[-1]
    middle = min((point for point in points[1:-1] if point[0] > start[0]),
                 key=lambda point: abs(point[0] - (start[0] + end[0]) / 2))
    for left, right in ((start, middle), (middle, end)):
        elapsed = right[0] - left[0]
        required = max(PCT_STEP, rate * elapsed / 86400 * GROWTH_SHARE)
        if elapsed < seconds / 4 or right[1] - left[1] + 1e-6 < required:
            return False
    return True


def _points(prior, timestamp, pct, since):
    points = []
    for point in prior.get("points") or []:
        if not isinstance(point, list) or len(point) != 2:
            continue
        at, used = map(_number, point)
        if (at is not None and used is not None and 0 <= used <= 100
                and max(since, timestamp - WARN_CONFIRM - MAX_AGE) <= at < timestamp
                and (not points or at > points[-1][0])):
            points.append([at, used])
    points.append([timestamp, pct])
    return points[-512:]


def mark_announced(state: dict) -> dict:
    """Persist this version only after delivery of the eligible forecast notice."""
    return {**state, "members": {
        key: {**entry, "active": _severity(entry.get("ready")) or _severity(entry.get("active"))}
        for key, entry in state.get("members", {}).items()
    }}


def _broken(old_state, announced_level, current):
    """Keep announced members unresolved, but discard all elapsed confirmation."""
    members = {}
    old = old_state.get("members")
    for key, entry in (old if isinstance(old, dict) else {}).items():
        if not isinstance(entry, dict):
            continue
        active = min(announced_level, _severity(entry.get("active")))
        if active:
            members[key] = {"mount": entry.get("mount"), "kind": entry.get("kind", "space"),
                            "active": active, "ready": active}
    state = {"members": members, "reset_at": current}
    if announced_level and (not old_state or old_state.get("legacy")):
        state["legacy"] = True
    if _number(old_state.get("ts")) is not None:
        state["ts"] = old_state["ts"]
    return state


def _recovery_member(entry, active, observed, timestamp, newer, restart):
    pair = (entry.get("mount"), entry.get("kind", "space"))
    member = {"mount": pair[0], "kind": pair[1], "active": active, "ready": active}
    healthy = (not restart and observed is not None and pair in observed
               and (newer or _number(entry.get("clear_ts")) == timestamp))
    if healthy:
        # Retain delivery-confirmed identity until the collector completes its
        # recovery hold. The same fresh forecast may sustain, but not start, it.
        member.update(ready=0, clear_ts=timestamp)
    return member, healthy


def evaluate(previous, items, forecast_ts, now: datetime, *, announced_level=0,
             observed: set[tuple[str, str]] | None = None, restart=False) -> Decision:
    """Return a notice level, eligible items and JSON state to persist each tick.

    None level means unknown, never recovery. Pass items=None on offline/stale/
    replayed evidence, restart=True after an agent reporting gap, and observed
    filesystem/resource pairs from the current valid disk inventory. Persist
    state before daytime/mute/delivery gates; announced_level is the successfully
    announced collector state, never the raw forecast level.

    Only a newer forecast snapshot advances a warning hold or starts recovery.
    Repeated fresh healthy evidence may sustain the collector's recovery hold
    while the same announced filesystem remains observed.
    """
    old_state = previous if isinstance(previous, dict) else {}
    old = old_state.get("members") if isinstance(old_state.get("members"), dict) else {}
    timestamp, current = _number(forecast_ts), now.timestamp()
    last_snapshot = _number(old_state.get("ts"))
    if (timestamp is None or not 0 <= current - timestamp <= MAX_AGE
            or not isinstance(items, list) or not all(_valid_item(item) for item in items)
            or (last_snapshot is not None and timestamp < last_snapshot)):
        return Decision(None, [], _broken(old_state, announced_level, current), False)
    # Repetition may sustain an established recovery candidate, but cannot
    # establish one or add time to an unsent warning. Preserve ts across gaps.
    newer = last_snapshot is None or timestamp > last_snapshot
    reset_at = _number(old_state.get("reset_at")) or 0
    if restart:
        old = _broken(old_state, announced_level, current)["members"]
        reset_at = current
    current_items = {_identity(item): item for item in items}
    if len(current_items) != len(items):
        return Decision(None, [], _broken(old_state, announced_level, current), False)
    legacy = bool(announced_level and (not old_state or old_state.get("legacy")))
    members, eligible, unresolved, pending = {}, [], False, False
    for key, entry in old.items():
        if not isinstance(entry, dict):
            continue
        active = min(announced_level, _severity(entry.get("active")))
        if active and key not in current_items:
            members[key], healthy = _recovery_member(entry, active, observed, timestamp, newer, restart)
            unresolved |= not healthy
    for key, item in current_items.items():
        prior = old.get(key) if isinstance(old.get(key), dict) else {}
        active = min(announced_level, _severity(prior.get("active")))
        if legacy:
            active = min(announced_level, _level(item["eta_h"], announced_level))
        pair = (item["mount"], item.get("kind", "space"))
        # The forecast may still be fresh after this mount stopped reporting.
        if observed is not None and pair not in observed:
            if active:
                members[key] = {"mount": pair[0], "kind": pair[1], "active": active, "ready": active}
                unresolved = True
            continue
        raw = _level(item["eta_h"], active)
        if not raw:
            if active:
                members[key], healthy = _recovery_member(prior, active, observed, timestamp, newer, restart)
                unresolved |= not healthy
            continue
        last = _number(prior.get("last"))
        continuous = last is not None and 0 <= timestamp - last <= MAX_AGE
        since = _number(prior.get("since")) if continuous else None
        since = since if since is not None and since <= current else max(timestamp, reset_at)
        critical = _number(prior.get("critical_since")) if continuous else None
        if item["eta_h"] > CRIT_H:
            critical = None
        elif critical is None or critical > current:
            critical = max(timestamp, reset_at)
        points = _points(prior if continuous else {}, timestamp, item["pct"], since)
        ready = active
        if item["eta_h"] <= IMMINENT_H or (
                critical is not None and timestamp - critical >= CRIT_CONFIRM
                and _growth_confirmed([point for point in points if point[0] >= critical],
                                      CRIT_CONFIRM, item["rate"])):
            ready = 2
        elif timestamp - since >= WARN_CONFIRM and _growth_confirmed(points, WARN_CONFIRM, item["rate"]):
            ready = max(ready, 1)
        ready = min(ready, raw)
        members[key] = {"mount": pair[0], "kind": pair[1], "since": since,
                        "critical_since": critical, "last": timestamp, "ready": ready,
                        "active": active, "points": points}
        if ready:
            eligible.append((ready, item))
        else:
            pending = True
    # Legacy incidents have no recorded filesystem identity. Bind their first
    # valid healthy inventory before starting recovery, then require these same
    # filesystems on every repeated snapshot throughout the collector hold.
    if announced_level and not members and not pending and observed and newer and not restart:
        members = {kind + ":" + mount: {"mount": mount, "kind": kind, "active": announced_level,
                                       "ready": 0, "clear_ts": timestamp}
                   for mount, kind in observed}
    state = {"ts": timestamp, "members": members}
    if legacy and not members and not observed:
        state["legacy"] = True
    if reset_at > timestamp:
        state["reset_at"] = reset_at
    eligible.sort(key=lambda entry: entry[1]["eta_h"])
    level = max((entry[0] for entry in eligible), default=0)
    complete = not unresolved
    # Do not announce that exhaustion no longer threatens while another forecast
    # is still being confirmed, or while an announced mount is unobserved.
    if announced_level and not level and (unresolved or pending or not members):
        return Decision(None, [], state, False)
    if unresolved:
        level = max(level, announced_level)
    return Decision(level, [entry[1] for entry in eligible], state, complete)
