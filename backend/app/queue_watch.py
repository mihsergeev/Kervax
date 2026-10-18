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
И во всех трех случаях в очереди сейчас не меньше MIN_DEPTH сообщений: шестнадцать писем, которые
воркер забирает пачкой раз в несколько часов, будили человека и через две минуты давали отбой.
Очереди мертвых писем (dlq, dead, failed) и служебные очереди Celery и RabbitMQ (pidbox, celeryev,
amq.*) не судим: первые копятся по замыслу, вторые живут сами по себе.

Очереди в Redis (Celery, Sidekiq, RQ, BullMQ, Laravel, Yii) присылает helper queues-setup: extras
"queues", брокер на каждую связку экземпляр Redis + система + база + префикс. Судятся так же, ключ -
по источнику "контейнер redis, Celery, db 1". С queues-setup 0.3 там же очереди в таблицах баз
(engine "pg" или "mysql"): Yii, Laravel, Symfony Messenger, Delayed Job, Solid Queue, GoodJob, Que,
Oban, River, pg-boss, Hangfire - брокер на таблицу, ключ "на хосте, Yii, база app" (и таблица, если
она названа не так, как у системы по умолчанию). Заглушить очередь можно насовсем (alert_mutes
"qstuck:<ключ>") или на время (alert_snoozes с тем же ключом): и алерт хода, и порог глубины. Состояние - в
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
MIN_DEPTH = 100  # и в очереди не меньше сотни: о десятке сообщений не алертим
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


SYSTEM_LABEL = {"celery": "Celery", "sidekiq": "Sidekiq", "rq": "RQ", "bullmq": "BullMQ",
                "laravel": "Laravel", "yii": "Yii", "symfony": "Symfony Messenger", "delayed_job": "Delayed Job",
                "solid_queue": "Solid Queue", "good_job": "GoodJob", "que": "Que", "oban": "Oban",
                "river": "River", "pgboss": "pg-boss", "hangfire": "Hangfire"}
ENGINE_LABEL = {"redis": "Redis", "pg": "PostgreSQL", "mysql": "MySQL"}
# таблица системы по умолчанию: ее в подписи не называем
DEFAULT_TABLE = {"yii": "queue", "laravel": "jobs", "symfony": "messenger_messages", "delayed_job": "delayed_jobs",
                 "solid_queue": "solid_queue_ready_executions", "good_job": "good_jobs", "que": "que_jobs",
                 "oban": "oban_jobs", "river": "river_job", "pgboss": "pgboss.job", "hangfire": "hangfire.jobqueue"}


def where_text(w: str) -> str:
    """'container redis-1' -> 'контейнер redis-1', 'pod ns/redis-0' -> 'под ns/redis-0'"""
    w = str(w or "")
    if w.startswith("container "):
        return "контейнер " + w[len("container "):]
    if w.startswith("pod "):
        return "под " + w[len("pod "):]
    return "на хосте" if w in ("", "host") else w


def _table_note(system: str, table: str) -> str:
    """Таблица в подписи, если она названа не как у системы по умолчанию (префикс, своя схема)"""
    d = DEFAULT_TABLE.get(system, "")
    t = table.lower()
    return "" if not t or t in (d, d.split(".")[-1]) else table


def helper_brokers(rep: dict) -> list[dict]:
    """Очереди от helper'а queues-setup (в Redis и в таблицах баз), в виде брокеров как у RabbitMQ:
    {"kind" (система), "label", "engine" ("redis", "pg", "mysql"), "source" (часть ключа и подпись в
    алерте), "where" (подпись в карточке), "ns", "db", "queues", "total"}"""
    block = ((rep or {}).get("extras") or {}).get("queues")
    out = []
    for b in (block or {}).get("brokers") or [] if isinstance(block, dict) else []:
        if not isinstance(b, dict) or not b.get("system"):
            continue
        system = str(b["system"])
        label = SYSTEM_LABEL.get(system, system)
        engine = str(b.get("engine") or "redis")
        wt = where_text(str(b.get("where") or ""))
        qs = [dict(q, vhost="/") for q in b.get("queues") or [] if isinstance(q, dict) and q.get("name")]
        if engine == "redis":
            ns, db = str(b.get("ns") or ""), int(b.get("db") or 0)
            src = f"{wt}, {label}" + (f" {ns}" if ns else "") + (f", db {db}" if db else "")
            where = "Redis, " + wt + (f", db {db}" if db else "") + (f", префикс {ns}" if ns else "")
        else:
            base, tb = str(b.get("base") or ""), _table_note(system, str(b.get("table") or ""))
            ns, db = "", 0
            tail = (f", база {base}" if base else "") + (f", таблица {tb}" if tb else "")
            src = f"{wt}, {label}{tail}"
            where = f"{ENGINE_LABEL.get(engine, engine)}, {wt}{tail}"
        out.append({"kind": system, "label": label, "engine": engine, "source": src, "where": where,
                    "ns": ns, "db": db, "queues": qs, "total": int(b.get("total") or len(qs))})
    return out


