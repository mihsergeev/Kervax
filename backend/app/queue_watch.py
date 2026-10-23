"""Очередь растет и не разбирается: без ручного порога.

Агент отдает глубину очередей RabbitMQ (services[], prometheus-плагин). Порог глубины
(queue_alert_depth) по умолчанию 0 и стоит руками у единиц, да и общего числа нет: полмиллиона в
очереди краулера бывает нормой, а сотня писем - аварией. Поэтому смотрим не на глубину, а на ход.

У каждой очереди свой "ход" с момента, когда она последний раз заметно убыла: начало t0, глубина
в начале d0, пик p и флаг "ни разу не уменьшилась" (nd). Ход начинается заново, когда очередь
опустилась ниже пика больше чем на DIP (живой воркер разбирает пачками, и очередь ходит пилой).
Алерт, когда очередь
- дольше SLOW_S не уменьшилась ни на одно сообщение и выросла хотя бы на SLOW_GROW: не отдает
  ничего, хотя потребители есть, или умер воркер редкой очереди (пять писем в час за полчаса не
  наберут сотню);
- или растет дольше STUCK_S и выросла хотя бы на GROW_MIN, и в ней хотя бы в BASE_X раз больше
  обычного: разбирают медленнее, чем кладут, и уже заметно для этой очереди. Обычное - медиана
  суточных пиков за неделю (dm), пик считается по ходу, который кончился: очередь дошла до него и
  пошла вниз, ее разобрали. Полтысячи в очереди, которая днем ходит до тысячи, - это наплыв, его
  разберут. Пока ни одного разобранного хода нет, обычного не знаем и судим только по росту;
- или у нее нет ни одного потребителя (агент 2.38), а сообщения с тех пор копятся: воркер умер,
  ждать три часа незачем. Только если очередь не разбирают без потребителей: кто забирает
  сообщения опросом (basic.get) или подключается на минуту раз в час, держит потребителей нулем
  почти все время. Такую очередь видно: она убывает, когда потребителей нет (zc), и неделю после
  этого их отсутствие у нее не судим;
- или стоит (queues-setup 0.4 присылает, когда стала готовой самая старая задача, oldest): самая
  старая ждет дольше AGE_S, и ее никто не взял за STALL_S - очередь, в которую больше не пишут,
  не растет, и правила роста ее не видят.
И во всех случаях в очереди сейчас не меньше MIN_DEPTH сообщений: шестнадцать писем, которые
воркер забирает пачкой раз в несколько часов, будили человека и через две минуты давали отбой.
Условие должно продержаться HOLD_S, только потом алерт ("bad" - с какого момента оно выполняется):
перезапуск воркера, выкладка, наплыв на десять минут давали отбой в ту же минуту или через
несколько минут после алерта, и реагировать было не на что.
Об очереди, по которой алерт уже ушел, отбой - когда в ней меньше половины MIN_DEPTH (и рост меньше
половины GROW_MIN, глубина меньше половины порога по обычному уровню): глубина, которая ходит около
сотни, не должна слать алерт и отбой по очереди.
Очереди мертвых писем (dlq, dead, failed) и служебные очереди Celery и RabbitMQ (pidbox, celeryev,
amq.*) не судим: первые копятся по замыслу, вторые живут сами по себе.

Очереди в Redis (Celery, Sidekiq, RQ, BullMQ, Laravel, Yii) присылает helper queues-setup: extras
"queues", брокер на каждую связку экземпляр Redis + система + база + префикс. Судятся так же, ключ -
по источнику "контейнер redis, Celery, db 1". С queues-setup 0.3 там же очереди в таблицах баз
(engine "pg" или "mysql"): Yii, Laravel, Symfony Messenger, Delayed Job, Solid Queue, GoodJob, Que,
Oban, River, pg-boss, Hangfire - брокер на таблицу, ключ "на хосте, Yii, база app" (и таблица, если
она названа не так, как у системы по умолчанию). Заглушить очередь можно насовсем (alert_mutes
"qstuck:<ключ>") или на время (alert_snoozes с тем же ключом): и алерт хода, и порог глубины. Состояние - в
alert_state["qwatch"]: {ключ очереди: {"t0", "d0", "p", "nd", "last", "seen", "oc", "ocs", "c", "z0", "zd",
"zc", "dm", "bad"}}, ключ как у порога (collector.queue_key); oc - когда стала готовой самая
старая задача, ocs - с какого отчета она та же; c - потребителей в прошлом отчете, z0 и zd - с какого
момента их нет и сколько тогда было в очереди, zc - когда очередь последний раз убыла без
потребителей, dm - пик разобранных ходов по суткам {номер суток UTC: глубина}. Ведет цикл алертов,
пока нода на связи; провал в отчетах дольше GAP_S начинает ход заново: что было с очередью, пока нода
молчала, неизвестно. Очередь, которой нет в отчете дольше KEEP_S, забываем, а ее обычный уровень и
разбор без потребителей помним неделю.

Упавшие задачи (queues-setup 0.4, раз в 5 минут): у брокера "failed" - сколько упало за час (h1) и
сутки (d1), за какое окно известно (span: у Laravel и Symfony счет по id строк, история копится с
установки helper'а), какие классы падали чаще и первая строка последней ошибки. Laravel failed_jobs -
отдельный брокер без очередей (system "laravel_failed"). Алерт только на резкий всплеск: за час не
меньше FAIL_MIN и в FAIL_X раз больше обычного часа суток; пока суток истории нет (меньше BASE_SPAN),
обычного не знаем, и нужно не меньше FAIL_NOBASE. Отбой - когда за час меньше половины порога. Мьют -
"qfail:<источник брокера>".
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app import podnames, tg_fmt
from app.models import Server

STUCK_S = 1800  # растет полчаса
GROW_MIN = 100  # и выросла хотя бы на сотню
SLOW_S = 3 * 3600  # или три часа не убыла ни на одно сообщение
SLOW_GROW = 5
HOLD_S = 600  # условие держится 10 минут - тогда алерт: отбой через пару минут после алерта не нужен
PULL_KEEP = 7 * 86400  # убывала без потребителей - неделю их отсутствие у этой очереди не судим
BASE_DAYS = 7  # обычный уровень - по суточным пикам за неделю
BASE_X = 2  # и растущая очередь должна быть хотя бы вдвое выше него
AGE_S = 3600  # или самая старая задача ждет час
STALL_S = 1800  # и ее не взяли за полчаса
MIN_DEPTH = 100  # и в очереди не меньше сотни: о десятке сообщений не алертим
DIP_REL, DIP_ABS = 0.1, 5  # убыла от пика на 10% (и не меньше чем на 5) - разбирают, ход заново
GAP_S = 600  # отчетов не было 10 минут - ход заново
KEEP_S = 3600  # очередь пропала из отчета (список урезан до самых глубоких) - забываем через час
MUTE_PREFIX = "qstuck:"
FAIL_PREFIX = "qfail:"
FAIL_MIN = 100  # упавших за час
FAIL_X = 5  # во столько раз больше обычного часа
FAIL_NOBASE = 500  # обычного не знаем - нужно столько
BASE_SPAN = 4 * 3600  # обычный час считаем, когда истории хотя бы столько
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
                "river": "River", "pgboss": "pg-boss", "hangfire": "Hangfire", "laravel_failed": "Laravel failed_jobs"}
ENGINE_LABEL = {"redis": "Redis", "pg": "PostgreSQL", "mysql": "MySQL"}
# таблица системы по умолчанию: ее в подписи не называем
DEFAULT_TABLE = {"yii": "queue", "laravel": "jobs", "symfony": "messenger_messages", "delayed_job": "delayed_jobs",
                 "solid_queue": "solid_queue_ready_executions", "good_job": "good_jobs", "que": "que_jobs",
                 "oban": "oban_jobs", "river": "river_job", "pgboss": "pgboss.job", "hangfire": "hangfire.jobqueue",
                 "laravel_failed": "failed_jobs"}


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


def _failed(b: dict) -> dict | None:
    """Упавшие задачи брокера из отчета helper'а, числа - числами"""
    f = b.get("failed")
    if not isinstance(f, dict):
        return None
    out = {k: int(f.get(k) or 0) for k in ("h1", "d1", "span")}
    out["top"] = [{"q": str(t.get("q") or ""), "job": str(t.get("job") or ""), "n": int(t.get("n") or 0)}
                  for t in f.get("top") or [] if isinstance(t, dict)][:3]
    for k in ("err", "err_q", "err_job"):
        if f.get(k):
            out[k] = str(f[k])[:300]
    return out


