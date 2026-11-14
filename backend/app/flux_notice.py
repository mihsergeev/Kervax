"""Continuous, per-object confirmation before a Flux failure deserves a notice.

Persist ``eps`` on every evaluation, including unknown observations. The caller
owns delivery and recovery: a zero level here must never manufacture a recovery
for a notice that was not sent. ``_complete=False`` cannot confirm recovery.
"""
from datetime import datetime, timezone
from math import isfinite

DOWN_HOLD = 60 * 60
STALE_HOLD = 2 * 60 * 60
MAX_GAP = 120
MAX_SOURCE_AGE = 15 * 60
TRANSIENT = frozenset({"Progressing", "Reconciling", "ProgressingWithRetry", "Unknown"})


def identity(item: dict) -> str:
    return f"{item['kind']}|{item['where']}"


def _date(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(value) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        number = float(value)
        return number if isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def step(previous, items, now: datetime, *, hold_seconds: float = DOWN_HOLD,
         observed: bool = True, reset: bool = False, sample_at=None) -> dict:
    """Return condition context for one alert kind's complete resource inventory.

    ``items`` includes healthy resources so a positive Ready can end an episode.
    Resource identity excludes changing reason/message: an ongoing failed rollout
    does not become a new incident on each retry. Existing transient reasons
    remain excluded; they neither start a failure nor prove positive recovery.

    The caller must pass report freshness/online status via ``observed`` and any
    report continuity break via ``reset``. Where the agent supplies a helper
    timestamp, pass ``sample_at`` too: fresh transport cannot refresh stale data.
    Without it the legacy agent contract only proves report freshness.
    """
    old = previous if isinstance(previous, dict) and not reset else {}
    ctx = {"eps": {}, "since": None, "sig": [], "items": [], "raw": 0,
           "_complete": False}
    if not observed or not isinstance(items, list):
        return ctx
    stamp = _timestamp(sample_at) if sample_at is not None else None
    if sample_at is not None and (stamp is None or not -60 <= now.timestamp() - stamp <= MAX_SOURCE_AGE):
        return ctx

    complete = True
    inventory = {}
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("kind"), str)
                or not item["kind"] or not isinstance(item.get("where"), str)
                or not item["where"] or type(item.get("ready")) is not bool
                or any(item.get(field) is not None and not isinstance(item[field], str)
                       for field in ("reason", "message"))):
            complete = False
            continue
        key = identity(item)
        if key in inventory:
            complete = False
            inventory[key] = None  # contradictory/duplicate evidence is unknown
        else:
            inventory[key] = item

    starts = []
    for key, item in sorted(inventory.items()):
        if item is None or (not item["ready"] and item.get("reason") in TRANSIENT):
            complete = False
            continue
        if item["ready"]:
            continue
        ctx["raw"] += 1
        prior = old.get(key)
        prior = prior if isinstance(prior, dict) else {}
        since, last = _date(prior.get("since")), _date(prior.get("last"))
        prior_stamp = _timestamp(prior.get("sample"))
        continuous = (since is not None and last is not None
                      and since <= last <= now
                      and 0 <= (now - last).total_seconds() <= MAX_GAP
                      and (stamp is None or prior_stamp is None or stamp >= prior_stamp))
        since = since if continuous else now
        episode = {"since": since.isoformat(), "last": now.isoformat()}
        if stamp is not None:
            episode["sample"] = stamp
        ctx["eps"][key] = episode
        starts.append(since)
        if (now - since).total_seconds() >= max(hold_seconds, 0):
            ctx["sig"].append(key)
            ctx["items"].append(item)

    ctx["since"] = min(starts).isoformat() if starts else None
    # Another still-unconfirmed failure is not proof that Flux is healthy.
    ctx["_complete"] = complete and (not ctx["raw"] or bool(ctx["items"]))
    return ctx
