"""Часто перезапускается: контейнер Docker или под Kubernetes, который падает раз в час-другой.

Быстрый цикл (3 перезапуска за 15 минут) ловят docker_loop и CrashLoopBackOff, а медленный -
нет: между падениями контейнер "работает", а сервис раз в час теряет соединения, очереди и
кэш. Счетчики перезапусков (RestartCount) агент шлет давно; панель запоминает их приросты за
сутки в alert_state["restarts"]: {"docker:<имя>" | "pod:<ns>/<имя>": {"rc", "ev": [[время, n]]}}.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

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


def step(prev: dict | None, rc: int, now: datetime, last: dict | None = None, pod: bool = False) -> dict:
    """Новое состояние счетчика: прирост RestartCount с прошлого раза - событие [время, сколько].
    Счетчик уменьшился - контейнер пересоздали (деплой), это не падение, а новая точка отсчета.
    last - как кончился прошлый запуск пода (агент 2.43); его нет в отчете - держим известный.
    pod - у пода помним, с какого момента он такой ("seen", см. YOUNG_S)."""
    p = prev if isinstance(prev, dict) else {}
    ev = []
    for e in p.get("ev") or []:
        t = _ts(e[0]) if isinstance(e, list) and len(e) == 2 else None
        if t is not None and (now - t).total_seconds() <= WINDOW:
            ev.append(e)
    was = p.get("rc")
    if isinstance(was, int) and rc > was:
        ev.append([now.isoformat(), rc - was])
    out = {"rc": rc, "ev": ev[-_KEEP:]}
    if pod:
        # новый под или пересозданный под тем же именем (счетчик сбросился, так у StatefulSet и
        # DaemonSet) - с сейчас. У подов, которые панель видела раньше без отметки, ее нет: старые
        if not isinstance(prev, dict) or (isinstance(was, int) and rc < was):
            out["seen"] = now.isoformat()
        elif p.get("seen"):
            out["seen"] = p["seen"]
    known = last or (p.get("last") if isinstance(p.get("last"), dict) else None)
    if known and rc:
        out["last"] = known
    return out


# Поля last, которые панель помнит между отчетами. Причина прошлого завершения пропадает из
# отчета, когда под удаляют на выкладке: kubelet оставляет у пода один мертвый контейнер, только
# что убитый, а прошлый (по нему Last State) стирает. Воркеры, которые по плану выходят с кодом 0,
# на минуту выкладки становились "причина неизвестна" и давали алерт, а на следующем отчете,
# когда старых подов уже нет, - отбой "поды восстановлены". И так на каждой выкладке.
_LAST_KEYS = ("c", "r", "x", "t", "lim", "probe", "pmsg", "log", "msg")


def _last_of(p: dict | None) -> dict | None:
    last = (p or {}).get("last")
    if not isinstance(last, dict):
        return None
    return {k: last[k] for k in _LAST_KEYS if last.get(k) not in (None, "")} or None


def update(state: dict | None, rep: dict, now: datetime) -> dict:
    """Новое alert_state["restarts"] по отчету; пропавшие контейнеры и поды забываем. Если в
    отчете нет самого источника (агент на минуту потерял доступ к Docker или к кластеру), его
    счетчики держим как были: иначе прошлые сутки перезапусков потерялись бы."""
    old = state if isinstance(state, dict) else {}
    pods = _pods(rep)
    new = {k: step(old.get(k), rc, now, _last_of(pods.get(k)), k.startswith("pod:")) for k, rc in targets(rep).items()}
    has_docker = bool(((rep or {}).get("docker") or {}).get("access"))
    has_pods = "pods" in (((rep or {}).get("kube") or {}))
    for k, v in old.items():
        if k not in new and ((k.startswith("docker:") and not has_docker) or (k.startswith("pod:") and not has_pods)):
            new[k] = v
    # давление на ноде в те минуты, когда перезапускались поды: им пачка подтверждается как
    # подвисание ноды (bursts), а не общая беда одного приложения или плановый выход
    psi = _psi_snap(rep)
    keep = [x for x in old.get(PSI_KEY) or [] if isinstance(x, list) and len(x) == 2
            and (t := _ts(x[0])) is not None and (now - t).total_seconds() <= WINDOW]
    stamp = now.isoformat()
    moved = any(k.startswith("pod:") and v.get("ev") and v["ev"][-1][0] == stamp for k, v in new.items())
    if psi and moved:
        keep.append([now.isoformat(), psi])
    if keep:
        new[PSI_KEY] = keep[-_KEEP:]
    return new


PSI_KEY = "_psi"  # в alert_state["restarts"]: [[время, {c, mf, ms, if, is}]] - не объект
# Подвисание ноды по PSI за минуту: процессы ждут CPU, память или диск заметную долю времени.
# Пороги ниже алерта pressure: там давление держится, а здесь хватает минуты, в которую все упало
_PSI_STALL = {"c": 25.0, "ms": 10.0, "mf": 3.0, "is": 10.0, "if": 3.0}


def _psi_snap(rep: dict) -> dict | None:
    psi = (rep or {}).get("psi")
    if not isinstance(psi, dict):
        return None
    out = {}
    for k, src in (("c", "cpu_some_60"), ("ms", "mem_some_60"), ("mf", "mem_full_60"),
                   ("is", "io_some_60"), ("if", "io_full_60")):
        try:
            out[k] = round(float(psi.get(src) or 0), 1)
        except (TypeError, ValueError):
            out[k] = 0.0
    return out


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
# Но разом выходят и воркеры одного приложения: на двух нодах три деплоймента yii работали по
# 20 минут и выходили сами с кодом 0, в одну и ту же минуту, и это давало 57 "пачек" за сутки.
# Поэтому в пачке не считаются поды с плановым выходом (exit0) и заглушенные, а подвисание ноды
# нужно подтвердить: давлением на ноде в ту минуту (PSI из отчета, _psi) или причинами самих подов
# (таймаут пробы, потерянный lease kube-api). Без подтверждения перезапуски считаются по подам.
BURST_PODS = 3  # ворклоадов в одной пачке
BURST_GAP = 120  # секунд от первого перезапуска пачки: отчеты идут раз в минуту
BURST_MIN = 3  # пачек за сутки до алерта: одна-две (перезагрузка ноды, разовый затык) - не беда
MIN_OOM = 3  # OOMKilled за сутки: лимит мал или течет память, понятно, что делать
BURSTS_KEY = "bursts"  # ключ строки про пачки: мьют restarts:bursts
_SIG = {9: "SIGKILL", 15: "SIGTERM", 6: "SIGABRT", 11: "SIGSEGV"}


# признаки того, что под упал из-за подвисшей ноды или kube-api, а не по своей причине
_NODE_SIGNS = re.compile(r"leader ?election|lease|context deadline exceeded|Client\.Timeout|awaiting headers|"
                         r"request canceled|i/o timeout|TLS handshake timeout", re.I)


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
    line = str(last.get("msg") or last.get("log") or "")
    # выход с кодом 0 - плановый, если перед ним не потерян lease: envoy-gateway после "leader
    # election lost" выходит именно с нулем
    if x == 0 and r in ("Completed", "") and not _NODE_SIGNS.search(line):
        return "exit0", "сам вышел с кодом 0"
    sig = _SIG.get(x - 128) if x > 128 else None
    head = f"exit {x}" + (f" ({sig})" if sig else f" ({r})" if r and r not in ("Error", "Completed") else "")
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





def _node_sign(last: dict | None) -> bool | None:
    """True - под упал так, как падают при подвисшей ноде (проба не дождалась ответа, потерян
    lease), False - своя причина (OOM, ошибка приложения), None - причина неизвестна"""
    ck, _t = cause(last)
    if not ck:
        return None
    if ck == "probe":
        return bool(_NODE_SIGNS.search(str((last or {}).get("pmsg") or ""))) or not (last or {}).get("pmsg")
    if ck == "crash":
        return bool(_NODE_SIGNS.search(f"{(last or {}).get('msg') or ''} {(last or {}).get('log') or ''}"))
    return False


def _stall(snap: object) -> bool:
    return isinstance(snap, dict) and any(float(snap.get(k) or 0) >= v for k, v in _PSI_STALL.items())


def bursts(rs: dict, ctrl: dict[str, str], now: datetime, skip: set[str] | None = None,
           lasts: dict[str, dict | None] | None = None) -> list[dict]:
    """Пачки перезапусков подов за сутки: {"t" (последний в пачке), "pods" (ключи), "wl" (ворклоады),
    "node" (подвисание ноды подтверждено), "psi" (самое сильное давление в эти минуты)}. skip - поды,
    которые в пачки не идут (плановый выход, заглушенные); lasts - как кончился прошлый запуск пода."""
    skip = skip or set()
    evs = sorted((t, k) for k, cs in rs.items() if k.startswith("pod:") and k not in skip
                 for t, _n in _events(cs, now))
    groups: list[list[tuple[datetime, str]]] = []
    for t, k in evs:
        if groups and (t - groups[-1][0][0]).total_seconds() <= BURST_GAP:
            groups[-1].append((t, k))
        else:
            groups.append([(t, k)])
    psi = [(t, x[1]) for x in rs.get(PSI_KEY) or [] if isinstance(x, list) and len(x) == 2
           and (t := _ts(x[0])) is not None]
    out = []
    for g in groups:
        wl = {podnames.pod_key(k, ctrl.get(k, "")) for _t, k in g}
        if len(wl) < BURST_PODS:
            continue
        t0, t1 = g[0][0], g[-1][0]
        snaps = [x for t, x in psi if (t0 - timedelta(seconds=90)) <= t <= (t1 + timedelta(seconds=90))]
        stall = [x for x in snaps if _stall(x)]
        keys = sorted({k for _t, k in g})
        signs = [v for v in (_node_sign((lasts or {}).get(k)) for k in keys) if v is not None]
        node = bool(stall) or (len(signs) >= 2 and sum(signs) * 2 >= len(signs))
        top = max(stall or snaps, key=lambda x: max(float(x.get(k) or 0) for k in _PSI_STALL), default=None)
        out.append({"t": t1, "pods": keys, "wl": wl, "ev": {(k, t) for t, k in g}, "node": node,
                    "psi": top if stall else None})
    return out


# Выкатка: новый под не встает, падает, его чинят следующей выкаткой, и он уходит. По порогу это
# был алерт через пять минут, а через час "поды больше не перезапускаются" - пара сообщений о
# том, что разработчики и так видят в своем CI. Поэтому перезапуски пода в первые YOUNG_S его
# жизни (с "seen", когда панель его впервые увидела или он пересоздан) не алертят. Пока рядом
# работает и готов под прошлой ревизии того же деплоймента, сервис жив, и ждем ROLLOUT_S.
# Под и после выдержки падает дальше (перезапуск в первые CONT_S после нее) - выкатка зависла:
# алерт со всеми перезапусками. Затих - ранние перезапуски в счет не идут ни сейчас, ни потом.
YOUNG_S = 1800
ROLLOUT_S = 3600
CONT_S = 600


def _old_revision_alive(pods: dict[str, dict]) -> set[str]:
    """Поды деплойментов, рядом с которыми работает и готов под другой ревизии того же
    деплоймента: идет выкатка, старая ревизия держит сервис. Ревизия - хеш ReplicaSet в имени."""
    revs: dict[str, list[tuple[str, str, bool]]] = {}
    for k, p in pods.items():
        if p.get("owner") != "ReplicaSet":
            continue
        rev = podnames.revision(str(p.get("name") or ""))
        if rev:
            up = p.get("phase") == "Running" and bool(p.get("ready"))
            revs.setdefault(podnames.pod_key(k, str(p.get("ctrl") or "")), []).append((k, rev, up))
    out = set()
    for items in revs.values():
        for k, rev, _up in items:
            if any(up and r != rev for _k, r, up in items):
                out.add(k)
    return out


def _young(cs: dict | None, evs: list[tuple[datetime, int]], rollout: bool, gone: bool,
           now: datetime) -> tuple[datetime | None, set[datetime]]:
    """Выдержка нового пода (см. YOUNG_S): (до какого момента ждем или None, ранние перезапуски,
    которые не в счет)."""
    seen = _ts((cs or {}).get("seen"))
    if seen is None:
        return None, set()
    if gone:
        # под ушел из отчета (его убрала следующая выкатка), а состояние помнит его до конца тика:
        # прошлой ревизии рядом уже не видно, и выдержка сжималась бы до получаса - алерт в минуту
        # починки. Ушедший под - не новость
        return None, {t for t, _c in evs}
    until = seen + timedelta(seconds=ROLLOUT_S if rollout else YOUNG_S)
    early = {t for t, _c in evs if t < until}
    if now < until:
        return until, early
    late = [t for t, _c in evs if t >= until]
    if late and min(late) <= until + timedelta(seconds=CONT_S):
        return None, set()  # падал и после выдержки без передышки: выкатка зависла
    return None, early


def rollout_hold(s: Server, now: datetime) -> set[str]:
    """Поды новой ревизии в первые ROLLOUT_S, пока рядом работает прошлая: "под не работает"
    (kube_pod) про них ждет, как и перезапуски (_young). Под, которого панель еще не запомнила
    (первый отчет с ним), - новый; запомненный без "seen" - старый."""
    rs = (s.alert_state or {}).get("restarts")
    rs = rs if isinstance(rs, dict) else {}
    out = set()
    for k in _old_revision_alive(_pods(s.last_report)):
        cs = rs.get(k)
        seen = _ts(cs.get("seen")) if isinstance(cs, dict) else now
        if seen is not None and (now - seen).total_seconds() < ROLLOUT_S:
            out.add(k)
    return out


def _pod_rows(s: Server, now: datetime) -> tuple[list[dict], list[dict]]:
    """Поды с перезапусками за сутки и пачки. У пода: n - всего, solo - не в пачках, cause, alert -
    повод для алерта: OOMKilled от MIN_OOM, остальное (кроме выхода с кодом 0) - от MIN не в пачках;
    перезапуски выкатки (_young) не в счет, wait - до какого момента ждем нового пода"""
    st = s.alert_state or {}
    rs = st.get("restarts") if isinstance(st.get("restarts"), dict) else {}
    ctrl = podnames.ctrls(s.last_report)
    pods = _pods(s.last_report)
    muted = muted_keys(s)
    # причина из отчета, а нет ее там - последняя известная (см. _LAST_KEYS)
    lasts = {k: (pods.get(k) or {}).get("last") or (rs[k].get("last") if isinstance(rs[k], dict) else None)
             for k in rs if k.startswith("pod:")}
    skip = {k for k in lasts if cause(lasts[k])[0] == "exit0" or podnames.muted(muted, k, ctrl.get(k, ""))}
    bl = bursts(rs, ctrl, now, skip, lasts)
    # в счет пода не идут только перезапуски из пачек, где подтверждено подвисание ноды
    in_burst = set().union(*(b["ev"] for b in bl if b["node"])) if bl else set()
    rollout = _old_revision_alive(pods)
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
        ck, ctext = cause(lasts.get(key))
        wait, early = _young(rs[key], evs, key in rollout, key not in pods, now)
        n_a = sum(c for t, c in evs if t not in early)
        solo_a = sum(c for t, c in evs if (key, t) not in in_burst and t not in early)
        alert = wait is None and ck != "exit0" and (n_a >= MIN_OOM if ck == "oom" else solo_a >= MIN)
        rows.append({"key": key, "n": n, "solo": solo, "last": max(t for t, _c in evs), "times": sorted(t for t, _c in evs),
                     "ck": ck, "cause": ctext, "alert": alert, "ctrl": ctrl.get(key, ""), "pod": p,
                     "lastinfo": lasts.get(key), "wait": wait, "rollout": key in rollout})
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
    snaps = [b["psi"] for b in bl if b.get("psi")]
    if snaps:
        # самое сильное давление в минуты пачек: чего не хватало ноде
        peak = {k: max(float(x.get(k) or 0) for x in snaps) for k in _PSI_STALL}
        parts = [f"{what} {peak[some]:.0f}%" for some, full, what in (("c", "", "CPU"), ("ms", "mf", "память"),
                                                                     ("is", "if", "диск"))
                 if peak[some] >= _PSI_STALL[some] or (full and peak[full] >= _PSI_STALL[full])]
        why = f"в эти минуты процессы ждали {', '.join(parts) or 'ресурсы'} времени"
    else:
        why = "пробы и запросы к kube-api не дождались ответа"
    return (f"поды перезапускаются пачками: {len(bl)} {_times(len(bl))} за сутки по {size} разом"
            f"{_ago(max(b['t'] for b in bl), now)} ({', '.join(names[:3])}{more}) - {where}: {why}, "
            "смотрите, что грузит ноду")


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
    nb = [b for b in bl if b["node"]]
    if len(nb) >= BURST_MIN and BURSTS_KEY not in muted:
        out.append({"key": BURSTS_KEY, "kind": "pod", "name": BURSTS_KEY, "n": len(nb),
                    "text": _burst_text(nb, rows, now)})
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
        if key == PSI_KEY:
            continue
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
        "why": r["cause"], "last": r["lastinfo"], "alert": r["alert"],
        "mute": MUTE_PREFIX + podnames.pod_key(r["key"], r["ctrl"]),
        "muted": podnames.muted(muted, r["key"], r["ctrl"]),
        # новый под: перезапуски выкатки до этого момента не алертят (rollout - рядом жива прошлая ревизия)
        "wait": r["wait"].isoformat() if r["wait"] else None, "rollout": r["rollout"],
    } for r in sorted(rows, key=lambda r: (not r["alert"], -r["n"], r["key"]))]
    return {
        "pods": pods,
        # только подтвержденные подвисания ноды: одновременный выход воркеров одного приложения - не пачка
        "bursts": [{"t": b["t"].isoformat(), "pods": [k[4:] for k in b["pods"]], "psi": b["psi"]}
                   for b in [b for b in bl if b["node"]][-20:]],
        "burst_min": BURST_MIN,
    }


# -- короткий текст для Telegram (формат A, app/tg_fmt.py) --
# Под перезапускается
# tech1/scheduler-dispatch: нет связи с базой

from app import tg_fmt  # noqa: E402

# строка лога упавшего пода -> причина словами; первая подошедшая
_LINE_CAUSES = (
    (re.compile(r"no connection to|could not connect to server|connection to server .* failed|"
                r"Can't connect to (?:local )?MySQL|Lost connection to MySQL|MySQL server has gone away|"
                r"server closed the connection unexpectedly|SQLSTATE\[(?:08\w{3}|HY000\] \[2002)|"
                r"(?:Connection refused|ECONNREFUSED).{0,40}(?:5432|3306|27017)|(?:5432|3306|27017)\b.{0,40}"
                r"(?:refused|timed? ?out|reset)", re.I), "нет связи с базой"),
    (re.compile(r"redis.{0,60}(?:refused|timed? ?out|connect)|6379\b.{0,40}(?:refused|timed? ?out|reset)", re.I),
     "нет связи с Redis"),
    (re.compile(r"SQLSTATE", re.I), "ошибка SQL"),
    (re.compile(r"leader ?election lost|lost lease|failed to renew lease", re.I), "потерял лидерство"),
    (re.compile(r"out of memory|OutOfMemoryError|Cannot allocate memory|heap limit", re.I), "кончилась память"),
    (re.compile(r"address already in use|EADDRINUSE", re.I), "порт уже занят"),
    (re.compile(r"no such host|Name or service not known|getaddrinfo|ENOTFOUND|Temporary failure in name resolution",
                re.I), "не резолвится адрес"),
    (re.compile(r"connection refused|ECONNREFUSED", re.I), "отказ в соединении"),
    (re.compile(r"deadline exceeded|timed? ?out|timeout", re.I), "таймаут"),
    (re.compile(r"permission denied|EACCES", re.I), "нет прав"),
    (re.compile(r"no such file or directory|ENOENT", re.I), "нет файла"),
    (re.compile(r"segmentation fault|SIGSEGV", re.I), "segfault"),
    (re.compile(r"\bpanic:", re.I), "panic"),
)
_K8S_SIZE = re.compile(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?")
_K8S_MUL = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def _k8s_size(lim: str) -> str:
    """Лимит Kubernetes по-человечески: 512Mi -> 512мб, 1Gi -> 1гб."""
    m = _K8S_SIZE.fullmatch(lim.strip())
    if not m:
        return lim.strip()
    return tg_fmt.size(float(m.group(1)) * _K8S_MUL.get(m.group(2) or "", 1))


def cause_short(why: str) -> str:
    """Причина из текста cause() коротко: "OOM при лимите 512мб", "нет связи с базой", "exit 1"."""
    w = " ".join(str(why or "").split())
    if not w:
        return ""
    if w.startswith("OOMKilled при лимите "):
        return "OOM при лимите " + _k8s_size(w.removeprefix("OOMKilled при лимите "))
    if w.startswith("OOMKilled"):
        return "OOM, кончилась память ноды"
    m = re.match(r"убит по (\S+)-пробе", w)
    if m:
        return f"не прошел {m.group(1)}-пробу"
    if w.startswith("сам вышел"):
        return "вышел сам"
    m = re.match(r"exit (\d+)(?: \(([^)]*)\))?(?:: (.*))?$", w)
    if not m:
        return w if len(w) <= 60 else w[:57].rstrip() + "..."
    line = m.group(3) or ""
    human = next((txt for rx, txt in _LINE_CAUSES if rx.search(line)), "")
    return human or f"exit {m.group(1)}" + (f" ({m.group(2)})" if m.group(2) else "")


def _short_pod(name: str) -> str:
    ns, _, wl = str(name or "?").partition("/")
    return podnames.label(ns, wl) if wl else ns


def _why(x: dict) -> str:
    """Причина прошлого завершения пода: поле, если оно есть, иначе хвост текста после ": "."""
    return str(x.get("cause") or x.get("why") or str(x.get("text") or "").partition(": ")[2])


def tg_short(items) -> tuple[str, list[str]] | None:
    """(что случилось, конкретика) по элементам problems() одного вида (docker или pod). Пачки -
    про ноду и ворклоады в них, под - ворклоад без хеша и причина коротко, контейнер - сколько раз
    за сутки. Остальное именами."""
    xs = [x for x in items or [] if isinstance(x, dict)]
    if not xs:
        return None
    burst = next((x for x in xs if x.get("key") == BURSTS_KEY), None)
    pods = [x for x in xs if x.get("kind") == "pod" and x is not burst]
    docker = [x for x in xs if x.get("kind") == "docker"]
    if burst is not None:
        m = re.search(r"\(([^()]+)\) - подвисает", str(burst.get("text") or ""))
        facts = [m.group(1)] if m else []
        if pods:
            facts.append(tg_fmt.fact("Еще", tg_fmt.names(_short_pod(x.get("name")) for x in pods)))
        return "Поды перезапускаются пачками, подвисает нода", facts
    if pods:
        x = pods[0]
        why = cause_short(_why(x))
        facts = [f"{_short_pod(x.get('name'))}: {why}" if why else _short_pod(x.get("name"))]
        if len(pods) > 1:
            facts.append(tg_fmt.fact("Еще", tg_fmt.names(_short_pod(y.get("name")) for y in pods[1:])))
        return ("Под перезапускается" if len(pods) == 1 else "Поды перезапускаются"), facts
    if docker:
        x = docker[0]
        n = int(x.get("n") or 0)
        facts = [f"{n} {_times(n)} за сутки"] if n else []
        if len(docker) > 1:
            facts.append(tg_fmt.fact("Еще", tg_fmt.names(str(y.get("name")) for y in docker[1:])))
        return f"{x.get('name')} часто перезапускается", facts
    return None