def helper_brokers(rep: dict) -> list[dict]:
    """Очереди от helper'а queues-setup (в Redis и в таблицах баз), в виде брокеров как у RabbitMQ:
    {"kind" (система), "label", "engine" ("redis", "pg", "mysql"), "source" (часть ключа и подпись в
    алерте), "where" (подпись в карточке), "ns", "db", "queues", "total", "failed" (упавшие задачи или None)}"""
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
                    "ns": ns, "db": db, "queues": qs, "total": int(b.get("total") or len(qs)), "failed": _failed(b)})
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
    """Очереди ноды: {"key", "name", "vhost", "source", "kind", "depth", "consumers", "oldest"}"""
    out = []
    for b in brokers(rep):
        src = b["source"]
        for q in b["queues"]:
            if not isinstance(q, dict) or not q.get("name"):
                continue
            c, o = q.get("consumers"), q.get("oldest")
            out.append({"key": _key(src, q), "name": str(q["name"]), "vhost": str(q.get("vhost") or "/"),
                        "source": src, "kind": b["kind"], "depth": depth(q),
                        "consumers": int(c) if isinstance(c, (int, float)) else None,
                        "oldest": int(o) if isinstance(o, (int, float)) and o > 0 else None})
    return out


def _muted(s: Server, now: datetime, prefix: str) -> set[str]:
    keys = {m[len(prefix):] for m in (s.alert_mutes or []) if m.startswith(prefix)}
    for k, until in (getattr(s, "alert_snoozes", None) or {}).items():
        if not str(k).startswith(prefix):
            continue
        try:
            u = datetime.fromisoformat(str(until))
        except ValueError:
            continue
        if (u if u.tzinfo else u.replace(tzinfo=timezone.utc)) > now:
            keys.add(k[len(prefix):])
    return keys


