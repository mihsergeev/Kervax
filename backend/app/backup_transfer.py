"""Sustained loss of receiving throughput on an active backup store."""

from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite

from app import bsrv_mirror

STATE = "backup_transfer_sample"
MIB = 1024**2
BASE_MIN = 8 * MIB
BASE_SECONDS = 10 * 60
SLOW_SECONDS = 30 * 60
STALL_SECONDS = 60 * 60
MAX_GAP = 3 * 60
FRESH_LOCK = 10 * 60


def _number(value):
    if type(value) in (int, float) and 0 <= value <= 2**63 - 1 and isfinite(value):
        return value
    return None


def _workload(server, rep, clock, now, previous_work):
    """Empty means observed idle. None means the helper could not confirm work."""
    mirror = bsrv_mirror.mirror_report(server, now)
    if mirror and abs(clock - mirror["ts"]) > MAX_GAP:
        mirror = None
    if mirror and mirror.get("running") and mirror.get("current"):
        return "mirror:" + str(mirror["current"])
    store = rep.get("backup_server") or {}
    if not store.get("present"):
        return "" if mirror else None
    helper = bsrv_mirror.bsrv_report(server, now)
    if not helper or abs(clock - helper["ts"]) > MAX_GAP or store.get("storage_unavailable"):
        return None
    if store.get("running") is False:
        return ""
    # A new snapshot or a change in the set of locked repositories invalidates
    # the reference. An aging lock alone cannot confirm recovery.
    locks = [
        (str(repo["name"]), _number(repo.get("last_activity")) or 0, _number(repo.get("lock_ts")) or 0)
        for repo in store.get("repos") or []
        if isinstance(repo, dict) and repo.get("valid") and repo.get("name") and repo.get("locked")
    ]
    if not locks:
        return ""
    work = sha256(repr(sorted((name, snapshot) for name, snapshot, _ in locks)).encode()).hexdigest()
    if any(-60 <= clock - ts <= FRESH_LOCK for _, _, ts in locks):
        return work
    return None if work == previous_work else ""


def observe(server, now, previous, *, restart=False):
    """Track one workload using fresh agent samples, even across panel restarts."""
    rep = server.last_report or {}
    clock = _number(rep.get("clock_unix"))
    rx, write = (_number(rep.get(key)) for key in ("net_rx", "disk_write"))
    if not clock or clock > now.timestamp() + MAX_GAP or rx is None or write is None:
        return previous, False
    previous = previous if isinstance(previous, dict) else {}
    work = _workload(server, rep, clock, now, previous.get("work"))
    if work is None:
        return previous, False
    if not work:
        return None, True
    # Both ingress and writes must be present to establish a reference. Both
    # must drop to diagnose a slowdown, so cache writeback alone cannot trip it.
    low, high = min(rx, write), max(rx, write)
    previous = previous if work == previous.get("work") else {}
    gap = clock - previous.get("clock", 0)
    if gap <= 0 and not restart:
        return previous, gap == 0
    contiguous = 0 < gap <= MAX_GAP and not restart
    sample = {**previous, "work": work, "clock": clock}
    if not previous.get("ready"):
        base = previous.get("base", 0)
        if not contiguous or base < BASE_MIN or low < max(BASE_MIN, base / 2):
            sample.update(base_at=clock, base=low)
        else:
            sample.update(base=min(base, low), ready=clock - previous["base_at"] >= BASE_SECONDS)
    base = sample["base"]
    slow = sample.get("ready") and high < base / 4
    # A confirmed mirror pull identifies the job from its start. A restic
    # lock can also belong to local maintenance, so require measured transfer.
    stalled = (sample.get("ready") or work.startswith("mirror:")) and high < MIB / 8
    slow_at = previous.get("slow_at") if contiguous else None
    stall_at = previous.get("stall_at") if contiguous else None
    sample["slow_at"] = (slow_at or clock) if slow else None
    sample["stall_at"] = (stall_at or clock) if stalled else None
    if sample["stall_at"] and clock - sample["stall_at"] >= STALL_SECONDS:
        sample["problem"] = "stalled"
    elif sample["slow_at"] and clock - sample["slow_at"] >= SLOW_SECONDS and not stalled:
        sample["problem"] = "slow"
    elif high >= (base / 2 if sample.get("ready") else MIB / 8):
        sample["problem"] = None
        sample["problem_since"] = None
    if sample.get("problem"):
        sample["rate"] = high
        sample["problem_since"] = previous.get("problem_since") or sample["slow_at"] or sample["stall_at"]
    return sample, True


def problem(sample):
    if not sample or not sample.get("problem"):
        return 0, "", None
    if sample["problem"] == "stalled":
        detail = "Передача бэкапа зависла: почти нет записи и входящего трафика больше часа"
    else:
        detail = f"Передача бэкапа замедлилась: {sample['rate'] / MIB:.1f} из {sample['base'] / MIB:.1f} МиБ/с"
    since = datetime.fromtimestamp(sample["problem_since"], timezone.utc).isoformat()
    return 2, detail, since
