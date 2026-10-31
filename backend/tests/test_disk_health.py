"""Здоровье физических дисков: блок helper'а diskhealth-setup и алерт "Диск: поломка"."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
            "disks": [_disk("nvme1n1", "DEMO-NVME-DEAD-01", size=0, ok=False, err="dead", health="",
                            temp=None, wear=None, spare=None, spare_thr=None, media=None, crit="",
                            poh=None),
                      _disk("nvme0n1", "DEMO-NVME-OK-01")],
            "missing": [],
            "io": {"count": 40, "last": ["nvme nvme1: Identify namespace failed (-5)"]}}


def test_degraded_raid_and_dead_disk_are_critical():
    probs = disk_health_problems(_k8sd(1000))
    assert {p[0] for p in probs} == {3}
    keys = sorted(k for _l, k, _t in probs if k)
    assert keys == ["dead:DEMO-NVME-DEAD-01", "raid:md42", "raid:md43"]
    text = disk_health_text(probs)
    # одна беда двух массивов - одной строкой, умерший диск - с моделью и серийником
    assert text.splitlines()[0] == "RAID md42, md43 (raid1): работает 1 из 2 дисков"
    assert "диск nvme1n1 (Samsung SSD 990 PRO 2TB, DEMO-NVME-DEAD-01) не определяется (размер 0)" in text


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
    """Тестовая нода 05.10.2026: SSD выработал ресурс (202 = 0), SMART говорит FAILED.
    helper до 0.5 не говорит, какой атрибут провален: менять надо, но диск работает - проблема,
    а не авария."""
    worn = _disk("sda", "DEMO-SATA-WORN-01", type="sata", health="FAILED!", failing=True, wear=100,
                 realloc=1, pending=0, uncorr=0, crc=0)
    dying = _disk("sdb", "X1", type="sata", health="FAILED!", failing=True, wear=None)
    probs = disk_health_problems({"v": 1, "ts": 1, "disks": [worn, dying]})
    assert (2, "smart:DEMO-SATA-WORN-01",
            "диск sda (Samsung SSD 990 PRO 2TB, DEMO-SATA-WORN-01): SMART считает диск неисправным, "
            "ресурс выработан (износ 100%)") in probs
    assert probs[0] == (3, "smart:X1", "диск sdb (Samsung SSD 990 PRO 2TB, X1): SMART считает диск неисправным")


def test_smart_failed_only_by_wear_attribute_is_not_a_problem():
    """helper 0.5 называет провалившиеся атрибуты (failnow): FAILED только по остатку ресурса -
    износ, а не поломка. По любому другому атрибуту - авария, сколько бы ни было износа."""
    worn = _disk("sda", "W1", type="sata", health="FAILED!", failing=True, wear=100,
                 failnow="Percent_Lifetime_Remain")
    bad = _disk("sdb", "W2", type="sata", health="FAILED!", failing=True, wear=100,
                failnow="Percent_Lifetime_Remain,Reallocated_Sector_Ct")
    probs = disk_health_problems({"v": 1, "ts": 1, "disks": [worn, bad]})
    assert [(lvl, k) for lvl, k, _t in probs] == [(3, "smart:W2")]


def test_nvme_wear_over_100_with_clean_smart_is_healthy():
    """Повод: два NVMe в RAID1 с Percentage Used 212% и 137%, запас 100%, ни одной ошибки, журнал
    ядра чистый - а панель красила их красным. Percentage Used - оценка вендора (бывает до 255),
    за 100% диск выходит из гарантии, а не из строя."""
    disks = [_disk("nvme0n1", "N0", wear=212, poh=40000), _disk("nvme1n1", "N1", wear=137, poh=40000),
             _disk("nvme2n1", "N2", wear=93), _disk("nvme3n1", "N3", wear=100, spare=97)]
    block = {"v": 1, "ts": 1, "raid": [_raid("md0", "nvme0n1p1 nvme1n1p1", active=2)], "disks": disks}
    assert disk_health_problems(block) == []
    assert disk_health_problems(block, short=True) == []


def test_real_signs_of_a_dying_nvme():
    disks = [
        _disk("nvme0n1", "S1", spare=15),                     # запас ниже 20% - пора менять
        _disk("nvme1n1", "S2", spare=8, spare_thr=10),        # ниже порога, а бит еще не стоит
        _disk("nvme2n1", "S3", spare=5, crit="0x01", health="FAILED!", failing=True),
        _disk("nvme3n1", "S4", media=3, wear=212),            # давние ошибки носителя - тоже признак
        _disk("nvme4n1", "S5", grow={"ioerr": 4}),            # журнал ошибок NVMe: новые ошибки I/O
        _disk("nvme5n1", "S6", crit="0x0c"),
        _disk("nvme6n1", "S7", spare=99),                     # начал тратить запас - только заметка
    ]
    by_key = {k: (lvl, t) for lvl, k, t in disk_health_problems({"v": 1, "ts": 1, "disks": disks}, short=True)}
    assert by_key["spare:S1"] == (2, "диск nvme0n1: запас ячеек 15% (порог 10%) - пора менять")
    assert by_key["spare:S2"] == (3, "диск nvme1n1: запас ячеек 8% при пороге 10% - менять")
    # бит 0 сам говорит про запас: одна строка, без отдельной про SMART
    assert by_key["crit:S3"] == (3, "диск nvme2n1: NVMe сообщает - запас ячеек ниже порога (0x01)")
    assert "spare:S3" not in by_key and "smart:S3" not in by_key
    assert by_key["media:S4"] == (2, "диск nvme3n1: ошибок носителя 3 (данные не прочитались) - пора менять")
    assert by_key["errlog:S5"][0] == 2 and "журнале ошибок NVMe" in by_key["errlog:S5"][1]
    assert by_key["crit:S6"] == (3, "диск nvme5n1: NVMe сообщает - надежность снижена из-за ошибок носителя "
                                    "или внутренних, диск перешел в режим только чтения (0x0c)")
    assert not any(k.endswith(":S7") for k in by_key)


def test_growing_counters_crc_and_nvme_warning():
    disks = [
        _disk("sda", "A", grow={"realloc": 8, "pending": 3}),
        _disk("sdb", "B", grow={"crc": 5}),
        _disk("nvme1n1", "D", crit="0x04"),
        _disk("nvme2n1", "E", crit="0x02"),
        # статичные счетчики SATA без роста - не новость
        _disk("sdc", "F", type="sata", realloc=32, media=4),
        _disk("nvme3n1", "G", media=2, grow={"media": 2}),
    ]
    probs = disk_health_problems({"v": 1, "ts": 1, "disks": disks})
    by_key = {k: (lvl, t) for lvl, k, t in probs}
    assert by_key["grow:A"] == (2, "диск sda (Samsung SSD 990 PRO 2TB, A): за сутки выросли ошибки - "
                                   "переназначенные сектора +8, нечитаемые сектора +3")
    assert by_key["crc:B"][0] == 1 and "кабель" in by_key["crc:B"][1]
    assert by_key["crit:D"][0] == 3
    assert by_key["hot:E"][0] == 2
    assert not any(k.endswith(":F") for k in by_key)
    # рост и так говорит про ошибки носителя: отдельной строки нет
    assert "grow:G" in by_key and "media:G" not in by_key


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

    from app.alerts import full_text

    await collector.evaluate_servers(factory, cfg, now)
    assert len(sent) == 1
    full = full_text(sent[0])
    assert sent[0].startswith("🚨💽 ") and "RAID md42, md43 (raid1): работает 1 из 2 дисков" in full
    assert "не определяется (размер 0)" in full and "ошибки ввода-вывода в журнале ядра: 40" in full
    # в чате - что сломано: какой массив, без пустой строки "что случилось"
    assert "<b></b>" not in sent[0] and "RAID md42" in sent[0], sent[0]

    await tick(_k8sd(0))  # ничего нового
    assert len(sent) == 1

    rebuild = _k8sd(0)
    rebuild["raid"][0]["sync"] = "recovery=12.0%"  # тот же массив, другой текст - не новость
    await tick(rebuild)
    assert len(sent) == 1

    # у последнего живого диска пошли ошибки: уровень тот же, но об этом надо сказать
    second = _k8sd(0)
    second["disks"][1] = _disk("nvme0n1", "DEMO-NVME-OK-01", grow={"media": 2})
    await tick(second)
    assert len(sent) == 2 and "за сутки выросли ошибки - ошибки носителя +2" in full_text(sent[1])
    assert "nvme0n1" in sent[1].split("\n")[1]  # новое - первым, в строке "что случилось"
    await tick(second)
    assert len(sent) == 2

    third = _k8sd(0)
    third["missing"] = [{"serial": "OTHER", "model": "m", "dev": "sda", "last": 1}]
    await tick(third)
    assert len(sent) == 3 and "пропал диск sda" in full_text(sent[2])
    assert "диск sda" in sent[2].split("\n")[1]

    fixed = _k8sd(0)
    fixed["raid"] = [_raid("md42", "nvme0n1p3 nvme1n1p3", active=2), _raid("md43", "nvme0n1p4 nvme1n1p4", active=2)]
    fixed["disks"] = [_disk("nvme1n1", "NEW1"), _disk("nvme0n1", "DEMO-NVME-OK-01")]
    fixed["io"] = {"count": 0, "last": []}
    await tick(fixed)
    # ошибки ядра держатся час после последней пачки - отбоя еще нет
    assert len(sent) == 3
    now = now + timedelta(hours=1)
    await tick(fixed)
    assert len(sent) == 4 and sent[3].startswith("✅") and "диски снова в порядке" in sent[3]
    assert "\n" not in sent[3]
    await engine.dispose()


async def test_open_wear_alert_closes_quietly(tmp_path, monkeypatch):
    """Алерт, открытый раньше только из-за износа (wear:, wearsoon:), после обновления панели
    закрывается без "диски снова в порядке": диски не чинились, просто износ больше не поломка."""
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'dw.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    block = {"v": 1, "ts": int(now.timestamp()) - 30, "virt": "none", "container": False, "smart": True,
             "raid": [_raid("md0", "nvme0n1p1 nvme1n1p1", active=2)],
             "disks": [_disk("nvme0n1", "N0", wear=212), _disk("nvme1n1", "N1", wear=137)],
             "missing": [], "io": {"count": 0, "last": []}}
    async with factory() as s:
        s.add(Server(name="db-a", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, cpu_alert_percent=0, mem_alert_percent=0,
                     last_report={"clock_unix": int(now.timestamp()), "uptime_seconds": 100000,
                                  "extras": {"disk-health": block}},
                     alert_state={"disk_health": 1, "disk_health_sig": ["wear:N0", "wear:N1"],
                                  "disk_health_told": ["wear:N0", "wear:N1"]}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    assert sent == []
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        assert int((srv.alert_state or {}).get("disk_health") or 0) == 0
        assert not any(p["kind"] == "disk_health" for p in collector.server_problems(srv, now))
    await engine.dispose()


def test_virtual_disks_of_a_vm_are_listed_and_quiet():
    # helper 0.2 на ВМ перечисляет виртуальные диски без SMART: проблем по ним не бывает, а
    # "пропавшим" отсоединенный облачный том не становится (на ВМ helper не помнит диски)
    vd = _disk("sda", "", model="Virtual disk", type="spi", rota=1, err="virtual", health="",
               temp=None, wear=None, spare=None, spare_thr=None, media=None, crit="", poh=None)
    block = {"v": 1, "ts": 1000, "virt": "vmware", "container": False, "smart": False, "raid": [],
             "disks": [vd, dict(vd, dev="sdb", ok=False)], "missing": [], "io": {"count": 0, "last": []}}
    assert disk_health_problems(block) == []
    src = (Path(__file__).resolve().parents[2] / "agent" / "diskhealth-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.5" in src and "\r" not in src
    body = src[src.index("elif ! $container; then"):src.index("EOF_VLSBLK\nfi")]
    assert 'add_disk "$name" "$model" "$serial" "$tran" "$size" "$rota" true virtual' in body
    assert "SEEN_NOW" not in body and "PRESENT" not in body and "smartctl" not in body
    # разбор lsblk один на железо и ВМ
    assert src.count("lsblk -d -n -b -P") == 1 and src.count("$(lsblk_disks)") == 2

