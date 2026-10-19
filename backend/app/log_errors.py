"""Ошибки в логах контейнеров и подов: всплеск против обычного часа.

Сервис, который начал падать, обычно первым говорит об этом в своем логе: исключения,
трейсбеки, FATAL базы, [error] nginx. Сотни логов никто не читает, а лог, в котором ошибки есть
всегда, делает grep бесполезным. Helper logerr-setup раз в минуту считает строки с ошибками во
всех логах контейнеров ноды (json-file Docker и логи подов Kubernetes), читая только новые байты,
и кладет счет в 10-минутные корзины за сутки: extras "logerr" - {"v": 1, "ts", "logs", "read",
"skipped", "sources": [{"where", "container", "h1", "d1", "lines1", "span", "last_ts", "last"}]}.
h1 и d1 - строк с ошибками за последний час и сутки, lines1 - всех строк за час, span - сколько
лог под наблюдением (до суток), last - последняя строка с ошибкой (пароли скрыты). В отчете только
логи, где за сутки были ошибки.

Беда - только всплеск: за час не меньше LOG_MIN и в LOG_X раз больше обычного часа (сутки без
последнего часа на число часов). Истории меньше BASE_SPAN - обычного не знаем, нужно LOG_NOBASE.
В LOG_HOT раз больше обычного и от LOG_HOT_MIN - проблема, иначе предупреждение. Лог, где ошибок
всегда много, не алертит, пока их не стало в разы больше. После ушедшего алерта отбой при
половине порогов.

Всплеск должен держаться: с helper'а 0.3 в отчете есть счет по 10-минутным корзинам последнего
часа ("hb", шесть чисел, последнее - текущая неполная), и выше порога (на корзину - шестая часть
часового) должны быть хотя бы SUSTAIN из них. Иначе бот, упершийся в limit_req на пять минут,
держал алерт целый час, пока его строки не выходили из часового окна.

Копии одного ворклоада (поды деплоймента, стейтфулсета, демонсета) - один лог: три реплики
ingress-nginx давали три сообщения подряд, по одному на каждую, перешедшую порог. Обычный час
копии без истории (новый под после выкатки) - ее нынешний час: всплеском она не считается.

Заглушить лог можно насовсем (alert_mutes "logerr:<ключ>") или на время (alert_snoozes с тем же
ключом); ключ - "<где>|<контейнер пода>", например "контейнер shop-php|", у подов - по
ворклоаду: "под shop/web|php". Старые мьюты на отдельный под глушат его ворклоад.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app import podnames
from app.models import Server
from app.queue_watch import where_text

MUTE_PREFIX = "logerr:"
LOG_MIN = 200  # строк с ошибками за час - меньше не шлем
LOG_X = 5  # во столько раз больше обычного часа
# Обычного не знаем - нужно столько. Было 2000: в первые часы после раскатки helper'а пришли
# алерты на ZooKeeper, который давно и всегда сыпет BindException, - без истории постоянный поток не
# отличить от всплеска. 20000 за час (5 строк в секунду) - уже авария, даже если так было всегда
LOG_NOBASE = 20000
LOG_HOT = 20  # во столько раз больше обычного - проблема
LOG_HOT_MIN = 1000
BASE_SPAN = 4 * 3600  # обычный час считаем, когда истории хотя бы столько
MAX_AGE = 600  # блок старше 10 минут - helper встал, судить не по чему
SUSTAIN = 4  # из шести 10-минутных корзин последнего часа выше порога - всплеск держится


def block(rep: dict) -> dict | None:
    b = ((rep or {}).get("extras") or {}).get("logerr")
    if not isinstance(b, dict) or b.get("v") != 1:
        return None
    ts = float(b.get("ts") or 0)
    ref = float((rep or {}).get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
    if ts <= 0 or ref - ts > MAX_AGE:
        return None
    return b


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def label(src: dict) -> str:
    """Где лог словами: "контейнер shop-php", "под shop/web (php)", у копий ворклоада
    "3 пода default/ingress-nginx-controller (controller)" """
    w = where_text(str(src.get("where") or ""))
    n = int(src.get("pods") or 1)
    if n > 1 and w.startswith("под "):
        w = f"{n} {_plural(n, 'под', 'пода', 'подов')} {w[4:]}"
    c = str(src.get("container") or "")
    return f"{w} ({c})" if c else w


def key(src: dict) -> str:
    return f"{where_text(str(src.get('where') or ''))}|{src.get('container') or ''}"


def norm_key(k: str) -> str:
    """Ключ лога отдельного пода ("под ns/web-6b8b56c769-f4k8m|php", так было до 1.4.278) -> ключ
    его ворклоада ("под ns/web|php"); остальные как есть"""
    w, sep, c = str(k).partition("|")
    if not w.startswith("под ") or "/" not in w:
        return str(k)
    ns, _, name = w[4:].partition("/")
    return f"под {ns}/{podnames.workload(name)}{sep}{c}"


def base(src: dict) -> float | None:
    """Обычный час: ошибки за сутки без последнего часа на число часов; None - истории мало"""
    if "base" in src:
        return src["base"]  # у копий ворклоада посчитан при сведении
    span = min(int(src.get("span") or 0), 86400)
    if span < BASE_SPAN:
        return None
    return max(int(src.get("d1") or 0) - int(src.get("h1") or 0), 0) * 3600 / (span - 3600)


def spike(src: dict, held: bool = False) -> int:
    """Уровень всплеска: 0 - нет, 1 - предупреждение, 2 - проблема. held - алерт уже ушел, отбой
    при половине порогов"""
    k = 0.5 if held else 1
    h1, b = int(src.get("h1") or 0), base(src)
    if h1 < LOG_MIN * k:
        return 0
    if b is None:
        if h1 < LOG_NOBASE * k:
            return 0
    elif h1 < LOG_X * b * k:
        return 0
    if not held and not _sustained(src, b):
        return 0
    if b is None:
        return 1
    return 2 if h1 >= LOG_HOT * b and h1 >= LOG_HOT_MIN else 1


def _sustained(src: dict, b: float | None) -> bool:
    """Держится ли всплеск: из корзин последнего часа выше порога хотя бы SUSTAIN. Helper до 0.3
    корзин не шлет - судим по часу, как раньше"""
    hb = src.get("hb")
    if not isinstance(hb, list) or not hb:
        return True
    need = max(LOG_MIN, LOG_NOBASE if b is None else LOG_X * b) / 6
    return sum(1 for v in hb if isinstance(v, (int, float)) and v >= need) >= SUSTAIN


def _n(v: int | float) -> str:
    return f"{int(v):,}".replace(",", " ")


def why(src: dict) -> str:
    h1, b = int(src.get("h1") or 0), base(src)
    if b is None:
        usual = "обычного еще не знаем: истории меньше 4 часов"
    else:
        usual = f"обычно около {_n(round(b))} в час" if b >= 1 else "обычно их почти нет"
    return f"{_n(h1)} строк с ошибками за час, {usual}"


def _muted(s: Server, now: datetime) -> set[str]:
    """Заглушенные ключи; мьют на отдельный под (до 1.4.278) глушит его ворклоад"""
    keys = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    for k, until in (getattr(s, "alert_snoozes", None) or {}).items():
        if not str(k).startswith(MUTE_PREFIX):
            continue
        try:
            u = datetime.fromisoformat(str(until))
        except ValueError:
            continue
        if (u if u.tzinfo else u.replace(tzinfo=timezone.utc)) > now:
            keys.add(k[len(MUTE_PREFIX):])
    return keys | {norm_key(k) for k in keys}


def muted_keys(s: Server, now: datetime) -> set[str]:
    return _muted(s, now)


def _held(s: Server) -> set[str]:
    return {norm_key(str(k)) for k in ((s.alert_state or {}).get("log_errors_sig") or [])}


def _raw(rep: dict) -> list[dict]:
    return [x for x in (block(rep) or {}).get("sources") or [] if isinstance(x, dict)]


def sources(rep: dict) -> list[dict]:
    """Логи из отчета, копии одного ворклоада сведены в один: счет сложен, корзины сложены
    поштучно, пример - самый свежий, "pods" - сколько копий"""
    ctrl = podnames.ctrls(rep)
    groups: dict[tuple[str, str], list[dict]] = {}
    for x in _raw(rep):
        w = str(x.get("where") or "")
        if w.startswith("pod ") and "/" in w:
            ns, _, name = w[4:].partition("/")
            w = f"pod {ns}/{podnames.workload(name, ctrl.get(f'pod:{ns}/{name}', ''))}"
        groups.setdefault((w, str(x.get("container") or "")), []).append(x)
    out = []
    for (w, c), ms in groups.items():
        if len(ms) == 1:
            out.append({**ms[0], "where": w})
            continue
        bases = [base(m) for m in ms]
        hbs = [m.get("hb") for m in ms]
        last = max(ms, key=lambda m: int(m.get("last_ts") or 0))
        g = {"where": w, "container": c, "pods": len(ms),
             "h1": sum(int(m.get("h1") or 0) for m in ms), "d1": sum(int(m.get("d1") or 0) for m in ms),
             "lines1": sum(int(m.get("lines1") or 0) for m in ms),
             "span": max(int(m.get("span") or 0) for m in ms),
             "last_ts": int(last.get("last_ts") or 0), "last": last.get("last") or "",
             # обычный час копии без истории - ее нынешний: новый под всплеска не делает
             "base": None if all(b is None for b in bases)
             else sum(b if b is not None else int(m.get("h1") or 0) for b, m in zip(bases, ms))}
        if all(isinstance(h, list) and h and len(h) == len(hbs[0]) for h in hbs):
            g["hb"] = [sum(int(v or 0) for v in col) for col in zip(*hbs)]
        out.append(g)
    return out


def problems(s: Server, now: datetime) -> list[dict]:
    """Логи со всплеском, без заглушенных: {"key", "label", "level", "why", "text"}"""
    held, muted = _held(s), _muted(s, now)
    out = []
    for x in sources(s.last_report or {}):
        k = key(x)
        lvl = spike(x, k in held)
        if not lvl or k in muted:
            continue
        w = why(x)
        text = f"ошибки в логе: {label(x)}, {w}"
        last = str(x.get("last") or "").strip()
        if last:
            text += f"\n↳ {last[:200]}"
        out.append({"key": k, "label": label(x), "level": lvl, "why": w, "text": text})
    out.sort(key=lambda x: (-x["level"], x["key"]))
    return out


def view(s: Server, now: datetime) -> dict | None:
    """Для раздела "Ошибки в логах": {"ts", "logs", "skipped", "sources"}; у лога "key", "label",
    счет, обычный час ("base", None - истории мало), всплеск ("level", "why") и мьют"""
    b = block(s.last_report or {})
    if b is None:
        return None
    held, muted = _held(s), _muted(s, now)
    perm = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    snz = {str(k)[len(MUTE_PREFIX):]: str(v) for k, v in (getattr(s, "alert_snoozes", None) or {}).items()
           if str(k).startswith(MUTE_PREFIX)}
    rows = []
    for x in sources(s.last_report or {}):
        k = key(x)
        bs = base(x)
        row = {"key": k, "label": label(x), "h1": int(x.get("h1") or 0), "d1": int(x.get("d1") or 0),
               "lines1": int(x.get("lines1") or 0), "base": round(bs, 1) if bs is not None else None,
               "last": str(x.get("last") or "")[:300], "last_ts": int(x.get("last_ts") or 0)}
        lvl = spike(x, k in held)
        if lvl:
            row["level"], row["why"] = lvl, why(x)
        if k in perm:
            row["mute"] = {"perm": True}
        elif k in muted and k in snz:
            row["mute"] = {"until": snz[k]}
        rows.append(row)
    return {"ts": int(b.get("ts") or 0), "logs": int(b.get("logs") or 0), "skipped": int(b.get("skipped") or 0),
            "sources": rows}
