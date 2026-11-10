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

Рост сам по себе не беда: Prometheus, набравший 125 МБ за четыре дня на ноде с гигабайтами
свободной памяти, будил алертом в три ночи. Беда - когда при таком росте (всех растущих вместе)
свободной памяти сервера хватит меньше чем на CRIT_DAYS суток: тогда алерт и "Что сломано". Раньше
алертило и "меньше недели", но с запасом в неделю ночью никто не встает: такой рост, как и любой
другой, виден в карточке с прогнозом, на сколько хватит памяти.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import math
from zoneinfo import ZoneInfo

from app import podnames, tg_fmt
from app.models import Server

LEAK_DAYS = 4  # суток подряд растет дно: 5 точек
KEEP_DAYS = 8
MIN_GROWTH = 0.2  # всего выросло на 20% от начала
MIN_GROWTH_MB = 100  # и не меньше 100 МБ
MIN_STEP = 0.01  # каждый суточный шаг - не меньше 1% от начала
MIN_STEP_MB = 5
STEP_RATIO = 4  # самый большой шаг не больше чем в 4 раза больше самого маленького
CRIT_DAYS = 3  # свободной памяти при таком росте хватит меньше чем на трое суток - алерт
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
    dk = dk if isinstance(dk, dict) else {}
    if dk.get("access"):
        for c in dk.get("containers") or []:
            if not isinstance(c, dict) or c.get("state") != "running" or not c.get("name"):
                continue
            if int(c.get("mem") or 0) >= 2**20 and not _cache_db(str(c.get("image") or "")):
                out["docker:" + str(c["name"])] = int(c["mem"]) >> 20
    for p in podnames.inventory(rep):
        if p.get("owner") not in _POD_OWNERS or not p.get("name") or p.get("image"):
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


SAMPLE_MAX_AGE = 180  # metrics-server timestamp, not the arrival time of a cached sample


def fresh(s: Server, now: datetime) -> bool:
    if not s.last_seen:
        return False
    seen = s.last_seen.replace(tzinfo=s.last_seen.tzinfo or now.tzinfo)
    return -60 <= (now - seen).total_seconds() <= (s.offline_after_seconds or 120)


def inventory_complete(rep: dict, kind: str) -> bool:
    """Only a successful full list proves absence. Access alone is a version probe.

    In agent 2.61 nonempty lists come only from successful unpaginated bulk reads;
    nil/error and successful empty are indistinguishable. Keep that narrow legacy
    compatibility, without overriding an explicit false marker from a newer agent.
    top_mem is a bounded ranking, so absence there never proves a process is gone.
    """
    block, marker, field = ({"pod": ("kube", "pods_complete", "pods"),
                             "docker": ("docker", "containers_complete", "containers")}
                            .get(kind, ("", "", "")))
    data = rep.get(block)
    if not isinstance(data, dict) or data.get("access") is not True:
        return False
    items = data.get(field, [])
    if not isinstance(items, list) or any(not isinstance(x, dict) or not x.get("name") for x in items):
        return False
    if marker in data:
        return data[marker] is True
    return (rep.get("agent_version") == "2.61" and bool(items)
            and (kind != "pod" or len(items) < 400))


def members(rep: dict) -> dict[str, dict]:
    """Active identities, independently of whether a memory sample is available."""
    out = {}
    docker = rep.get("docker") or {}
    for c in (docker.get("containers") or []) if isinstance(docker, dict) else []:
        if isinstance(c, dict) and c.get("name") and c.get("state") == "running" and not _cache_db(str(c.get("image") or "")):
            out["docker:" + str(c["name"])] = c
    for pod in podnames.inventory(rep):
        if pod.get("owner") in _POD_OWNERS and pod.get("name") and not pod.get("image") \
                and pod.get("phase") in (None, "", "Running"):
            out[f"pod:{pod.get('ns') or '?'}/{pod['name']}"] = pod
    for proc in rep.get("top_mem") or []:
        if isinstance(proc, dict) and proc.get("comm") and not _cache_db(str(proc["comm"])):
            out[f"proc:{proc['comm']}@{proc.get('user') or '?'}"] = proc
    return out


