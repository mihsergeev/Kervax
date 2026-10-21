"""Лог упавшего Job прямо в алерте: почему упал, видно в чате, без похода в панель.

Новый упавший Job (его нет в kube_jobs_seen) - просим у агента хвост лога его пода обычной
kube-командой logs (стадия планировщика перед серверными алертами). Алерт ждет ответ до
_WAIT секунд: агент забирает команды раз в секунду, так что ответ обычно готов к следующему
тику. Пода в отчете нет (подчистили по ttl) или агент не ответил - алерт уходит без лога.

Состояние в памяти планировщика: после перезапуска (деплоя) лог просто запросится снова."""
from __future__ import annotations

import html
import re

from app import alerts

_WAIT = 90        # сек ждем лог, дальше алерт без него
_TAIL = 60        # строк просим у агента: последние бывают пустыми или служебными
_JOBS = 2         # логов на один алерт: больше - не влезет в сообщение
_LINES = 15
_CHARS = 1400     # на все логи алерта: сообщение Telegram - до 4096 символов
_LINE_MAX = 200
_GIST_MAX = 100   # суть ошибки в чате - одна строка
_KEEP = 86400

# агент просит логи с timestamps=true: метка в начале строки в чате только мешает
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d) ")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# (id сервера, ключ Job) -> {"at", "asked", "cmd", "pod", "reason", "done", "ok", "log"}
_reqs: dict[tuple[int, str], dict] = {}


def reset() -> None:
    _reqs.clear()


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def job_key(j: dict) -> str:
    return f"{j.get('ns')}/{j.get('name')}@{_int(j.get('ts'))}"


def _is_job_pod(pod: str, job: str) -> bool:
    """Под Job называется <job>-<5 символов>, у индексного Job - <job>-<номер>-<5 символов>.
    Длинное имя Kubernetes обрезает до 58 символов перед случайным хвостом."""
    if re.fullmatch(re.escape(job) + r"-(?:\d+-)?[a-z0-9]{5}", pod):
        return True
    base = f"{job}-"
    return len(base) > 58 and len(pod) == 63 and pod.startswith(base[:58])


def find_pod(rep: dict, job: dict) -> dict | None:
    """Под упавшего Job из отчета агента: сначала упавший, иначе любой его."""
    ns, name = job.get("ns"), str(job.get("name") or "")
    if not name:
        return None
    pods = [p for p in (rep.get("kube") or {}).get("pods") or []
            if isinstance(p, dict) and p.get("ns") == ns and p.get("owner") == "Job"
            and _is_job_pod(str(p.get("name") or ""), name)]
    failed = [p for p in pods if p.get("phase") == "Failed"]
    return (failed or pods or [None])[0]


def tail_text(raw: str, lines: int = _LINES, chars: int = _CHARS) -> str:
    """Последние строки лога без меток времени и цветовых кодов, не длиннее chars."""
    out = []
    for ln in (raw or "").splitlines():
        ln = _CTRL.sub("", _ANSI.sub("", _TS.sub("", ln.rstrip())))
        if not ln.strip():
            continue
        out.append(ln if len(ln) <= _LINE_MAX else ln[:_LINE_MAX] + "...")
    out = out[-lines:]
    while len(out) > 1 and sum(len(x) + 1 for x in out) > chars:
        out.pop(0)
    if out and len(out[0]) > chars:
        out[0] = "..." + out[0][-chars:]
    return "\n".join(out)


# строка с ошибкой: в чат идет она, а не хвост лога
_ERR = re.compile(r"(?i)\b(?:error|exception|fatal|failed|failure|panic|denied|refused|timed? ?out|"
                  r"not found|no such|cannot|can't|unable|invalid|killed)\b|ошибк|не удалось")
# кадры стека: "at fn (file:1:2)", "File "x.py", line 3" - не суть, а где
_FRAME = re.compile(r"^\s+(?:at |File \"|\.\.\.)|^\s*Traceback \(most recent call last\)")
# уровень и служебное в начале строки: "[ERROR]", "ERROR:", "level=error msg="
_LEVEL = re.compile(r"^\s*(?:\[?(?:error|err|fatal|crit(?:ical)?|warn(?:ing)?)\]?:?\s+|level=\w+\s+msg=)",
                    re.I)


