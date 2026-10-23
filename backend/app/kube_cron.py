"""CronJob кластера: упал или не запускается, и почему.

Раньше панель знала только время последнего запуска и последнего успеха и писала "нет успешного
прогона N дн" обо всем подряд. А это разные беды: прогоны идут и падают (смотреть лог) или
запусков нет совсем (смотреть сам CronJob). Агент 2.50 присылает последний Job (чем кончился,
у упавшего - под, код выхода, строку лога без секретов), последнее значимое событие CronJob
(Job не создается, пропущен запуск, Forbid при идущем прошлом), concurrencyPolicy и
startingDeadlineSeconds. Старый агент - без них, причина тогда короче.

Смену расписания панель помнит сама (alert_state["cron_sched"]): после "раз в неделю" ->
"каждый день" прошлый успех был 5 дней назад по старому расписанию, и это не "не отработал".
Отсчет тогда идет от момента смены."""
from __future__ import annotations

from datetime import datetime

from app import job_logs

STATE_KEY = "cron_sched"
# «в процессе» с запасом: запланированный прогон не считаем упавшим ещё 2 ч (длительный дамп)
SLACK = 2 * 3600
_LOG_MAX = 160


def cronjobs(rep: dict | None) -> list[dict]:
    return [c for c in (((rep or {}).get("kube") or {}).get("cronjobs") or []) if isinstance(c, dict)]


def key(cj: dict) -> str:
    return f"{cj.get('ns', '?')}/{cj.get('name', '?')}"


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def track(prev: dict | None, rep: dict | None, now: datetime) -> dict:
    """Расписания CronJob между отчетами: {ns/имя: {"s": расписание, "at": когда сменилось}}.
    Впервые увиденный CronJob - без отметки: смены не было, есть только то, что видим."""
    p = prev if isinstance(prev, dict) else {}
    out = {}
    for cj in cronjobs(rep):
        k, s = key(cj), str(cj.get("schedule") or "")
        was = p.get(k)
        if isinstance(was, dict) and was.get("s") == s:
            out[k] = was
        else:
            out[k] = {"s": s, "at": now.isoformat() if isinstance(was, dict) else ""}
    return out


def changed_at(state: dict | None, name: str) -> float:
    """Когда сменилось расписание CronJob (unix), 0 - не менялось при панели."""
    rec = ((state or {}).get(STATE_KEY) or {}).get(name)
    try:
        return datetime.fromisoformat(rec["at"]).timestamp() if isinstance(rec, dict) and rec.get("at") else 0.0
    except (TypeError, ValueError):
        return 0.0


def interval(sched: str) -> int:
    """Грубая оценка интервала cron-расписания (5 полей) в секундах. Неизвестно → сутки.
    Нужна, чтобы порог «давно нет успеха» был адекватен частоте (ежечасный ≠ ежедневный)."""
    p = (sched or "").split()
    if len(p) != 5:
        return 86400
    minute, hour, dom, _mon, dow = p
    if minute.startswith("*/"):
        return max(int(minute[2:] or 1) * 60, 60)
    if minute == "*":
        return 60
    if hour == "*":
        return 3600
    if hour.startswith("*/"):
        return max(int(hour[2:] or 1) * 3600, 3600)
    if dow != "*":
        return 7 * 86400          # по дню недели → еженедельно
    if dom.startswith("*/"):
        return max(int(dom[2:] or 1) * 86400, 86400)
    if dom != "*":
        return 28 * 86400         # конкретное число месяца → ~ежемесячно
    return 86400                  # M H * * * → ежедневно


def _days(sec: float) -> str:
    return f"{round(sec / 86400, 1)} дн"


