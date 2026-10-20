"""Алерт заполнения диска и inode: выдержка, гистерезис отбоя и быстрое заполнение.

Место скачет само: docker тянет и распаковывает образ, сборка пишет временные файлы, качается
модель LLM, ночной дамп ложится рядом с базой и уезжает в бэкап. Раньше алерт шел на первом же
отчете выше порога, а отбой на первом ниже: нода разработки с 82% подскакивала до 87% на две
минуты, и приходила пара "диск 86%" и "снова в норме"; прод, который сидит на 84%, за неделю
дал семь пар у порога 85% (заходил на 85.1-85.7 на 2-9 минут), а отбой приходил с текстом
"85% (было 85%)". Реагировать там было не на что.

Теперь:
- заход за порог алертит, только если продержался HOLD_S (полчаса). Нырок ниже порога
  отсчет сбрасывает;
- критичный уровень (95% по умолчанию) и быстрое заполнение - сразу, без выдержки: это авария,
  через минуты писать будет некуда. Быстрое - когда последние FAST_WINDOW_S место растет ровно
  (каждая треть окна растет) и при таком темпе раздел заполнится за FAST_ETA_S. Разовый рывок
  на распаковке образа так не выглядит: первая треть окна ровная;
- отбой - только когда значение ниже нижнего порога на HYST пунктов и держится там CALM_S. Пока
  алерт висит, уровень не снижается: 2 -> 1 -> 2 у порога 90% давало новое сообщение на каждый
  заход, а беда одна. Отбой придет один, когда диск правда освободят.
"""
from __future__ import annotations

from datetime import datetime, timezone

HOLD_S = 1800  # выше порога столько подряд - алерт (кроме критичного и быстрого заполнения)
HYST = 3.0  # отбой ниже нижнего порога на столько пунктов
CALM_S = 1800  # и держится там столько
CRIT = 3  # уровень без выдержки
FAST_WINDOW_S = 600  # окно, по которому судим о быстром заполнении
FAST_MIN_SPAN_S = 480  # точки окна должны покрывать хотя бы столько: дыра в отчетах - не рост
FAST_MIN_POINTS = 6
FAST_ETA_S = 1800  # при нынешнем темпе раздел заполнится быстрее - авария


def _iso(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        d = datetime.fromisoformat(v)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def fast_fill(points: list[tuple[float, float]] | None, value: float, now: datetime) -> dict | None:
    """Раздел заполняется ровно и быстро: {"rate": % в минуту, "eta_min": минут до 100%} или None.
    points - (unix, %) худшего раздела за последние минуты, по возрастанию времени; текущее
    значение добавляется последней точкой. Окно делим на три части: расти должна каждая, темп -
    по самой медленной. Один рывок (образ распаковался за минуту) дает рост только в последней
    трети и сюда не попадает - он ждет обычной выдержки."""
    t = now.timestamp()
    pts = [(float(a), float(b)) for a, b in points or [] if t - FAST_WINDOW_S <= float(a) < t - 1]
    pts.append((t, float(value)))
    if len(pts) < FAST_MIN_POINTS or pts[-1][0] - pts[0][0] < FAST_MIN_SPAN_S:
        return None
    t0, t3 = pts[0][0], pts[-1][0]
    step = (t3 - t0) / 3

    def at(x: float) -> tuple[float, float]:
        return min(pts, key=lambda p: abs(p[0] - x))

    marks = [pts[0], at(t0 + step), at(t0 + 2 * step), pts[-1]]
    rates = []
    for (ta, va), (tb, vb) in zip(marks, marks[1:]):
        if tb <= ta:
            return None
        rates.append((vb - va) / (tb - ta))
    rate = min(rates)
    if rate <= 0:
        return None
    eta = (100.0 - value) / rate
    if eta > FAST_ETA_S:
        return None
    return {"rate": round(rate * 60, 1), "eta_min": max(1, round(eta / 60))}


def step(state: dict, key: str, value: float, raw: int, low: float | None, now: datetime,
         points: list[tuple[float, float]] | None = None) -> tuple[int, dict]:
    """Уровень алерта key ("disk" или "inode") на этот тик и что запомнить.
    value - заполнение худшего раздела, raw - уровень по порогам прямо сейчас (0-3), low - нижний
    из включенных порогов, points - история худшего раздела для быстрого заполнения. В ответе
    since (с какого момента выше порога), cool (с какого момента ниже линии отбоя) и fast."""
    prev = int(state.get(key, 0) or 0)
    since = cool = None
    fast = fast_fill(points, value, now) if raw else None
    lvl = 0
    if prev and low is not None:
        if value >= low - HYST:
            # беда та же: выше линии отбоя уровень только растет
            lvl, since = max(prev, raw), state.get(f"{key}_since") or now.isoformat()
        else:
            started = _iso(state.get(f"{key}_cool")) or now
            if (now - started).total_seconds() < CALM_S:
                lvl, since, cool = prev, state.get(f"{key}_since"), started.isoformat()
    elif raw:
        since = state.get(f"{key}_since") or now.isoformat()
        begun = _iso(since) or now
        if raw >= CRIT or fast or (now - begun).total_seconds() >= HOLD_S:
            lvl = raw
    return lvl, {"since": since, "cool": cool, "fast": fast}


def fast_text(fast: dict | None) -> str:
    """Вставка в алерт: почему он пришел без выдержки."""
    if not fast:
        return ""
    eta = int(fast["eta_min"])
    return f"заполняется быстро: +{fast['rate']}% в минуту, до 100% ~{eta} мин"
