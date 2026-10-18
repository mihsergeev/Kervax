"""GPU NVIDIA (helper gpu-setup) и LLM-сервисы (агент 2.36).

Helper раз в минуту отдает extras["gpu"]: что видит nvidia-smi (загрузка, память, температура,
питание, частоты, ECC, причины замедления, процессы с их контейнером, подом или юнитом), ошибки
Xid драйвера из журнала ядра за сутки (helper 0.3 - и более старые с загрузки драйвера, по одной на
карту и код) и карты NVIDIA на шине PCI с привязанным драйвером. Агент
2.36 сам отдает карты на шине (gpu_pci, по ним панель просит helper) и метрики vLLM, SGLang и TEI
за последние 5 минут (llm): очередь, KV-кэш, запросы и токены в секунду, перцентили задержек.

Что алертит: драйвер не отвечает (после обновления ядра без пересборки модуля - классика), карта
пропала с шины, ошибки Xid, после которых карте нужен сброс или замена, перегрев с замедлением,
LLM-сервис не отвечает, стоит очередь при полном KV-кэше, задержки выросли в разы против обычного.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

STALE = 600  # блок helper'а старше 10 минут: nvidia-smi завис или таймер не идет
XID_HOLD = 3600  # ошибка Xid держится проблемой час: за это время ее разберут или она повторится
DAY = 86400
HEAT_MARGIN = 5  # предупреждение за 5 градусов до замедления, если карта не говорит свой предел
FROZEN_S = 3600  # показания nvidia-smi не меняются час (helper 0.2): им нельзя верить
LLM_BOOT = 900  # модель грузится минутами: молчание /metrics первые 15 минут процесса - не авария
QUEUE_MIN = 5  # очередь меньше - обычная пачка запросов
KV_FULL = 95.0
TTFT_MIN, TTFT_X = 2.0, 3.0  # TTFT p95 от 2 с и втрое выше обычного
TPOT_MIN, TPOT_X = 0.15, 2.0  # на токен от 150 мс и вдвое выше обычного
BASE_HOURS = 6  # обычное значение - медиана часовых медиан, нужно хотя бы 6 часов истории
BASE_KEEP = 24

# Xid, после которых карте нужен сброс, перезагрузка или замена. Остальные (13, 31, 43, 45, 69) -
# ошибки приложений: упала программа, а не карта; они видны в карточке, но не алертят.
XID_HW = {
    48: "двойная ошибка ECC памяти: нужен сброс GPU или перезагрузка",
    61: "внутренняя ошибка микрокода: нужен сброс GPU",
    62: "внутренняя ошибка микрокода: нужен сброс GPU",
    63: "ошибка ECC, сбойная память ждет исключения: нужен сброс GPU",
    64: "не удалось исключить сбойную память: карту под замену",
    74: "ошибка NVLink",
    79: "GPU отвалилась от шины",
    92: "много исправимых ошибок ECC",
    94: "ошибка ECC памяти, приложение на карте остановлено",
    95: "неисправимая ошибка ECC памяти: нужен сброс GPU",
    119: "GSP не отвечает: GPU зависла, нужен сброс или перезагрузка",
    120: "ошибка GSP: нужен сброс GPU или перезагрузка",
    140: "неисправимая ошибка ECC: нужен сброс GPU",
    154: "драйвер требует восстановления GPU",
}
XID_APP = {
    13: "ошибка приложения (graphics engine exception)",
    31: "приложение обратилось к чужой памяти (MMU fault)",
    43: "приложение остановлено драйвером",
    45: "приложение снято драйвером",
    69: "ошибка графического движка",
}
XID_WARN = {92, 94}  # карта работает дальше - предупреждение
# После этих Xid карта ждет сброса: проблема держится до перезагрузки сервера или драйвера, а не час
# (Xid 62 месяц назад - карта так и висит). 63 и 64 тут нет: сбойную память, которая ждет сброса,
# helper видит сам (remap/retire), и это проходит после сброса
XID_RESET = {48, 61, 62, 74, 79, 95, 119, 120, 140, 154}
ENGINE = {"vllm": "vLLM", "sglang": "SGLang", "tei": "TEI", "litellm": "LiteLLM"}
# /metrics просит ключ: так настроено, а не сломалось (у LiteLLM require_auth_for_metrics_endpoint)
LOCKED = ("http 401", "http 403")
_KIND = {"container": "контейнер", "pod": "под", "unit": "юнит"}
_ERR = {
    "refused": "порт закрыт",
    "timeout": "не ответил за 3 секунды",
    "local": "слушает только localhost внутри контейнера",
    "noaddr": "не найден адрес контейнера",
    "net": "ошибка сети",
    "read": "ответ оборвался",
}


def _f(v) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    f = _f(v)
    return None if f is None else int(round(f))


def bus_key(bus: str) -> str:
    """Шина:устройство без домена и функции: у nvidia-smi 00000000:41:00.0, в sysfs 0000:41:00.0,
    в Xid PCI:0000:41:00 - все это "41:00"."""
    parts = str(bus or "").lower().split(":")
    if len(parts) < 2:
        return str(bus or "").lower()
    return parts[-2] + ":" + parts[-1].split(".")[0]


def block(rep: dict) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("gpu")
    if not isinstance(b, dict) or b.get("v") != 1:
        return None
    return b


def age(b: dict, now: datetime, rep: dict | None = None) -> float:
    """Возраст блока по часам ноды (clock_unix отчета): у ноды с уехавшими часами иначе блок
    казался бы старым или свежим не по делу"""
    try:
        ref = float((rep or {}).get("clock_unix") or 0) or now.timestamp()
        return ref - float(b.get("ts") or 0)
    except (TypeError, ValueError):
        return 1e9


def pci_list(rep: dict) -> list[dict]:
    """Карты NVIDIA на шине: из helper'а, без него - из отчета агента 2.36"""
    b = block(rep)
    src = (b or {}).get("pci") if b else None
    if not isinstance(src, list):
        src = (rep or {}).get("gpu_pci")
    return [p for p in (src or []) if isinstance(p, dict) and p.get("bus")]


