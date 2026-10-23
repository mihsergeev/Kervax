"""У своего лимита памяти: контейнер, под или юнит молча тормозит, упершись в limit.

Повод - ночь 24.10.2026: под MariaDB с лимитом 384Mi (пресет bitnami) занял его весь, ядро
выкидывало его кэш и код, база тут же читала их с диска снова. Под не падал и под OOM не попадал,
а страница сайта рендерилась 8-12 секунд. Алерта не было: ни OOM, ни перезапуска, ни памяти
сервера (ее было полно).

Данные - mem_limits агента (2.51): по cgroup v2 каждого контейнера, пода и юнита, у которого
занято от 90% лимита, - memory.current и лимит, PSI memory (some/full avg60, full avg10), скорость
major faults и refault в секунду. Поля нет - агент старый, судить не о чем (None).

Сам процент ничего не значит: кэш страниц заполняет любой лимит до краев (на живых нодах поды
по 97% лимита, у которых анонимной памяти 20-50%, давление и faults по нулям). Беда - у лимита
(HOT_RATIO) и при этом давление памяти (FULL_MIN: доля времени, когда все процессы cgroup стоят в
ожидании памяти) или major faults / refault: вытесненное приходится читать с диска обратно. У того
пода было PSI full 2.6-5, major faults 87-1340/с, refault файлов десятки тысяч в секунду; у всех
остальных подов и контейнеров на тех же нодах - ноль.

Дебаунс на каждый контейнер или под отдельно - alert_state["memlim"]: {ключ: {"s": начало эпизода,
"l": последний замер у лимита, "v": он сам}}. Эпизод держится HOLD_S - признак и алерт; замеры с
просветом до GAP_S - один эпизод (минутная пауза в нагрузке его не рвет); отбой - после COOL_S без
единого замера у лимита. Спайк (импорт, прогрев кэша после старта) так не алертит.

Решение человека "так задумано" (или мьют пода, контейнера) - memlim:<ключ> в alert_mutes: под -
по ворклоаду, имя пода меняется с выкладкой.
"""
from __future__ import annotations

from datetime import datetime

from app import podnames, tg_fmt
from app.models import Server

