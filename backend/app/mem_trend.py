"""Утечка памяти: контейнер, под или процесс, чья память растет день за днем.

Как и прогноз заполнения диска, смотрим на суточные минимумы, а не на текущее значение: пила
сборщика мусора и разовые всплески дно не двигают, а утечка поднимает его каждый день. История
дна - в alert_state["mem_trend"]: {ключ: {"d": день, "m": минимум за день, "h": [[день, минимум]]}}
в мегабайтах (мельче не нужно, а запись в базу реже),
ключи "docker:<имя>", "pod:<ns>/<имя>", "proc:<имя>@<пользователь>". Данные агент шлет давно:
память контейнеров и подов этой ноды, топ процессов по приватной памяти.

Утечкой считаем рост дна LEAK_DAYS суток подряд, каждый шаг заметный и сопоставимый с
остальными: прогрев кэша после старта тоже поднимает дно, но затухает (300, 120, 50, 20 МБ),
и такой ряд отсекается отношением шагов. Базы с буферным пулом (MySQL, ClickHouse, MongoDB)
законно растут до своего лимита - их не судим.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from app.models import Server

LEAK_DAYS = 4  # суток подряд растет дно: 5 точек
KEEP_DAYS = 8
MIN_GROWTH = 0.2  # всего выросло на 20% от начала
MIN_GROWTH_MB = 100  # и не меньше 100 МБ
MIN_STEP = 0.01  # каждый суточный шаг - не меньше 1% от начала
MIN_STEP_MB = 5
STEP_RATIO = 4  # самый большой шаг не больше чем в 4 раза больше самого маленького
MUTE_PREFIX = "leak:"
_POD_OWNERS = frozenset({"Deployment", "StatefulSet", "ReplicaSet", "DaemonSet"})
# буферный пул растет до лимита и держится: это не утечка
_BUFFER_POOL = ("mysql", "mariadb", "percona", "clickhouse", "mongo")


def _cache_db(name: str) -> bool:
    low = name.lower()
    return any(x in low for x in _BUFFER_POOL)


def targets(rep: dict) -> dict[str, int]:
    """Память сейчас, МБ: контейнеры Docker, поды этой ноды (СУБД-поды агент помечает образом -
    их пропускаем), процессы из топа по приватной памяти (самый большой из одноименных)."""
    out: dict[str, int] = {}
    dk = (rep or {}).get("docker") or {}
    if dk.get("access"):
        for c in dk.get("containers") or []:
            if not isinstance(c, dict) or c.get("state") != "running" or not c.get("name"):
                continue
            if int(c.get("mem") or 0) >= 2**20 and not _cache_db(str(c.get("image") or "")):
                out["docker:" + str(c["name"])] = int(c["mem"]) >> 20
    for p in ((rep or {}).get("kube") or {}).get("pods") or []:
        if not isinstance(p, dict) or p.get("owner") not in _POD_OWNERS or not p.get("name") or p.get("image"):
            continue
        mem = int(p.get("mem") or ((p.get("use") or {}).get("mem") if isinstance(p.get("use"), dict) else 0) or 0)
        if mem >= 2**20:
            out[f"pod:{p.get('ns') or '?'}/{p['name']}"] = mem >> 20
    for p in (rep or {}).get("top_mem") or []:
        if not isinstance(p, dict) or not p.get("comm") or _cache_db(str(p["comm"])):
            continue
        private = (int(p.get("rss") or 0) - int(p.get("shared") or 0)) >> 20
        key = f"proc:{p['comm']}@{p.get('user') or '?'}"
        if private > 0 and private > out.get(key, 0):
            out[key] = private
    return out


def step(prev: dict | None, v: int, now: datetime) -> dict:
    """Дно за текущие сутки; сутки кончились - их минимум уходит в историю"""
    day = now.date().isoformat()
    p = prev if isinstance(prev, dict) else {}
    h = [x for x in p.get("h") or [] if isinstance(x, list) and len(x) == 2]
    if p.get("d") == day:
        return {"d": day, "m": min(int(p.get("m") or v), v), "h": h}
    if p.get("d") and p.get("m") is not None:
        h = (h + [[p["d"], int(p["m"])]])[-KEEP_DAYS:]
    return {"d": day, "m": v, "h": h}


def update(state: dict | None, rep: dict, now: datetime) -> dict:
    """Новое alert_state["mem_trend"]. Пропавшее из отчета держим сутки (контейнер пересоздали,
    процесс выпал из топа на минуту), дальше забываем."""
    old = state if isinstance(state, dict) else {}
    new = {k: step(old.get(k), v, now) for k, v in targets(rep).items()}
    keep_from = (now.date() - timedelta(days=1)).isoformat()
    for k, v in old.items():
        if k not in new and isinstance(v, dict) and str(v.get("d") or "") >= keep_from:
            new[k] = v
    return new


def leak(cs: dict | None) -> dict | None:
    """Растет ли дно LEAK_DAYS суток подряд: {"first", "last", "rate" (в сутки), "series"} в байтах"""
    h = (cs or {}).get("h") or []
    pts = h[-(LEAK_DAYS + 1):]
    if len(pts) < LEAK_DAYS + 1:
        return None
    try:
        days = [date.fromisoformat(str(d)) for d, _v in pts]
    except ValueError:
        return None
    if any((b - a).days != 1 for a, b in zip(days, days[1:])):
        return None  # были пропуски: нода молчала, ряд не непрерывный
    vals = [int(v) for _d, v in pts]
    steps = [b - a for a, b in zip(vals, vals[1:])]
    base = vals[0]
    min_step = max(base * MIN_STEP, MIN_STEP_MB)
    if base <= 0 or any(s < min_step for s in steps):
        return None
    if max(steps) > STEP_RATIO * min(steps):
        return None
    total = vals[-1] - base
    if total < max(base * MIN_GROWTH, MIN_GROWTH_MB):
        return None
    mb = 2**20
    return {"first": base * mb, "last": vals[-1] * mb, "rate": total * mb // LEAK_DAYS, "series": [v * mb for v in vals]}


def _fmt(b: float) -> str:
    if b >= 2**30:
        return f"{b / 2**30:.1f} ГБ"
    return f"{b / 2**20:.0f} МБ"


def problems(s: Server, now: datetime) -> list[dict]:
    """Что течет: {"key", "kind" (docker|pod|proc), "name", "rate", "eta_days", "level", "text"}.
    Процессы - только если не течет ни один контейнер и под: процесс в контейнере видно и в
    топе процессов, а сказать про это надо один раз. Свободной памяти хватит меньше чем на трое
    суток - проблема, иначе предупреждение."""
    st = (s.alert_state or {}).get("mem_trend")
    if not isinstance(st, dict):
        return []
    muted = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    rep = s.last_report or {}
    free = max(int(rep.get("mem_total") or 0) - int(rep.get("mem_used") or 0), 0)
    out = []
    for key in sorted(st):
        if key in muted:
            continue
        lk = leak(st[key])
        if lk is None:
            continue
        kind, _, name = key.partition(":")
        eta = round(free / lk["rate"], 1) if free and lk["rate"] > 0 else None
        what = {"docker": "контейнера", "pod": "пода"}.get(kind, "процесса")
        label = name
        if kind == "proc":
            comm, _, user = name.partition("@")
            label = f"{comm} (пользователь {user})"
        text = (f"память {what} {label} растет {LEAK_DAYS} суток подряд: {_fmt(lk['first'])} -> "
                f"{_fmt(lk['last'])}, +{_fmt(lk['rate'])} в сутки")
        if eta is not None and eta < 30:
            text += f", свободной памяти хватит примерно на {eta:g} сут"
        out.append({"key": key, "kind": kind, "name": name, "rate": lk["rate"], "eta_days": eta,
                    "series": lk["series"], "level": 2 if eta is not None and eta <= 3 else 1, "text": text})
    if any(x["kind"] != "proc" for x in out):
        out = [x for x in out if x["kind"] != "proc"]
    return out
