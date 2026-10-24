"""Свои бэкапы ноды: заданные без панели cron-задания, systemd-таймеры и скрипты с метриками.

Сервер обычно приходит в панель уже со своими бэкапами: pg_dump по крону, таймер с дампом
Neo4j, ансибл-роль, пишущая метрики для node_exporter. Переделывать их под панель незачем,
поэтому панель за ними только СЛЕДИТ. Находит их root-helper (backup-setup, custom-scan) и
кладёт в отчёт агента блок extras["custom-backups"] — без командных строк и тел скриптов,
только имена, расписания, времена, размеры, пути и имена контейнеров. Здесь из этого
получается статус: работает, упал, давно не отрабатывал, не понять.
"""

import re
from datetime import datetime

from app import tg_fmt
from app.schemas import CustomBackupDb, CustomBackupOut

EXTRA_KEY = "custom-backups"

# Самый редкий разумный бэкап — ежемесячный; реже этого расписанию не верим.
_MAX_INTERVAL = 31 * 86400
_DAY = 86400


# Сведения helper'а устарели: он обновляет их раз в 5 минут, и три часа тишины значат, что
# он больше не запускается. Судить по застывшему снимку нельзя — через двое суток он дал бы
# «нет свежего бэкапа» по заданиям, которые на самом деле исправно работают.
_SCAN_STALE = 3 * 3600


def _block(server) -> dict:
    rep = getattr(server, "last_report", None) or {}
    block = ((rep.get("extras") or {}).get(EXTRA_KEY)) or {}
    return block if isinstance(block, dict) else {}


# Файл-флаг для правил Prometheus, а не бэкап: restic_backup_monitoring_disabled.prom кладут, чтобы
# заглушить алерты restic на хосте. helper видит в имени "backup" и отдает его заданием без статуса,
# и в разделе висит вечное "не понять"
_FLAG_RE = re.compile(r"monitoring_disabled|alerts?_(silence|disabled)|silence")


def _is_flag(job: dict) -> bool:
    return job.get("kind") == "metrics" and bool(_FLAG_RE.search(str(job.get("name") or "").lower()))


# Обновлялка, а не бэкап: gitlab-autoupgrade перед каждым шагом делает gitlab-backup и rsync-снимок
# для своего отката, helper видел в теле rsync и отдавал ее своим бэкапом. Запускается она, когда
# есть что обновлять, и следить за ней как за бэкапом незачем. Решает имя: update/upgrade и ни слова
# про бэкап. helper с 0.41 таких не отдает, здесь - для нод со старым helper'ом и только по таймерам:
# у крона сюда доходит одно имя скрипта, а слово про бэкап бывает в аргументах
# ("v-update-sys-queue backup" у HestiaCP - очередь бэкапов), командную строку видит только helper
_UPDATER_RE = re.compile(r"upgrade|update|unattended")
_BACKUP_WORD_RE = re.compile(r"backup|dump|bkp|snapshot")


def _is_updater(job: dict) -> bool:
    if job.get("kind") != "systemd":
        return False
    text = " ".join(str(job.get(k) or "") for k in ("name", "desc", "unit", "script")).lower()
    return bool(_UPDATER_RE.search(text)) and not _BACKUP_WORD_RE.search(text)


def jobs_of(server) -> list[dict]:
    jobs = _block(server).get("jobs")
    return [j for j in (jobs or []) if isinstance(j, dict) and j.get("id")
            and not _is_flag(j) and not _is_updater(j)]


_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}


def _dow_num(v: str) -> int | None:
    v = v.strip().lower()
    if v.isdigit() and int(v) <= 7:
        return int(v) % 7
    return _DOW_NAMES.get(v[:3])


def dow_gap_days(dow: str) -> int | None:
    """Самый длинный перерыв между днями недели cron (3,6 -> 4: с субботы до среды), в днях.
    None - поле не разобрать. Диапазон с переходом через неделю (fri-mon) cron не понимает."""
    days: set[int] = set()
    for part in dow.split(","):
        rng, _, st = part.partition("/")
        step = int(st) if st.isdigit() and int(st) > 0 else 1
        if rng == "*":
            a, b = 0, 6
        elif "-" in rng:
            x, y = rng.split("-", 1)
            a, b = _dow_num(x), _dow_num(y)
            if b == 0 and y.strip() == "7":
                b = 7  # 1-7: до воскресенья включительно
        else:
            a = b = _dow_num(rng)
        if a is None or b is None or b < a:
            return None
        days.update(d % 7 for d in range(a, b + 1, step))
    if not days:
        return None
    s = sorted(days)
    return max([y - x for x, y in zip(s, s[1:])] + [s[0] + 7 - s[-1]])