def muted_keys(s: Server, now: datetime) -> set[str]:
    """Очереди, по которым сейчас не алертим: насовсем (alert_mutes) или на время (alert_snoozes)"""
    return _muted(s, now, MUTE_PREFIX)


def step(e: dict | None, d: int, ts: int, oldest: int | None = None, consumers: int | None = None) -> dict:
    """Следующее состояние хода очереди по новой глубине, самой старой задаче и потребителям"""
    gap = not isinstance(e, dict) or ts - int(e.get("seen") or 0) > GAP_S
    dip = not gap and d < int(e.get("p") or 0) - max(DIP_ABS, DIP_REL * int(e.get("p") or 0))
    if gap or dip:
        out = {"t0": ts, "d0": d, "p": d, "nd": True, "last": d, "seen": ts}
        prev = {} if gap else e
        if prev.get("bad"):
            out["bad"] = prev["bad"]
    else:
        last = int(e.get("last") or 0)
        out = {**e, "p": max(int(e.get("p") or 0), d), "nd": bool(e.get("nd")) and d >= last, "last": d, "seen": ts}
        prev = e
    old = e if isinstance(e, dict) else {}
    for k in ("oc", "ocs", "c", "z0", "zd"):
        out.pop(k, None)
    if oldest:
        # та же самая старая задача (ее время то же) - ее не взяли; другая - очередь движется
        same = prev.get("oc") == oldest and prev.get("ocs")
        out["oc"], out["ocs"] = oldest, int(prev["ocs"]) if same else ts
    # пики разобранных ходов по суткам (обычный уровень) и когда убывала без потребителей помним и
    # через провал в отчетах: это не про текущий ход
    day = ts // 86400
    dm = {str(k): int(v) for k, v in (old.get("dm") or {}).items() if str(k).isdigit() and int(k) >= day - BASE_DAYS}
    if dip:
        dm[str(day)] = max(dm.get(str(day), 0), int(e.get("p") or 0))
    if dm:
        out["dm"] = dm
    else:
        out.pop("dm", None)
    zc = int(old.get("zc") or 0)
    if consumers is not None:
        out["c"] = consumers
    if consumers == 0:
        if prev.get("c") == 0 and d < int(prev.get("last") or 0):
            zc = ts  # потребителей не было и нет, а очередь убыла: разбирают без них
        if prev.get("c") == 0 and prev.get("z0"):
            out["z0"], out["zd"] = int(prev["z0"]), int(prev.get("zd") or 0)
        else:
            out["z0"], out["zd"] = ts, d
    if zc and ts - zc <= PULL_KEEP:
        out["zc"] = zc
    else:
        out.pop("zc", None)
    return out