def gist(raw: str) -> str:
    """Суть ошибки из лога одной строкой: последняя строка, похожая на ошибку (у Python это
    "ValueError: ...", у Go - "panic: ...", у Node - "Error: ..."), без кадров стека и уровня.
    Похожей нет - пусто: случайная последняя строка ("Done.") в чате только путает."""
    for ln in reversed(tail_text(raw, lines=_TAIL, chars=10 ** 6).splitlines()):
        if _FRAME.search(ln) or not _ERR.search(ln):
            continue
        ln = _LEVEL.sub("", ln).strip().strip('"')
        if ln:
            return ln if len(ln) <= _GIST_MAX else ln[:_GIST_MAX].rstrip() + "..."
    return ""


def entry(sid: int, key: str, now_ts: float) -> dict:
    return _reqs.setdefault((sid, key), {"at": now_ts, "asked": False, "cmd": None, "pod": "",
                                         "reason": "", "done": False, "ok": False, "log": ""})


def ask(sid: int, rep: dict, job: dict, now_ts: float) -> tuple[str, str] | None:
    """Стадия планировщика: какой под спросить про этот Job (ns, имя), None - не надо.
    Пода нет - лога не будет, алерт ждать нечего."""
    r = entry(sid, job_key(job), now_ts)
    if r["asked"] or r["done"]:
        return None
    r["asked"] = True
    pod = find_pod(rep, job)
    if pod is None:
        r["done"] = True
        return None
    r["pod"], r["reason"] = str(pod.get("name")), str(pod.get("reason") or pod.get("phase") or "")
    return str(job.get("ns")), r["pod"]


def asked(sid: int, job: dict, cmd_id: int) -> None:
    _reqs[(sid, job_key(job))]["cmd"] = cmd_id


def waiting_cmds() -> list[int]:
    return [r["cmd"] for r in _reqs.values() if r["cmd"] and not r["done"]]


def answered(cmd_id: int, ok: bool, result: str) -> None:
    for r in _reqs.values():
        if r["cmd"] == cmd_id:
            r["done"], r["ok"], r["log"] = True, ok, result or ""


def collect(sid: int, rep: dict, jobs: list[dict], now_ts: float) -> list[dict] | None:
    """Логи для алерта о jobs (первые _JOBS). None - ответ агента еще ждем, алерт отложить.
    Иначе список {"pod", "reason", "ok", "log"} по тем, у кого под нашелся."""
    out, wait = [], False
    for j in jobs[:_JOBS]:
        key = (sid, job_key(j))
        if key not in _reqs and find_pod(rep, j) is None:
            continue  # пода нет и не было: ни спрашивать, ни ждать
        r = entry(sid, job_key(j), now_ts)
        if not r["done"]:
            if now_ts - r["at"] < _WAIT:
                wait = True
            continue
        if r["pod"]:
            out.append({"pod": r["pod"], "reason": r["reason"], "ok": r["ok"], "log": r["log"]})
    return None if wait else out


def attach(msg: alerts.Msg, logs: list[dict]) -> alerts.Msg:
    """Дописать к алерту хвосты логов: строка "лог пода" и сам лог блоком <pre>."""
    parts = []
    budget = _CHARS // max(1, len(logs))
    for x in logs:
        head = f"↳ под {html.escape(x['pod'])}" + (f" ({html.escape(x['reason'])})" if x["reason"] else "")
        text = tail_text(x["log"], chars=budget) if x["ok"] else ""
        if text:
            parts.append(f"\n{head}, конец лога:\n<pre>{html.escape(text)}</pre>")
        elif x["ok"]:
            parts.append(f"\n{head}: в лог ничего не написал")
        else:
            parts.append(f"\n{head}: лог получить не удалось")
    if not parts:
        return msg
    # у короткого алерта лог только в полном тексте: в чате он не читается, смотрят в панели.
    # В чат - одна строка с сутью ошибки, если она в логе нашлась
    g = next((gist(x["log"]) for x in logs if x["ok"] and gist(x["log"])), "")
    from app import tg_fmt
    return msg.tail("".join(parts), "\n" + html.escape(tg_fmt.clean(g)) if g else "")


def prune(now_ts: float) -> None:
    for k in [k for k, r in _reqs.items() if now_ts - r["at"] > _KEEP]:
        _reqs.pop(k, None)
