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


def problems(s: Server, now: datetime) -> list[dict]:
    """Что перезапускается чаще MIN раз за сутки: {"key", "kind" (docker|pod), "name", "n", "text"}.
    Поды - по ворклоаду (key "pod:<ns>/<деплоймент>"): два пода одного деплоймента - одна строка,
    а мьют переживает деплой, после которого у подов новые имена. Без заглушенных
    ("restarts:<ключ>", в том числе старый мьют на под того же ворклоада) и без контейнеров, по
    которым уже ушел алерт о быстром цикле (docker_loop): там и так все сказано."""
    st = s.alert_state or {}
    rs = st.get("restarts") if isinstance(st.get("restarts"), dict) else {}
    dk = st.get("docker") if isinstance(st.get("docker"), dict) else {}
    muted = muted_keys(s)
    ctrl = podnames.ctrls(s.last_report)
    groups: dict[str, list[tuple[str, int, datetime | None]]] = {}
    for key in sorted(rs):
        if podnames.muted(muted, key, ctrl.get(key, "")):
            continue
        n, last = count(rs[key], now)
        if n < MIN:
            continue
        kind, _, name = key.partition(":")
        if kind == "docker" and (dk.get(name) or {}).get("alerted_loop"):
            continue
        groups.setdefault(podnames.pod_key(key, ctrl.get(key, "")), []).append((name, n, last))
    out = []
    for gk, items in groups.items():
        kind, _, gname = gk.partition(":")
        total = sum(n for _name, n, _last in items)
        last = max((t for _n, _c, t in items if t is not None), default=None)
        if len(items) == 1:
            what = "контейнер" if kind == "docker" else "под"
            text = f"{what} {items[0][0]} перезапускался {total} {_times(total)} за сутки{_ago(last, now)}"
        else:
            text = f"поды {gname} ({len(items)} шт.) перезапускались {total} {_times(total)} за сутки{_ago(last, now)}"
        out.append({"key": gk, "kind": kind, "name": gname, "n": total, "text": text})
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
