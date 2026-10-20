"""Часто перезапускается: контейнер Docker или под Kubernetes, который падает раз в час-другой.

Быстрый цикл (3 перезапуска за 15 минут) ловят docker_loop и CrashLoopBackOff, а медленный -
нет: между падениями контейнер "работает", а сервис раз в час теряет соединения, очереди и
кэш. Счетчики перезапусков (RestartCount) агент шлет давно; панель запоминает их приросты за
сутки в alert_state["restarts"]: {"docker:<имя>" | "pod:<ns>/<имя>": {"rc", "ev": [[время, n]]}}.
"""
from __future__ import annotations

from datetime import datetime

from app import podnames
from app.models import Server

WINDOW = 86400  # сутки
MIN = 5  # перезапусков за сутки: раз в день по расписанию - не проблема, раз в пару часов - да
_KEEP = 60  # событий на объект
MUTE_PREFIX = "restarts:"
_POD_OWNERS = frozenset({"Deployment", "StatefulSet", "ReplicaSet", "DaemonSet"})


def _ts(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def targets(rep: dict) -> dict[str, int]:
    """Счетчики перезапусков из отчета: контейнеры Docker (если есть доступ к сокету) и поды
    контроллеров. Поды Job не в счет: их перезапуск - повтор задания, про него kube_job."""
    out: dict[str, int] = {}
    dk = (rep or {}).get("docker") or {}
    if dk.get("access"):
        for c in dk.get("containers") or []:
            if isinstance(c, dict) and c.get("name"):
                out["docker:" + str(c["name"])] = int(c.get("restarts") or 0)
    for p in ((rep or {}).get("kube") or {}).get("pods") or []:
        if isinstance(p, dict) and p.get("owner") in _POD_OWNERS and p.get("name"):
            out[f"pod:{p.get('ns') or '?'}/{p['name']}"] = int(p.get("restarts") or 0)
    return out


def step(prev: dict | None, rc: int, now: datetime) -> dict:
    """Новое состояние счетчика: прирост RestartCount с прошлого раза - событие [время, сколько].
    Счетчик уменьшился - контейнер пересоздали (деплой), это не падение, а новая точка отсчета."""
    p = prev if isinstance(prev, dict) else {}
    ev = []
    for e in p.get("ev") or []:
        t = _ts(e[0]) if isinstance(e, list) and len(e) == 2 else None
        if t is not None and (now - t).total_seconds() <= WINDOW:
            ev.append(e)
    last = p.get("rc")
    if isinstance(last, int) and rc > last:
        ev.append([now.isoformat(), rc - last])
    return {"rc": rc, "ev": ev[-_KEEP:]}


def update(state: dict | None, rep: dict, now: datetime) -> dict:
    """Новое alert_state["restarts"] по отчету; пропавшие контейнеры и поды забываем. Если в
    отчете нет самого источника (агент на минуту потерял доступ к Docker или к кластеру), его
    счетчики держим как были: иначе прошлые сутки перезапусков потерялись бы."""
    old = state if isinstance(state, dict) else {}
    new = {k: step(old.get(k), rc, now) for k, rc in targets(rep).items()}
    has_docker = bool(((rep or {}).get("docker") or {}).get("access"))
    has_pods = "pods" in (((rep or {}).get("kube") or {}))
    for k, v in old.items():
        if k not in new and ((k.startswith("docker:") and not has_docker) or (k.startswith("pod:") and not has_pods)):
            new[k] = v
    return new


def count(cs: dict | None, now: datetime) -> tuple[int, datetime | None]:
    """Перезапусков за сутки и когда последний"""
    n, last = 0, None
    for e in (cs or {}).get("ev") or []:
        t = _ts(e[0]) if isinstance(e, list) and len(e) == 2 else None
        if t is not None and (now - t).total_seconds() <= WINDOW:
            n += int(e[1] or 0)
            last = t if last is None or t > last else last
    return n, last


def _times(n: int) -> str:
    if n % 10 in (2, 3, 4) and not 12 <= n % 100 <= 14:
        return "раза"
    return "раз"


def _ago(last: datetime | None, now: datetime) -> str:
    if last is None:
        return ""
    m = max(int((now - last).total_seconds() // 60), 0)
    return f", последний {m} мин назад" if m < 120 else f", последний {m // 60} ч назад"


def muted_keys(s: Server) -> set[str]:
    return {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}


# Пачка: за пару минут перезапустились поды трех и больше разных ворклоадов. Так бывает, когда
# подвисает сама нода или kube-api: пробы не отвечают вовремя, контроллеры теряют лидерство
# ("leader election lost") и выходят, и все это разом. Чинить надо ноду, а не поды, поэтому такие
# перезапуски не идут в счет пода, а пачки - одной строкой про ноду.
BURST_PODS = 3  # ворклоадов в одной пачке
BURST_GAP = 120  # секунд от первого перезапуска пачки: отчеты идут раз в минуту
BURST_MIN = 3  # пачек за сутки до алерта: одна-две (перезагрузка ноды, разовый затык) - не беда
MIN_OOM = 3  # OOMKilled за сутки: лимит мал или течет память, понятно, что делать
BURSTS_KEY = "bursts"  # ключ строки про пачки: мьют restarts:bursts
_SIG = {9: "SIGKILL", 15: "SIGTERM", 6: "SIGABRT", 11: "SIGSEGV"}


def cause(last: dict | None) -> tuple[str, str]:
    """Как кончился прошлый запуск пода (поле last агента 2.43): (вид, текст). Вид: oom, probe,
    exit0 (вышел сам с кодом 0 - плановый перезапуск, не падение), crash, "" - не знаем."""
    if not isinstance(last, dict):
        return "", ""
    r, x = str(last.get("r") or ""), int(last.get("x") or 0)
    if r == "OOMKilled":
        lim = str(last.get("lim") or "")
        return "oom", (f"OOMKilled при лимите {lim}" if lim else "OOMKilled, лимита нет: кончилась память ноды")
    probe = str(last.get("probe") or "")
    if probe:
        pm = str(last.get("pmsg") or "")
        return "probe", f"убит по {probe}-пробе" + (f" ({_short(pm, 100)})" if pm else "")
    if x == 0 and r in ("Completed", ""):
        return "exit0", "сам вышел с кодом 0"
    sig = _SIG.get(x - 128) if x > 128 else None
    head = f"exit {x}" + (f" ({sig})" if sig else f" ({r})" if r and r != "Error" else "")
    line = str(last.get("msg") or last.get("log") or "")
    return "crash", head + (f": {_short(line, 140)}" if line else "")


def _short(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def _pods(rep: dict | None) -> dict[str, dict]:
    out = {}
    for p in ((rep or {}).get("kube") or {}).get("pods") or []:
        if isinstance(p, dict) and p.get("name"):
            out[f"pod:{p.get('ns') or '?'}/{p['name']}"] = p
    return out


def _events(cs: dict | None, now: datetime) -> list[tuple[datetime, int]]:
    out = []
    for e in (cs or {}).get("ev") or []:
        t = _ts(e[0]) if isinstance(e, list) and len(e) == 2 else None
        if t is not None and (now - t).total_seconds() <= WINDOW:
            out.append((t, int(e[1] or 0)))
    return out


def bursts(rs: dict, ctrl: dict[str, str], now: datetime) -> list[dict]:
    """Пачки перезапусков подов за сутки: {"t" (последний в пачке), "pods" (ключи), "wl" (ворклоады)}"""
    evs = sorted((t, k) for k, cs in rs.items() if k.startswith("pod:") for t, _n in _events(cs, now))
    groups: list[list[tuple[datetime, str]]] = []
    for t, k in evs:
        if groups and (t - groups[-1][0][0]).total_seconds() <= BURST_GAP:
            groups[-1].append((t, k))
        else:
            groups.append([(t, k)])
    out = []
    for g in groups:
        wl = {podnames.pod_key(k, ctrl.get(k, "")) for _t, k in g}
        if len(wl) >= BURST_PODS:
            out.append({"t": g[-1][0], "pods": sorted({k for _t, k in g}), "wl": wl, "ev": {(k, t) for t, k in g}})
    return out


def _pod_rows(s: Server, now: datetime) -> tuple[list[dict], list[dict]]:
    """Поды с перезапусками за сутки и пачки. У пода: n - всего, solo - не в пачках, cause, alert -
    повод для алерта: OOMKilled от MIN_OOM, остальное (кроме выхода с кодом 0) - от MIN не в пачках"""
    st = s.alert_state or {}
    rs = st.get("restarts") if isinstance(st.get("restarts"), dict) else {}
    ctrl = podnames.ctrls(s.last_report)
    pods = _pods(s.last_report)
    bl = bursts(rs, ctrl, now)
    in_burst = set().union(*(b["ev"] for b in bl)) if bl else set()
    rows = []
    for key in sorted(rs):
        if not key.startswith("pod:"):
            continue
        evs = _events(rs[key], now)
        if not evs:
            continue
        n = sum(c for _t, c in evs)
        solo = sum(c for t, c in evs if (key, t) not in in_burst)
        p = pods.get(key) or {}
        ck, ctext = cause(p.get("last"))
        alert = ck != "exit0" and (n >= MIN_OOM if ck == "oom" else solo >= MIN)
        rows.append({"key": key, "n": n, "solo": solo, "last": max(t for t, _c in evs), "times": sorted(t for t, _c in evs),
                     "ck": ck, "cause": ctext, "alert": alert, "ctrl": ctrl.get(key, ""), "pod": p})
    return rows, bl


def _burst_text(bl: list[dict], rows: list[dict], now: datetime) -> str:
    sizes = [len(b["wl"]) for b in bl]
    size = f"{min(sizes)}-{max(sizes)}" if min(sizes) != max(sizes) else str(sizes[0])
    freq: dict[str, int] = {}
    for b in bl:
        for w in b["wl"]:
            freq[w] = freq.get(w, 0) + 1
    names = [w.split("/", 1)[-1] for w, _n in sorted(freq.items(), key=lambda x: (-x[1], x[0]))]
    more = f" и еще {len(names) - 3}" if len(names) > 3 else ""
    by_key = {r["key"]: r for r in rows}
    nodes = {str((by_key.get(k) or {}).get("pod", {}).get("node") or "") for b in bl for k in b["pods"]} - {""}
    where = f"подвисает нода {next(iter(nodes))}" if len(nodes) == 1 else "подвисает kube-api или etcd"
    return (f"поды перезапускаются пачками: {len(bl)} {_times(len(bl))} за сутки по {size} разом"
            f"{_ago(max(b['t'] for b in bl), now)} ({', '.join(names[:3])}{more}) - похоже, {where}: "
            "пробы и запросы к kube-api не успевают, смотрите нагрузку и память ноды")


def problems(s: Server, now: datetime) -> list[dict]:
    """Что перезапускается слишком часто: {"key", "kind" (docker|pod), "name", "n", "text"}.
    Поды - по ворклоаду (key "pod:<ns>/<деплоймент>"): два пода одного деплоймента - одна строка,
    а мьют переживает деплой, после которого у подов новые имена. У пода в тексте причина прошлого
    завершения (агент 2.43), пороги - по ней (_pod_rows); пачки - одна строка про ноду (ключ
    bursts). Без заглушенных ("restarts:<ключ>", в том числе старый мьют на под того же
    ворклоада) и без контейнеров, по которым уже ушел алерт о быстром цикле (docker_loop)."""
    st = s.alert_state or {}
    rs = st.get("restarts") if isinstance(st.get("restarts"), dict) else {}
    dk = st.get("docker") if isinstance(st.get("docker"), dict) else {}
    muted = muted_keys(s)
    ctrl = podnames.ctrls(s.last_report)
    groups: dict[str, list[tuple[str, int, datetime | None, str]]] = {}
    for key in sorted(rs):
        if not key.startswith("docker:") or key in muted:
            continue
        n, last = count(rs[key], now)
        if n < MIN or (dk.get(key[7:]) or {}).get("alerted_loop"):
            continue
        groups[key] = [(key[7:], n, last, "")]
    rows, bl = _pod_rows(s, now)
    for r in rows:
        if r["alert"] and not podnames.muted(muted, r["key"], r["ctrl"]):
            groups.setdefault(podnames.pod_key(r["key"], r["ctrl"]), []).append(
                (r["key"][4:], r["n"], r["last"], r["cause"]))
    out = []
    if len(bl) >= BURST_MIN and BURSTS_KEY not in muted:
        out.append({"key": BURSTS_KEY, "kind": "pod", "name": BURSTS_KEY, "n": len(bl),
                    "text": _burst_text(bl, rows, now)})
    for gk, items in groups.items():
        kind, _, gname = gk.partition(":")
        total = sum(n for _name, n, _last, _c in items)
        last = max((t for _n, _c, t, _x in items if t is not None), default=None)
        why = max(items, key=lambda x: x[2] or datetime.min.replace(tzinfo=now.tzinfo))[3]
        if len(items) == 1:
            what = "контейнер" if kind == "docker" else "под"
            text = f"{what} {items[0][0]} перезапускался {total} {_times(total)} за сутки{_ago(last, now)}"
        else:
            text = f"поды {gname} ({len(items)} шт.) перезапускались {total} {_times(total)} за сутки{_ago(last, now)}"
        out.append({"key": gk, "kind": kind, "name": gname, "n": total, "text": text + (f": {why}" if why else "")})
    return out


def view(s: Server, now: datetime) -> dict[str, int]:
    """Перезапусков за сутки по каждому, у кого они были: для разделов "Докер" и "Кубер"."""
    rs = (s.alert_state or {}).get("restarts")
    out = {}
    for key, cs in (rs if isinstance(rs, dict) else {}).items():
        n, _last = count(cs, now)
        if n:
            out[key] = n
    return out


def kube_view(s: Server, now: datetime) -> dict | None:
    """Раздел "Перезапуски" кластера: поды с перезапусками за сутки (когда, причина прошлого
    завершения, повод ли для алерта) и пачки. Сюда ведет алерт kube_restarts."""
    rows, bl = _pod_rows(s, now)
    if not rows:
        return None
    muted = muted_keys(s)
    pods = [{
        "ns": r["key"][4:].partition("/")[0], "name": r["key"][4:].partition("/")[2], "n": r["n"],
        "solo": r["solo"], "times": [t.isoformat() for t in r["times"][-30:]], "kind": r["ck"],
        "why": r["cause"], "last": r["pod"].get("last"), "alert": r["alert"],
        "mute": MUTE_PREFIX + podnames.pod_key(r["key"], r["ctrl"]),
        "muted": podnames.muted(muted, r["key"], r["ctrl"]),
    } for r in sorted(rows, key=lambda r: (not r["alert"], -r["n"], r["key"]))]
    return {
        "pods": pods,
        "bursts": [{"t": b["t"].isoformat(), "pods": [k[4:] for k in b["pods"]]} for b in bl[-20:]],
        "burst_min": BURST_MIN,
    }