HOT_RATIO = 0.95  # занято от лимита (memory.current, с кэшем)
FULL_MIN = 1.0  # PSI memory full avg60, %
MF_MIN = 50.0  # major faults в секунду
RF_MIN = 2000.0  # refault в секунду (~8мб/с прочитанного заново)
CRIT_FULL = 10.0  # PSI full от 10% - процессы стоят каждую десятую секунду: авария
HOLD_S = 10 * 60  # держится 10 минут - беда
GAP_S = 3 * 60  # просвет до 3 минут не рвет эпизод
COOL_S = 15 * 60  # отбой после 15 минут без замеров у лимита
KEEP_S = 86400  # прошедшие эпизоды видны в карточке сутки
MUTE_PREFIX = "memlim:"
_KIND = {"container": "docker", "pod": "pod", "unit": "unit"}


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _ts(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def block(rep: dict | None) -> list[dict] | None:
    """mem_limits агента; None - агент старше 2.51 (поля нет), [] - никого у лимита."""
    ml = (rep or {}).get("mem_limits")
    if not isinstance(ml, list):
        return None
    return [x for x in ml if isinstance(x, dict) and x.get("kind") in _KIND and _f(x.get("lim")) > 0]


def _ctrls(rep: dict) -> dict[str, str]:
    """"ns/под" -> ctrl агента ("statefulset/имя") по списку подов кластера"""
    out = {}
    for p in ((rep or {}).get("kube") or {}).get("pods") or []:
        if isinstance(p, dict) and p.get("name") and p.get("ctrl"):
            out[f"{p.get('ns') or '?'}/{p['name']}"] = str(p["ctrl"])
    return out


def name_of(x: dict, pod_names: dict[str, str] | None = None) -> str:
    """Имя как в отчете; под без kube-api ("pod 386fef04") - по отчету контроллера кластера."""
    name = str(x.get("name") or "?")
    if x.get("kind") == "pod" and name.startswith("pod ") and x.get("u"):
        name = (pod_names or {}).get(str(x["u"]), name)
    return name


def key_of(x: dict, name: str, ctrls: dict[str, str] | None = None) -> str:
    """docker:<имя>, unit:<юнит>, pod:<ns>/<ворклоад> (перезапуск пода - тот же ключ)."""
    kind = _KIND[str(x.get("kind"))]
    if kind != "pod":
        return f"{kind}:{name}"
    if name.startswith("pod "):
        return f"pod:?/{name}"
    return podnames.pod_key("pod:" + name, (ctrls or {}).get(name, ""))


def hot(x: dict) -> bool:
    """У лимита и тормозит: давление памяти или вытесненное читается обратно."""
    ratio = _f(x.get("cur")) / _f(x.get("lim")) if _f(x.get("lim")) else 0.0
    return ratio >= HOT_RATIO and (_f(x.get("full")) >= FULL_MIN or _f(x.get("mf")) >= MF_MIN
                                   or _f(x.get("rf")) >= RF_MIN)


def _snap(x: dict, name: str) -> dict:
    v = {k: x.get(k) for k in ("lim", "cur", "anon", "full", "f10", "some", "mf", "rf")}
    v["kind"], v["name"] = _KIND[str(x.get("kind"))], name
    return v


def _active(cs: dict, now_ts: float) -> bool:
    s, last = _ts(cs.get("s")), _ts(cs.get("l"))
    return s is not None and last is not None and last - s >= HOLD_S and now_ts - last <= COOL_S


def update(state: dict | None, rep: dict, now: datetime, pod_names: dict[str, str] | None = None) -> dict | None:
    """Новое alert_state["memlim"]; None - агент старый, состояние не трогаем."""
    items = block(rep)
    if items is None:
        return None
    old = state if isinstance(state, dict) else {}
    t = now.timestamp()
    ctrls = _ctrls(rep)
    new: dict[str, dict] = {}
    for k, cs in old.items():
        last = _ts(cs.get("l")) if isinstance(cs, dict) else None
        if last is not None and t - last <= KEEP_S:
            new[k] = cs
    for x in items:
        if not hot(x):
            continue
        name = name_of(x, pod_names)
        k = key_of(x, name, ctrls)
        cs = dict(new.get(k) or {})
        last = _ts(cs.get("l"))
        # новый эпизод - только если прошлый уже кончился: замеров давно не было и алерт не держится
        if last is None or (t - last > GAP_S and not _active(cs, t)):
            cs["s"] = t
        cs["l"], cs["v"] = t, _snap(x, name)
        new[k] = cs
    return new


def decided(s: Server) -> set[str]:
    """Ключи, про которые отметили "так задумано" (или заглушили)."""
    return {m[len(MUTE_PREFIX):] for m in (getattr(s, "alert_mutes", None) or []) if m.startswith(MUTE_PREFIX)}


def lim_text(b) -> str:
    """Лимит так, как его пишут в манифесте: 384Mi, 1Gi; не ровный - по-человечески."""
    b = int(_f(b))
    if b and b % 2**30 == 0:
        return f"{b // 2**30}Gi"
    if b and b % 2**20 == 0:
        return f"{b // 2**20}Mi"
    return tg_fmt.size(b)


def _num(v: float) -> str:
    return f"{v:.1f}".removesuffix(".0") if v < 10 else f"{v:.0f}"


def facts_text(v: dict) -> str:
    """'используется 99%, PSI full 4.8, major faults 87/с' - что из этого есть"""
    lim = _f(v.get("lim"))
    # вниз: 99.7% - "99%", 100% только когда занято все
    parts = [f"используется {min(int(_f(v.get('cur')) / lim * 100), 100)}%" if lim else ""]
    if _f(v.get("full")) >= 0.1:
        parts.append(f"PSI full {_num(_f(v.get('full')))}")
    if _f(v.get("mf")) >= 1:
        parts.append(f"major faults {tg_fmt.count(_f(v.get('mf')))}/с")
    elif _f(v.get("rf")) >= 1:
        parts.append(f"refault {tg_fmt.count(_f(v.get('rf')))}/с")
    return ", ".join(p for p in parts if p)


_WHAT = {"pod": "под", "docker": "контейнер", "unit": "юнит"}
_ADVICE = {"pod": "поднимите limit памяти пода", "docker": "поднимите лимит памяти контейнера (mem_limit)",
           "unit": "поднимите MemoryMax / MemoryHigh юнита"}


def problems(s: Server, now: datetime, with_decided: bool = False) -> list[dict]:
    """Кто сейчас тормозит у лимита (эпизод держится HOLD_S и не кончился): {"key", "kind", "name",
    "level", "since", "text", "v", "decided"}. Без with_decided - без отмеченных "так задумано"."""
    st = (s.alert_state or {}).get("memlim")
    if not isinstance(st, dict) or block(s.last_report or {}) is None:
        return []
    done = decided(s)
    t = now.timestamp()
    out = []
    for k in sorted(st):
        cs = st[k]
        if not isinstance(cs, dict) or not _active(cs, t) or not isinstance(cs.get("v"), dict):
            continue
        dec = podnames.muted(done, k)
        if dec and not with_decided:
            continue
        v = cs["v"]
        kind = k.partition(":")[0]
        name = shown(k)
        text = (f"{_WHAT.get(kind, '')} {name} уперся в лимит памяти {lim_text(v.get('lim'))}: {facts_text(v)}"
                f" - тормозит, но не падает; {_ADVICE.get(kind, '')}")
        out.append({"key": k, "kind": kind, "name": name, "level": 2 if _f(v.get("full")) >= CRIT_FULL else 1,
                    "since": datetime.fromtimestamp(_ts(cs.get("s")) or t, now.tzinfo).isoformat(),
                    "text": text, "v": v, "decided": dec})
    return out


def shown(key: str) -> str:
    """Имя для людей из ключа: контейнер и юнит как есть, под - ns/ворклоад без хеша"""
    return key.partition(":")[2].removeprefix("?/")


def view(s: Server, now: datetime, pod_names: dict[str, str] | None = None) -> dict | None:
    """Для карточки: кто тормозит сейчас (с отмеченными "так задумано"), кто у лимита без давления
    (кэш - нормально, серым) и прошедшие за сутки эпизоды. None - агент старый."""
    rep = s.last_report or {}
    items = block(rep)
    if items is None:
        return None
    bad = problems(s, now, with_decided=True)
    bad_keys = {x["key"] for x in bad}
    ctrls = _ctrls(rep)
    near = []
    for x in items:
        k = key_of(x, name_of(x, pod_names), ctrls)
        if k not in bad_keys:
            near.append({"key": k, "kind": _KIND[str(x["kind"])], "name": shown(k), "v": _snap(x, shown(k)), "hot": hot(x)})
    st = (s.alert_state or {}).get("memlim") or {}
    t = now.timestamp()
    past = []
    for k, cs in sorted(st.items()):
        if k in bad_keys or not isinstance(cs, dict) or not isinstance(cs.get("v"), dict):
            continue
        s0, last = _ts(cs.get("s")), _ts(cs.get("l"))
        if s0 is None or last is None or last - s0 < HOLD_S:
            continue  # спайк, не эпизод
        past.append({"key": k, "kind": k.partition(":")[0], "name": shown(k), "v": cs["v"],
                     "from": datetime.fromtimestamp(s0, now.tzinfo).isoformat(),
                     "to": datetime.fromtimestamp(last, now.tzinfo).isoformat(), "ago_s": int(t - last)})
    return {"bad": bad, "near": near, "past": past}


# -- короткий текст для Telegram (формат A, app/tg_fmt.py) --
# Под default/wordpress-mariadb уперся в лимит памяти 384Mi
# Используется 99%, PSI full 4.8, major faults 87/с - тормозит, но не падает

def tg_short(items) -> tuple[str, list[str]] | None:
    xs = [x for x in items or [] if isinstance(x, dict) and isinstance(x.get("v"), dict)]
    if not xs:
        return None
    x = xs[0]
    what = f"{tg_fmt.cap(_WHAT.get(str(x.get('kind')), ''))} {x.get("name") or "?"} уперся в лимит памяти {lim_text(x['v'].get('lim'))}"
    facts = [tg_fmt.cap(facts_text(x["v"])) + " - тормозит, но не падает"]
    if len(xs) > 1:
        facts.append(tg_fmt.fact("Еще", tg_fmt.names(str(y.get("name") or "?") for y in xs[1:])))
    return what, facts
