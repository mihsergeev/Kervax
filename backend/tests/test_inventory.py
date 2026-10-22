"""Инвентаризация железа (app/inventory.py, /api/report/inventory)."""
from datetime import datetime, timezone

from app.inventory import disk_kind
from app.models import Server, User

_BARE = {
    "cpu_model": "AMD EPYC 7502P 32-Core Processor", "cpu_cores": 64, "cpu_freq": 2894.5,
    "mem_total": 270_000_000_000, "swap_total": 0, "os": "Ubuntu 24.04.1 LTS", "is_vm": False, "virt": "",
    "uptime_seconds": 3_600_000, "os_upd": {"kernel": "6.8.0-47-generic"},
    "disks": [{"mount": "/", "total": 1_900_000_000_000, "used": 1, "avail": 2}],
    "extras": {
        "disk-health": {"v": 1, "ts": 1, "virt": "none", "raid": [
            {"dev": "md0", "level": "raid1", "map": "[UU]", "state": "active"},
            {"dev": "md1", "level": "raid1", "map": "[U_]", "state": "active"},
        ], "disks": [
            {"dev": "nvme1n1", "model": "SAMSUNG MZQL21T9HCJR", "serial": "S1", "type": "nvme", "size": 1_920_383_410_176, "rota": 0},
            {"dev": "nvme0n1", "model": "SAMSUNG MZQL21T9HCJR", "serial": "S2", "type": "nvme", "size": 1_920_383_410_176, "rota": 0},
            {"dev": "sda", "model": "TOSHIBA MG08ACA16TE", "serial": "S3", "type": "sata", "size": 16_000_900_661_248, "rota": 1},
            {"dev": "sdb", "model": "Micron 5300", "serial": "S4", "type": "sata", "size": 1_920_383_410_176, "rota": 0},
        ]},
        "gpu": {"v": 1, "ts": 1, "gpus": [{"i": 0, "name": "NVIDIA H100 80GB HBM3"}, {"i": 1, "name": "NVIDIA H100 80GB HBM3"}]},
    },
}

_VM = {
    "cpu_model": "Intel Xeon Processor (Icelake)", "cpu_cores": 4, "cpu_freq": None, "mem_total": 8_000_000_000,
    "os": "Debian 12", "is_vm": True, "virt": "KVM", "uptime_seconds": 600,
    # без helper'а disk-health: объем по разделам, второй раздел - тот же диск через bind-mount
    "disks": [{"mount": "/", "total": 100, "used": 10, "avail": 90},
              {"mount": "/var/lib/docker", "total": 100, "used": 10, "avail": 90},
              {"mount": "/data", "total": 50, "used": 1, "avail": 49}],
    "gpu_pci": [{"bus": "0000:41:00.0", "dev": "20b5", "drv": "nvidia"}],
}


async def _fill(client) -> None:
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        s.add_all([
            Server(name="web-a", group_name="prod", token_hash="t-a", enabled=True, last_seen=now,
                   offline_after_seconds=120, external_ip="203.0.113.10", local_ip="10.0.0.5", last_report=_BARE),
            Server(name="stage-a", group_name="stage", token_hash="t-b", enabled=True, last_seen=now,
                   offline_after_seconds=120, external_ip="203.0.113.11", local_ip="203.0.113.11", last_report=_VM),
            Server(name="new-a", group_name="prod", token_hash="t-c", enabled=True),
            Server(name="old-a", group_name="prod", token_hash="t-d", enabled=False, last_report=_VM),
        ])
        await s.commit()


async def test_inventory_rows(client, auth_headers):
    await _fill(client)
    r = await client.get("/api/report/inventory", headers=auth_headers)
    assert r.status_code == 200, r.text
    rows = {x["name"]: x for x in r.json()["servers"]}
    # выключенный сервер в инвентаризацию не попадает, по группе и имени
    assert [x["name"] for x in r.json()["servers"]] == ["new-a", "web-a", "stage-a"]

    a = rows["web-a"]
    assert a["threads"] == 64 and a["freq_mhz"] == 2894 and a["mem"] == 270_000_000_000
    assert a["virt"] == "" and a["os"] == "Ubuntu 24.04.1 LTS" and a["kernel"] == "6.8.0-47-generic"
    assert [(d["dev"], d["kind"]) for d in a["disks"]] == [
        ("nvme0n1", "nvme"), ("nvme1n1", "nvme"), ("sda", "hdd"), ("sdb", "ssd")]
    assert a["disks"][2]["model"] == "TOSHIBA MG08ACA16TE" and a["disks"][2]["size"] == 16_000_900_661_248
    assert [(x["dev"], x["degraded"]) for x in a["raid"]] == [("md0", False), ("md1", True)]
    assert a["gpus"] == ["NVIDIA H100 80GB HBM3"] * 2
    assert a["ip"] == "203.0.113.10" and a["local_ip"] == "10.0.0.5" and a["online"] and a["uptime"] == 3_600_000

    b = rows["stage-a"]
    assert b["virt"] == "KVM" and b["freq_mhz"] is None
    # helper'а нет: дисков не знаем, объем по разделам без повторов
    assert b["disks"] is None and b["fs_total"] == 150
    assert b["gpus"] == ["NVIDIA"] and b["local_ip"] == ""

    # агент еще не присылал отчет: строка есть, данных нет
    n = rows["new-a"]
    assert not n["reported"] and n["threads"] == 0 and n["disks"] is None and n["gpus"] == []


async def test_inventory_scope(client, auth_headers):
    await _fill(client)
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        u = (await s.execute(User.__table__.select().where(User.username == "admin"))).first()
        user = await s.get(User, u.id)
        user.server_groups = ["stage"]
        await s.commit()
    r = await client.get("/api/report/inventory", headers=auth_headers)
    assert [x["name"] for x in r.json()["servers"]] == ["stage-a"]
    async with f() as s:
        user = await s.get(User, u.id)
        user.server_groups = None
        user.sections = ["sites"]
        await s.commit()
    r = await client.get("/api/report/inventory", headers=auth_headers)
    assert r.json()["servers"] == []


def test_disk_kind():
    assert disk_kind({"dev": "nvme0n1", "type": ""}, False) == "nvme"
    assert disk_kind({"dev": "sda", "type": "sas", "rota": 1}, False) == "hdd"
    assert disk_kind({"dev": "sda", "rota": 0}, False) == "ssd"
    assert disk_kind({"dev": "vda", "rota": 1, "err": "virtual"}, False) == "virt"
    assert disk_kind({"dev": "sda", "rota": 0}, True) == "virt"
    assert disk_kind({"dev": "sda"}, False) == ""