def has_gpu(rep: dict) -> bool:
    b = block(rep)
    return bool(pci_list(rep)) or bool(b and b.get("gpus"))


def _gpus(b: dict) -> list[dict]:
    return [g for g in (b.get("gpus") or []) if isinstance(g, dict)]


def frozen(g: dict) -> int:
    """Сколько секунд показания карты не меняются ни на сотую ватта (0 - живые или не знаем)"""
    s = _i(g.get("frz")) or 0
    return s if s >= FROZEN_S else 0


def _gpu_of_bus(b: dict) -> dict[str, int]:
    return {bus_key(g.get("bus") or ""): _i(g.get("i")) or 0 for g in _gpus(b)}


def short_model(name: str) -> str:
    s = str(name or "")
    for p in ("NVIDIA ", "GeForce ", "Tesla "):
        s = s.replace(p, "")
    return s.strip()


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _req(n: float) -> str:
    n = round(n)
    return f"{n} {_plural(n, 'запрос', 'запроса', 'запросов')}"


def _dur(sec: float) -> str:
    sec = int(sec)
    if sec >= 86400:
        return f"{sec // 86400} сут"
    if sec >= 3600:
        return f"{sec // 3600} ч"
    return f"{max(sec // 60, 1)} мин"


def _drv_cause(b: dict) -> str:
    kernel = str(b.get("kernel") or "")
    if b.get("mod") is False and b.get("mod_built") is False:
        return f": модуля nvidia нет для ядра {kernel} - ядро обновилось, а драйвер под него не собран (DKMS)"
    if b.get("mod") is False:
        return ": модуль ядра nvidia не загружен"
    why = str(b.get("nvrm") or b.get("err") or "")[:160]
    return f" ({why})" if why else ""