def _sample_fresh(rep: dict, key: str, now: datetime) -> bool:
    if key not in targets(rep):
        return False
    if not key.startswith("pod:"):
        return True
    pod = members(rep).get(key, {})
    if int(pod.get("mem") or 0) > 0:  # this agent's local cgroup read
        return True
    use = pod.get("use")
    ts = use.get("ts") if isinstance(use, dict) else None
    return isinstance(ts, (int, float)) and -60 <= now.timestamp() - ts <= SAMPLE_MAX_AGE


def _budget(total, used) -> int | None:
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (total, used)):
        return None
    return int(total - used) if total > 0 and 0 <= used <= total else None


def _node_budget(rep: dict, node: str, now: datetime) -> int | None:
    kube = rep.get("kube") or {}
    for n in (kube.get("nodes") or []) if isinstance(kube, dict) else []:
        if not isinstance(n, dict) or n.get("name") != node:
            continue
        cap = n["cap"] if isinstance(n.get("cap"), dict) else {}
        use = n["use"] if isinstance(n.get("use"), dict) else {}
        free, ts = _budget(cap.get("mem"), use.get("mem")), use.get("ts")
        if free is not None and isinstance(ts, (int, float)) and -60 <= now.timestamp() - ts <= SAMPLE_MAX_AGE:
            return free
    return None


def _pool(rep: dict, key: str, now: datetime) -> tuple[str, int | None]:
    if not key.startswith("pod:"):
        hostname = str(rep.get("hostname") or "")
        free = _node_budget(rep, hostname, now) if hostname else None
        if free is not None:
            return "node:" + hostname, free  # the same physical worker as its local pods
        return "host", _budget(rep.get("mem_total"), rep.get("mem_used"))
    pod = members(rep).get(key, {})
    node = str(pod.get("node") or "")
    if not node:
        return "unknown:" + key, None
    return "node:" + node, _node_budget(rep, node, now)


def _identity_changed(cs: dict, item: dict) -> bool:
    # Older state has no UID: its first binding is not proof of replacement.
    # StatefulSet names can be reused, so two known different UIDs start a new series.
    return bool(cs.get("u")) and bool(item.get("u")) and cs["u"] != item["u"]


def previous_keys(s: Server) -> set[str]:
    state = s.alert_state or {}
    known = (s.known_issues or {}).get("mem_leak") or {}
    keys = set(state.get("mem_leak_sig") or []) | set(state.get("mem_leak_told") or []) | set(known.get("sig") or [])
    if keys:
        return keys
    return {k for k, v in (state.get("mem_trend") or {}).items() if leak(v) is not None}


def recovery_confirmed(s: Server, now: datetime, keys=None) -> bool:
    """No recovery timer from missing/failed inventory or missing worker metrics."""
    if not fresh(s, now):
        return False
    rep, st = s.last_report or {}, (s.alert_state or {}).get("mem_trend") or {}
    check = set(previous_keys(s) if keys is None else keys)
    if not check:
        return False
    current = members(rep)
    for key in check:
        kind = key.partition(":")[0]
        complete = inventory_complete(rep, kind)
        if key not in current:
            if not complete:
                return False
            continue
        if kind == "pod" and _identity_changed(st.get(key) or {}, current[key]):
            if not complete:
                return False
            continue
        if not _sample_fresh(rep, key, now):
            return False
        if leak(st.get(key)) is not None and _pool(rep, key, now)[1] is None:
            return False
    return True


