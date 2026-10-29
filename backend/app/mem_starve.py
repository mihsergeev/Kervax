"""Сервер задыхается без памяти: свопится и встает, а OOM-killer молчит.

Повод (29.10.2026): на сервере с 23гб памяти две параллельные сборки Gradle в контейнерах без
лимита подняли committed memory с 24 до 41гб. Swap 2.8гб был забит еще до них, поэтому своп почти
стоял (40 страниц в секунду), а память все равно кончилась: ядро выкидывало код и кэш процессов
и тут же читало их с диска снова (major faults ~1000/с, чтение 125мб/с). OOM-killer не сработал:
формально свободное было. Через семь минут после старта сборок цикл агента завис и сервер стоял
десять минут (load 316, dockerd не отвечал). Алерт давления ждал общую выдержку 15 минут и не
успел, а "не на связи" пришел без слова о памяти.

Здесь:
- signs(rep): признаки из отчета - сколько памяти осталось (MemAvailable), swap, давление памяти
  (PSI), подкачка, major faults (агент 2.58), load
- starving(rep, going): память кончилась на всем сервере, а не у лимита одного контейнера, и
  процессы из-за этого стоят. Идет эпизод (going) - пороги ниже, чтобы край не мигал
- text/short_facts: строка алерта "Сервер задыхается" и факты для чата
- snapshot/before_*: что было в последнем отчете перед пропажей агента; back_*: причина в отбое
  "снова на связи" - была ли нехватка памяти, убивал ли OOM-killer, кто ел память
"""
from __future__ import annotations

import re
from datetime import datetime

from app import tg_fmt

# Память кончилась на всем сервере: осталось (MemAvailable) не больше стольких процентов. Без
# этого давление памяти бывает и у одного контейнера, уперевшегося в свой лимит (app/mem_limit.py)
_AVAIL_MAX = 10.0
_AVAIL_KEEP = 15.0
# И процессы стоят: давление памяти (PSI) или страницы читаются с диска. Порог full avg10 выше
# avg60: десять секунд скачут сильнее
_FULL10, _FULL60, _SOME60 = 10.0, 5.0, 20.0
_MAJFLT = 500.0
# в эпизоде - до спада заметно ниже, чтобы 9-11% свободного не рвали выдержку на каждом отчете
_FULL60_KEEP, _SOME60_KEEP, _MAJFLT_KEEP = 2.0, 10.0, 200.0
# Выдержка алерта: короткий всплеск (сборка на минуту-две съела память и сама отпустила) - не
# повод, на одном сервере такой был 28.10: 99% памяти и подкачка 40мб/с две минуты, и все. Сервер
# со сборками Gradle встал через полторы минуты после 94% памяти - его ловит не эта выдержка, а
# пропажа агента с причиной (snapshot) и быстрый "не на связи" (collector.offline_extra)
HOLD_S = 180
# Перед пропажей "что-то было не так" - тогда в алерте последние метрики, иначе молчим о них:
# обрыв сети на здоровом сервере с "память 40%, load 0.3" читать незачем
_ODD_AVAIL, _ODD_SWAP, _ODD_SOME, _ODD_FULL, _ODD_IO_FULL, _ODD_CPU = 15.0, 90.0, 10.0, 5.0, 30.0, 95.0
_ODD_LOAD_PER_CORE = 4.0
# ни памяти, ни swap: свободно не больше стольких процентов при забитом swap (или без него)
_EDGE_AVAIL = 5.0
# сколько назвать тех, кто ел память
_TOP = 3


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def signs(rep: dict | None) -> dict | None:
    """Признаки нехватки памяти из отчета. None - отчета нет или в нем нет памяти."""
    rep = rep if isinstance(rep, dict) else {}
    total = _f(rep.get("mem_total")) or 0
    if total <= 0:
        return None
    used = _f(rep.get("mem_used")) or 0
    psi = rep.get("psi") if isinstance(rep.get("psi"), dict) else {}
    st, su = _f(rep.get("swap_total")) or 0, _f(rep.get("swap_used")) or 0
    load = rep.get("load") if isinstance(rep.get("load"), list) else []
    return {
        "total": total,
        "mem": round(min(max(used / total * 100, 0), 100), 1),
        "avail": round(min(max(100 - used / total * 100, 0), 100), 1),
        "swap": round(su / st * 100) if st > 0 else None,
        "swio": (_f(rep.get("swap_in")) or 0) + (_f(rep.get("swap_out")) or 0),
        "majflt": _f(rep.get("majflt")),
        "full10": _f(psi.get("mem_full_10")) or 0, "full60": _f(psi.get("mem_full_60")) or 0,
        "some10": _f(psi.get("mem_some_10")) or 0, "some60": _f(psi.get("mem_some_60")) or 0,
        "io_full60": _f(psi.get("io_full_60")) or 0,
        "psi": bool(psi),
        "load": _f(load[0]) if load else None,
        "cores": int(_f(rep.get("cpu_cores")) or 0),
        "cpu": _f(rep.get("cpu_percent")),
    }