def cron_interval(sched: str) -> int:
    """Грубая оценка интервала cron-расписания (5 полей или @daily) в секундах."""
    s = (sched or "").strip()
    macros = {"@hourly": 3600, "@daily": _DAY, "@midnight": _DAY, "@weekly": 7 * _DAY,
              "@monthly": 28 * _DAY, "@yearly": 365 * _DAY, "@annually": 365 * _DAY}
    if s in macros:
        return macros[s]
    p = s.split()
    if len(p) != 5:
        return _DAY
    minute, hour, dom, _mon, dow = p
    # Дни недели решают раньше всего: задание по средам и субботам молчит до 4 дней, сколько бы
    # раз в день оно ни шло. Раньше любой список дней считался "раз в неделю"
    if dow != "*":
        gap = dow_gap_days(dow)
        if gap is None:
            return 7 * _DAY
        if gap >= 2:
            return gap * _DAY

    def step(v: str) -> int:
        try:
            return max(int(v.split("/", 1)[1]), 1)
        except (IndexError, ValueError):
            return 1

    if minute == "*":
        return 60
    if minute.startswith("*/"):
        return step(minute) * 60
    if hour == "*":
        return 3600
    if hour.startswith("*/"):
        return step(hour) * 3600
    if "," in hour:
        return max(_DAY // (hour.count(",") + 1), 3600)
    if dom.startswith("*/"):
        return step(dom) * _DAY
    if dom != "*":
        return 28 * _DAY
    return _DAY


def calendar_interval(cal: str) -> int:
    """Грубая оценка интервала systemd OnCalendar."""
    c = (cal or "").strip().lower()
    named = {"minutely": 60, "hourly": 3600, "daily": _DAY, "weekly": 7 * _DAY,
             "monthly": 28 * _DAY, "quarterly": 90 * _DAY, "yearly": 365 * _DAY,
             "annually": 365 * _DAY}
    if c in named:
        return named[c]
    if not c:
        return _DAY
    parts = c.split()
    # "Mon *-*-* 03:00:00" / "Mon..Fri ..." — день недели в начале
    if parts and parts[0][:3] in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        return 7 * _DAY if "," not in parts[0] and ".." not in parts[0] else _DAY
    tm = parts[-1] if ":" in parts[-1] else ""
    date = parts[0] if len(parts) > 1 or "-" in parts[0] else "*-*-*"
    hh = tm.split(":")[0] if tm else ""
    if hh in ("*",) or hh.startswith("*/"):
        return 3600
    if "," in hh:
        return max(_DAY // (hh.count(",") + 1), 3600)
    d = date.split("-")
    if len(d) == 3 and d[2] not in ("*",) and not d[2].startswith("*/"):
        return 28 * _DAY
    return _DAY


def _interval(job: dict) -> int:
    kind = job.get("kind")
    if kind == "systemd":
        nxt, prev = int(job.get("next_ts") or 0), int(job.get("prev_ts") or 0)
        if nxt > prev > 0:
            return min(max(nxt - prev, 3600), _MAX_INTERVAL)
        return min(calendar_interval(job.get("schedule") or ""), _MAX_INTERVAL)
    if kind == "cron":
        return min(cron_interval(job.get("schedule") or ""), _MAX_INTERVAL)
    return _DAY  # одни метрики без расписания: считаем ежедневным


def stale_after(job: dict) -> int:
    """Когда молчание — уже проблема: два интервала расписания, но не меньше двух часов.
    Ежедневный бэкап имеет право не отработать одну ночь (перезагрузка, окно обслуживания),
    а вот второй пропуск подряд — это уже поломка, а не случайность."""
    return max(_interval(job) * 2, 2 * 3600)


def _ago(seconds: float) -> str:
    if seconds < 2 * 3600:
        return f"{max(int(seconds // 60), 1)} мин"
    if seconds < 2 * _DAY:
        return f"{int(seconds // 3600)} ч"
    return f"{seconds / _DAY:.1f} дн".replace(".0 ", " ")


def evaluate(job: dict, now: datetime, ignored: set[str], fresh: bool = True) -> CustomBackupOut:
    nowts = now.timestamp()
    ok = int(job.get("ok") if job.get("ok") is not None else -1)
    ok_ts = int(job.get("ok_ts") or 0)
    run_ts = int(job.get("run_ts") or 0)
    after = stale_after(job)
    status, problem = "unknown", ""
    if job.get("id") in ignored:
        status = "ignored"
    elif not fresh:
        pass  # снимок застыл: ни «работает», ни «упал» по нему утверждать нельзя
    elif job.get("running"):
        status = "running"
    elif job.get("kind") == "systemd" and job.get("enabled") is False:
        # Таймер выключают руками: задание только ставят или его прогоны на время сняли, человек
        # об этом знает. Беда - только когда бэкап уже работал и свежего нет дольше обычного.
        # Выключенный и ни разу не запускавшийся таймер - недоделанная установка, не алерт
        last = max(ok_ts, run_ts, int(job.get("prev_ts") or 0))
        if last > 0 and nowts - last > after:
            status, problem = "disabled", f"таймер выключен, последний прогон {_ago(nowts - last)} назад"
        else:
            status = "off"
    elif ok == 0:
        fail = job.get("fail") or ""
        if fail == "state":
            status, problem = "failed", "скрипт отметил последний запуск как неудачный"
        elif fail == "state_old":
            # скрипт пишет итог после каждого прогона, а после последнего не записал ничего
            status, problem = "failed", "последний запуск не записал итог — скрипт, похоже, прервался"
        elif fail == "restic":
            status, problem = "failed", "последний запуск не сохранил снимок restic"
        else:
            res = (job.get("result") or "").strip()
            why = {"timeout": "не уложился в таймаут", "exit-code": "завершился с ошибкой",
                   "signal": "убит сигналом", "core-dump": "упал"}.get(res, "завершился с ошибкой")
            status, problem = "failed", f"последний прогон {why}"
        bad = [d.get("name") for d in (job.get("dbs") or []) if d.get("ok") == 0]
        if bad:
            problem += ": " + ", ".join(str(b) for b in bad[:5])
    elif ok_ts > 0:
        age = nowts - ok_ts
        if age > after:
            status, problem = "stale", f"нет свежего бэкапа {_ago(age)}"
        else:
            status = "ok"
    elif ok == 1:
        status = "ok"
    elif job.get("kind") == "cron" and job.get("run_src") == "journal" and run_ts > 0:
        # Итога не видно, но запуски видны по журналу cron. Тишина в журнале — это уже
        # не «не понять», а поломка: cron не запускает задание (снят, сломан, нода лежала).
        # Время записи лога так не читаем: скрипт может писать в лог только при ошибке.
        age = nowts - run_ts
        if age > after:
            status, problem = "stale", f"cron не запускал задание {_ago(age)}"
        else:
            status = "ran"
    dbs = [
        CustomBackupDb(
            name=str(d.get("name") or "")[:120], ok=int(d.get("ok") if d.get("ok") is not None else -1),
            ts=int(d.get("ts") or 0), size_bytes=int(d.get("size_bytes") or 0),
        )
        for d in (job.get("dbs") or [])[:30] if isinstance(d, dict)
    ]
    return CustomBackupOut(
        id=str(job.get("id"))[:120],
        kind=str(job.get("kind") or "")[:16],
        name=str(job.get("name") or "")[:200],
        desc=str(job.get("desc") or "")[:200],
        schedule=str(job.get("schedule") or "")[:120],
        engines=[str(e)[:40] for e in (job.get("engines") or [])][:8],
        files=bool(job.get("files")),
        containers=[str(c)[:120] for c in (job.get("containers") or [])][:8],
        status=status,
        problem=problem,
        ok_ts=ok_ts,
        run_ts=run_ts,
        next_ts=int(job.get("next_ts") or 0),
        size_bytes=int(job.get("size_bytes") or 0),
        duration_sec=int(job.get("duration_sec") or 0),
        dir=str(job.get("dir") or "")[:300],
        log=str(job.get("log") or "")[:300],
        script=str(job.get("script") or "")[:300],
        unit=str(job.get("unit") or "")[:200],
        result=str(job.get("result") or "")[:40],
        metrics=str(job.get("metrics") or "")[:300],
        run_src=str(job.get("run_src") or "")[:16],
        state=str(job.get("state") or "")[:300],
        evidence=str(job.get("evidence") or "")[:16],
        fail=str(job.get("fail") or "")[:16],
        dbs=dbs,
        stale_after=after,
        ignored=status == "ignored",
        scan_stale=not fresh,
    )


def views(server, now: datetime) -> list[CustomBackupOut]:
    ignored = set(getattr(server, "custom_backup_ignored", None) or [])
    scanned = int(_block(server).get("ts") or 0)
    fresh = now.timestamp() - scanned <= _SCAN_STALE
    return [evaluate(j, now, ignored, fresh) for j in jobs_of(server)]


_PROBLEM = ("failed", "stale", "disabled")


def problems(server, now: datetime) -> list[str]:
    """«имя (что не так)» по заданиям, требующим внимания. Для алерта и главной."""
    return sorted(
        f"{v.name} ({v.problem})" for v in views(server, now) if v.status in _PROBLEM
    )


# ---- короткий алерт в Telegram (формат A, app/tg_fmt.py) ----
# "Упал свой бэкап files-sync": что и какое задание, конкретика только если она есть (таймаут, базы)

_AGO = re.compile(r"(\d+(?:\.\d+)?) (мин|ч|дн)")
_AGO_S = {"мин": 60, "ч": 3600, "дн": _DAY}


def _ago_back(text: str) -> str:
    """"нет свежего бэкапа 2.5 дн" -> "2 дня" (как пишет tg_fmt.dur)"""
    m = _AGO.search(text)
    return tg_fmt.dur(float(m.group(1)) * _AGO_S[m.group(2)]) if m else ""


def _parse(item: str) -> tuple[str, str]:
    """Строка problems() "имя (что не так)" -> (имя, что не так)"""
    m = re.match(r"^(.*) \((.*)\)$", str(item), re.S)
    return (m.group(1), m.group(2)) if m else (str(item), "")


def names(own: list[str]) -> list[str]:
    """Имена заданий из problems(): о ком алерт (повтор с другим заданием - новость)"""
    return sorted({_parse(x)[0] for x in own})


def _short_one(item: str) -> tuple[str, list[str]]:
    """Строка problems() -> (что случилось, конкретика)"""
    name, problem = _parse(item)
    problem, _, bad = problem.partition(": ")
    facts = [tg_fmt.fact("Базы", tg_fmt.names(bad.split(", ")))] if bad else []
    if problem.startswith("таймер выключен"):
        ago = _ago_back(problem)
        return f"Свой бэкап {name} не запускается", [f"таймер выключен, последний прогон {ago} назад" if ago else "таймер выключен"]
    if problem.startswith("нет свежего бэкапа"):
        ago = _ago_back(problem)
        return f"Свой бэкап {name} давно не отрабатывал", [f"последний успешный {ago} назад"] if ago else []
    if problem.startswith("cron не запускал"):
        ago = _ago_back(problem)
        return f"Cron не запускает свой бэкап {name}", [f"последний запуск {ago} назад"] if ago else []
    detail = {"не уложился в таймаут": "не уложился в таймаут", "убит сигналом": "убит сигналом",
              "не записал итог": "прервался, итог не записан", "не сохранил снимок restic": "снимок restic не сохранен"}
    why = next((v for k, v in detail.items() if k in problem), "")
    return f"Упал свой бэкап {name}", ([why] if why else []) + facts


def tg_short(own: list[str]) -> tuple[str, list[str]] | None:
    """Короткий алерт по problems() (строки "имя (что не так)"): что - про первое задание, остальные
    именами"""
    if not own:
        return None
    what, facts = _short_one(own[0])
    rest = [_parse(x)[0] for x in own[1:]]
    if rest:
        facts = facts[:1] + [tg_fmt.fact("Еще", tg_fmt.names(rest))]
    return what, facts


def has_file_backup(server, now: datetime) -> bool:
    """Есть ли свой файловый бэкап ноды (restic/borg/rsync…): тогда «бэкап не настроен»
    неправда. Дамп одной базы бэкапом всей ноды не считается."""
    return any(v.files and v.status != "ignored" for v in views(server, now))


def label(v: CustomBackupOut) -> str:
    where = {"cron": "cron", "systemd": "таймер", "metrics": "метрики"}.get(v.kind, v.kind)
    return f"{v.name} ({where}{f' {v.schedule}' if v.schedule else ''})"


def db_cover(server, now: datetime, engine: str, instance: str, instances: int):
    """Задание, которое бэкапит этот экземпляр СУБД, или None.

    Экземпляр — имя контейнера (пусто — нативная установка или под). Засчитываем, если
    задание явно ссылается на этот контейнер; без ссылок — только когда такой базы на
    ноде ровно одна. Иначе дамп одной postgres закрывал бы находку по второй — ровно та
    ошибка, из-за которой раньше молча пропадали из аудита непокрытые базы."""
    for v in views(server, now):
        if v.status == "ignored" or engine not in v.engines:
            continue
        if instance and instance in v.containers:
            return v
        if not v.containers and (not instance or instances == 1):
            return v
    return None
