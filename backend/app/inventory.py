"""Инвентаризация железа: что стоит на серверах - процессор, память, диски, RAID, GPU, ОС.

Отдельно ничего не собираем, все уже приходит:
- отчет агента: модель CPU, потоки, текущая частота, RAM, ОС, ВМ или Baremetal, аптайм, разделы;
- helper disk-health (report.d/disk-health.json): физические диски с моделью, серийником, типом
  и размером, RAID из /proc/mdstat. На ВМ он перечисляет виртуальные диски;
- helper gpu: модели карт. Без него - сколько карт NVIDIA видно на шине;
- страна по IP - офлайн-таблица (app/geoip.py).

Железо меняется редко, поэтому блок helper'а берем любой давности, а не только свежий, как для
алертов: диск, замолчавший час назад, в инвентаризации все равно есть. Без helper'а disk-health
размер дисков считаем по разделам, а тип не знаем - так и показываем.
"""

from datetime import datetime

from app import geoip, gpu
from app.collector import seen_online
from app.models import Server


def _num(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def disk_kind(d: dict, vm: bool) -> str:
    """nvme | ssd | hdd | virt; пусто - не понять (нет ни транспорта, ни признака вращения)"""
    if vm or d.get("err") == "virtual":
        return "virt"
    if str(d.get("type") or "").lower() == "nvme" or str(d.get("dev") or "").startswith("nvme"):
        return "nvme"
    rota = d.get("rota")
    if rota in (1, "1", True):
        return "hdd"
    if rota in (0, "0", False):
        return "ssd"
    return ""


def _disks(rep: dict, vm: bool) -> tuple[list[dict] | None, list[dict]]:
    """Физические диски и RAID из блока disk-health; None вместо дисков - helper'а нет"""
    b = ((rep.get("extras") or {}).get("disk-health")) or {}
    if not isinstance(b, dict) or b.get("v") != 1:
        return None, []
    vm = vm or str(b.get("virt") or "none") != "none"
    disks = []
    for d in b.get("disks") or []:
        if not isinstance(d, dict):
            continue
        disks.append({
            "dev": str(d.get("dev") or ""),
            "model": str(d.get("model") or "").strip(),
            "serial": str(d.get("serial") or "").strip(),
            "kind": disk_kind(d, vm),
            "size": int(_num(d.get("size")) or 0),
        })
    disks.sort(key=lambda x: x["dev"])
    raid = []
    for r in b.get("raid") or []:
        if not isinstance(r, dict) or not r.get("dev"):
            continue
        raid.append({
            "dev": str(r.get("dev")),
            "level": str(r.get("level") or ""),
            "map": str(r.get("map") or ""),
            "state": str(r.get("state") or ""),
            "sync": str(r.get("sync") or ""),
            # массив без одного из дисков: в карте "_" или есть выпавшие
            "degraded": "_" in str(r.get("map") or "") or bool(r.get("failed")),
        })
    return disks, raid


def _fs_total(rep: dict) -> int:
    """Объем по разделам, когда дисков не видно: одинаковые точки (bind-mount, overlay того же
    раздела) считаем один раз"""
    seen: set[tuple] = set()
    total = 0
    for d in rep.get("disks") or []:
        if not isinstance(d, dict):
            continue
        key = (d.get("total"), d.get("used"), d.get("avail"))
        if key in seen:
            continue
        seen.add(key)
        total += int(_num(d.get("total")) or 0)
    return total


def _gpus(rep: dict) -> list[str]:
    b = gpu.block(rep)
    names = [str(g.get("name") or "").strip() for g in ((b or {}).get("gpus") or []) if isinstance(g, dict)]
    names = [n for n in names if n]
    if names:
        return names
    # helper'а нет: карты на шине без моделей
    return ["NVIDIA"] * len(gpu.pci_list(rep))


def server_row(s: Server, now: datetime) -> dict:
    rep = s.last_report or {}
    vm = bool(rep.get("is_vm")) or bool(rep.get("virt"))
    disks, raid = _disks(rep, vm)
    ip = s.external_ip or s.local_ip or ""
    freq = _num(rep.get("cpu_freq"))
    return {
        "id": s.id,
        "name": s.name,
        "group": s.group_name or "",
        "enabled": s.enabled,
        "online": seen_online(s, now),
        "reported": bool(rep),
        "cpu_model": str(rep.get("cpu_model") or "").strip(),
        "threads": int(_num(rep.get("cpu_cores")) or 0),
        "freq_mhz": round(freq) if freq else None,
        "mem": int(_num(rep.get("mem_total")) or 0),
        "swap": int(_num(rep.get("swap_total")) or 0),
        "disks": disks,
        "fs_total": _fs_total(rep),
        "raid": raid,
        "gpus": _gpus(rep),
        "os": s.os or str(rep.get("os") or ""),
        "kernel": str((rep.get("os_upd") or {}).get("kernel") or ""),
        "virt": str(rep.get("virt") or ("VM" if vm else "")),
        "ip": ip,
        "local_ip": s.local_ip if s.local_ip and s.local_ip != ip else "",
        "country": geoip.country_of(s.external_ip) or geoip.country_of(s.agent_ip),
        "uptime": int(_num(rep.get("uptime_seconds")) or 0),
        "last_seen": s.last_seen.isoformat() if s.last_seen else None,
    }