def _stalls(sg: dict, going: bool) -> bool:
    mf = sg["majflt"] or 0
    if going:
        return sg["full60"] >= _FULL60_KEEP or sg["some60"] >= _SOME60_KEEP or mf >= _MAJFLT_KEEP
    return (sg["full10"] >= _FULL10 or sg["full60"] >= _FULL60 or sg["some60"] >= _SOME60
            or mf >= _MAJFLT)


def starving(rep: dict | None, going: bool = False) -> bool:
    """Память кончилась на всем сервере и процессы из-за этого стоят. going - эпизод уже идет."""
    sg = signs(rep)
    return bool(sg) and sg["avail"] <= (_AVAIL_KEEP if going else _AVAIL_MAX) and _stalls(sg, going)


def on_edge(sg: dict | None, going: bool = False) -> bool:
    """Агент замолчал, а в последнем отчете памяти не было: стояли процессы (пороги эпизода - PSI
    только начал расти) или не осталось ни памяти, ни свободного swap. Тогда молчание - почти
    наверняка зависший без памяти сервер"""
    if not sg:
        return False
    if sg["avail"] <= (_AVAIL_KEEP if going else _AVAIL_MAX) and _stalls(sg, True):
        return True
    return sg["avail"] <= _EDGE_AVAIL and (sg["swap"] is None or sg["swap"] >= _ODD_SWAP)


def _psi_words(sg: dict) -> str:
    full = max(sg["full10"], sg["full60"])
    if full >= 1:
        return f"все процессы разом стоят {full:.0f}% времени"
    some = max(sg["some10"], sg["some60"])
    return f"процессы ждут память {some:.0f}% времени" if some >= 1 else ""


def _swap_words(sg: dict) -> str:
    if sg["swap"] is None:
        return "swap нет"
    return f"swap занят на {sg['swap']}%"


def text(rep: dict) -> str:
    """Строка алерта "Сервер задыхается": "кончилась память: свободно 3% из 23гб, swap занят на
    100%, все процессы разом стоят 40% времени, с диска читается 1034 страниц/с"."""
    sg = signs(rep) or {}
    parts = [f"свободно {tg_fmt.pct(sg.get('avail'))} из {tg_fmt.size(sg.get('total'))}", _swap_words(sg)]
    parts.append(_psi_words(sg))
    if (sg.get("majflt") or 0) >= _MAJFLT_KEEP:
        parts.append(f"с диска читается {sg['majflt']:.0f} страниц/с")
    if (sg.get("swio") or 0) >= 1024 ** 2:
        parts.append(f"подкачка {tg_fmt.size(sg['swio'])}/с")
    return "кончилась память, сервер может встать: " + ", ".join(p for p in parts if p)


def what(rep: dict) -> str:
    """Что случилось, для чата"""
    sg = signs(rep) or {}
    return "Кончилась память, сервер свопится" if sg.get("swap") is not None else "Кончилась память"


def numbers(sg: dict, psi_name: bool = True) -> str:
    """"свободно 3%, swap 100%, PSI full 40%" - коротко, для чата"""
    parts = [f"свободно {tg_fmt.pct(sg['avail'])}"]
    if sg["swap"] is not None:
        parts.append(f"swap {sg['swap']}%")
    full = max(sg["full10"], sg["full60"])
    if sg["psi"] and full >= 1:
        parts.append(f"PSI full {full:.0f}%" if psi_name else f"стоят {full:.0f}% времени")
    return ", ".join(parts)


# --- кто ел память ---

