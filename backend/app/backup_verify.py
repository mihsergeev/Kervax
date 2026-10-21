"""Проверка восстановления бэкапа (helper backup-setup 0.34).

Раз в неделю после успешного бэкапа helper восстанавливает из последнего снапшота несколько
файлов, которые с тех пор не менялись, и сверяет их sha256 с диском, а у каждого движка с
дампами читает из снапшота свежий дамп: его первые мегабайты должны распаковаться в дамп этого
движка. Итог - report.d/backup-verify.json.

Бэкап, который каждую ночь "успешен", но не восстанавливается, - худший вариант: ни один
другой алерт его не покажет.
"""
from __future__ import annotations

import posixpath
import re
from datetime import datetime, timedelta, timezone

from app import tg_fmt

STALE = timedelta(days=10)  # неделя между проверками плюс запас на отложенный бэкап


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def block(rep: dict) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("backup-verify")
    if not isinstance(b, dict) or b.get("v") != 1 or b.get("skip") or not b.get("ts"):
        return None
    return b


def _ts(b: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(b.get("ts") or 0), timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def problems(rep: dict, now: datetime) -> list[tuple[int, str]]:
    """(уровень, текст): 2 - бэкап не восстанавливается, 1 - проверка не прошла или давно не шла."""
    b = block(rep)
    if b is None:
        return []
    out: list[tuple[int, str]] = []
    files = b.get("files") if isinstance(b.get("files"), dict) else {}
    bad = [str(x) for x in files.get("bad") or []]
    if bad:
        n = len(bad)
        out.append((2, f"проверка восстановления: {n} {_plural(n, 'файл', 'файла', 'файлов')} из снапшота "
                       f"{'не совпал' if _plural(n, 1, 2, 2) == 1 else 'не совпали'} с диском ({', '.join(bad[:3])})"))
    for d in b.get("dumps") or []:
        if isinstance(d, dict) and d.get("ok") is False:
            out.append((2, f"дамп {d.get('engine') or '?'} из снапшота не читается ({d.get('file') or '?'})"))
    err = str(b.get("err") or "").strip()
    if err:
        out.append((1, f"проверка восстановления не выполнилась: {err[:160]}"))
    ts = _ts(b)
    if ts is not None and now - ts > STALE:
        days = (now - ts).days
        out.append((1, f"восстановление не проверялось {days} {_plural(days, 'день', 'дня', 'дней')}"))
    out.sort(key=lambda x: -x[0])
    return out


# ---- короткий алерт в Telegram (формат A, app/tg_fmt.py) ----
# "Не прошла проверка восстановления / нет бинарника restic": ошибка restic словами, без строки лога

# (что ищем в ошибке, как сказать) - по порядку: первое совпадение
_ERRS = (
    (re.compile(r"failed to execute \S*restic\S*: No such file|restic\S*: (command )?not found|"
                r"restic\S*: No such file", re.I), "нет бинарника restic"),
    (re.compile(r"failed to execute \S*restic\S*: permission denied", re.I), "нет прав на запуск restic"),
    (re.compile(r"permission denied", re.I), "нет прав доступа"),
    (re.compile(r"wrong password|no key found", re.I), "не подходит пароль репозитория"),
    (re.compile(r"already locked|unable to create lock", re.I), "репозиторий заблокирован"),
    (re.compile(r"does not exist|unable to open config|is there a repository", re.I), "репозиторий не найден"),
    (re.compile(r"no space left", re.I), "кончилось место"),
    (re.compile(r"connection refused|timed? ?out|no route to host|dial tcp|network is unreachable", re.I),
     "нет связи с репозиторием"),
    (re.compile(r"no (matching )?snapshots?", re.I), "нет снапшотов"),
)


def _err_short(err: str) -> str:
    for rx, say in _ERRS:
        if rx.search(err):
            return say
    # незнакомая ошибка - ее начало, до 60 символов по словам
    err = re.sub(r"\s+", " ", err).strip()
    if len(err) <= 60:
        return err
    return err[:60].rsplit(" ", 1)[0] + " ..."


def _short_one(text: str) -> tuple[str, list[str], str]:
    """Строка problems() -> (что случилось, конкретика, короткое название для "Еще")"""
    t = str(text)
    if t.startswith("проверка восстановления: "):
        m = re.search(r"\(([^()]*)\)$", t)
        files = [posixpath.basename(f) or f for f in (m.group(1).split(", ") if m else [])]
        n = re.match(r"проверка восстановления: (\d+)", t)
        more = int(n.group(1)) - len(files) if n else 0
        fact = (f"файлы не совпали с диском: {tg_fmt.names(files)}" + (f" и еще {more}" if more > 0 else "")
                if files else "файлы из снапшота не совпали с диском")
        return "Бэкап не восстанавливается", [fact], "файлы не совпали"
    m = re.match(r"дамп (\S+) из снапшота не читается", t)
    if m:
        return "Бэкап не восстанавливается", [f"дамп {m.group(1)} из снапшота не читается"], f"дамп {m.group(1)} не читается"
    if t.startswith("проверка восстановления не выполнилась"):
        err = t.partition(": ")[2]
        return "Не прошла проверка восстановления", [_err_short(err)] if err else [], "проверка не прошла"
    if t.startswith("восстановление не проверялось"):
        return tg_fmt.cap(t), [], "давно не проверялось"
    what, facts = tg_fmt.split_detail(t)
    return what, facts, tg_fmt.low(what)


def tg_short(bv: list[tuple[int, str]]) -> tuple[str, list[str]] | None:
    """Короткий алерт по problems() (уровень, текст): что - про первую беду (худшую), остальные
    короткими названиями"""
    if not bv:
        return None
    what, facts, _tag = _short_one(bv[0][1])
    rest = [_short_one(t)[2] for _l, t in bv[1:]]
    if rest:
        facts = facts[:1] + [tg_fmt.fact("Еще", tg_fmt.names(rest))]
    return what, facts


def view(rep: dict, now: datetime) -> dict | None:
    """Для карточки бэкапа: когда проверено, что совпало, что не так."""
    b = block(rep)
    if b is None:
        return None
    files = b.get("files") if isinstance(b.get("files"), dict) else {}
    dumps = [d for d in b.get("dumps") or [] if isinstance(d, dict)]
    probs = problems(rep, now)
    ts = _ts(b)
    return {"ts": ts.isoformat() if ts else None, "files_ok": int(files.get("ok") or 0),
            "dumps_ok": [str(d.get("engine")) for d in dumps if d.get("ok")],
            "level": probs[0][0] if probs else 0, "problems": [t for _l, t in probs]}