def _local_day(now: datetime, tz: str | None) -> date:
    """Сутки по поясу панели: по UTC они начинались в 03:00 МСК, и ночной бэкап делился на двое
    суток, а дно одних из них было не дном."""
    if not tz:
        return now.date()
    try:
        return now.astimezone(ZoneInfo(tz)).date()
    except Exception:  # noqa: BLE001 - кривой пояс в настройках: как раньше, по UTC
        return now.date()


def step(prev: dict | None, v: int, now: datetime, tz: str | None = None) -> dict:
    """Дно за текущие сутки; сутки кончились - их минимум уходит в историю"""
    day = _local_day(now, tz).isoformat()
    p = prev if isinstance(prev, dict) else {}
    h = [x for x in p.get("h") or [] if isinstance(x, list) and len(x) == 2]
    if p.get("d") == day:
        return {"d": day, "m": min(int(p.get("m") or v), v), "h": h}
    if p.get("d") and p.get("m") is not None:
        h = (h + [[p["d"], int(p["m"])]])[-KEEP_DAYS:]
    return {"d": day, "m": v, "h": h}


def update(state: dict | None, rep: dict, now: datetime, tz: str | None = None) -> dict:
    """Новое alert_state["mem_trend"]. Подтвержденно исчезнувшее держим сутки для истории. Неполный снимок
    и выпадение процесса из ограниченного top не доказывают исчезновение: прежний ряд сохраняем.
    tz - пояс панели: сутки по нему."""
    old = state if isinstance(state, dict) else {}
    current = members(rep)
    new = {}
    for k, v in targets(rep).items():
        if not _sample_fresh(rep, k, now):
            continue
        prev, item = old.get(k), current.get(k, {})
        if k.startswith("pod:") and prev and _identity_changed(prev, item):
            if not inventory_complete(rep, "pod"):
                continue
            prev = None
        new[k] = step(prev, v, now, tz)
        uid = item.get("u") or (prev or {}).get("u")
        if k.startswith("pod:") and uid:
            new[k]["u"] = uid
    keep_from = (_local_day(now, tz) - timedelta(days=1)).isoformat()
    for k, v in old.items():
        if k not in new and isinstance(v, dict) and (not inventory_complete(rep, k.partition(":")[0])
                or k in current or str(v.get("d") or "") >= keep_from):
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
    """Что растет: {"key", "kind" (docker|pod|proc), "name", "rate", "eta_days", "level", "text"}.
    Процессы - только если не растет ни один контейнер и под: процесс в контейнере видно и в
    топе процессов, а сказать про это надо один раз. eta_days - на сколько суток хватит свободной
    памяти соответствующей ноды, если ее объекты продолжат расти так же. Pod использует только
    свежие метрики своей worker-ноды. Без достоверного запаса ETA отсутствует, сам тренд виден;
    прежний объявленный/известный уровень сохраняется до подтвержденного восстановления."""
    st = (s.alert_state or {}).get("mem_trend")
    if not isinstance(st, dict):
        return []
    muted = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    rep = s.last_report or {}
    ctrl = podnames.ctrls(rep)
    current = members(rep)
    report_fresh = fresh(s, now)
    prior = s.alert_state or {}
    known = (s.known_issues or {}).get("mem_leak") or {}
    prior_sig = set(prior.get("mem_leak_sig") or prior.get("mem_leak_told") or [])
    known_sig = set(known.get("sig") or [])
    out = []
    for key in sorted(st):
        kind = key.partition(":")[0]
        complete = report_fresh and inventory_complete(rep, kind)
        if complete and (key not in current or kind == "pod" and _identity_changed(st[key], current[key])):
            continue
        # мьют на под глушит и его ворклоад: после деплоя у пода новое имя
        if podnames.muted(muted, key, ctrl.get(key, "")):
            continue
        lk = leak(st[key])
        if lk is None:
            continue
        kind, _, name = key.partition(":")
        what = {"docker": "контейнера", "pod": "пода"}.get(kind, "процесса")
        label = name
        if kind == "proc":
            comm, _, user = name.partition("@")
            label = f"{comm} (пользователь {user})"
        text = (f"память {what} {label} растет {LEAK_DAYS} суток подряд: {_fmt(lk['first'])} -> "
                f"{_fmt(lk['last'])}, +{_fmt(lk['rate'])} в сутки")
        out.append({"key": key, "kind": kind, "name": name, "rate": lk["rate"], "series": lk["series"],
                    "text": text})
    # A controller's host processes are unrelated to pods on another worker.
    local_pods = any(x["kind"] == "pod" and current.get(x["key"], {}).get("node") == rep.get("hostname")
                     and rep.get("hostname") for x in out)
    if local_pods or any(x["kind"] == "docker" for x in out):
        out = [x for x in out if x["kind"] != "proc"]
    pools = {x["key"]: _pool(rep, x["key"], now) for x in out}
    rates = {}
    for x in out:
        pool, _ = pools[x["key"]]
        if report_fresh and _sample_fresh(rep, x["key"], now):
            rates[pool] = rates.get(pool, 0) + x["rate"]
    for x in out:
        key = x["key"]
        pool, free = pools[key]
        valid = report_fresh and _sample_fresh(rep, key, now) and free is not None
        if x["kind"] == "pod" and _identity_changed(st[key], current.get(key, {})):
            valid = False
        rate = rates.get(pool, 0)
        eta = round(free / rate, 1) if valid and rate > 0 else None
        x["eta_days"] = eta
        x["level"] = 2 if eta is not None and eta < CRIT_DAYS else 0
        if not valid:
            # Preserve an announced/known problem while evidence is unavailable, without
            # inventing an ETA or opening a new problem from an unverified old series.
            levels = [int(prior.get("mem_leak") or 0) if not prior_sig or key in prior_sig else 0,
                      int(known.get("level") or 0) if not known_sig or key in known_sig else 0]
            x["level"] = max(levels)
            x["text"] += ("; запас памяти ноды сейчас неизвестен" if report_fresh and _sample_fresh(rep, key, now)
                          else "; актуальность тренда сейчас не подтверждена")
        elif x["level"]:
            where = "ноды " + pool[5:] if pool.startswith("node:") else "сервера"
            x["text"] += f", свободной памяти {where} ({_fmt(free)}) при таком росте хватит примерно на {eta:g} сут"
    return out