def _proc_label(p: dict) -> str:
    """Процесс словами: у java по командной строке видно, что это (сборка Gradle, jar)."""
    comm = tg_fmt.field(p.get("comm") or "?", 40)
    cmd = str(p.get("cmdline") or "")
    if comm == "java" and cmd:
        if "GradleDaemon" in cmd or "gradle" in cmd.lower():
            return "java (Gradle)"
        m = re.search(r"-jar\s+(\S+\.jar)", cmd)
        if m:
            return f"java ({tg_fmt.field(m.group(1).rsplit('/', 1)[-1], 40)})"
    return comm


def eaters(rep: dict | None) -> list[tuple[str, float]]:
    """Кто ел память: [(кто, байт)], больше всех первыми. Агент 2.58 подписывает процессы топа
    владельцем - тогда "контейнер X (java)"; старые - контейнеры и поды по их памяти, без них -
    процессы топа одноименные вместе."""
    rep = rep if isinstance(rep, dict) else {}
    top = [p for p in rep.get("top_mem") or [] if isinstance(p, dict)]
    by: dict[str, float] = {}
    comm_of: dict[str, set] = {}
    if any("kind" in p for p in top):
        for p in top:
            priv = max((_f(p.get("rss")) or 0) - (_f(p.get("shared")) or 0), 0.0)
            kind, owner = str(p.get("kind") or ""), tg_fmt.field(p.get("owner") or "", 60)
            key = (f"контейнер {owner}" if kind == "container" else f"под {owner}" if kind == "pod"
                   else owner if kind == "unit" else _proc_label(p))
            by[key] = by.get(key, 0.0) + priv
            if kind in ("container", "pod"):
                comm_of.setdefault(key, set()).add(_proc_label(p))
    else:
        for c in ((rep.get("docker") or {}).get("containers") or []):
            if isinstance(c, dict) and _f(c.get("mem")):
                by[f"контейнер {tg_fmt.field(c.get('name') or '?', 60)}"] = _f(c.get("mem")) or 0
        for p in ((rep.get("kube") or {}).get("pods") or []):
            if isinstance(p, dict) and _f(p.get("mem")):
                by[f"под {tg_fmt.field(p.get('ns') or '?', 40)}/{tg_fmt.field(p.get('name') or '?', 60)}"] = \
                    _f(p.get("mem")) or 0
        if not by:
            for p in top:
                key = _proc_label(p)
                by[key] = by.get(key, 0.0) + max((_f(p.get("rss")) or 0) - (_f(p.get("shared")) or 0), 0.0)
    out = []
    for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:_TOP]:
        if v <= 0:
            continue
        cs = sorted(comm_of.get(k) or [])
        out.append((f"{k} ({', '.join(cs[:2])})" if len(cs) else k, v))
    return out


def eaters_text(items: list) -> str:
    return ", ".join(f"{k} {tg_fmt.size(v)}" for k, v in items)


# --- агент замолчал: что было перед этим ---

def _odd(sg: dict) -> bool:
    """В последнем отчете что-то было не так: есть смысл показать его цифры"""
    load_bad = sg["load"] is not None and sg["cores"] > 0 and sg["load"] >= sg["cores"] * _ODD_LOAD_PER_CORE
    return (sg["avail"] <= _ODD_AVAIL or (sg["swap"] or 0) >= _ODD_SWAP and sg["swio"] >= 1024 ** 2
            or sg["some60"] >= _ODD_SOME or max(sg["full10"], sg["full60"]) >= _ODD_FULL
            or sg["io_full60"] >= _ODD_IO_FULL or (sg["cpu"] or 0) >= _ODD_CPU or load_bad)


def snapshot(rep: dict | None, seen_at: datetime | None, going: bool = False) -> dict | None:
    """Снимок последнего отчета перед пропажей агента, если в нем было что-то не так. Хранится в
    alert_state["offline_before"] до отбоя: к нему last_report уже перепишет новый отчет."""
    sg = signs(rep)
    if not sg or not _odd(sg):
        return None
    rep = rep or {}
    return {
        "ts": seen_at.isoformat() if seen_at else None,
        "starving": on_edge(sg, going),
        "mem": sg["mem"], "avail": sg["avail"], "swap": sg["swap"], "swio": sg["swio"],
        "full": max(sg["full10"], sg["full60"]), "some": max(sg["some10"], sg["some60"]),
        "io_full": sg["io_full60"], "load": sg["load"], "cores": sg["cores"], "cpu": sg["cpu"],
        "majflt": sg["majflt"], "psi": sg["psi"],
        "oom": rep.get("oom_total"), "up": rep.get("uptime_seconds"),
        "top": [[k, v] for k, v in eaters(rep)],
    }


