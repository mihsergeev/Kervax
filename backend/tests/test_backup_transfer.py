from datetime import datetime, timedelta, timezone

from app import backup_transfer, collector
from test_pressure import NOW, _life, _srv

MIB = backup_transfer.MIB


def _report(now, rate=20, *, write=None, snapshot=123, locked=True, mirror=False):
    clock = int(now.timestamp())
    helper = {"v": 1, "ts": clock}
    if mirror:
        details = {"extras": {"backup-mirror": {**helper, "running": locked, "current": "store/app"}}}
    else:
        repo = {"name": "app", "valid": True, "locked": locked, "lock_ts": clock, "last_activity": snapshot}
        details = {"backup_server": {"present": True, "running": True, "repos": [repo]},
                   "extras": {"backup-server": helper}}
    return {**details, "clock_unix": clock, "uptime_seconds": clock - 1_700_000_000,
            "net_rx": rate * MIB, "disk_write": (rate if write is None else write) * MIB,
            "disk_devs": [{"dev": "sda", "await": 650, "util": 30}],
            "psi": {"io_full_60": 65}}


def _sample(minute, previous=None, rate=20, **kw):
    now = NOW + timedelta(minutes=minute)
    return backup_transfer.observe(_srv(_report(now, rate, **kw)), now, previous)


def test_transfer_needs_contiguous_samples_and_both_rates():
    sample = None
    for minute in range(11):
        sample, complete = _sample(minute, sample)
        assert complete
    assert sample["ready"]
    for minute in range(11, 46):
        sample, _ = _sample(minute, sample, write=1)
    assert not backup_transfer.problem(sample)[0]
    for minute in range(46, 76):
        sample, _ = _sample(minute, sample, 2)
    assert not backup_transfer.problem(sample)[0]
    assert _sample(75, sample, 2)[0] == sample
    sample, _ = _sample(90, sample, 2)
    assert not backup_transfer.problem(sample)[0]
    for minute in range(91, 121):
        sample, _ = _sample(minute, sample, 2)
    level, text, _ = backup_transfer.problem(sample)
    assert level == 2 and "2.0 из 20.0" in text
    now = NOW + timedelta(minutes=121)
    aged = _report(now, 0)
    aged["backup_server"]["repos"][0]["lock_ts"] -= 11 * 60
    assert backup_transfer.observe(_srv(aged), now, sample) == (sample, False)
    replaced, _ = _sample(121, sample, 2, snapshot=124)
    assert not backup_transfer.problem(replaced)[0]
    assert _sample(121, sample, locked=False) == (None, True)


def test_confirmed_mirror_can_stall_from_start_but_primary_idle_lock_cannot():
    mirror = primary = None
    for minute in range(61):
        mirror, _ = _sample(minute, mirror, 0, mirror=True)
        primary, _ = _sample(minute, primary, 0)
    assert "зависла" in backup_transfer.problem(mirror)[1]
    assert not backup_transfer.problem(primary)[0]
    assert not backup_transfer.problem(_sample(61, mirror, 10, mirror=True)[0])[0]


def test_old_latency_is_hidden_only_on_backup_hosts():
    state = {"disk_slow_since": (NOW - timedelta(hours=8)).isoformat(), "disk_slow": 2}
    primary = _report(NOW)
    mirror = _report(NOW, mirror=True)
    regular = {k: v for k, v in primary.items() if k not in {"backup_server", "extras"}}
    for report in (primary, mirror, regular):
        s = _srv(report, state)
        assert ("disk_slow" in collector._server_conditions(s, NOW)) == (report is regular)
        assert any(p["kind"] == "disk_slow" for p in collector.server_problems(s, NOW)) == (report is regular)


async def test_scheduler_one_incident_and_no_legacy_recovery(tmp_path, monkeypatch):
    start = datetime.now(timezone.utc)
    snapshot = int(start.timestamp()) - 3600
    tick, sent, engine = await _life(tmp_path, monkeypatch, _report(start, snapshot=snapshot),
                                    {"disk_slow": 2, "pressure": 2, "pressure_res": "io"})

    async def sample(minute, rate):
        await tick(0 if minute == 0 else 1, _report(start + timedelta(minutes=minute), rate, snapshot=snapshot))

    try:
        for minute in range(11):
            await sample(minute, 20)
        for minute in range(11, 41):
            await sample(minute, 2)
        assert not sent
        await sample(41, 2)
        assert len(sent) == 1 and "Передача бэкапа замедлилась" in sent[0]
        for minute in range(42, 104):
            await sample(minute, 0)
        assert len(sent) == 1
        for minute in range(104, 121):
            await sample(minute, 20)
        assert len(sent) == 2 and "просадка скорости бэкапа" in sent[1]
    finally:
        await engine.dispose()


async def test_stale_lock_and_mirror_alerts_wait_for_new_issues(tmp_path, monkeypatch):
    start = datetime.now(timezone.utc)
    t0 = int(start.timestamp())

    def report(now, *, missing=False, new=False):
        result = _report(now, snapshot=t0)
        repo = result["backup_server"]["repos"][0]
        repo["lock_ts"] = t0 - 2 * 86400
        copy = {"src": "store", "repo": "app", "enabled": True, "last_ok": t0 - 3 * 86400}
        result["extras"]["backup-mirror"] = {"v": 1, "ts": int(now.timestamp()), "copies": [copy]}
        if missing:
            result["extras"] = {}
            result["backup_server"]["repos"] = []
        if new:
            result["backup_server"]["repos"].append({**repo, "name": "db"})
            result["extras"]["backup-mirror"]["copies"].append({**copy, "repo": "db"})
        return result

    tick, sent, engine = await _life(tmp_path, monkeypatch, report(start))
    try:
        await tick(0)
        assert len(sent) == 2
        await tick(1, report(start + timedelta(minutes=1), missing=True))
        assert len(sent) == 2
        await tick(1440, report(start + timedelta(minutes=1441)))
        related = ("висит лок", "висячих локов", "копия на резервном", "копии на резервном")
        assert sum(any(part in message.lower() for part in related) for message in sent) == 2
        await tick(1, report(start + timedelta(minutes=1442), new=True))
        assert sum(any(part in message.lower() for part in related) for message in sent) == 4
    finally:
        await engine.dispose()
