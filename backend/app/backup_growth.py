"""Сколько каждый прогон бэкапа добавил в репозиторий (helper backup-setup 0.32, report.d).

restic в конце прогона пишет "Added to the repository: 221.756 GiB (96.562 GiB stored)": первое -
новые данные до сжатия, второе - сколько реально легло в репозиторий. Helper собирает это из
журнала юнита по каждому прогону и держит последние 40. Панель рисует прирост, показывает, кто
раздувает бэкап-сервер, и предупреждает, когда нода вдруг добавила в разы больше обычного: на
feed-a и stats-a уходило по 200-300 ГБ за ночь, и этого не было видно, пока бэкап-сервер
не начал заполняться.
"""

from datetime import datetime

EXTRA_KEY = "backup-runs"
_MIN_HISTORY = 5  # прогонов до последнего: меньше - не с чем сравнивать
_HISTORY = 14  # с какими прогонами сравниваем последний
_JUMP_RATIO = 3.0  # во сколько раз больше обычного
_JUMP_MIN = 10 * 2**30  # и хотя бы на 10 ГиБ: скачок со 100 МБ до 400 МБ - не повод
_FRESH = 36 * 3600  # последний прогон старше - судить по нему уже незачем


def runs(server) -> list[dict]:
    """Прогоны по времени: {ts, add, st, proc, files, fnew, fchg, dur}."""
    block = (((server.last_report or {}).get("extras") or {}).get(EXTRA_KEY)) or {}
    if not isinstance(block, dict) or block.get("v") != 1:
        return []
    out = [r for r in block.get("runs") or [] if isinstance(r, dict) and isinstance(r.get("ts"), (int, float))]
    return sorted(out, key=lambda r: r["ts"])


def median(vals: list[float]) -> float:
    v = sorted(vals)
    if not v:
        return 0.0
    m = len(v) // 2
    return float(v[m]) if len(v) % 2 else (v[m - 1] + v[m]) / 2


def fmt_size(b: float) -> str:
    """Байты коротко, как в тексте алерта: 192 ГБ, 3.4 ГБ, 850 МБ."""
    gb = b / 2**30
    if gb >= 100:
        return f"{gb:.0f} ГБ"
    if gb >= 1:
        return f"{gb:.1f} ГБ"
    return f"{b / 2**20:.0f} МБ"


def usual(rs: list[dict]) -> float:
    """Обычный прирост за прогон (после сжатия): медиана прошлых прогонов без последнего."""
    return median([float(r.get("st") or 0) for r in rs[-1 - _HISTORY:-1]])


def jump(server, now: datetime) -> dict | None:
    """Последний прогон добавил в разы больше обычного: {level, text, st, usual, ts}. None - норма,
    мало истории или прогон давний."""
    rs = runs(server)
    if len(rs) < _MIN_HISTORY + 1:
        return None
    last = rs[-1]
    if now.timestamp() - float(last["ts"]) > _FRESH:
        return None
    st, base = float(last.get("st") or 0), usual(rs)
    if st < _JUMP_RATIO * base or st - base < _JUMP_MIN:
        return None
    return {
        "level": 1, "st": st, "usual": base, "ts": last["ts"],
        "text": f"бэкап добавил {fmt_size(st)} за прогон (обычно {fmt_size(base)})",
    }