def _snap_numbers(sn: dict) -> str:
    """"память 96%, swap 100%, PSI full 40%, load 13 на 6 ядрах" - что было в последнем отчете"""
    parts = []
    if (sn.get("cpu") or 0) >= _ODD_CPU:
        parts.append(f"CPU {sn['cpu']:.0f}%")
    parts.append(f"память {tg_fmt.pct(sn.get('mem'))}")
    if sn.get("swap") is not None:
        parts.append(f"swap {sn['swap']}%")
    if sn.get("psi") and (sn.get("full") or 0) >= 1:
        parts.append(f"PSI full {sn['full']:.0f}%")
    elif sn.get("psi") and (sn.get("some") or 0) >= 1:
        parts.append(f"PSI some {sn['some']:.0f}%")
    if (sn.get("io_full") or 0) >= _ODD_IO_FULL:
        parts.append(f"диск ждут {sn['io_full']:.0f}% времени")
    if sn.get("load") is not None:
        parts.append(f"load {sn['load']:.0f}" + (f" на {sn['cores']} ядрах" if sn.get("cores") else ""))
    return ", ".join(parts)


def before_full(sn: dict | None, at: str = "") -> str:
    """Строка в полный текст "не на связи": "перед пропажей (11:46) кончалась память: ... -
    похоже, сервер встал без памяти. Больше всех ели: ..." at - время последнего отчета"""
    if not sn:
        return ""
    head = f"перед пропажей{f' ({at})' if at else ''}"
    top = eaters_text(sn.get("top") or [])
    if sn.get("starving"):
        out = f"{head} кончалась память: {_snap_numbers(sn)} - похоже, сервер встал без памяти"
        return out + (f". Больше всех ели: {top}" if top else "")
    return f"{head}: {_snap_numbers(sn)}"


def before_short(sn: dict | None) -> str:
    """То же одной строкой для чата"""
    if not sn:
        return ""
    if sn.get("starving"):
        return f"Перед этим кончилась память: {_snap_numbers(sn)}"
    return f"Перед этим: {_snap_numbers(sn)}"


# --- вернулся: что это было ---

def _oom_after(sn: dict, rep: dict) -> int | None:
    """Сколько процессов убил OOM-killer, пока агент молчал. None - не узнать (перезагрузка, старый агент)."""
    was, now_ = sn.get("oom"), rep.get("oom_total")
    up_was, up_now = _f(sn.get("up")), _f(rep.get("uptime_seconds"))
    if was is None or now_ is None or (up_was is not None and up_now is not None and up_now < up_was):
        return None
    try:
        return max(int(now_) - int(was), 0)
    except (TypeError, ValueError):
        return None


def rebooted(sn: dict, rep: dict) -> bool:
    up_was, up_now = _f(sn.get("up")), _f(rep.get("uptime_seconds"))
    return up_was is not None and up_now is not None and up_now < up_was


def back_lines(sn: dict | None, rep: dict | None) -> list[str]:
    """Причина к отбою "снова на связи", строками для чата: что было перед пропажей, убивал ли
    OOM-killer и кто ел память. Пусто - перед пропажей все было в порядке."""
    if not sn:
        return []
    rep = rep if isinstance(rep, dict) else {}
    if not sn.get("starving"):
        return [f"Перед пропажей: {_snap_numbers(sn)}"]
    if rebooted(sn, rep):
        tail = "сервер перезагрузился"
    else:
        n = _oom_after(sn, rep)
        tail = ("OOM не было, память отпустило само" if n == 0
                else f"OOM-killer убил {n} {_procs(n)}" if n else "")
    lines = ["Кончилась память, сервер встал в свопе" + (f", {tail}" if tail else "")]
    top = eaters_text(sn.get("top") or [])
    if top:
        lines.append(f"Ели память: {top}")
    return lines


def _procs(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "процесс"
    return "процесса" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "процессов"


def before_home(sn) -> str:
    """Строка к "оффлайн" в "Что сломано": "перед этим кончилась память: свободно 4%, swap 100%" """
    return tg_fmt.low(before_short(sn)) if isinstance(sn, dict) else ""