def update(state: dict | None, rep: dict, now: datetime, held: set[str] | None = None) -> dict:
    """Ход всех очередей ноды по отчету. held - по каким очередям алерт уже ушел (queue_stuck_sig):
    у них отбой с гистерезисом, и "bad" (с какого момента выполняется условие алерта) - по нему же"""
    ts = int(now.timestamp())
    held = held or set()
    st = {k: v for k, v in (state or {}).items() if isinstance(v, dict) and ts - int(v.get("seen") or 0)
          <= (BASE_DAYS * 86400 if v.get("dm") or v.get("zc") else KEEP_S)}
    for q in queues(rep):
        e = step(st.get(q["key"]), q["depth"], ts, q["oldest"], q["consumers"])
        h = q["key"] in held
        if why(e, q["depth"], ts, q["consumers"], h)[0] or stalled(e, q["depth"], ts, h):
            e["bad"] = int(e.get("bad") or ts)
        else:
            e.pop("bad", None)
        st[q["key"]] = e
    return st


def usual(e: dict | None, ts: int) -> int | None:
    """Обычный уровень очереди: медиана суточных пиков разобранных ходов за неделю. None - ни одного
    разобранного хода не видели, обычного не знаем"""
    if not isinstance(e, dict):
        return None
    vals = sorted(int(v) for k, v in (e.get("dm") or {}).items()
                  if str(k).isdigit() and int(k) >= ts // 86400 - BASE_DAYS)
    return vals[len(vals) // 2] if vals else None


def pull(e: dict | None, ts: int) -> bool:
    """Очередь разбирают без потребителей (опросом или наездами): за неделю убывала, когда их не было"""
    return isinstance(e, dict) and bool(e.get("zc")) and ts - int(e["zc"]) <= PULL_KEEP


def why(e: dict | None, d: int, ts: int, consumers: int | None = None, held: bool = False) -> tuple[str, int]:
    """Почему очередь не разбирается и сколько секунд: ("nocons", сколько нет потребителей), ("slow",
    сколько не отдает ни одного), ("grow", сколько растет) или ("", 0). held - алерт по ней уже ушел:
    отбой, когда в ней меньше половины MIN_DEPTH, рост от начала хода меньше половины GROW_MIN или
    глубина меньше половины порога по обычному уровню"""
    if not isinstance(e, dict) or ts - int(e.get("seen") or 0) > GAP_S or d < (MIN_DEPTH // 2 if held else MIN_DEPTH):
        return "", 0
    if consumers == 0 and e.get("z0") and not pull(e, ts) and d - int(e.get("zd") or 0) >= SLOW_GROW:
        return "nocons", ts - int(e["z0"])
    age, grow = ts - int(e.get("t0") or ts), d - int(e.get("d0") or 0)
    if age >= SLOW_S and e.get("nd") and grow >= SLOW_GROW:
        return "slow", age
    base = usual(e, ts)
    if base is not None and d < BASE_X * base / (2 if held else 1):
        return "", 0  # наплыв в пределах обычного для этой очереди: разберут
    if age >= STUCK_S and grow >= (GROW_MIN // 2 if held else GROW_MIN):
        return "grow", age
    return "", 0


def stuck(e: dict | None, d: int, ts: int, consumers: int | None = None, held: bool = False) -> int:
    """Сколько секунд очередь не разбирается (0 - разбирается), см. why"""
    return why(e, d, ts, consumers, held)[1]


def stalled(e: dict | None, d: int, ts: int, held: bool = False) -> int:
    """Сколько секунд ждет самая старая задача очереди, которая стоит: та же задача не взята за
    STALL_S и ждет дольше AGE_S (0 - не стоит)"""
    if not isinstance(e, dict) or ts - int(e.get("seen") or 0) > GAP_S or d < (MIN_DEPTH // 2 if held else MIN_DEPTH):
        return 0
    oc, ocs = int(e.get("oc") or 0), int(e.get("ocs") or 0)
    if not oc or not ocs or ts - oc < AGE_S or ts - ocs < STALL_S:
        return 0
    return ts - oc


def _dur(sec: int) -> str:
    if sec >= 86400:
        return f"{sec // 86400} сут {sec % 86400 // 3600} ч"
    if sec >= 3600:
        return f"{sec // 3600} ч {sec % 3600 // 60} мин"
    return f"{max(sec // 60, 1)} мин"


def _n(v: int) -> str:
    return f"{v:,}".replace(",", " ")


def _held(s: Server, kind: str) -> set[str]:
    """По каким ключам алерт этого вида уже ушел (sig в alert_state)"""
    return {str(k) for k in ((s.alert_state or {}).get(f"{kind}_sig") or [])}


def _stuck_all(s: Server, now: datetime) -> list[dict]:
    st = (s.alert_state or {}).get("qwatch")
    if not isinstance(st, dict):
        return []
    ts = int(now.timestamp())
    held = _held(s, "queue_stuck")
    out = []
    for q in queues(s.last_report or {}):
        if skipped(q["name"]):
            continue
        e = st.get(q["key"])
        h = q["key"] in held
        kind, age = why(e, q["depth"], ts, q["consumers"], h)
        wait = stalled(e, q["depth"], ts, h)
        if not age and not wait:
            continue
        if not h and ts - int(e.get("bad") or ts) < HOLD_S:
            continue  # еще не продержалось: перезапуск воркера или наплыв проходят за минуты
        where = f" ({q['source']})" if q["source"] else ""
        old = ts - q["oldest"] if q["oldest"] and ts > q["oldest"] else 0
        d0 = int(e.get("zd") or 0) if kind == "nocons" else int(e.get("d0") or 0)
        base = usual(e, ts)
        if age:
            moved = f"{_n(d0)} -> {_n(q['depth'])} сообщений"
            if kind == "nocons":
                text = f"очередь {q['name']}{where} без потребителей {_dur(age)}, сообщения копятся: {moved}"
            elif kind == "slow":
                text = f"очередь {q['name']}{where} {_dur(age)} не отдает ни одного сообщения: {moved}"
            else:
                text = f"очередь {q['name']}{where} растет {_dur(age)} и не разбирается: {moved}"
            if base is not None and kind != "nocons":
                text += f", обычно до {_n(base)}"
            if old >= AGE_S:
                text += f", самая старая задача ждет {_dur(old)}"
            since = ts - age
        else:
            kind = "stall"
            text = (f"очередь {q['name']}{where} стоит: самая старая задача ждет {_dur(wait)}, "
                    f"в очереди {_n(q['depth'])}")
            since = int(e.get("ocs") or ts)
        out.append({"key": q["key"], "name": q["name"], "source": q["source"], "age": age or ts - since, "d0": d0,
                    "depth": q["depth"], "consumers": q["consumers"], "wait": old, "stall": not age, "why": kind,
                    "base": base, "level": 2, "text": text,
                    "since": datetime.fromtimestamp(since, timezone.utc).isoformat()})
    out.sort(key=lambda x: -x["age"])
    return out


def problems(s: Server, now: datetime) -> list[dict]:
    """Очереди, которые не разбираются или стоят: {"key", "name", "source", "age", "d0", "depth",
    "consumers", "wait", "stall", "why" ("nocons", "slow", "grow", "stall"), "base" (обычный уровень или
    None), "level", "text", "since"}, без заглушенных"""
    muted = muted_keys(s, now)
    return [x for x in _stuck_all(s, now) if x["key"] not in muted]


def view(s: Server, now: datetime) -> dict[str, dict]:
    """Для строк очередей в карточке: {ключ: {"age", "d0", "muted", "stall", "wait", "why"}} у тех, что
    не разбираются или стоят (те же, что в алерте: после выдержки)"""
    muted = muted_keys(s, now)
    return {x["key"]: {"age": x["age"], "d0": x["d0"], "muted": x["key"] in muted, "stall": x["stall"],
                       "wait": x["wait"], "why": x["why"]} for x in _stuck_all(s, now)}


def mute_view(s: Server, now: datetime, prefix: str = MUTE_PREFIX) -> dict[str, dict]:
    """Заглушенные очереди (или упавшие задачи брокеров, prefix FAIL_PREFIX) для карточки:
    {ключ: {"perm": True} | {"until": iso}}"""
    out: dict[str, dict] = {m[len(prefix):]: {"perm": True} for m in (s.alert_mutes or []) if m.startswith(prefix)}
    active = _muted(s, now, prefix)
    for k, until in (getattr(s, "alert_snoozes", None) or {}).items():
        key = str(k)[len(prefix):]
        if str(k).startswith(prefix) and key not in out and key in active:
            out[key] = {"until": str(until)}
    return out


# ---- упавшие задачи ----

def _base(f: dict) -> float | None:
    """Обычный час: упавшие за сутки без последнего часа на число часов; None - истории мало"""
    span = min(int(f.get("span") or 0), 86400)
    if span < BASE_SPAN:
        return None
    return max(int(f.get("d1") or 0) - int(f.get("h1") or 0), 0) * 3600 / (span - 3600)


def spike(f: dict | None, held: bool = False) -> bool:
    """Резкий всплеск упавших задач за час; held - алерт уже ушел, отбой при половине порога"""
    if not f:
        return False
    k = 0.5 if held else 1
    h1, base = int(f.get("h1") or 0), _base(f)
    if h1 < FAIL_MIN * k:
        return False
    return h1 >= FAIL_X * base * k if base is not None else h1 >= FAIL_NOBASE * k


def _fail_all(s: Server) -> list[dict]:
    held = _held(s, "queue_failed")
    out = []
    for b in helper_brokers(s.last_report or {}):
        f = b["failed"]
        if not spike(f, b["source"] in held):
            continue
        base = _base(f)
        text = f"всплеск упавших задач ({b['source']}): {_n(f['h1'])} за час"
        if base is not None:
            text += f", обычно около {_n(round(base))} в час"
        top = [t for t in f["top"] if t["job"] or t["q"]]
        if top:
            t = top[0]
            text += "; чаще всего " + (t["job"] or "задачи") + (f" в очереди {t['q']}" if t["q"] else "")
        if f.get("err"):
            text += "\n↳ " + f["err"]
        out.append({"key": b["source"], "source": b["source"], "h1": f["h1"], "base": base, "level": 2, "text": text})
    out.sort(key=lambda x: -x["h1"])
    return out


def failed_problems(s: Server, now: datetime) -> list[dict]:
    """Брокеры со всплеском упавших задач: {"key" (источник брокера), "source", "h1", "base", "level",
    "text"}, без заглушенных"""
    muted = _muted(s, now, FAIL_PREFIX)
    return [x for x in _fail_all(s) if x["key"] not in muted]


def failed_muted(s: Server, now: datetime) -> set[str]:
    return _muted(s, now, FAIL_PREFIX)


def helper_view(s: Server) -> list[dict]:
    """Брокеры от helper'а (Redis, таблицы баз) для карточки (без служебных полей); у упавших задач -
    обычный час (base) и всплеск ли сейчас (spike)"""
    held = _held(s, "queue_failed")
    out = []
    for b in helper_brokers(s.last_report or {}):
        v = {k: b[k] for k in ("kind", "label", "engine", "source", "where", "queues", "total")}
        if b["failed"]:
            v["failed"] = {**b["failed"], "base": _base(b["failed"]), "spike": spike(b["failed"], b["source"] in held)}
        out.append(v)
    return out


# ---- короткий алерт в Telegram (формат A, app/tg_fmt.py) ----
# Где и что - без подписей и счета за период: "Очередь mail не разбирается / нет потребителей,
# 25 тыс. в app-rabbitmq-1". Хелперы where_short, where_in и times_usual берут и соседние модули
# (воркеры, кроны, логи, PHP-FPM)

def where_short(w: str, pods: bool = True) -> str:
    """Где коротко: "контейнер shop-php" -> "shop-php", "под shop/web-6b8b56c769-f4k8m" -> "shop/web"
    (ворклоад без хеша, неймспейс - только если он что-то добавляет: не default и не то же имя),
    "на хосте" - как есть. pods=False - имя пода уже ворклоад (сведено раньше)"""
    w = where_text(w)
    if w.startswith("контейнер "):
        return w[len("контейнер "):]
    if w.startswith("под "):
        ns, _, name = w[len("под "):].rpartition("/")
        wl = podnames.workload(name) if pods else name
        return wl if not ns or ns in ("?", "default", wl) else f"{ns}/{wl}"
    return w


def where_in(w: str, pods: bool = True) -> str:
    """"в shop-php", "в web", "на хосте" """
    s = where_short(w, pods)
    return s if s == "на хосте" else f"в {s}"


def times_usual(n: float, base: float | None) -> str:
    """"в 10 раз больше обычного"; обычного не знаем или почти ноль - пусто: скажет вызывающий"""
    if base is None or base < 1:
        return ""
    x = round(n / base)
    if x < 2:
        return "больше обычного"
    return f"в {x} {_plural_times(x)} больше обычного"


def _plural_times(n: int) -> str:
    return "раза" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "раз"


def _system_of(rest: str) -> str:
    """Система брокера из хвоста источника helper'а ("Celery ns, db 1" -> "Celery")"""
    for label in sorted(SYSTEM_LABEL.values(), key=len, reverse=True):
        if rest.startswith(label):
            return label
    return rest.split(", ")[0].strip()


def _src_in(source: str) -> str:
    """Брокер очереди коротко: "в app-rabbitmq-1", у очередей helper'а и система: "в redis (Celery)" """
    w, _, rest = str(source or "").partition(", ")
    system = _system_of(rest) if rest else ""
    return where_in(w) + (f" ({system})" if system else "")


def tg_short(qs: list[dict]) -> tuple[str, list[str]] | None:
    """Короткий алерт "очередь не разбирается" (queue_stuck) по problems(): что - про первую (дольше
    всех), строка - сколько в очереди и как давно, вторая - скорость, обычный уровень и потребители,
    а если очередей несколько - остальные именами"""
    if not qs:
        return None
    q = qs[0]
    where, n = _src_in(q.get("source") or ""), tg_fmt.count(q.get("depth") or 0)
    age = int(q.get("age") or 0)
    kind = q.get("why") or ("stall" if q.get("stall") else "nocons" if q.get("consumers") == 0 else "grow")
    if kind == "stall":
        what = f"Очередь {q['name']} стоит"
        facts = [f"задача ждет {tg_fmt.dur(q.get('wait') or age)}, {n} {where}"]
    else:
        what = f"Очередь {q['name']} не разбирается"
        if kind == "nocons":
            head = f"нет потребителей {tg_fmt.dur(age)}"
        elif kind == "slow":
            head = f"{tg_fmt.dur(age)} не отдает ни одного"
        else:
            head = f"растет {tg_fmt.dur(age)}"
        facts = [f"{head}, {n} {where}"]
        more = []
        rate = (int(q.get("depth") or 0) - int(q.get("d0") or 0)) * 60 / age if age > 0 else 0
        if rate >= 1:
            more.append(f"+{tg_fmt.count(rate)} в мин")
        if q.get("base") is not None and kind != "nocons":
            more.append(f"обычно до {tg_fmt.count(q['base'])}")
        if q.get("consumers"):
            more.append(f"потребителей {q['consumers']}")
        if more:
            facts.append(", ".join(more))
    rest = [x["name"] for x in qs[1:] if x["name"] != q["name"]]
    if rest:
        # строк конкретики не больше двух: другие очереди важнее скорости первой
        facts = [facts[0], tg_fmt.fact("Еще", tg_fmt.names(rest))]
    return what, facts


def tg_short_failed(fp: list[dict]) -> tuple[str, list[str]] | None:
    """Короткий алерт "падают задачи" (queue_failed) по failed_problems(): "failed_jobs (Laravel),
    в 100 раз больше обычного"; остальные брокеры именами"""
    if not fp:
        return None

    def name(x: dict) -> str:
        w, _, rest = str(x.get("source") or "").partition(", ")
        system = _system_of(rest) if rest else ""
        if system == SYSTEM_LABEL["laravel_failed"]:
            system = "failed_jobs (Laravel)"
        place = where_in(w)
        return f"{system} {place}" if system else place

    x = fp[0]
    base = x.get("base")
    more = times_usual(int(x.get("h1") or 0), base)
    if not more and base is not None:
        more = "обычно задачи не падают"
    facts = [name(x) + (f", {more}" if more else "")]
    rest = [name(y) for y in fp[1:]]
    if rest:
        facts.append(tg_fmt.fact("Еще", tg_fmt.names(rest)))
    return "Падают задачи", facts


def tg_short_depth(queue: str, source: str, value: int | float) -> tuple[str, list[str]]:
    """Короткий алерт "очередь выше порога" (вид queue, ctx queue/source/value в collector):
    "Выросла очередь emails / 15 тыс. в app-rabbitmq-1". Порог не пишем: он в панели"""
    return f"Выросла очередь {queue or '?'}", [f"{tg_fmt.count(value or 0)} {_src_in(source or 'rabbitmq')}"]