def _skipped(rep: dict) -> list[dict]:
    block = ((rep or {}).get("extras") or {}).get("queues")
    return [x for x in (block or {}).get("skipped") or [] if isinstance(x, dict)] if isinstance(block, dict) else []


def redis_skipped(rep: dict) -> list[str]:
    """Экземпляры Redis, пароль которых helper не нашел: очереди в них не видны"""
    return [where_text(str(x.get("where") or "")) for x in _skipped(rep) if (x.get("engine") or "redis") == "redis"]


def db_skipped(rep: dict) -> list[dict]:
    """Базы, куда helper не вошел (why "auth"), и таблицы очередей, которые он не считает каждую
    минуту: больше миллиона строк, выполненные задачи не удаляются (why "big"): {"why", "where", "rows"}"""
    out = []
    for x in _skipped(rep):
        engine = str(x.get("engine") or "redis")
        if engine == "redis":
            continue
        where = f"{ENGINE_LABEL.get(engine, engine)}, {where_text(str(x.get('where') or ''))}"
        if x.get("why") == "big":
            system = str(x.get("system") or "")
            where += f", {SYSTEM_LABEL.get(system, system)}, база {x.get('base') or ''}, таблица {x.get('table') or ''}"
            out.append({"why": "big", "where": where, "rows": int(x.get("rows") or 0)})
        else:
            out.append({"why": "auth", "where": where})
    return out


def brokers(rep: dict) -> list[dict]:
    """Все брокеры ноды: RabbitMQ (агент) и очереди от helper'а (Redis, таблицы баз): {"kind", "source", "queues"}"""
    out = [{"kind": "rabbitmq", "source": str(svc.get("source") or ""), "queues": svc.get("queues") or []}
           for svc in (rep or {}).get("services") or []
           if isinstance(svc, dict) and svc.get("kind") == "rabbitmq"]
    return out + helper_brokers(rep)


def queues(rep: dict) -> list[dict]:
    """Очереди ноды: {"key", "name", "vhost", "source", "kind", "depth", "consumers"}"""
    out = []
    for b in brokers(rep):
        src = b["source"]
        for q in b["queues"]:
            if not isinstance(q, dict) or not q.get("name"):
                continue
            c = q.get("consumers")
            out.append({"key": _key(src, q), "name": str(q["name"]), "vhost": str(q.get("vhost") or "/"),
                        "source": src, "kind": b["kind"], "depth": depth(q),
                        "consumers": int(c) if isinstance(c, (int, float)) else None})
    return out


def muted_keys(s: Server, now: datetime) -> set[str]:
    """Очереди, по которым сейчас не алертим: насовсем (alert_mutes) или на время (alert_snoozes)"""
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
    return keys


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
    if not isinstance(e, dict) or ts - int(e.get("seen") or 0) > GAP_S or d < MIN_DEPTH:
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
    muted = muted_keys(s, now)
    return [x for x in _stuck_all(s, now) if x["key"] not in muted]


def view(s: Server, now: datetime) -> dict[str, dict]:
    """Для строк очередей в карточке: {ключ: {"age", "d0", "muted"}} у тех, что растут и не разбираются"""
    muted = muted_keys(s, now)
    return {x["key"]: {"age": x["age"], "d0": x["d0"], "muted": x["key"] in muted} for x in _stuck_all(s, now)}


def mute_view(s: Server, now: datetime) -> dict[str, dict]:
    """Заглушенные очереди для карточки: {ключ: {"perm": True} | {"until": iso}}"""
    out: dict[str, dict] = {m[len(MUTE_PREFIX):]: {"perm": True} for m in (s.alert_mutes or [])
                            if m.startswith(MUTE_PREFIX)}
    for k, until in (getattr(s, "alert_snoozes", None) or {}).items():
        key = str(k)[len(MUTE_PREFIX):]
        if str(k).startswith(MUTE_PREFIX) and key not in out and key in muted_keys(s, now):
            out[key] = {"until": str(until)}
    return out


def helper_view(s: Server) -> list[dict]:
    """Брокеры от helper'а (Redis, таблицы баз) для карточки (без служебных полей)"""
    return [{k: b[k] for k in ("kind", "label", "engine", "source", "where", "queues", "total")}
            for b in helper_brokers(s.last_report or {})]
