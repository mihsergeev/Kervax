"""Мьют поломки диска снимается сам, когда диск заменили и массив собрался.

Повод: на ноде с RAID5 умирал SSD (SMART FAILED), алерт "Диск: поломка" заглушили насовсем. Хостер
диск заменил, массив пересобрался, а мьют остался: следующую поломку дисков на этой ноде никто бы
не увидел. Бессрочный мьют диска на самом деле "пока этот диск не заменят".

Привязка (alert_state["disk_health_mute"]): ставится сборщиком на первом тике, когда видит мьют.
serials - диски, из-за которых глушили: из того, о чем сказал последний алерт до мьюта
(disk_health_sig, так работает и мьют, поставленный до этой версии), и из того, что сломано
сейчас. present - какие диски стояли на ноде в тот момент: нужен мьюту без конкретного диска
(глушили развалившийся массив или ошибки ядра), там заменой считается ушедший диск плюс
появившийся новый.

Снимается, когда: свежий блок helper'а, диски привязки пропали из отчета (и не числятся среди
пропавших: пропавший без замены - еще не замена), все массивы снова полные, без восстановления, и
в журнале ядра больше нет ошибок ввода-вывода.
Пока идет ребилд, мьют держится: иначе сразу пришел бы алерт про восстановление массива. Мьют
сняли руками - привязка уходит вместе с ним.
"""

from __future__ import annotations

import re
from datetime import datetime

KEY = "disk_health"
STATE = "disk_health_mute"
# виды поломок в disk_health_problems с серийником диска после двоеточия
_SERIAL_KIND = re.compile(r"^(?:dead|smart|crit|hot|grow|crc|wear|wearsoon|missing):(.+)$")


def serials_of(keys) -> set[str]:
    """Серийники дисков из ключей поломок (sig алерта)."""
    out = set()
    for k in keys or []:
        m = _SERIAL_KIND.match(str(k))
        if m and m.group(1) != "?":
            out.add(m.group(1))
    return out


def present(block: dict) -> set[str]:
    """Серийники дисков, которые нода видит сейчас."""
    return {str(d["serial"]) for d in block.get("disks") or []
            if isinstance(d, dict) and d.get("serial")}


def _missing(block: dict) -> set[str]:
    return {str(m["serial"]) for m in block.get("missing") or [] if isinstance(m, dict) and m.get("serial")}


def arrays_whole(block: dict) -> bool:
    """Все массивы полные: собраны, все диски на месте, восстановление не идет."""
    for r in block.get("raid") or []:
        if not isinstance(r, dict):
            continue
        try:
            tot, act = int(r.get("total") or 0), int(r.get("active") or 0)
        except (TypeError, ValueError):
            return False
        if (r.get("state") == "inactive" or act < tot or str(r.get("failed") or "").strip()
                or str(r.get("sync") or "").strip()):
            return False
    return True


def anchor(block: dict, told_sig, now_sig, now: datetime) -> dict:
    """Привязка мьюта: какие диски глушили и что стояло на ноде."""
    return {"serials": sorted(serials_of(told_sig) | serials_of(now_sig)),
            "present": sorted(present(block)), "at": now.isoformat()}


def replaced(anc: dict, block: dict) -> list[str]:
    """Серийники замененных дисков привязки, если замена закончена (массивы полные); иначе []."""
    if not arrays_whole(block):
        return []
    now_present = present(block)
    gone_list = _missing(block)
    serials = [str(x) for x in anc.get("serials") or []]
    if serials:
        if any(x in now_present or x in gone_list for x in serials):
            return []
        return serials
    was = {str(x) for x in anc.get("present") or []}
    gone = sorted(was - now_present - gone_list)
    return gone if gone and now_present - was else []


def step(anc: dict | None, muted: bool, block: dict | None, told_sig, now_sig,
         now: datetime, io_held: bool = False) -> tuple[dict | None, list[str]]:
    """Один тик сборщика: (новая привязка, замененные диски). Непустой список - мьют пора снять.
    block None - судить не по чему (нода молчит, helper не отчитался): все как было. io_held -
    ошибки ввода-вывода в журнале ядра еще держатся (час после последней пачки): снятый мьют
    тут же дал бы алерт про них."""
    if not muted:
        return None, []
    if block is None:
        return anc, []
    if not isinstance(anc, dict):
        return anchor(block, told_sig, now_sig, now), []
    return anc, [] if io_held else replaced(anc, block)


def lift_text(serials: list[str]) -> str:
    """Строка для хронологии, журнала и Telegram."""
    if not serials:
        return "мьют снят: диск заменен, массив собран"
    what = "диск " + serials[0] if len(serials) == 1 else "диски " + ", ".join(serials)
    return f"мьют снят: {what} заменен{'ы' if len(serials) > 1 else ''}, массив собран"
