"""Заглушка одного диска (app/drive_mute.py): этот диск с тем, что у него есть сейчас."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import drive_mute, mute_watch
from app.collector import disk_health_problems

GB = 1024 ** 3
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def _nvme(dev: str, serial: str, **kw) -> dict:
    d = {"dev": dev, "model": "NVMe SSD 512GB", "serial": serial, "type": "nvme", "size": 512 * GB,
         "rota": 0, "ok": True, "err": "", "health": "PASSED", "failing": False, "temp": 34, "wear": 7,
         "spare": 100, "spare_thr": 10, "media": 0, "errlog": 0, "ioerr": 0, "crit": "0x00", "poh": 38000,
         "realloc": None, "pending": None, "uncorr": None, "crc": None, "grow": {}}
    d.update(kw)
    return d


def _raid(members1: str = "nvme1n1p1 nvme0n1p1", active1: int = 2) -> list[dict]:
    return [{"dev": "md0", "state": "active", "level": "raid1", "total": 2, "active": active1,
             "map": "[UU]", "members": members1, "failed": "", "spare": "", "sync": ""},
            {"dev": "md1", "state": "active", "level": "raid1", "total": 2, "active": 2, "map": "[UU]",
             "members": "nvme0n1p2 nvme1n1p2", "failed": "", "spare": "", "sync": ""}]


def _block(b: dict | None = None, a: dict | None = None, raid: list | None = None, missing: list | None = None) -> dict:
    disks = [x for x in (a if a is not None else _nvme("nvme0n1", "DISK-A"), b) if x is not None]
    return {"v": 1, "ts": int(NOW.timestamp()), "virt": "none", "container": False, "smart": True,
            "raid": raid if raid is not None else _raid(), "disks": disks, "missing": missing or [],
            "io": {"count": 0, "last": []}}


SICK = _nvme("nvme1n1", "DISK-B", media=8)
MUTES = ["drive:DISK-B"]


def _split(block: dict, state=None, mutes=MUTES):
    return drive_mute.split(mutes, state, block, disk_health_problems(block), NOW)


def test_muted_disk_hidden_other_disk_alerts():
    sp = _split(_block(SICK, a=_nvme("nvme0n1", "DISK-A", media=3)))
    assert [k for _l, k, _t in sp.kept] == ["media:DISK-A"]
    assert [k for _l, k, _t in sp.hidden["DISK-B"]] == ["media:DISK-B"]
    base = sp.state["DISK-B"]
    assert base["c"]["media"] == 8 and base["keys"] == ["media:DISK-B"] and base["dev"] == "nvme1n1"
    assert base["arrays"] == {"md0": 1, "md1": 1}
    assert drive_mute.base_text(base) == "ошибок носителя 8"


def test_not_muted_nothing_hidden():
    sp = _split(_block(SICK), mutes=["disk_health"])
    assert [k for _l, k, _t in sp.kept] == ["media:DISK-B"] and not sp.hidden and not sp.state


def test_same_state_stays_hidden():
    base = _split(_block(SICK)).state
    sp = _split(_block(dict(SICK, temp=40)), base)
    assert not sp.kept and not sp.worse and sp.state == base


def test_more_errors_is_worse():
    base = _split(_block(SICK)).state
    sp = _split(_block(dict(SICK, media=12, grow={"media": 4})), base)
    assert sp.worse == {"DISK-B": "ошибок носителя 12, было 8"}
    assert {k for _l, k, _t in sp.kept} == {"grow:DISK-B"} and "DISK-B" not in sp.state


def test_new_signs_are_worse():
    base = _split(_block(SICK)).state
    assert "запас ячеек" in _split(_block(dict(SICK, spare=60)), base).worse["DISK-B"]
    assert "предупреждение NVMe" in _split(_block(dict(SICK, crit="0x04")), base).worse["DISK-B"]
    assert "SMART" in _split(_block(dict(SICK, health="FAILED", failing=True)), base).worse["DISK-B"]
    assert "не отвечает" in _split(_block(dict(SICK, ok=False, err="dead")), base).worse["DISK-B"]
    assert "новые ошибки чтения или записи" in _split(_block(dict(SICK, ioerr=5, grow={"ioerr": 1})), base).worse["DISK-B"]


def test_dropped_from_array_is_worse_and_raid_alerts_anyway():
    base = _split(_block(SICK)).state
    blk = _block(SICK, raid=_raid("nvme0n1p1", 1))
    sp = _split(blk, base)
    assert sp.worse == {"DISK-B": "выпал из массива md0"}
    assert any(k == "raid:md0" for _l, k, _t in sp.kept)


def test_missing_is_worse_replaced_is_gone():
    base = _split(_block(SICK)).state
    sp = _split(_block(None, missing=[{"serial": "DISK-B", "dev": "nvme1n1", "model": "x", "last": 1}]), base)
    assert "диск пропал" in sp.worse["DISK-B"]
    sp = _split(_block(_nvme("nvme1n1", "DISK-NEW")), base)
    assert sp.gone == ["DISK-B"] and not sp.worse and not sp.state


def test_media_after_day_window_not_new():
    """Глушили, когда ошибки как раз выросли (grow): через сутки рост ушел из окна, остался media: -
    это то же, а не новый признак."""
    blk0 = _block(dict(SICK, grow={"media": 2}))
    base = _split(blk0).state
    assert base["DISK-B"]["keys"] == ["grow:DISK-B"]
    sp = _split(_block(SICK), base)
    assert not sp.worse and not sp.kept


def test_present_for_mute_watch():
    assert "drive:" in mute_watch.PRESENT_PREFIXES
    assert mute_watch.judge("drive:DISK-B", {}, [], None, {"drive:": {"DISK-B": "ошибок 8"}}, set()) == (True, "ошибок 8")
    assert mute_watch.judge("drive:DISK-B", {}, [], None, {"drive:": {}}, set()) == (False, "")


async def test_collector_end_to_end(tmp_path, monkeypatch):
    """Сквозной: открытый алерт по диску закрывается молча, заглушенный диск не алертит, второй диск
    алертит, стало хуже - заглушка снимается и алерт идет, замена диска снимает заглушку молча."""
    from app import collector
    from app.alerts import full_text
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import AuditLog, Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'dr.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def report(block: dict) -> dict:
        block = dict(block, ts=int(now.timestamp()) - 30)
        return {"clock_unix": int(now.timestamp()), "uptime_seconds": 100000, "extras": {"disk-health": block}}

    async with factory() as s:
        s.add(Server(name="node-a", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, cpu_alert_percent=0, mem_alert_percent=0, created_at=now - timedelta(days=30),
                     alert_mutes=[], alert_state={}, last_report=report(_block(SICK))))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(block: dict, mutes: list[str] | None = None) -> Server:
        nonlocal now
        now = now + timedelta(minutes=2)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.last_report = report(block)
            if mutes is not None:
                srv.alert_mutes = mutes
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)
        async with factory() as s:
            return (await s.execute(select(Server))).scalars().one()

    srv = await tick(_block(SICK))
    assert len(sent) == 1 and "ошибок носителя 8" in full_text(sent[0])

    # заглушили диск: алерт закрыт молча, базовая линия записана, в "Что сломано" пусто
    srv = await tick(_block(SICK), ["drive:DISK-B"])
    assert len(sent) == 1 and not srv.alert_state.get("disk_health")
    assert srv.alert_state["drive_mute"]["DISK-B"]["c"]["media"] == 8
    assert not [p for p in collector.server_problems(srv, now) if p["kind"] == "disk_health"]
    assert srv.alert_state["mute_live"]["drive:DISK-B"]["on"] == 1
    assert drive_mute.view(srv)["DISK-B"]["c"]["media"] == 8

    # второй диск начал сыпаться - алерт про него
    srv = await tick(_block(SICK, a=_nvme("nvme0n1", "DISK-A", media=2)))
    assert len(sent) == 2 and "nvme0n1" in full_text(sent[1]) and "nvme1n1" not in full_text(sent[1])
    srv = await tick(_block(SICK))
    assert len(sent) == 3 and sent[2].startswith("✅")

    # заглушенному диску хуже: заглушка снята, алерт с причиной
    srv = await tick(_block(dict(SICK, media=11, grow={"media": 3})))
    assert len(sent) == 4 and "был заглушен, стало хуже: ошибок носителя 11, было 8" in full_text(sent[3])
    assert "drive:DISK-B" not in (srv.alert_mutes or []) and not srv.alert_state.get("drive_mute")
    async with factory() as s:
        rows = list(await s.scalars(select(AuditLog).where(AuditLog.action == "mute_lifted")))
    assert [r.detail for r in rows] == ["заглушка диска nvme1n1 (DISK-B) снята: стало хуже - ошибок носителя 11, было 8"]

    # заглушили снова, потом диск заменили: заглушка уходит молча
    sick2 = dict(SICK, media=11)
    srv = await tick(_block(sick2), ["drive:DISK-B"])
    n = len(sent)
    srv = await tick(_block(_nvme("nvme1n1", "DISK-NEW")))
    assert srv.alert_mutes == [] and not srv.alert_state.get("drive_mute")
    assert all("DISK-B" not in full_text(x) or x.startswith("✅") for x in sent[n:])
    await engine.dispose()