# -- короткий текст для Telegram (формат A, app/tg_fmt.py) --
# Память растет 4 дня
# Под: access-hub, уже 925мб

_KIND_LABEL = {"pod": "Под", "docker": "Контейнер", "proc": "Процесс"}


def _short_name(x: dict) -> str:
    """access-hub (под без хеша), rest-server, dockerd или php-fpm7.4 (idv)."""
    kind, name = x.get("kind"), str(x.get("name") or "?")
    if kind == "pod":
        ns, _, pod = name.partition("/")
        return podnames.label(ns, pod) if pod else name
    if kind == "proc":
        comm, _, user = name.partition("@")
        return comm if user in ("", "?", "root") else f"{comm} ({user})"
    return name


def tg_short(items) -> tuple[str, list[str]] | None:
    """(что случилось, конкретика) по растущей памяти: вход - problems() с уровнем (то, что алертит).
    Первое растущее с тем, сколько уже занимает, остальное именами."""
    xs = [x for x in items or [] if isinstance(x, dict)]
    if not xs:
        return None
    x = xs[0]
    series = x.get("series") or []
    now_b = series[-1] if series else None
    val = _short_name(x) + (f", уже {tg_fmt.size(now_b)}" if now_b else "")
    facts = [tg_fmt.fact(_KIND_LABEL.get(str(x.get("kind")), ""), val)]
    if len(xs) > 1:
        facts.append(tg_fmt.fact("Еще", tg_fmt.names(_short_name(y) for y in xs[1:])))
    return f"Память растет {tg_fmt.days(LEAK_DAYS)}", facts
