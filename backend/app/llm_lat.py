"""Задержки LLM-сервисов выше обычного: всплески для раздела LLM и алерт только при устойчивой
деградации под нагрузкой.

Задержка до первого токена растет вместе с длиной промпта: пара запросов с длинным контекстом
дает p95 в разы выше обычного, а сервер в порядке, очереди нет. Если на такое слать алерт, человек
переходит в панель, а там уже пусто: окно агента - 5 минут, запросы кончились. Поэтому всплески
не алертят, а копятся здесь на сутки и видны в разделе LLM: когда, до скольких и сколько было
запросов.

Алерт - только когда задержка в разы выше обычного держится HOLD подряд и в каждом пятиминутном
окне не меньше MIN_REQ запросов: тогда это не пара случайных запросов, а сервис под нагрузкой
отвечает медленно. Сверху ложится общая выдержка алерта (15 минут по умолчанию).

Состояние - alert_state["llm_lat"]: {ключ сервиса: {"t": минута, "run": {метрика: начало серии
под нагрузкой}, "seen": когда видели, "ev": [всплески за сутки]}}.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import gpu

MIN_REQ = 30  # запросов за пятиминутное окно (0.1 в секунду): на меньшем числе p95 - пара запросов
HOLD = 900  # столько подряд под нагрузкой (плюс общая выдержка алерта) - уже не всплеск
GAP = 180  # пропуск до 3 минут не разрывает всплеск
KEEP = 86400  # всплески видны в разделе LLM сутки
MAX_EV = 20  # всплесков на сервис: больше не показать, да и не нужно
METRICS = (("ttft", gpu.TTFT_MIN, gpu.TTFT_X, "до первого токена"),
           ("tpot", gpu.TPOT_MIN, gpu.TPOT_X, "на токен"))


def _ts(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v)) if v else None
    except ValueError:
        return None


def requests(x: dict) -> int | None:
    """Сколько запросов завершилось за окно агента"""
    rs, win = gpu._f(x.get("req_s")), gpu._f(x.get("win"))
    return round(rs * win) if rs is not None and win else None


def slow(x: dict, base: dict | None, m: str, lo: float, mult: float) -> tuple[float, float] | None:
    """(p95, обычное), если задержка в разы выше обычного"""
    v, b = gpu._p95(x, m), gpu.baseline(base, x, m)
    return (v, b) if v is not None and b and v >= lo and v >= b * mult else None


def update(st: dict | None, rep: dict, base: dict | None, now: datetime) -> dict:
    """Раз в минуту: всплески задержек и серии "медленно под нагрузкой" по каждому сервису"""
    st = {k: v for k, v in (st or {}).items() if isinstance(v, dict)}
    minute = int(now.timestamp() // 60)
    for x in gpu.llm_list(rep):
        if not x.get("ok") or not x.get("win"):
            continue
        k = gpu.llm_key(x)
        e = st.get(k) or {}
        if e.get("t") == minute:
            continue
        run = dict(e.get("run") or {})
        evs = [dict(v) for v in (e.get("ev") or []) if isinstance(v, dict)]
        n, wait = requests(x), gpu._f(x.get("waiting")) or 0
        for m, lo, mult, _name in METRICS:
            hit = slow(x, base, m, lo, mult)
            if not hit:
                run.pop(m, None)
                continue
            v, b = hit
            if n is not None and n >= MIN_REQ:
                run.setdefault(m, now.isoformat())
            else:
                run.pop(m, None)
            last = next((ev for ev in reversed(evs) if ev.get("m") == m), None)
            end = _ts(last.get("to")) if last else None
            if last is None or end is None or (now - end).total_seconds() > GAP:
                last = {"m": m, "from": now.isoformat(), "peak": v, "at": now.isoformat(), "base": b,
                        "req": [n, n] if n is not None else None, "wait": wait}
                evs.append(last)
            last["to"] = now.isoformat()
            if v > (last.get("peak") or 0):
                last["peak"], last["at"] = v, now.isoformat()
            if n is not None:
                lo_n, hi_n = last.get("req") or [n, n]
                last["req"] = [min(lo_n, n), max(hi_n, n)]
            last["wait"] = max(last.get("wait") or 0, wait)
            if m in run and (now - (_ts(run[m]) or now)).total_seconds() >= HOLD:
                last["load"] = True  # держалось под нагрузкой: по нему алерт
        evs = [ev for ev in evs if (now - (_ts(ev.get("to")) or now)).total_seconds() <= KEEP][-MAX_EV:]
        st[k] = {"t": minute, "seen": now.isoformat(), "run": run, "ev": evs}
    # сервис пропал больше суток назад - его всплески уже не показать
    return {k: v for k, v in st.items()
            if (now - (_ts(v.get("seen")) or now)).total_seconds() <= KEEP}


def _dur(s: float) -> str:
    m = int(s // 60)
    return f"{m} мин" if m < 60 else f"{m // 60} ч {m % 60} мин" if m % 60 else f"{m // 60} ч"


def problems(rep: dict, base: dict | None, st: dict | None, now: datetime) -> list[tuple[int, str, str]]:
    """Задержка в разы выше обычного держится HOLD подряд при MIN_REQ запросов в каждом окне"""
    out = []
    for x in gpu.llm_list(rep):
        if not x.get("ok"):
            continue
        e = (st or {}).get(gpu.llm_key(x)) or {}
        n = requests(x)
        for m, lo, mult, name in METRICS:
            since = _ts((e.get("run") or {}).get(m))
            hit = slow(x, base, m, lo, mult)
            if not since or not hit or n is None or n < MIN_REQ:
                continue
            held = (now - since).total_seconds()
            if held < HOLD:
                continue
            v, b = hit
            out.append((1, f"{gpu.llm_label(x)}: задержка {name} p95 {gpu._sec(v)}, обычно {gpu._sec(b)}, "
                           f"держится {_dur(held)} при {n} запросах за 5 минут",
                        f"llm:{m}:{gpu.llm_key(x)}"))
    return out


def recent(st: dict | None, x: dict, now: datetime) -> list[dict]:
    """Всплески сервиса за сутки для раздела LLM, свежие первыми"""
    evs = ((st or {}).get(gpu.llm_key(x)) or {}).get("ev") or []
    cut = now - timedelta(seconds=KEEP)
    out = []
    for ev in evs:
        if not isinstance(ev, dict) or (_ts(ev.get("to")) or cut) < cut:
            continue
        out.append({k: ev.get(k) for k in ("m", "from", "to", "peak", "at", "base", "req", "wait")}
                   | {"load": bool(ev.get("load"))})
    return out[::-1]
