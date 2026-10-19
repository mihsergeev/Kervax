"""Что именно было в алерте: короткое "что" и "почему" из текста, как его получил человек.

В истории алертов (AlertEvent) есть вид и объект (сервер или монитор), а конкретика - какой юнит
упал, какой под перезапускается, какой репозиторий опоздал, кто ест CPU - только в тексте. Отчет
"что шумело" без нее бесполезен: строка "Systemd: упал юнит, 4" не говорит, что чинить.

Тексты собираются по одному образцу: "<значки> <объект> — <что>: <подробности>", несколько бед
через "; ", строки "↳ ..." ниже - причина или строка лога. Частые виды разобраны отдельно,
остальное - общим правилом. Одно сообщение может дать несколько строк (три пода, пять
репозиториев). Ключ для группировки - "что" без чисел: "диск 85%" и "диск 91%" - одна беда.
"""

import re

WHAT_MAX = 90
NOTE_MAX = 110
# больше строк из одного сообщения не берем: 23 опоздавших репозитория - это одна беда сервера
_ITEMS_MAX = 8

_HEAD = re.compile(r"^[^\w(/]+")
_AGO = re.compile(r",?\s*(?:последний\s+)?\d+\s*(?:мин|ч|дн)\.?\s+назад")
# числа отдельными словами ("85%", "1 ч 4 мин"), но не внутри имен: agent-2-1, pod-0, sdb1
_NUM = re.compile(r"(?<![\w.-])\d+(?:[.,]\d+)?(?![\w-])")
# хвосты имен подов: <deployment>-<хеш replicaset>-<5 знаков>, у Job из CronJob - -<время>
_POD = re.compile(r"-[bcdfghjklmnpqrstvwxz2456789]{8,10}-[bcdfghjklmnpqrstvwxz2456789]{5}$")
_JOB = re.compile(r"-\d{8,}$")
_PCT = re.compile(r"^(.+?) (\d+(?:\.\d+)?)% ≥ (\d+(?:\.\d+)?)%(?:\s*-\s*(.*))?$")
_NGINX_TS = re.compile(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d \[\w+\] \d+#\d+: ")
_KEYPRINT = re.compile(r":? *(?:ssh-)?[A-Z0-9-]+ SHA256:\S+")


def _cut(s: str, n: int) -> str:
    s = " ".join(s.split()).rstrip(" ,;:-")
    return s if len(s) <= n else s[: n - 3].rstrip(" ,;:-") + "..."


def key(what: str) -> str:
    return _NUM.sub("#", what.lower())


def _obj(name: str) -> str:
    return _JOB.sub("", _POD.sub("", name))


def _split(s: str, sep: str = ", ") -> list[str]:
    """Разбить список по sep, не разрезая скобки: "a (x, y), b" -> ["a (x, y)", "b"]."""
    out, depth, cur, i = [], 0, "", 0
    while i < len(s):
        if s.startswith(sep, i) and depth == 0:
            out.append(cur)
            cur, i = "", i + len(sep)
            continue
        ch = s[i]
        depth += ch == "("
        depth -= ch == ")" and depth > 0
        cur += ch
        i += 1
    out.append(cur)
    return [x.strip() for x in out if x.strip()]


def _paren(s: str) -> tuple[str, str]:
    """"name (почему)" -> ("name", "почему"); без скобок в конце - (s, "")."""
    m = re.match(r"^(.*?)\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)$", s)
    return (m.group(1), m.group(2)) if m and m.group(1) else (s, "")


def _body(text: str) -> tuple[str, list[str]]:
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    if not lines:
        return "", []
    first = lines[0]
    body = first.split(" — ", 1)[1] if " — " in first else _HEAD.sub("", first)
    body = re.sub(r"\s*·\s*монитор\s*$", "", body)
    body = _AGO.sub("", body).strip()
    extra = [ln.lstrip("↳").strip() for ln in lines[1:]]
    # "↳ и еще сайты на том же сервере" - не подробность этой беды
    return body, [ln for ln in extra if ln and not ln.startswith("и ")]


def _part(s: str, sep: str = ": ") -> tuple[str, str]:
    """Разрезать по первому sep вне скобок: двоеточие внутри "(на хосте: ...)" не граница."""
    depth = 0
    for i, ch in enumerate(s):
        depth += ch == "("
        depth -= ch == ")" and depth > 0
        if depth == 0 and s.startswith(sep, i):
            return s[:i], s[i + len(sep):]
    return s, ""


def _generic(item: str, extra: list[str]) -> list[tuple[str, str]]:
    head, tail = _part(item)
    if not tail:
        what, why = _paren(head)
        if not why and len(what) > 40 and ", " in what:
            # "репозиторий чартов недоступен, развернутое работает, ..." - суть до запятой
            what, why = what.split(", ", 1)
            why = extra[0] if extra else why
        return [(what, why or (extra[0] if extra else ""))]
    # "дамп-CronJob не отрабатывает (приостановлен / прогон упал): ns/name (почему)"
    head = re.sub(r"\s*\([^()]*\)$", "", head)
    tail = _NGINX_TS.sub("", tail)
    tail = re.sub(r"^\d+ шт\.: ", "", tail)
    # пояснение в конце ("- взлом даст root на сервере") и второе двоеточие - уже не суть
    tail = _part(re.sub(r" - [^()]*$", "", tail))[0]
    parts = _split(tail)
    if any(_paren(p)[1] for p in parts):
        # список объектов с причинами: каждый - своя строка
        out = []
        for p in parts[:_ITEMS_MAX]:
            p = re.sub(r" и еще \d+$", "", p)
            name, why = _paren(p)
            out.append((f"{head}: {_obj(name)}", why))
        return out
    tail = tail.split(". ")[0].split(" - ")[0]
    return [(head, tail or (extra[0] if extra else ""))]


def _units(body: str, extra: list[str]) -> list[tuple[str, str]] | None:
    m = re.match(r"^упал (\S+): (.+)$", body)
    if m:
        unit, why = m.groups()
        if extra and (why.startswith("код") or why.startswith("ошибка")):
            why += ": " + extra[0].removeprefix(unit + ": ")
        return [(f"упал {unit}", why)]
    m = re.match(r"^упали юниты: (.+?)(?: и еще \d+)?$", body)
    if m:
        return [(f"упал {u}", w) for u, w in (_paren(p) for p in _split(m.group(1))[:_ITEMS_MAX])]
    return None


def _restarts(body: str, extra: list[str]) -> list[tuple[str, str]] | None:
    out = [(f"перезапускается под {_obj(p)}", f"{n} раз за сутки")
           for p, n in re.findall(r"под (\S+) перезапускался (\d+) раз", body)]
    return out or None


def _access(body: str, extra: list[str]) -> list[tuple[str, str]] | None:
    body = re.sub(r"^изменился доступ: ", "", body)
    out = []
    for item in _split(body, "; ")[:_ITEMS_MAX]:
        item = _KEYPRINT.sub("", item).split(", раньше")[0]
        out.append(_paren(item))
    return out


def _pct(body: str) -> list[tuple[str, str]] | None:
    # "CPU ходит вокруг порога: 3 срабатывания за 6 ч" - та же беда, что "CPU 96% >= 90%"
    flap = re.match(r"^(.+?) ходит вокруг порога", body)
    if flap:
        return [(f"{flap.group(1)} выше порога", "")]
    m = _PCT.match(body)
    if not m:
        return None
    name, cur, thr, det = m.groups()
    names = []
    if det:
        det = det.split(";")[0]
        det = det.split(": ", 1)[-1]
        det = re.sub(r"^.*? у ", "", det)
        names = [re.sub(r"\s+~?[\d.,]+\s*\S*$", "", x.strip()) for x in det.split(", ")]
    note = f"{cur}%" + (f": {', '.join(n for n in names[:3] if n)}" if names else "")
    return [(f"{name} выше порога", note)]


_STUCK_ONE = re.compile(r"^завис (.+?) \(PID \d+\) в ожидании диска или сети (.+?)(?:, wchan (\S+))?(?: - .*)?$")
_STUCK_ITEM = re.compile(r"^(.+?) \(PID \d+(?:, wchan (\S+))?\) (.+)$")


def _pressure(body: str, extra: list[str]) -> list[tuple[str, str]] | None:
    out = []
    for item in _split(body, "; "):
        head, _, tail = item.partition(": ")
        # зависшие (агент 2.40): "завис postgres (PID 4242) в ожидании диска или сети 7 мин, wchan x"
        # или перечень "зависли в ожидании диска или сети 3 процесса: postgres (PID 1) 9 мин, ..."
        one = _STUCK_ONE.match(item)
        if one:
            out.append((f"завис {one.group(1)}", one.group(2) + (f", wchan {one.group(3)}" if one.group(3) else "")))
            continue
        if head.startswith("зависли в ожидании диска или сети ") and "D-state" not in head:
            # хвост " - sda занят ..." (причина давления на диск) и " и еще N" - не процессы
            for p in _split(re.sub(r" и еще \d+$", "", tail.split(" - ")[0]))[:3]:
                m = _STUCK_ITEM.match(p)
                if m:
                    out.append((f"завис {m.group(1)}", m.group(3) + (f", wchan {m.group(2)}" if m.group(2) else "")))
            continue
        if "D-state" in head:
            procs = [re.sub(r"\s*\(\d+\)$", "", x) for x in _split(tail)]
            out.append(("процессы зависли в D-state", ", ".join(procs[:3])))
        else:
            dev = re.search(r"- (\w+) занят", tail)
            why = re.sub(r"^все процессы разом ", "", tail.split(" - ")[0])
            if dev and head.startswith("диск "):
                head = f"диск {dev.group(1)} {head[5:]}"
            out.append((head, why))
    return out or None


def _web5xx(body: str, extra: list[str]) -> list[tuple[str, str]] | None:
    pct = re.match(r"^([\d.]+)% запросов", body)
    site = re.search(r"Больше всего: ([^\s(]+)", body)
    path = re.search(r"Чаще всего падает: (\S+)", body)
    # "Почему: app-1:8000 не принимает соединения - 44, ..." (helper 0.26): первая причина без
    # числа; адрес с точками, поэтому до ", ", ". Заглавной" или конца
    why = re.search(r"Почему: (.+?)(?: - \d+)?(?:, |\. (?=[А-ЯA-Z])|\.?$)", body)
    if not pct:
        return None
    what = f"ошибки 5xx на {site.group(1)}" if site else "ошибки 5xx"
    return [(what, f"{pct.group(1)}% запросов" + (f", чаще {path.group(1)}" if path else "")
             + (f", {why.group(1)}" if why else ""))]


def _exposed(body: str, extra: list[str]) -> list[tuple[str, str]] | None:
    out = []
    for item in _split(re.sub(r"^открыто в интернет: ", "", body), "; ")[:_ITEMS_MAX]:
        m = re.match(r"^(.+? \d+)\s*(?:\(([^)]*)\))?", item)
        if m:
            out.append((f"открыт в интернет {m.group(1)}", m.group(2) or ""))
    return out or None


def _cert(body: str, extra: list[str]) -> list[tuple[str, str]] | None:
    m = re.match(r"^сертификат (.+?) (истек\S*|истекает[^(]*?)\s*(?:\(|,|$)", body)
    return [(f"сертификат {m.group(1)}", m.group(2))] if m else None


def _domain(body: str, extra: list[str]) -> list[tuple[str, str]] | None:
    m = re.match(r"^регистрация домена (\S+) (истекает через .+?|истекла.*?)\.?(?: \(|$)", body)
    return [(f"домен {m.group(1)}", m.group(2))] if m else None


_KINDS = {
    "units": _units,
    "kube_restarts": _restarts,
    "access_change": _access,
    "pressure": _pressure,
    "web_5xx": _web5xx,
    "domain": _domain,
    "exposed": _exposed,
    "web_cert": _cert,
}


def merge_notes(notes: list[str]) -> str:
    """"почему" для строки отчета из всех срабатываний (по времени). "96%: ffmpeg, prometheus"
    из разных срабатываний - пик и кто чаще всех ел; остальное - последнее."""
    if not notes:
        return ""
    pcts = [m for m in (re.match(r"^(\d+(?:\.\d+)?)%(?:: (.*))?$", n) for n in notes) if m]
    if pcts:
        top = max(float(m.group(1)) for m in pcts)
        names: dict[str, int] = {}
        for m in pcts:
            for x in (m.group(2) or "").split(", "):
                if x:
                    names[x] = names.get(x, 0) + 1
        best = sorted(names, key=lambda x: -names[x])[:3]
        return f"до {top:g}%" + (f": {', '.join(best)}" if best else "")
    return notes[-1]


def items(kind: str, text: str) -> list[tuple[str, str]]:
    """[(что, почему)] из текста алерта. "что" - конкретный объект беды, "почему" - коротко."""
    body, extra = _body(text)
    if not body:
        return []
    out = None
    if kind in _KINDS:
        out = _KINDS[kind](body, extra)
    if not out:
        out = _pct(body)
    if not out:
        out = [x for item in _split(body, "; ") for x in _generic(item, extra)]
    seen, res = set(), []
    for what, why in out[:_ITEMS_MAX]:
        what = what.strip()
        if what and key(what) not in seen:
            seen.add(key(what))
            res.append((_cut(what, WHAT_MAX), _cut(why, NOTE_MAX)))
    return res