def _xids(b: dict, now: datetime, hold: float | None = None) -> list[dict]:
    """Xid за сутки (или за hold секунд), свежие первыми; одна строка на карту и код - последняя"""
    seen: dict[tuple[str, int], dict] = {}
    for x in b.get("xid") or []:
        if not isinstance(x, dict):
            continue
        code, ts = _i(x.get("code")), _f(x.get("ts"))
        if code is None or ts is None:
            continue
        if hold is not None and now.timestamp() - ts > hold:
            continue
        k = (bus_key(x.get("bus") or ""), code)
        if k not in seen or ts > seen[k]["ts"]:
            seen[k] = {"ts": ts, "bus": str(x.get("bus") or ""), "code": code, "text": str(x.get("text") or "")[:200]}
    return sorted(seen.values(), key=lambda x: -x["ts"])


def _load(b: dict) -> float:
    """Когда загружен драйвер (helper 0.3): ошибки до этого ушли вместе со старым драйвером"""
    return _f(b.get("nv_load")) or 0.0


def _waits_reset(x: dict, b: dict) -> bool:
    """Карта после этой ошибки ждет сброса: драйвер с тех пор не перезагружали. Xid 154 говорит сам,
    что нужно: "... to 0x0 (None)" - восстанавливать больше нечего"""
    if x["code"] not in XID_RESET or x["ts"] < _load(b):
        return False
    return not (x["code"] == 154 and re.search(r"\bto 0x0\b", x["text"]))


def problems(rep: dict, now: datetime) -> list[tuple[int, str, str, str | None]]:
    """(уровень, текст, ключ, с какого момента) для алерта "gpu": драйвер, пропавшая карта, Xid,
    сбойная память. Без helper'а судить не о чем: его отсутствие флагает "Требует действий"."""
    b = block(rep)
    if b is None:
        return []
    a = age(b, now, rep)
    if a > STALE:
        return [(1, f"данные о GPU не обновлялись {_dur(a)}: helper gpu-setup не отрабатывает "
                    "(nvidia-smi мог зависнуть на неисправной карте)", "gpu:stale", None)]
    if not b.get("smi"):
        return []  # драйвер не установлен: карта не используется, это видно в карточке
    if not b.get("ok"):
        return [(2, f"GPU недоступны: драйвер NVIDIA не отвечает{_drv_cause(b)}", "gpu:drv", None)]
    out: list[tuple[int, str, str, str | None]] = []
    seen = _gpu_of_bus(b)
    xids = _xids(b, now)
    lost: set[str] = set()  # у пропавшей карты ее Xid 79 уже в строке "пропала", второй строкой не повторяем
    for p in pci_list(rep):
        drv = str(p.get("drv") or "")
        k = bus_key(p["bus"])
        if drv not in ("nvidia", "") or k in seen:
            continue  # vfio-pci - отдана в виртуальную машину, nouveau - не наш драйвер
        txt = f"GPU {p['bus']} пропала: на шине PCI она есть, драйвер NVIDIA ее не видит"
        x79 = next((x for x in xids if x["code"] == 79 and bus_key(x["bus"]) == k), None)
        if x79:
            txt += " (Xid 79: отвалилась от шины)"
        lost.add(k)
        out.append((2, txt, f"gpu:lost:{k}", datetime.fromtimestamp(x79["ts"], timezone.utc).isoformat() if x79 else None))
    for g in _gpus(b):
        i = _i(g.get("i"))
        if g.get("remap_fail"):
            out.append((2, f"GPU {i}: не удалось исключить сбойную память - карту под замену", f"gpu:rf:{i}", None))
        elif g.get("remap") or g.get("retire"):
            out.append((1, f"GPU {i}: сбойная память ждет исключения - нужен сброс GPU или перезагрузка",
                        f"gpu:remap:{i}", None))
        fz = frozen(g)
        if fz:
            out.append((1, f"GPU {i}: показания nvidia-smi не меняются {_dur(fz)} - загрузка, питание и "
                           "температура застыли, перегрев по ним не увидеть", f"gpu:frozen:{i}",
                        datetime.fromtimestamp(now.timestamp() - fz, timezone.utc).isoformat()))
        eu = _i(g.get("ecc_u"))
        if eu:
            out.append((1, f"GPU {i}: неисправимых ошибок ECC с загрузки драйвера - {eu}", f"gpu:ecc:{i}", None))
    for x in xids:
        if x["code"] not in XID_HW or bus_key(x["bus"]) in lost or x["ts"] < _load(b):
            continue
        if now.timestamp() - x["ts"] > XID_HOLD and not _waits_reset(x, b):
            continue
        gi = seen.get(bus_key(x["bus"]))
        who = f"GPU {gi}" if gi is not None else f"GPU {x['bus']}"
        since = datetime.fromtimestamp(x["ts"], timezone.utc).isoformat()
        out.append((1 if x["code"] in XID_WARN else 2, f"{who}: ошибка Xid {x['code']} - {XID_HW[x['code']]}",
                    f"gpu:xid:{bus_key(x['bus'])}:{x['code']}:{int(x['ts'])}", since))
    out.sort(key=lambda p: -p[0])
    return out


