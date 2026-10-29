"""Заглушка одного диска: этот диск с тем, что у него есть сейчас, а не все диски сервера.

Повод: дедик с двумя NVMe в зеркале, у одного 8 ошибок носителя при SMART PASSED. Диск работает,
второй здоров, менять пока не собираются. Мьют вида disk_health (app/disk_mute.py) заглушил бы и второй
диск: начни сыпаться и он, алерта бы не было.

Ключ в alert_mutes - "drive:<серийник>" (в ЦОД диск меняют по серийнику, имя устройства после
перезагрузки уезжает). Базовая линия в alert_state["drive_mute"]: {серийник: {dev, at, c: счетчики,
spare, crit, keys: признаки}}. Ставится сборщиком на первом тике, когда видит заглушку: то, что было на
диске, когда его глушили.

Пока не хуже, поломки этого диска не алертят и не красные. Хуже - заглушка снимается сама, и алерт
идет как обычно: вырос счетчик (ошибки носителя, переназначенные и нечитаемые сектора, ошибки чтения и
записи в журнале NVMe, CRC), появился новый признак (запас ячеек ниже, предупреждение NVMe, SMART FAILED,
диск не отвечает или пропал), диск выпал из массива. Массивы алертят сами по себе, их заглушка диска не
трогает. Серийник ушел с ноды без следа (не в "пропавших") - диск заменили, заглушка уходит молча.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

PREFIX = "drive:"
STATE = "drive_mute"
# счетчики, рост которых значит "стало хуже" (errlog не тут: в журнал ошибок NVMe пишут и отказы
# админ-команд, это шум; ошибки чтения и записи из него - ioerr)
COUNTERS = ("media", "realloc", "pending", "uncorr", "crc", "ioerr")
_COUNTER_TEXT = {
    "media": "ошибок носителя", "realloc": "переназначенных секторов", "pending": "нечитаемых секторов",
    "uncorr": "неисправимых ошибок", "crc": "ошибок CRC",
}
# ioerr - номер последней ошибки чтения или записи в журнале NVMe, а не их число: вырос - есть новые
_IOERR_TEXT = "новые ошибки чтения или записи в журнале NVMe"
_IOERR_BASE = "ошибки чтения или записи в журнале NVMe"
# признаки (виды ключей disk_health_problems): новый среди них - хуже. grow, errlog и crc - рост за
# сутки, их судим по счетчикам: иначе вчерашний рост, ушедший из окна суток и вернувшийся, был бы "новым"
_SIGNS = ("dead", "smart", "crit", "hot", "spare", "media", "missing")
_KIND_TEXT = {
    "dead": "диск не отвечает", "smart": "SMART считает диск неисправным", "crit": "предупреждение NVMe",
    "hot": "перегрев", "spare": "запас ячеек на исходе", "media": "ошибки носителя", "missing": "диск пропал",
}


def serials(mutes) -> set[str]:
    """Серийники заглушенных дисков."""
    return {str(m)[len(PREFIX):] for m in mutes or [] if str(m).startswith(PREFIX) and len(str(m)) > len(PREFIX)}


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _crit(d: dict) -> int:
    try:
        return int(str(d.get("crit") or "0"), 16)
    except ValueError:
        return 0


def disk_of(part: str) -> str:
    """Раздел -> диск: nvme0n1p2 -> nvme0n1, sda2 -> sda (как dhDiskOf во фронте)."""
    m = re.match(r"^(.*\d)p\d+$", part) or re.match(r"^(\D+)\d+$", part)
    return m.group(1) if m else part


def _arrays(block: dict, dev: str) -> dict[str, int]:
    """Массивы, где есть разделы диска: {md: сколько его разделов в строю}."""
    out: dict[str, int] = {}
    for r in block.get("raid") or []:
        if not isinstance(r, dict):
            continue
        n = sum(1 for p in str(r.get("members") or "").split() if disk_of(p) == dev)
        if n:
            out[str(r.get("dev") or "md?")] = n
    return out


def _disk(block: dict, ser: str) -> dict | None:
    return next((d for d in block.get("disks") or [] if isinstance(d, dict) and str(d.get("serial") or "") == ser), None)


def _keys_of(probs, ser: str) -> list[str]:
    return sorted({k for _l, k, _t in probs if k and k.split(":", 1)[-1] == ser and ":" in k})


def anchor(block: dict, ser: str, probs, now: datetime) -> dict | None:
    """Базовая линия диска: что у него есть сейчас. Диска на ноде нет - None (глушить нечего)."""
    d = _disk(block, ser)
    if d is None:
        return None
    dev = str(d.get("dev") or "")
    return {"dev": dev, "at": now.isoformat(), "c": {k: _int(d.get(k)) for k in COUNTERS},
            "spare": _num(d.get("spare")), "crit": _crit(d), "keys": _keys_of(probs, ser),
            "arrays": _arrays(block, dev)}


def worse(base: dict, block: dict, ser: str, probs) -> str:
    """Что стало хуже, чем при заглушке ("" - не стало)."""
    d = _disk(block, ser)
    keys = set(_keys_of(probs, ser))
    was = set(base.get("keys") or [])
    out: list[str] = []
    for k in sorted(keys - was):
        kind = k.split(":", 1)[0]
        if kind in _SIGNS and not (kind == "media" and "grow:" + ser in was):
            out.append(_KIND_TEXT[kind])
    if d is not None:
        c = base.get("c") or {}
        for k in COUNTERS:
            now_v, was_v = _int(d.get(k)), _int(c.get(k))
            if now_v > was_v:
                out.append(_IOERR_TEXT if k == "ioerr" else f"{_COUNTER_TEXT[k]} {now_v}, было {was_v}")
        sp, sp0 = _num(d.get("spare")), _num(base.get("spare"))
        if sp is not None and sp0 is not None and sp < sp0:
            out.append(f"запас ячеек {sp:g}%, был {sp0:g}%")
        new_bits = _crit(d) & ~_int(base.get("crit"))
        if new_bits and not any(x == _KIND_TEXT["crit"] or x == _KIND_TEXT["hot"] for x in out):
            out.append(f"предупреждение NVMe {new_bits:#04x}")
        dev = str(d.get("dev") or "")
        now_arr = _arrays(block, dev)
        for md, n in sorted((base.get("arrays") or {}).items()):
            if now_arr.get(md, 0) < _int(n):
                out.append(f"выпал из массива {md}")
    seen: list[str] = []
    for x in out:
        if x not in seen:
            seen.append(x)
    return ", ".join(seen)


def replaced(block: dict, ser: str) -> bool:
    """Диска с этим серийником больше нет на ноде и он не числится пропавшим: заменили."""
    if _disk(block, ser) is not None:
        return False
    return not any(isinstance(m, dict) and str(m.get("serial") or "") == ser for m in block.get("missing") or [])


@dataclass
class Split:
    """Поломки дисков с учетом заглушек: kept - алертят, hidden - заглушены ({серийник: поломки}),
    worse - заглушка снимается, стало хуже ({серийник: что}), gone - диск заменили, state - новые
    базовые линии (alert_state["drive_mute"])."""
    kept: list = field(default_factory=list)
    hidden: dict[str, list] = field(default_factory=dict)
    worse: dict[str, str] = field(default_factory=dict)
    gone: list[str] = field(default_factory=list)
    state: dict[str, dict] = field(default_factory=dict)


def split(mutes, state, block: dict, probs, now: datetime) -> Split:
    """Разложить поломки по заглушкам дисков. Чистая функция: сборщик пишет state, снимает worse и gone."""
    out = Split()
    muted = serials(mutes)
    old = state if isinstance(state, dict) else {}
    for ser in sorted(muted):
        base = old.get(ser) if isinstance(old.get(ser), dict) else None
        if base is None:
            base = anchor(block, ser, probs, now)
            if base is None:
                if replaced(block, ser):
                    out.gone.append(ser)
                continue
        elif replaced(block, ser):
            out.gone.append(ser)
            continue
        why = worse(base, block, ser, probs)
        if why:
            out.worse[ser] = why
            continue
        out.state[ser] = base
        out.hidden[ser] = []
    for p in probs:
        k = p[1]
        ser = k.split(":", 1)[1] if k and ":" in k else ""
        if ser in out.hidden and not k.startswith(("raid:", "raidf:")):
            out.hidden[ser].append(p)
        else:
            out.kept.append(p)
    return out


def hidden_text(probs) -> str:
    """Что у заглушенного диска сейчас (для "есть ли проблема" рядом с заглушкой)."""
    rows = [t for _l, _k, t in probs if t]
    return "; ".join(rows[:2])[:300]


def base_text(base: dict) -> str:
    """Что заглушили, словами: "ошибок носителя 8"."""
    c = base.get("c") or {}
    parts = [_IOERR_BASE if k == "ioerr" else f"{_COUNTER_TEXT[k]} {_int(c.get(k))}" for k in COUNTERS if _int(c.get(k))]
    for k in base.get("keys") or []:
        kind = str(k).split(":", 1)[0]
        if kind in _SIGNS and kind != "media":
            parts.append(_KIND_TEXT[kind])
    return ", ".join(parts)


def view(s) -> dict[str, dict]:
    """Для ServerOut.drive_mutes: заглушенные диски с базовой линией {серийник: {dev, at, c, spare, crit,
    keys}}. Заглушку уже поставили, а сборщик еще не записал базу - {} у серийника: фронт судит по диску."""
    st = (s.alert_state or {}).get(STATE)
    st = st if isinstance(st, dict) else {}
    return {ser: {k: v for k, v in (st.get(ser) or {}).items() if k != "arrays"}
            for ser in serials(s.alert_mutes)}
