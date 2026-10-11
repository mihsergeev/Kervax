"""Проверка восстановления бэкапа (helper backup-setup 0.34).

Раз в неделю после успешного бэкапа helper восстанавливает из последнего снапшота несколько
файлов, которые с тех пор не менялись, и сверяет их sha256 с диском, а у каждого движка с
дампами читает из снапшота свежий дамп: его первые мегабайты должны распаковаться в дамп этого
движка. Итог - report.d/backup-verify.json.

Бэкап, который каждую ночь "успешен", но не восстанавливается, - худший вариант: ни один
другой алерт его не покажет.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