def heat(rep: dict, now: datetime) -> tuple[int, str]:
    """Перегрев по самой горячей карте: замедление из-за температуры или температура дошла до
    порога замедления - проблема, за HEAT_MARGIN до него (или выше рабочего предела) - предупреждение"""
    b = block(rep)
    if b is None or not b.get("ok") or age(b, now, rep) > STALE:
        return 0, ""
    worst = (0, "", -1.0)
    for g in _gpus(b):
        if frozen(g):
            continue
        t = _f(g.get("temp"))
        if t is None:
            continue
        i = _i(g.get("i"))
        slow = _f(g.get("slow")) or 90.0
        maxop = _f(g.get("maxop"))
        warn = maxop if maxop and maxop < slow else slow - HEAT_MARGIN
        thr = g.get("thr") if isinstance(g.get("thr"), dict) else {}
        clk, clkm = _i(g.get("clk")), _i(g.get("clkm"))
        lvl, txt = 0, ""
        if thr.get("hw") or thr.get("sw") or t >= slow:
            lvl = 2
            txt = f"GPU {i} перегрев {round(t)}°C"
            if clk and clkm and clk < clkm:
                txt += f": частота снижена до {clk} из {clkm} МГц"
            else:
                txt += f" (замедление с {round(slow)}°C)"
        elif t >= warn:
            lvl, txt = 1, f"GPU {i} греется: {round(t)}°C, замедление начнется с {round(slow)}°C"
        if (lvl, t) > (worst[0], worst[2]):
            worst = (lvl, txt, t)
    return worst[0], worst[1]


def summary(rep: dict, now: datetime) -> dict | None:
    """Плашка в списке серверов: сколько карт, какие, средняя загрузка и память"""
    b = block(rep)
    pci = pci_list(rep)
    gs = _gpus(b) if b and b.get("ok") and age(b, now, rep) <= STALE else []
    if not gs and not pci:
        return None
    models: list[str] = []
    for g in gs:
        m = short_model(g.get("name") or "")
        if m and m not in models:
            models.append(m)
    util = [u for u in (_f(g.get("util")) for g in gs if not frozen(g)) if u is not None]
    mem = sum(_f(g.get("mem")) or 0 for g in gs)
    memt = sum(_f(g.get("memt")) or 0 for g in gs)
    return {
        "n": len(gs) or len([p for p in pci if p.get("drv") != "vfio-pci"]),
        "models": models,
        "util": round(sum(util) / len(util)) if util else None,
        "mem": round(mem / 1024, 1) if gs else None,  # ГиБ
        "memt": round(memt / 1024, 1) if gs else None,
        "helper": b is not None,
        "ok": bool(b and b.get("ok")),
        "frozen": sum(1 for g in gs if frozen(g)),
    }


