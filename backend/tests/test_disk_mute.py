"""Мьют поломки диска снимается сам, когда диск заменили и массив собрался (app/disk_mute.py)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import disk_mute, disk_wear
from app.collector import disk_health_problems

GB = 1024 ** 3
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _disk(dev: str, serial: str, **kw) -> dict:
    d = {"dev": dev, "model": "SATA SSD 1TB", "serial": serial, "type": "ssd", "size": 1000 * GB,
         "rota": 0, "ok": True, "err": "", "health": "PASSED", "failing": False, "temp": 35,
         "wear": 40, "poh": 30000, "realloc": 0, "pending": 0, "uncorr": 0, "crc": 0, "grow": {}}
    d.update(kw)
    return d


def _raid5(active: int = 3, sync: str = "") -> list[dict]:
    return [{"dev": "md0", "state": "active", "level": "raid1", "total": 3, "active": 3, "map": "[UUU]",
             "members": "sda1 sdb1 sdc1", "failed": "", "spare": "", "sync": ""},
            {"dev": "md1", "state": "active", "level": "raid5", "total": 3, "active": active,
             "map": "[" + "U" * active + "_" * (3 - active) + "]", "members": "sda2 sdb2 sdc2",
             "failed": "", "spare": "", "sync": sync}]


def _block(first: dict, active: int = 3, sync: str = "", missing: list | None = None) -> dict:
    return {"v": 1, "ts": int(NOW.timestamp()), "virt": "none", "container": False, "smart": True,
            "raid": _raid5(active, sync), "disks": [first, _disk("sdb", "DISK-B"), _disk("sdc", "DISK-C")],
            "missing": missing or [], "io": {"count": 0, "last": []}}


OLD = _disk("sda", "DISK-OLD", health="FAILED", failing=True, wear=100)
NEW = _disk("sda", "DISK-NEW", wear=43, poh=55000)


def test_anchor_from_last_alert_and_current_problems():
    anc, gone = disk_mute.step(None, True, _block(OLD), ["smart:DISK-OLD"], ["smart:DISK-OLD"], NOW)
    assert gone == [] and anc["serials"] == ["DISK-OLD"]
    assert anc["present"] == ["DISK-B", "DISK-C", "DISK-OLD"]
    # не заглушено - привязки нет, и старая уходит
    assert disk_mute.step(anc, False, _block(OLD), [], [], NOW) == (None, [])
    # нода молчит - судить не по чему
    assert disk_mute.step(anc, True, None, [], [], NOW) == (anc, [])


def test_not_lifted_during_rebuild_lifted_after():
    anc, _ = disk_mute.step(None, True, _block(OLD), ["smart:DISK-OLD"], [], NOW)
    # старый диск вынули, массив без одного
    assert disk_mute.step(anc, True, _block(_disk("sdx", "DISK-B"), active=2), [], [], NOW)[1] == []
    # новый стоит, идет восстановление
    assert disk_mute.step(anc, True, _block(NEW, active=2, sync="recovery=40.5%"), [], [], NOW)[1] == []
    # собрался
    assert disk_mute.step(anc, True, _block(NEW), [], [], NOW)[1] == ["DISK-OLD"]
    # собрался, но ошибки ядра еще держатся
    assert disk_mute.step(anc, True, _block(NEW), [], [], NOW, io_held=True)[1] == []


def test_same_disk_or_disk_still_listed_as_vanished_keeps_mute():
    anc, _ = disk_mute.step(None, True, _block(OLD), ["smart:DISK-OLD"], [], NOW)
    assert disk_mute.step(anc, True, _block(OLD), [], [], NOW)[1] == []
    gone = [{"serial": "DISK-OLD", "model": "m", "dev": "sda", "last": 1}]
    assert disk_mute.step(anc, True, _block(_disk("sdx", "DISK-B"), missing=gone), [], [], NOW)[1] == []


def test_old_mute_set_before_anchors_existed():
    """Мьют поставили до этой версии, диск уже заменен и идет ребилд: диск берется из того, о чем
    сказал последний алерт до мьюта."""
    anc, gone = disk_mute.step(None, True, _block(NEW, active=2, sync="recovery=5.0%"),
                               ["smart:DISK-OLD"], ["raid:md1"], NOW)
    assert anc["serials"] == ["DISK-OLD"] and gone == []
    assert disk_mute.step(anc, True, _block(NEW), [], [], NOW)[1] == ["DISK-OLD"]


def test_mute_without_disk_lifts_on_swap():
    """Глушили развалившийся массив без указания диска: заменой считается ушедший диск и новый."""
    anc, _ = disk_mute.step(None, True, _block(OLD, active=2), ["raid:md1"], ["raid:md1"], NOW)
    assert anc["serials"] == []
    assert disk_mute.step(anc, True, _block(OLD), [], [], NOW)[1] == []  # собрался на том же диске
    assert disk_mute.step(anc, True, _block(NEW), [], [], NOW)[1] == ["DISK-OLD"]


def test_lift_text():
    assert disk_mute.lift_text(["A1"]) == "мьют снят: диск A1 заменен, массив собран"
    assert disk_mute.lift_text(["A1", "B2"]) == "мьют снят: диски A1, B2 заменены, массив собран"


def test_used_replacement_disk_is_quiet():
    """Хостер ставит б/у диск: 55 тыс. часов и износ 43% - не повод для алерта, до 90% далеко."""
    block = _block(NEW)
    assert disk_health_problems(block) == []
    hist = disk_wear.update(None, block, NOW)
    assert not hist["DISK-NEW"].get("soon")
    assert disk_health_problems(block, wear_hist=hist) == []


async def test_lifted_by_collector(tmp_path, monkeypatch):
    """Сквозной: мьют держится весь ребилд, после сборки снимается одной строкой в Telegram и
    записью в журнале, отбоя про давний алерт нет, следующая поломка снова алертит."""
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import AuditLog, Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'dm.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def report(block: dict) -> dict:
        block = dict(block, ts=int(now.timestamp()) - 30)
        return {"clock_unix": int(now.timestamp()), "uptime_seconds": 100000, "extras": {"disk-health": block}}

    async with factory() as s:
        # так выглядит нода, где алерт про умирающий диск пришел и его заглушили насовсем
        s.add(Server(name="node-a", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, cpu_alert_percent=0, mem_alert_percent=0,
                     created_at=now - timedelta(days=30),
                     alert_mutes=["disk_health", "unit:foo.service"],
                     alert_state={"disk_health": 2, "disk_health_sig": ["smart:DISK-OLD"],
                                  "disk_health_told": ["smart:DISK-OLD"], "disk_health_told_lvl": 2},
                     last_report=report(_block(OLD))))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(block: dict) -> Server:
        nonlocal now
        now = now + timedelta(minutes=2)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.last_report = report(block)
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)
        async with factory() as s:
            return (await s.execute(select(Server))).scalars().one()

    srv = await tick(_block(OLD))
    assert sent == [] and srv.alert_state["disk_health_mute"]["serials"] == ["DISK-OLD"]
    srv = await tick(_block(NEW, active=2, sync="recovery=40.5%"))
    assert sent == [] and "disk_health" in srv.alert_mutes

    srv = await tick(_block(NEW))
    assert len(sent) == 1 and sent[0].startswith("✅")
    assert "мьют снят: диск DISK-OLD заменен, массив собран" in sent[0]
    assert srv.alert_mutes == ["unit:foo.service"]
    assert "disk_health_mute" not in srv.alert_state or srv.alert_state["disk_health_mute"] is None
    async with factory() as s:
        rows = list(await s.scalars(select(AuditLog).where(AuditLog.action == "mute_lifted")))
    assert [(r.username, r.target, r.detail) for r in rows] == [
        ("авто", "node-a", "мьют снят: диск DISK-OLD заменен, массив собран")]

    await tick(_block(NEW))  # никакого "снова в порядке" про давний алерт
    assert len(sent) == 1

    # мьюта больше нет: новая поломка видна
    await tick(_block(NEW, active=2))
    from app.alerts import full_text

    assert len(sent) == 2 and "RAID md1" in full_text(sent[1])
    # и в чате названо, что сломалось
    assert "<b></b>" not in sent[1] and "RAID md1" in sent[1], sent[1]
    await engine.dispose()