def _short(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def run_cause(last: dict | None) -> str:
    """Почему упал прогон: "BackoffLimitExceeded, OOMKilled, код 137". Пусто - не знаем."""
    last = last if isinstance(last, dict) else {}
    parts = []
    if last.get("reason"):
        parts.append(str(last["reason"]))
    pr = str(last.get("pr") or "")
    if pr and pr != "Error" and pr not in parts:
        parts.append(pr)
    if last.get("x") is not None and _int(last.get("x")) != 0:
        parts.append(f"код {_int(last.get('x'))}")
    return ", ".join(parts)


def _ev_why(cj: dict) -> str:
    """Событие CronJob, объясняющее, почему нет запусков. Только свежее последнего запуска:
    старая жалоба, после которой Job создавались, уже ничего не объясняет."""
    ev = cj.get("ev")
    if not isinstance(ev, dict) or _int(ev.get("t")) <= _int(cj.get("last_schedule")):
        return ""
    r, m = str(ev.get("r") or ""), str(ev.get("m") or "").replace(";", ",")
    dl = _int(cj.get("deadline"))
    dl_txt = f", startingDeadlineSeconds {dl}" if dl else ""
    if r == "JobAlreadyActive":
        return "Forbid, а прошлый Job еще идет"
    if r == "TooManyMissedTimes" or "too many missed" in m.lower():
        return "пропущено слишком много запусков" + dl_txt
    if r == "MissSchedule" or r == "MissingSchedule":
        return "пропущен запуск" + dl_txt
    if r == "FailedCreate":
        return "Job не создается: " + _short(m.removeprefix("Error creating job: "), 120)
    if r in ("InvalidSchedule", "UnknownTimeZone", "InvalidTimeZone"):
        return _short(f"{r}: {m}", 140)
    return _short(f"{r}: {m}" if m else r, 140) if ev.get("w") else ""


def _stale(cj: dict, state: dict | None, nowts: float) -> bool:
    """Не запускается: последний запуск (или смена расписания) старше двух интервалов."""
    every = interval(str(cj.get("schedule") or ""))
    ls = _int(cj.get("last_schedule"))
    ran = max(ls, changed_at(state, key(cj)))
    return bool(ran) and nowts - ran > max(every * 2, 2 * 3600)


def _all_stale(rep: dict, state: dict | None, nowts: float) -> bool:
    """Стоят все CronJob кластера (хотя бы два): дело не в задании, а в контроллере."""
    live = [c for c in cronjobs(rep) if not c.get("suspend") and _int(c.get("last_schedule"))]
    return len(live) >= 2 and all(_stale(c, state, nowts) for c in live)


def _active_pod_reason(rep: dict, cj: dict) -> str:
    """Почему висит идущий Job: под не скачал образ, не встал в расписание и т.п."""
    job = str(cj.get("active_job") or "")
    if not job:
        return ""
    pod = job_logs.find_pod(rep, {"ns": cj.get("ns"), "name": job})
    reason = str((pod or {}).get("reason") or "")
    return reason if reason and reason not in ("Completed",) else ""


def issue(rep: dict, cj: dict, now: datetime, state: dict | None, dumps: bool) -> dict | None:
    """Проблема одного CronJob: {"kind", "why", "detail", "log"} или None.
    kind: fail - прогоны идут и падают, nostart - запусков нет, hang - прогон висит,
    suspend - приостановлен (проблема только у дампа: бэкап встал).
    why - фраза для строки и "Что сломано", detail - то же без глагола (для алерта, где глагол
    в заголовке), log - строка из конца лога упавшего (агент уже прислал, без секретов)."""
    nowts = now.timestamp()
    if cj.get("suspend"):
        return {"kind": "suspend", "why": "приостановлен", "detail": "suspend", "log": ""} if dumps else None
    ls, ok = _int(cj.get("last_schedule")), _int(cj.get("last_success"))
    every = interval(str(cj.get("schedule") or ""))
    if cj.get("active"):
        if not (ls and nowts - ls > max(every * 2, 86400)):
            return None  # прогон идет - рано паниковать
        pod = _active_pod_reason(rep, cj)
        d = _days(nowts - ls)
        if cj.get("policy") == "Forbid":
            detail = f"прошлый Job идет {d}, Forbid не пускает новые"
        else:
            detail = f"прогон идет {d}, новых запусков нет"
        detail += f", под {pod}" if pod else ""
        return {"kind": "hang", "why": detail, "detail": detail, "log": ""}
    last = cj.get("last") if isinstance(cj.get("last"), dict) else {}
    cause = run_cause(last)
    line = _short(last.get("log") or "", _LOG_MAX) if last.get("st") == "failed" else ""
    since = changed_at(state, key(cj))
    stale_after = max(every * 2, 2 * 3600)
    what = "дампа" if dumps else "прогона"
    if ok > 0 and nowts - max(ok, since) > stale_after:
        if _stale(cj, state, nowts):
            why = _ev_why(cj) or ("контроллер не создает Job: стоят все CronJob кластера"
                                  if _all_stale(rep, state, nowts) else "")
            detail = _days(nowts - ls) + (f": {why}" if why else "")
            return {"kind": "nostart", "why": "не запускается " + detail, "detail": detail, "log": ""}
        if last.get("st") == "failed" or cause:
            detail = (cause or "прогон не удался") + f", успешного {what} нет {_days(nowts - ok)}"
            return {"kind": "fail", "why": "упал: " + detail, "detail": detail, "log": line}
        return {"kind": "stale", "why": f"нет успешного {what} {_days(nowts - ok)}",
                "detail": f"нет успешного {what} {_days(nowts - ok)}", "log": ""}
    if ls and (ok == 0 or ls - ok > SLACK) and last.get("st") != "running":
        if cause:
            return {"kind": "fail", "why": "упал: " + cause, "detail": cause, "log": line}
        return {"kind": "fail", "why": "последний прогон не удался", "detail": "прогон не удался", "log": line}
    return None


def issues(rep: dict | None, now: datetime, dumps: bool, state: dict | None = None) -> dict[str, dict]:
    """Проблемные CronJob: {ns/имя: issue()}. dumps=True - только дамп-CronJob'ы, иначе остальные."""
    from app.collector import _looks_dump_cron  # тот же матч дампа, что у аудита бэкапа

    rep = rep or {}
    out = {}
    for cj in cronjobs(rep):
        if _looks_dump_cron(cj) != dumps:
            continue
        if (it := issue(rep, cj, now, state, dumps)) is not None:
            out[key(cj)] = it
    return out