def view(rep: dict, now: datetime) -> dict | None:
    """Блок карточки: карты, процессы, Xid за сутки и те, после которых карта ждет сброса, карты на
    шине и драйвер"""
    b = block(rep)
    pci = pci_list(rep)
    if b is None and not pci:
        return None
    if b is None:
        return {"helper": False, "pci": [{"bus": p["bus"], "drv": str(p.get("drv") or "")} for p in pci]}
    gs = _gpus(b)
    seen = _gpu_of_bus(b)
    uuid_i = {str(g.get("uuid") or ""): _i(g.get("i")) for g in gs}
    gpus = []
    for g in gs:
        thr = g.get("thr") if isinstance(g.get("thr"), dict) else {}
        gpus.append({
            "i": _i(g.get("i")), "bus": str(g.get("bus") or ""), "name": short_model(g.get("name") or ""),
            "util": _f(g.get("util")), "mutil": _f(g.get("mutil")), "mem": _f(g.get("mem")), "memt": _f(g.get("memt")),
            "temp": _f(g.get("temp")), "pw": _f(g.get("pw")), "pwl": _f(g.get("pwl")),
            "clk": _i(g.get("clk")), "clkm": _i(g.get("clkm")), "ps": str(g.get("ps") or ""), "fan": _f(g.get("fan")),
            "pm": str(g.get("pm") or ""), "ecc_u": _i(g.get("ecc_u")), "ecc_c": _i(g.get("ecc_c")),
            "slow": _f(g.get("slow")), "maxop": _f(g.get("maxop")),
            "thermal": bool(thr.get("hw") or thr.get("sw")), "pcap": bool(thr.get("pcap")), "brake": bool(thr.get("brake")),
            "remap": bool(g.get("remap") or g.get("retire")), "remap_fail": bool(g.get("remap_fail")),
            "frozen": frozen(g),
        })
    procs = []
    for p in b.get("procs") or []:
        if not isinstance(p, dict):
            continue
        procs.append({"gpu": uuid_i.get(str(p.get("gpu") or "")), "pid": _i(p.get("pid")), "name": str(p.get("name") or ""),
                      "kind": str(p.get("kind") or ""), "owner": str(p.get("owner") or ""), "mem": _f(p.get("mem"))})
    procs.sort(key=lambda p: (-(p["mem"] or 0), p["gpu"] if p["gpu"] is not None else 99))
    xid = []
    for x in _xids(b, now):
        c, held, old = x["code"], _waits_reset(x, b), now.timestamp() - x["ts"] > DAY
        if old and not held:
            continue
        # hw - красная точка: аппаратная ошибка, после которой драйвер не перезагружали
        xid.append({**x, "gpu": seen.get(bus_key(x["bus"])), "hw": c in XID_HW and x["ts"] >= _load(b),
                    "held": held, "old": old, "desc": XID_HW.get(c) or XID_APP.get(c) or ""})
    ok = bool(b.get("ok"))
    return {
        "helper": True, "age": round(age(b, now, rep)), "stale": age(b, now, rep) > STALE,
        "smi": bool(b.get("smi")), "ok": ok, "err": str(b.get("err") or "")[:300],
        "cause": _drv_cause(b).lstrip(": ").strip(" ()") if b.get("smi") and not ok else "",
        "driver": str(b.get("driver") or ""), "cuda": str(b.get("cuda") or ""), "kernel": str(b.get("kernel") or ""),
        "pci": [{"bus": p["bus"], "drv": str(p.get("drv") or ""),
                 "lost": ok and str(p.get("drv") or "") in ("nvidia", "") and bus_key(p["bus"]) not in seen}
                for p in pci],
        "gpus": gpus, "procs": procs, "xid": xid,
    }


# --- LLM -------------------------------------------------------------------------------------

def llm_list(rep: dict) -> list[dict]:
    return [x for x in ((rep or {}).get("llm") or []) if isinstance(x, dict) and x.get("engine")]


def llm_key(x: dict) -> str:
    return f"{x.get('engine')}|{x.get('kind') or ''}|{x.get('name') or ''}|{x.get('port') or ''}"


