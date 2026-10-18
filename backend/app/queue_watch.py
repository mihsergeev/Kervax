"""Очередь растет и не разбирается: без ручного порога.

Агент отдает глубину очередей RabbitMQ (services[], prometheus-плагин). Порог глубины
(queue_alert_depth) по умолчанию 0 и стоит руками у единиц, да и общего числа нет: полмиллиона в
очереди краулера бывает нормой, а сотня писем - аварией. Поэтому смотрим не на глубину, а на ход.

У каждой очереди свой "ход" с момента, когда она последний раз заметно убыла: начало t0, глубина
в начале d0, пик p и флаг "ни разу не уменьшилась" (nd). Ход начинается заново, когда очередь
опустилась ниже пика больше чем на DIP (живой воркер разбирает пачками, и очередь ходит пилой).
Алерт, когда очередь
- растет дольше STUCK_S и выросла хотя бы на GROW_MIN: не разбирают или разбирают медленнее,
  чем кладут;
- или дольше SLOW_S не уменьшилась ни на одно сообщение и выросла хотя бы на SLOW_GROW: умерший
  воркер редкой очереди (пять писем в час) за полчаса не наберет сотню;
- или у нее нет ни одного потребителя (агент 2.38), и она STUCK_S не уменьшилась и выросла хотя бы
  на SLOW_GROW: воркер умер, ждать три часа незачем.
Очереди мертвых писем (dlq, dead, failed) и служебные очереди Celery и RabbitMQ (pidbox, celeryev,
amq.*) не судим: первые копятся по замыслу, вторые живут сами по себе. Состояние - в
alert_state["qwatch"]: {ключ очереди: {"t0", "d0", "p", "nd", "last", "seen"}}, ключ как у порога
(collector.queue_key). Ведет цикл алертов, пока нода на связи; провал в отчетах дольше GAP_S
начинает ход заново: что было с очередью, пока нода молчала, неизвестно.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.models import Server

STUCK_S = 1800  # растет полчаса
GROW_MIN = 100  # и выросла хотя бы на сотню
SLOW_S = 3 * 3600  # или три часа не убыла ни на одно сообщение
SLOW_GROW = 5
DIP_REL, DIP_ABS = 0.1, 5  # убыла от пика на 10% (и не меньше чем на 5) - разбирают, ход заново
GAP_S = 600  # отчетов не было 10 минут - ход заново
KEEP_S = 3600  # очередь пропала из отчета (список урезан до самых глубоких) - забываем через час
MUTE_PREFIX = "qstuck:"
# мертвые письма копятся по замыслу; pidbox и celeryev - служебные очереди воркеров Celery,
# amq.* - временные очереди самого RabbitMQ (ответы RPC и т.п.)
_SKIP = re.compile(r"(^|[._:-])(dlq|dlx|dead|deadletter|dead_letter|dead-letter|failed|parking)($|[._:-])"
                   r"|\.pidbox$|^celeryev\.|^amq\.|\.reply-|^reply[._-]", re.I)


def _key(source: str, q: dict) -> str:
    """Тот же ключ, что у порога очереди (collector.queue_key и queueKey во фронте)"""
    vh = (q.get("vhost") or "/").strip()
    return f"{source}|{vh}/{q.get('name') or ''}"


def depth(q: dict) -> int:
    return int(q.get("ready") or 0) + int(q.get("unacked") or 0)


def skipped(name: str) -> bool:
    return bool(_SKIP.search(name or ""))


def queues(rep: dict) -> list[dict]:
    """Очереди ноды: {"key", "name", "vhost", "source", "kind", "depth", "consumers"}"""
    out = []
    for svc in (rep or {}).get("services") or []:
        if not isinstance(svc, dict) or svc.get("kind") != "rabbitmq":
            continue
        src = str(svc.get("source") or "")
        for q in svc.get("queues") or []:
            if not isinstance(q, dict) or not q.get("name"):
                continue
            c = q.get("consumers")
            out.append({"key": _key(src, q), "name": str(q["name"]), "vhost": str(q.get("vhost") or "/"),
                        "source": src, "kind": "rabbitmq", "depth": depth(q),
                        "consumers": int(c) if isinstance(c, (int, float)) else None})
    return out


def step(e: dict | None, d: int, ts: int) -> dict:
    """Следующее состояние хода очереди по новой глубине"""
    if (not isinstance(e, dict) or ts - int(e.get("seen") or 0) > GAP_S
            or d < int(e.get("p") or 0) - max(DIP_ABS, DIP_REL * int(e.get("p") or 0))):
        return {"t0": ts, "d0": d, "p": d, "nd": True, "last": d, "seen": ts}
    last = int(e.get("last") or 0)
    return {**e, "p": max(int(e.get("p") or 0), d), "nd": bool(e.get("nd")) and d >= last, "last": d, "seen": ts}


def update(state: dict | None, rep: dict, now: datetime) -> dict:
    ts = int(now.timestamp())
    st = {k: v for k, v in (state or {}).items() if isinstance(v, dict) and ts - int(v.get("seen") or 0) <= KEEP_S}
    for q in queues(rep):
        st[q["key"]] = step(st.get(q["key"]), q["depth"], ts)
    return st


def stuck(e: dict | None, d: int, ts: int, consumers: int | None = None) -> int:
    """Сколько секунд очередь растет и не разбирается (0 - разбирается)"""
    if not isinstance(e, dict) or ts - int(e.get("seen") or 0) > GAP_S:
        return 0
    age, grow = ts - int(e.get("t0") or ts), d - int(e.get("d0") or 0)
    if age >= STUCK_S and grow >= GROW_MIN:
        return age
    if grow >= SLOW_GROW and e.get("nd") and (age >= SLOW_S or (consumers == 0 and age >= STUCK_S)):
        return age
    return 0


def _dur(sec: int) -> str:
    if sec >= 86400:
        return f"{sec // 86400} сут {sec % 86400 // 3600} ч"
    if sec >= 3600:
        return f"{sec // 3600} ч {sec % 3600 // 60} мин"
    return f"{max(sec // 60, 1)} мин"


def _n(v: int) -> str:
    return f"{v:,}".replace(",", " ")


def _stuck_all(s: Server, now: datetime) -> list[dict]:
    st = (s.alert_state or {}).get("qwatch")
    if not isinstance(st, dict):
        return []
    ts = int(now.timestamp())
    out = []
    for q in queues(s.last_report or {}):
        if skipped(q["name"]):
            continue
        e = st.get(q["key"])
        age = stuck(e, q["depth"], ts, q["consumers"])
        if not age:
            continue
        d0 = int(e.get("d0") or 0)
        where = f" ({q['source']})" if q["source"] else ""
        why = "нет ни одного потребителя" if q["consumers"] == 0 else "не разбирается"
        text = (f"очередь {q['name']}{where} растет {_dur(age)} и {why}: "
                f"{_n(d0)} -> {_n(q['depth'])} сообщений")
        out.append({"key": q["key"], "name": q["name"], "source": q["source"], "age": age, "d0": d0,
                    "depth": q["depth"], "consumers": q["consumers"], "level": 2, "text": text,
                    "since": datetime.fromtimestamp(ts - age, timezone.utc).isoformat()})
    out.sort(key=lambda x: -x["age"])
    return out


def problems(s: Server, now: datetime) -> list[dict]:
    """Очереди, которые растут и не разбираются: {"key", "name", "source", "age", "d0", "depth",
    "consumers", "level", "text", "since"}, без заглушенных. Без потребителей (агент 2.38) - так и
    сказано."""
    muted = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    return [x for x in _stuck_all(s, now) if x["key"] not in muted]


def view(s: Server, now: datetime) -> dict[str, dict]:
    """Для строк очередей в карточке: {ключ: {"age", "d0", "muted"}} у тех, что растут и не разбираются"""
    muted = {m[len(MUTE_PREFIX):] for m in (s.alert_mutes or []) if m.startswith(MUTE_PREFIX)}
    return {x["key"]: {"age": x["age"], "d0": x["d0"], "muted": x["key"] in muted} for x in _stuck_all(s, now)}
