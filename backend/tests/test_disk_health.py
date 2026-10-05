"""Здоровье физических дисков: блок helper'а diskhealth-setup и алерт "Диск: поломка"."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.collector import disk_health_block, disk_health_problems, disk_health_text

GB = 1024 ** 3


def _disk(dev: str, serial: str, **kw) -> dict:
    d = {"dev": dev, "model": "Samsung SSD 990 PRO 2TB", "serial": serial, "type": "nvme",
         "size": 2000 * GB, "rota": 0, "ok": True, "err": "", "health": "PASSED", "failing": False,
         "temp": 42, "wear": 7, "spare": 100, "spare_thr": 10, "media": 0, "crit": "0x00",
         "poh": 2007, "realloc": None, "pending": None, "uncorr": None, "crc": None, "grow": {}}
    d.update(kw)
    return d


def _raid(dev: str, part: str, total: int = 2, active: int = 1, sync: str = "") -> dict:
    return {"dev": dev, "state": "active", "level": "raid1", "total": total, "active": active,
            "map": "[" + "U" * active + "_" * (total - active) + "]", "members": part,
            "failed": "", "spare": "", "sync": sync}


def _k8sd(ts: int) -> dict:
    """Как было на k8s-d 05.10.2026: NVMe умер, оба RAID1 на одном диске."""
    return {"v": 1, "ts": ts, "virt": "none", "container": False, "smart": True,
            "raid": [_raid("md42", "nvme0n1p3"), _raid("md43", "nvme0n1p4")],
            "disks": [_disk("nvme1n1", "S7DNNU0Y719359D", size=0, ok=False, err="dead", health="",
                            temp=None, wear=None, spare=None, spare_thr=None, media=None, crit="",
                            poh=None),
                      _disk("nvme0n1", "S7HENU0Y566831J")],
            "missing": [],
            "io": {"count": 40, "last": ["nvme nvme1: Identify namespace failed (-5)"]}}


def test_degraded_raid_and_dead_disk_are_critical():
    probs = disk_health_problems(_k8sd(1000))
    assert {p[0] for p in probs} == {3}
    keys = sorted(k for _l, k, _t in probs if k)
    assert keys == ["dead:S7DNNU0Y719359D", "raid:md42", "raid:md43"]
    text = disk_health_text(probs)
    # одна беда двух массивов - одной строкой, умерший диск - с моделью и серийником
    assert text.splitlines()[0] == "RAID md42, md43 (raid1): работает 1 из 2 дисков"
    assert "диск nvme1n1 (Samsung SSD 990 PRO 2TB, S7DNNU0Y719359D) не определяется (размер 0)" in text


def test_rebuild_progress_and_spare():
    block = {"v": 1, "ts": 1, "raid": [
        _raid("md43", "nvme0n1p4 nvme1n1p4", sync="recovery=45.2%"),
        {**_raid("md1", "sda1 sdb1", total=2, active=2), "failed": "sdc1"},
        {**_raid("md2", "", total=0, active=0), "state": "inactive", "level": ""},
    ], "disks": [], "missing": [], "io": {"count": 0, "last": []}}
    probs = disk_health_problems(block)
    texts = [t for _l, _k, t in probs if t]
    assert "RAID md2 не собран (inactive)" in texts
    assert "RAID md43 (raid1): работает 1 из 2 дисков, идет восстановление 45.2%" in texts
    assert (2, "raidf:md1", "RAID md1: выпал sdc1, работу взял запасной диск") in probs
    assert probs[0][0] == 3


def test_smart_failed_worn_out_ssd_is_not_critical():
    """mon-a 05.10.2026: Micron 1100 выработал ресурс (202 = 0), SMART говорит FAILED.
    Менять надо, но диск работает: проблема, а не авария."""
    worn = _disk("sda", "170516095C98", type="sata", health="FAILED!", failing=True, wear=100,
                 realloc=1, pending=0, uncorr=0, crc=0)
    dying = _disk("sdb", "X1", type="sata", health="FAILED!", failing=True, wear=None)
    probs = disk_health_problems({"v": 1, "ts": 1, "disks": [worn, dying]})
    assert (2, "smart:170516095C98",
            "диск sda (Samsung SSD 990 PRO 2TB, 170516095C98): SMART считает диск неисправным, "
            "ресурс выработан (износ 100%)") in probs
    assert probs[0] == (3, "smart:X1", "диск sdb (Samsung SSD 990 PRO 2TB, X1): SMART считает диск неисправным")


def test_growing_counters_wear_crc_and_nvme_warning():
    disks = [
        _disk("sda", "A", grow={"realloc": 8, "pending": 3}),
        _disk("sdb", "B", grow={"crc": 5}),
        _disk("nvme0n1", "C", wear=93),
        _disk("nvme1n1", "D", crit="0x04"),
        _disk("nvme2n1", "E", crit="0x02"),
        # статичные счетчики без роста - не новость
        _disk("sdc", "F", realloc=32, media=4),
    ]
    probs = disk_health_problems({"v": 1, "ts": 1, "disks": disks})
    by_key = {k: (lvl, t) for lvl, k, t in probs}
    assert by_key["grow:A"] == (2, "диск sda (Samsung SSD 990 PRO 2TB, A): за сутки выросли ошибки - "
                                   "переназначенные сектора +8, нечитаемые сектора +3")
    assert by_key["crc:B"][0] == 1 and "кабель" in by_key["crc:B"][1]
    assert by_key["wear:C"] == (1, "диск nvme0n1 (Samsung SSD 990 PRO 2TB, C): износ 93%, пора планировать замену")
    assert by_key["crit:D"][0] == 3
    assert by_key["hot:E"][0] == 2
    assert not any(k.endswith(":F") for k in by_key)


def test_missing_disk():
    block = {"v": 1, "ts": 10_000, "disks": [], "missing": [
        {"serial": "WX1", "model": "WDC WD40EFRX", "dev": "sdb", "last": 10_000 - 7300}]}
    assert disk_health_problems(block) == [
        (3, "missing:WX1", "пропал диск sdb (WDC WD40EFRX, WX1), последний раз виден 2 ч назад")]


def test_block_freshness():
    rep = {"clock_unix": 10_000, "extras": {"disk-health": _k8sd(10_000 - 300)}}
    assert disk_health_block(rep) is not None
    rep["extras"]["disk-health"]["ts"] = 10_000 - 3600
    assert disk_health_block(rep) is None
    assert disk_health_block({"extras": {"disk-health": {"v": 2, "ts": 1}}}) is None
    assert disk_health_block({}) is None


def test_text_limit():
    probs = [(3, f"dead:{i}", f"диск {i}") for i in range(7)]
    assert disk_health_text(probs).splitlines()[-1] == "↳ и еще 2"


async def test_disk_health_alert_lifecycle(tmp_path, monkeypatch):
    """Сквозной: развалившийся RAID -> алерт; тот же набор поломок - тишина; новая поломка
    при том же уровне - новое сообщение; починили - отбой."""
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'dh.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def report(block: dict) -> dict:
        return {"clock_unix": int(now.timestamp()), "uptime_seconds": 100000,
                "extras": {"disk-health": block}}

    async with factory() as s:
        s.add(Server(name="k8s-d", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, cpu_alert_percent=0, mem_alert_percent=0,
                     last_report=report(_k8sd(int(now.timestamp()) - 60))))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(block: dict | None = None):
        nonlocal now
        now = now + timedelta(minutes=2)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            if block is not None:
                block["ts"] = int(now.timestamp()) - 30
                srv.last_report = report(block)
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)

    await collector.evaluate_servers(factory, cfg, now)
    assert len(sent) == 1
    assert sent[0].startswith("🚨💽 ") and "RAID md42, md43 (raid1): работает 1 из 2 дисков" in sent[0]
    assert "не определяется (размер 0)" in sent[0] and "ошибки ввода-вывода в журнале ядра: 40" in sent[0]

    await tick(_k8sd(0))  # ничего нового
    assert len(sent) == 1

    rebuild = _k8sd(0)
    rebuild["raid"][0]["sync"] = "recovery=12.0%"  # тот же массив, другой текст - не новость
    await tick(rebuild)
    assert len(sent) == 1

    # у последнего живого диска пошли ошибки: уровень тот же, но об этом надо сказать
    second = _k8sd(0)
    second["disks"][1] = _disk("nvme0n1", "S7HENU0Y566831J", grow={"media": 2})
    await tick(second)
    assert len(sent) == 2 and "за сутки выросли ошибки - ошибки носителя +2" in sent[1]
    await tick(second)
    assert len(sent) == 2

    third = _k8sd(0)
    third["missing"] = [{"serial": "OTHER", "model": "m", "dev": "sda", "last": 1}]
    await tick(third)
    assert len(sent) == 3 and "пропал диск sda" in sent[2]

    fixed = _k8sd(0)
    fixed["raid"] = [_raid("md42", "nvme0n1p3 nvme1n1p3", active=2), _raid("md43", "nvme0n1p4 nvme1n1p4", active=2)]
    fixed["disks"] = [_disk("nvme1n1", "NEW1"), _disk("nvme0n1", "S7HENU0Y566831J")]
    fixed["io"] = {"count": 0, "last": []}
    await tick(fixed)
    # ошибки ядра держатся час после последней пачки - отбоя еще нет
    assert len(sent) == 3
    now = now + timedelta(hours=1)
    await tick(fixed)
    assert len(sent) == 4 and sent[3].startswith("✅") and "диски снова в порядке" in sent[3]
    await engine.dispose()