def llm_label(x: dict) -> str:
    return f"{ENGINE.get(str(x.get('engine')), str(x.get('engine')))} {x.get('name') or ''}".strip()


def llm_where(x: dict) -> str:
    k = _KIND.get(str(x.get("kind") or ""))
    return f"{k} {x.get('name')}" if k else f"процесс {x.get('name')}"


def err_text(code: str) -> str:
    c = str(code or "")
    if c.startswith("http "):
        return f"ответ HTTP {c[5:]}"
    return _ERR.get(c, c)


def _p95(x: dict, m: str) -> float | None:
    v = x.get(m)
    return _f(v[1]) if isinstance(v, list) and len(v) >= 2 else None


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def base_update(st: dict | None, rep: dict, now: datetime) -> dict:
    """Обычные TTFT и TPOT каждого сервиса: p95 копится по часу (не чаще раза в минуту), час
    сворачивается в медиану, обычное - медиана последних BASE_KEEP часов"""
    st = {k: v for k, v in (st or {}).items() if isinstance(v, dict)}
    hour = int(now.timestamp() // 3600)
    minute = int(now.timestamp() // 60)
    for x in llm_list(rep):
        if not x.get("ok") or not x.get("win"):
            continue
        k = llm_key(x)
        e = dict(st.get(k) or {"h": hour, "cur": {}, "hist": {}})
        cur = {m: list(v) for m, v in (e.get("cur") or {}).items()}
        hist = {m: list(v) for m, v in (e.get("hist") or {}).items()}
        if e.get("h") != hour:
            for m, vals in cur.items():
                if vals:
                    hist[m] = (hist.get(m, []) + [round(_median(vals), 4)])[-BASE_KEEP:]
            cur = {}
        if e.get("t") != minute:
            for m in ("ttft", "tpot"):
                v = _p95(x, m)
                if v is not None:
                    cur[m] = (cur.get(m, []) + [v])[-60:]
        st[k] = {"h": hour, "t": minute, "cur": cur, "hist": hist}
    # сервис пропал двое суток назад - его история больше не нужна
    return {k: v for k, v in st.items() if hour - int(v.get("h") or 0) <= 48}


def baseline(st: dict | None, x: dict, m: str) -> float | None:
    hist = (((st or {}).get(llm_key(x)) or {}).get("hist") or {}).get(m) or []
    vals = [v for v in (_f(h) for h in hist) if v is not None]
    return round(_median(vals), 4) if len(vals) >= BASE_HOURS else None


def _sec(v: float) -> str:
    return f"{round(v * 1000)} мс" if v < 1 else f"{v:.1f} с"


def llm_problems(rep: dict, base: dict | None) -> list[tuple[int, str, str]]:
    """(уровень, текст, ключ) по LLM-сервисам; выдержку добавляют вызывающие"""
    out: list[tuple[int, str, str]] = []
    for x in llm_list(rep):
        label, k = llm_label(x), llm_key(x)
        if x.get("no_metrics"):
            continue
        if not x.get("ok"):
            err = str(x.get("err") or "")
            # local и noaddr - агенту некуда стучаться, ключ на /metrics - так настроено, сервис тут
            # ни при чем: это видно в карточке
            if err in ("local", "noaddr", *LOCKED) or (_f(x.get("up")) or 0) < LLM_BOOT:
                continue
            out.append((2, f"{label}: не отвечает на /metrics ({err_text(err)}), процесс работает {_dur(_f(x.get('up')) or 0)}",
                        f"llm:down:{k}"))
            continue
        run, wait, kv = _f(x.get("running")) or 0, _f(x.get("waiting")) or 0, _f(x.get("kv"))
        pre = _f(x.get("preempt")) or 0
        if wait >= QUEUE_MIN and wait >= run:
            out.append((1, f"{label}: в очереди {_req(wait)}, в работе {round(run)} - модель не успевает",
                        f"llm:queue:{k}"))
        if kv is not None and kv >= KV_FULL and (pre > 0 or wait > 0):
            why = f"за 5 минут вытеснено {_req(pre)}" if pre > 0 else f"ждут {round(wait)}"
            out.append((1, f"{label}: KV-кэш заполнен на {round(kv)}%, {why}", f"llm:kv:{k}"))
        for m, lo, mult, name in (("ttft", TTFT_MIN, TTFT_X, "до первого токена"), ("tpot", TPOT_MIN, TPOT_X, "на токен")):
            v, b = _p95(x, m), baseline(base, x, m)
            if v is not None and b and v >= lo and v >= b * mult:
                out.append((1, f"{label}: задержка {name} p95 {_sec(v)}, обычно {_sec(b)}", f"llm:{m}:{k}"))
    out.sort(key=lambda p: -p[0])
    return out


def pub_port(rep: dict, x: dict) -> int | None:
    """Порт на хосте, если сервис в контейнере docker и его порт проброшен: агент стучится во
    внутренний (у трех TEI он один - 8000), а люди и прокси знают проброшенный"""
    if x.get("kind") != "container":
        return None
    for c in ((rep.get("docker") or {}).get("containers") or []):
        if not isinstance(c, dict) or c.get("name") != x.get("name"):
            continue
        for p in c.get("ports") or []:
            if isinstance(p, dict) and p.get("priv") == x.get("port") and _i(p.get("pub")):
                return _i(p.get("pub"))
    return None


def llm_view(rep: dict, base: dict | None) -> list[dict]:
    out = []
    for x in llm_list(rep):
        out.append({
            **{k: x.get(k) for k in ("engine", "kind", "name", "model", "port", "up", "ok", "no_metrics", "win",
                                     "running", "waiting", "kv", "req_s", "prompt_tps", "gen_tps", "preempt",
                                     "errors", "aborted", "ttft", "tpot", "e2e")},
            "key": llm_key(x), "label": llm_label(x), "engine_name": ENGINE.get(str(x.get("engine")), str(x.get("engine"))),
            "where": llm_where(x), "err": err_text(str(x.get("err") or "")) if x.get("err") else "",
            "locked": not x.get("ok") and str(x.get("err") or "") in LOCKED,
            "pub_port": pub_port(rep, x),
            "base_ttft": baseline(base, x, "ttft"), "base_tpot": baseline(base, x, "tpot"),
        })
    return out


# --- точки для графиков (server_metrics раз в минуту) ---------------------------------------

def metric_gpu(extras: dict | None, now: datetime, clock: int = 0) -> list[dict] | None:
    """[{g, u, m, t, p}] по картам: загрузка, память и питание в %, температура"""
    b = block({"extras": extras or {}})
    if b is None or not b.get("ok") or age(b, now, {"clock_unix": clock}) > 180:
        return None
    out = []
    for g in _gpus(b):
        if frozen(g):
            continue  # застывшие показания на графике - ровная ложная линия, честнее пропуск
        memt, pwl = _f(g.get("memt")), _f(g.get("pwl"))
        mem, pw = _f(g.get("mem")), _f(g.get("pw"))
        out.append({
            "g": str(_i(g.get("i"))), "u": _f(g.get("util")),
            "m": round(mem / memt * 100, 1) if mem is not None and memt else None,
            "t": _f(g.get("temp")),
            "p": round(pw / pwl * 100, 1) if pw is not None and pwl else None,
        })
    return out or None


def metric_llm(llm: list[dict] | None) -> list[dict] | None:
    """[{k, run, wait, kv, gen, req, ttft, tpot, e2e}] по сервисам; задержки - p95"""
    out = []
    for x in llm or []:
        if not isinstance(x, dict) or not x.get("ok"):
            continue
        out.append({
            "k": llm_label(x), "run": _f(x.get("running")), "wait": _f(x.get("waiting")), "kv": _f(x.get("kv")),
            "gen": _f(x.get("gen_tps")), "req": _f(x.get("req_s")),
            "ttft": _p95(x, "ttft"), "tpot": _p95(x, "tpot"), "e2e": _p95(x, "e2e"),
        })
    return out or None
