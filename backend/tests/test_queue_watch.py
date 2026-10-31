"""Очередь растет и не разбирается (queue_watch): ход очереди, правило без порога, алерт, "Что сломано",
карточка."""
from datetime import datetime, timedelta, timezone
import os
import re
import shutil
import subprocess

import pytest

from app import collector, queue_watch as qw
from app.models import Server

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
SRC = "контейнер app-rabbitmq-1"


def _rep(consumers=None, **depths):
    qs = [{"name": n, "vhost": "/", "ready": d, "unacked": 0} for n, d in depths.items()]
    if consumers is not None:
        for q in qs:
            q["consumers"] = consumers
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": int(NOW.timestamp()),
            "services": [{"kind": "rabbitmq", "source": SRC, "queues": qs, "total": len(qs)}]}


def _srv(state=None, rep=None, mutes=None):
    return Server(id=1, name="app-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def _run(series, name="mail", step_s=60, consumers=None):
    """Прогон ряда глубин раз в step_s секунд, конец - в NOW: состояние и сколько очередь застряла"""
    st: dict = {}
    t0 = NOW - timedelta(seconds=step_s * (len(series) - 1))
    for i, d in enumerate(series):
        st = qw.update(st, _rep(consumers=consumers, **{name: d}), t0 + timedelta(seconds=step_s * i))
    return st, qw.stuck(st[f"{SRC}|//{name}"], series[-1], int(NOW.timestamp()), consumers)


def _watch(series, cons, st=None, end=NOW):
    """Цикл алертов раз в минуту по ряду (глубина, потребители), конец - в end: состояние и на какой
    минуте очередь впервые стала проблемой (алерт), None - не стала"""
    st = dict(st or {})
    t0 = end - timedelta(minutes=len(series) - 1)
    fired = None
    for i, (d, c) in enumerate(zip(series, cons)):
        t = t0 + timedelta(minutes=i)
        rep = _rep(consumers=c, mail=d)
        st = qw.update(st, rep, t)
        ps = qw.problems(_srv({"qwatch": st}, rep), t)
        if ps and fired is None:
            fired = (i, ps[0])
    return st, fired


def test_growing_queue_nobody_takes():
    # 40 минут растет по 5 в минуту: 0 -> 200
    st, age = _run([5 * i for i in range(41)])
    assert age == 2400
    # 20 минут - еще рано
    assert _run([5 * i for i in range(21)])[1] == 0
    # выросла мало (пара сообщений в минуту за полчаса не дает сотни) - не по первому правилу
    assert _run([i for i in range(41)])[1] == 0


def test_saw_queue_is_alive():
    # живой воркер разбирает пачками: пила от 100 до 300 два часа, конец на подъеме
    saw = [100 + (i % 10) * 20 for i in range(121)]
    assert _run(saw)[1] == 0
    # мелкие откаты на подъеме (меньше 10% от пика) ход не рвут: не успевают
    behind = [i * 100 - (120 if i % 4 == 0 and i else 0) for i in range(41)]
    assert _run(behind)[1] == 2400


def test_dead_worker_of_a_rare_queue():
    # пара писем в час, три часа ни одного не забрали, а в очереди и так больше сотни
    rare = [100 + i // 30 for i in range(181)]
    assert _run(rare)[1] == 180 * 60
    # хоть одно забрали - не мертвый
    taken = rare[:100] + [rare[100] - 1] + rare[101:]
    assert _run(taken)[1] == 0
    # стоит и не растет - не судим (очередь никто не пишет)
    assert _run([107] * 200)[1] == 0
    # без потребителей (агент 2.38) три часа ждать незачем: их нет 40 минут, а сообщения копятся
    st, age = _run([100 + i // 5 for i in range(41)], consumers=0)
    assert age == 2400
    e = st[f"{SRC}|//mail"]
    assert qw.why(e, 108, int(NOW.timestamp()), 0) == ("nocons", 2400)
    assert qw.stuck(e, 108, int(NOW.timestamp()), consumers=2) == 0


def test_a_dozen_messages_is_not_an_alert():
    # потребитель есть, но забирает пачкой раз в несколько часов: 0 -> 16 за четыре часа. Алерт
    # будил человека, а через две минуты воркер все разбирал и приходил отбой
    assert _run([i // 15 for i in range(241)])[1] == 0
    # и без потребителей десяток сообщений - не повод
    st, _a = _run([i // 5 for i in range(41)])
    assert qw.stuck(st[f"{SRC}|//mail"], 8, int(NOW.timestamp()), consumers=0) == 0


def test_gap_in_reports_starts_over():
    st, _age = _run([5 * i for i in range(41)])
    key = f"{SRC}|//mail"
    later = NOW + timedelta(minutes=30)
    st2 = qw.update(st, _rep(mail=400), later)
    assert st2[key]["t0"] == int(later.timestamp()) and st2[key]["d0"] == 400
    # очередь пропала из отчета больше чем на час - забыта
    assert key not in qw.update(st, _rep(other=1), NOW + timedelta(hours=2))


def test_problems_text_skip_and_mute():
    st: dict = {}
    for i in range(41):
        t = NOW - timedelta(minutes=40 - i)
        st = qw.update(st, _rep(mail=50 * i, **{"mail.dlq": 50 * i, "celeryev.1f2e": 50 * i}), t)
    rep = _rep(mail=2000, **{"mail.dlq": 2000, "celeryev.1f2e": 2000})
    s = _srv({"qwatch": st}, rep)
    ps = qw.problems(s, NOW)
    assert [p["text"] for p in ps] == [f"очередь mail ({SRC}) растет 40 мин и не разбирается: 0 -> 2 000 сообщений"]
    assert ps[0]["since"].startswith("2026-10-18T11:20")
    assert qw.view(s, NOW) == {f"{SRC}|//mail": {"age": 2400, "d0": 0, "muted": False, "stall": False, "wait": 0,
                                                  "why": "grow"}}
    # ноль потребителей в одном отчете - еще не "нет потребителей" (перезапуск воркера): растет, как и было
    rep["services"][0]["queues"][0]["consumers"] = 0
    assert "растет 40 мин" in qw.problems(_srv({"qwatch": st}, rep), NOW)[0]["text"]
    # заглушенная очередь - не проблема, в карточке видна с отметкой
    muted = _srv({"qwatch": st}, rep, mutes=[qw.MUTE_PREFIX + f"{SRC}|//mail"])
    assert qw.problems(muted, NOW) == []
    assert qw.view(muted, NOW)[f"{SRC}|//mail"]["muted"] is True


def test_alert_condition_and_home():
    st: dict = {}
    for i in range(41):
        st = qw.update(st, _rep(mail=50 * i), NOW - timedelta(minutes=40 - i))
    s = _srv({"qwatch": st}, _rep(mail=2000))
    lv, ctx = collector._server_conditions(s, NOW)["queue_stuck"]
    assert lv == 2 and ctx["sig"] == [f"{SRC}|//mail"] and ctx["detail"].startswith("очередь mail")
    rows = [r for r in collector.server_problems(s, NOW) if r["kind"] == "queue_stuck"]
    assert len(rows) == 1 and rows[0]["section"] == "services" and rows[0]["sec"] == "queues"
    assert rows[0]["mute"] == qw.MUTE_PREFIX + f"{SRC}|//mail"
    # заглушили после алерта: отбой не шлем, это не "стихло"
    muted = _srv({"qwatch": st}, _rep(mail=2000), mutes=[rows[0]["mute"]])
    assert collector._sig_all_quiet(muted, "queue_stuck", {f"{SRC}|//mail"}, NOW)
    assert collector._server_conditions(muted, NOW)["queue_stuck"][0] == 0


# ---- очереди в Redis (helper queues-setup) ----

def _redis_rep(depth):
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": int(NOW.timestamp()),
            "extras": {"queues": {"v": 1, "ts": 1, "skipped": [{"where": "container cache", "why": "auth"}],
                                  "brokers": [
                {"system": "celery", "where": "pod default/redis-0", "db": 1, "ns": "", "total": 1,
                 "queues": [{"name": "document_indexing", "ready": depth}]},
                {"system": "bullmq", "where": "container app-redis", "db": 0, "ns": "bull:", "total": 1,
                 "queues": [{"name": "email", "ready": 3, "unacked": 1}]}]}}}


def test_redis_queues_are_brokers_too():
    rep = _redis_rep(7)
    bs = qw.helper_brokers(rep)
    assert [b["source"] for b in bs] == ["под default/redis-0, Celery, db 1", "контейнер app-redis, BullMQ bull:"]
    assert bs[0]["where"] == "Redis, под default/redis-0, db 1"
    assert bs[1]["where"] == "Redis, контейнер app-redis, префикс bull:"
    q = {x["name"]: x for x in qw.queues(rep)}
    assert q["document_indexing"]["key"] == "под default/redis-0, Celery, db 1|//document_indexing"
    assert q["document_indexing"]["kind"] == "celery" and q["email"]["depth"] == 4
    assert qw.redis_skipped(rep) == ["контейнер cache"]
    # растет полчаса и не разбирается - как у RabbitMQ
    st: dict = {}
    for i in range(41):
        st = qw.update(st, _redis_rep(5 * i), NOW - timedelta(minutes=40 - i))
    s = _srv(state={"qwatch": st}, rep=_redis_rep(200))
    p = qw.problems(s, NOW)
    assert [x["name"] for x in p] == ["document_indexing"]
    assert p[0]["text"] == ("очередь document_indexing (под default/redis-0, Celery, db 1) растет 40 мин и не "
                            "разбирается: 0 -> 200 сообщений")


def test_mute_a_queue_for_a_while():
    st: dict = {}
    for i in range(41):
        st = qw.update(st, _redis_rep(5 * i), NOW - timedelta(minutes=40 - i))
    key = "под default/redis-0, Celery, db 1|//document_indexing"
    s = _srv(state={"qwatch": st}, rep=_redis_rep(200))
    s.alert_snoozes = {qw.MUTE_PREFIX + key: (NOW + timedelta(days=1)).isoformat()}
    assert qw.problems(s, NOW) == [] and qw.view(s, NOW)[key]["muted"] is True
    assert qw.mute_view(s, NOW) == {key: {"until": (NOW + timedelta(days=1)).isoformat()}}
    # срок вышел - снова алертит
    s.alert_snoozes = {qw.MUTE_PREFIX + key: (NOW - timedelta(minutes=1)).isoformat()}
    assert [x["name"] for x in qw.problems(s, NOW)] == ["document_indexing"]
    # насовсем
    s.alert_snoozes = {}
    s.alert_mutes = [qw.MUTE_PREFIX + key]
    assert qw.muted_keys(s, NOW) == {key}
    # порог глубины у заглушенной очереди тоже молчит
    s.queue_alert_depth = 100
    kinds = {p["kind"] for p in collector.server_problems(s, NOW)}
    assert "queue" not in kinds and "queue_stuck" not in kinds
    s.alert_mutes = []
    s.alert_state = {**s.alert_state, "queues": {key: 1}}  # алерт по порогу объявлен
    rows = {p["kind"]: p for p in collector.server_problems(s, NOW)}
    assert rows["queue"]["text"] == "переполнены очереди: document_indexing (200)"


def test_queues_helper_is_needed_where_redis_is():
    from app import setup_scripts
    assert setup_scripts.setup_needed("queues-setup", {"db_engines": ["PostgreSQL", "Redis"]})
    assert setup_scripts.setup_needed("queues-setup", {"extras": {"queues": {"brokers": []}}})
    # 0.3: очереди бывают и в таблицах баз
    assert setup_scripts.setup_needed("queues-setup", {"db_engines": ["PostgreSQL"]})
    assert setup_scripts.setup_needed("queues-setup", {"db_engines": ["MySQL/MariaDB"]})
    assert not setup_scripts.setup_needed("queues-setup", {"db_engines": ["ClickHouse"]})
    assert not setup_scripts.setup_needed("queues-setup", {"db_engines": []})


def _table_rep(ready):
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": int(NOW.timestamp()),
            "extras": {"queues": {"v": 1, "ts": 1, "brokers": [
                {"system": "yii", "engine": "pg", "where": "host", "base": "app", "table": "queue", "total": 1,
                 "queues": [{"name": "default", "ready": ready, "unacked": 2}]},
                {"system": "laravel", "engine": "mysql", "where": "container shop-db", "base": "shop",
                 "table": "tbl_jobs", "total": 0, "queues": []},
                {"system": "hangfire", "engine": "pg", "where": "pod jobs/pg-0", "base": "app",
                 "table": "hangfire.jobqueue", "total": 1, "queues": [{"name": "critical", "ready": 1}]}],
                "skipped": [{"where": "container cache", "why": "auth"},
                            {"where": "container legacy-db", "why": "auth", "engine": "mysql"},
                            {"where": "host", "why": "big", "engine": "pg", "base": "crm", "table": "queue",
                             "system": "yii", "rows": 3200000}]}}}


def test_queue_tables_are_brokers_too():
    rep = _table_rep(5)
    bs = qw.helper_brokers(rep)
    assert [b["engine"] for b in bs] == ["pg", "mysql", "pg"]
    # таблица по умолчанию в подписи не названа, своя - названа
    assert [b["source"] for b in bs] == ["на хосте, Yii, база app", "контейнер shop-db, Laravel, база shop, таблица tbl_jobs",
                                         "под jobs/pg-0, Hangfire, база app"]
    assert bs[0]["where"] == "PostgreSQL, на хосте, база app"
    assert bs[1]["where"] == "MySQL, контейнер shop-db, база shop, таблица tbl_jobs" and bs[1]["queues"] == []
    q = {x["name"]: x for x in qw.queues(rep)}
    assert q["default"]["key"] == "на хосте, Yii, база app|//default" and q["default"]["depth"] == 7
    # пропуски Redis и баз раздельно
    assert qw.redis_skipped(rep) == ["контейнер cache"]
    assert qw.db_skipped(rep) == [{"why": "auth", "where": "MySQL, контейнер legacy-db"},
                                  {"why": "big", "where": "PostgreSQL, на хосте, Yii, база crm, таблица queue", "rows": 3200000}]
    # судятся как остальные: растет 40 минут и не разбирается
    st: dict = {}
    for i in range(41):
        st = qw.update(st, _table_rep(5 * i), NOW - timedelta(minutes=40 - i))
    p = qw.problems(_srv(state={"qwatch": st}, rep=_table_rep(200)), NOW)
    assert [x["name"] for x in p] == ["default"]
    assert p[0]["text"].startswith("очередь default (на хосте, Yii, база app) растет 40 мин")


def test_snooze_accepts_a_queue_key():
    from app.schemas import AlertSnoozeIn
    k = "qstuck:под default/redis-0, Celery, db 1|//document_indexing"
    assert AlertSnoozeIn(kind=k, hours=24).kind == k


def test_helper_script():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "agent" / "queues-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.6" in src and "/bin/sh -c" in src and 'KERVAX_SETUP_WHEN="' in src
    # 0.5: клиенты в чужих namespace только от пользователя экземпляра, root контейнера - nobody
    assert 'nsenter -t "$P" -n -m' not in src and 'nsenter -t "$P" -m -n --' not in src
    assert src.count('nsenter -t "$P" $') >= 4 and "u=65534; g=65534" in src and '"/proc/$1/root$f"' not in src
    assert "pgrep -x postgres" in src and "pgrep -x mysqld" in src
    assert "REDISCLI_AUTH" in src and "\r" not in src
    # пароль только через stdin или окружение, не в командной строке
    assert "redis-cli -a" not in src and "--pass" not in src and "--requirepass \"$" not in src
    assert 'PGPASSWORD="$pw"' in src and 'MYSQL_PWD="$pw"' in src and "-p$" not in src
    # в базе только чтение и короткие запросы
    assert "default_transaction_read_only=on" in src and "statement_timeout=5000" in src
    assert "set session transaction read only" in src and "max_execution_time" in src
    # 0.4: самая старая задача и упавшие; обратный слэш в JSON удваивается так, что mawk его не теряет
    assert "enqueued_at" in src and "ZREVRANGEBYSCORE" in src and "failed_jobs" in src and "FAIL_EVERY=270" in src
    assert '_jn = split(s, _jp, "\\\\")' in src and 'gsub(/\\\\/, "\\\\\\\\", s)' not in src
    # mysql без --raw удваивает обратные слэши сам
    assert "-N -B -r --force" in src
    # секреты в строке ошибки прячутся
    assert "function mask(" in src and '"***"' in src


def test_helper_quotes_catalog_identifiers():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "agent" / "queues-setup.sh").read_text(encoding="utf-8")
    funcs = re.search(r"\n(db_ident\(\).*?\n}\ndb_table\(\).*?\n})\nepsql\(\)", src, re.S).group(1)
    assert src.count('t=$(db_table "$2" "$3") || return 1') == 2
    assert 't=$(db_table "$sch" "$tbl") || continue' in src
    bash = shutil.which("bash")
    if not bash or "system32" in bash.lower():
        pytest.skip("нет обычного bash")
    code = funcs + """
ENG=pg
    db_table "$PG_SCHEMA" "$PG_TABLE"
printf '\\n'
ENG=mysql
    db_table "$MY_SCHEMA" "$MY_TABLE"
printf '\\n'
"""
    env = {**os.environ, "PG_SCHEMA": 'tenant"schema', "PG_TABLE": 'q";select 1;-- _jobs',
           "MY_SCHEMA": 'tenant`schema', "MY_TABLE": 'q`;select 1;-- _jobs'}
    out = subprocess.run([bash, "-c", code], env=env, capture_output=True, text=True, timeout=15)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        '"tenant""schema"."q"";select 1;-- _jobs"',
        '`tenant``schema`.`q``;select 1;-- _jobs`',
    ]


# ---- 0.4: очередь стоит (самая старая задача), гистерезис, упавшие задачи ----

def _old_rep(depth, oldest):
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": int(NOW.timestamp()),
            "extras": {"queues": {"v": 1, "ts": 1, "brokers": [
                {"system": "sidekiq", "where": "container app-redis", "db": 0, "ns": "", "total": 1,
                 "queues": [{"name": "mailers", "ready": depth, "oldest": oldest}]}]}}}


def test_a_queue_that_stands_still():
    key = "контейнер app-redis, Sidekiq|//mailers"
    t0 = int((NOW - timedelta(hours=2)).timestamp())
    # 300 задач никто не берет: глубина не меняется, самая старая та же - правила роста молчат
    st: dict = {}
    for i in range(41):
        st = qw.update(st, _old_rep(300, t0), NOW - timedelta(minutes=40 - i))
    e = st[key]
    assert e["oc"] == t0 and e["ocs"] == int((NOW - timedelta(minutes=40)).timestamp())
    assert qw.stuck(e, 300, int(NOW.timestamp())) == 0
    assert qw.stalled(e, 300, int(NOW.timestamp())) == 7200
    p = qw.problems(_srv(state={"qwatch": st}, rep=_old_rep(300, t0)), NOW)
    assert p[0]["text"] == ("очередь mailers (контейнер app-redis, Sidekiq) стоит: самая старая задача ждет 2 ч 0 мин, "
                            "в очереди 300") and p[0]["stall"] is True
    assert qw.view(_srv(state={"qwatch": st}, rep=_old_rep(300, t0)), NOW)[key]["wait"] == 7200
    # очередь движется: самая старая каждый раз другая (воркер забирает по одной, кладут новые)
    st = {}
    for i in range(41):
        st = qw.update(st, _old_rep(300, t0 + 60 * i), NOW - timedelta(minutes=40 - i))
    assert qw.stalled(st[key], 300, int(NOW.timestamp())) == 0
    # десяток задач - не повод, даже если стоят
    st = {}
    for i in range(41):
        st = qw.update(st, _old_rep(12, t0), NOW - timedelta(minutes=40 - i))
    assert qw.stalled(st[key], 12, int(NOW.timestamp())) == 0
    # ждет меньше часа - рано
    t1 = int((NOW - timedelta(minutes=50)).timestamp())
    st = {}
    for i in range(41):
        st = qw.update(st, _old_rep(300, t1), NOW - timedelta(minutes=40 - i))
    assert qw.stalled(st[key], 300, int(NOW.timestamp())) == 0


def test_hysteresis_of_the_minimum_depth():
    st, _a = _run([5 * i for i in range(41)])
    e = st[f"{SRC}|//mail"]
    ts = int(NOW.timestamp())
    # алерт ушел при 200; в очереди 90 - еще не отбой (без алерта такая очередь не судится), 40 - отбой
    assert qw.stuck(e, 90, ts) == 0 and qw.stuck(e, 90, ts, held=True) == 2400
    assert qw.stuck(e, 40, ts, held=True) == 0
    # held берется из того, о чем алерт уже ушел (queue_stuck_sig)
    s = _srv({"qwatch": st, "queue_stuck_sig": [f"{SRC}|//mail"]}, _rep(mail=90))
    assert [p["name"] for p in qw.problems(s, NOW)] == ["mail"]
    assert qw.problems(_srv({"qwatch": st}, _rep(mail=90)), NOW) == []


def _fail_rep(h1, d1, span=86400, system="sidekiq", **kw):
    b = {"system": system, "where": "container app-redis", "db": 0, "ns": "", "total": 1,
         "queues": [{"name": "default", "ready": 0}],
         "failed": {"h1": h1, "d1": d1, "span": span,
                    "top": [{"q": "mailers", "job": "MailerJob", "n": 40}, {"q": "default", "job": "", "n": 2}],
                    "err": "Net::SMTPAuthenticationError: 535 password=***", "err_q": "mailers", "err_job": "MailerJob"}}
    b.update(kw)
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": int(NOW.timestamp()),
            "extras": {"queues": {"v": 1, "ts": 1, "brokers": [b]}}}


def test_spike_of_failed_jobs():
    # обычно ~2 в час, за последний час 300 - всплеск
    f = qw.helper_brokers(_fail_rep(300, 346))[0]["failed"]
    assert qw.spike(f) and round(qw._base(f)) == 2
    # много, но так всегда (около 200 в час) - не всплеск
    assert not qw.spike(qw.helper_brokers(_fail_rep(300, 300 + 23 * 200))[0]["failed"])
    # мало - не алерт, даже если обычно ноль
    assert not qw.spike(qw.helper_brokers(_fail_rep(60, 60))[0]["failed"])
    # истории меньше четырех часов: обычного не знаем, нужно 500
    assert not qw.spike(qw.helper_brokers(_fail_rep(300, 300, span=3600))[0]["failed"])
    assert qw.spike(qw.helper_brokers(_fail_rep(600, 600, span=3600))[0]["failed"])
    # алерт ушел - отбой, когда за час меньше половины порога
    f = qw.helper_brokers(_fail_rep(70, 116))[0]["failed"]
    assert not qw.spike(f) and qw.spike(f, held=True)
    s = _srv(rep=_fail_rep(300, 346))
    p = qw.failed_problems(s, NOW)
    assert p[0]["key"] == "контейнер app-redis, Sidekiq"
    assert p[0]["text"] == ("всплеск упавших задач (контейнер app-redis, Sidekiq): 300 за час, обычно около 2 в час; "
                            "чаще всего MailerJob в очереди mailers\n↳ Net::SMTPAuthenticationError: 535 password=***")
    v = qw.helper_view(s)[0]["failed"]
    assert v["spike"] is True and v["h1"] == 300 and v["top"][0]["job"] == "MailerJob"
    # мьют брокера: насовсем и на время
    s.alert_mutes = [qw.FAIL_PREFIX + "контейнер app-redis, Sidekiq"]
    assert qw.failed_problems(s, NOW) == []
    assert qw.mute_view(s, NOW, qw.FAIL_PREFIX) == {"контейнер app-redis, Sidekiq": {"perm": True}}
    s.alert_mutes = []
    s.alert_snoozes = {qw.FAIL_PREFIX + "контейнер app-redis, Sidekiq": (NOW + timedelta(days=1)).isoformat()}
    assert qw.failed_problems(s, NOW) == []


def test_failed_jobs_alert_and_home():
    s = _srv(rep=_fail_rep(300, 346))
    lv, ctx = collector._server_conditions(s, NOW)["queue_failed"]
    assert lv == 2 and ctx["sig"] == ["контейнер app-redis, Sidekiq"] and ctx["detail"].startswith("всплеск упавших задач")
    rows = [r for r in collector.server_problems(s, NOW) if r["kind"] == "queue_failed"]
    assert len(rows) == 1 and rows[0]["sec"] == "queues" and rows[0]["mute"] == "qfail:контейнер app-redis, Sidekiq"
    # заглушили после алерта - отбой не шлем
    s.alert_mutes = [rows[0]["mute"]]
    assert collector._sig_all_quiet(s, "queue_failed", {"контейнер app-redis, Sidekiq"}, NOW)
    assert collector._server_conditions(_srv(rep=_fail_rep(3, 20)), NOW)["queue_failed"][0] == 0
    from app.schemas import AlertSnoozeIn
    assert AlertSnoozeIn(kind="qfail:контейнер app-redis, Sidekiq", hours=24).kind.startswith("qfail:")


def test_laravel_failed_jobs_is_a_broker_of_its_own():
    rep = _fail_rep(5, 9, span=4000, system="laravel_failed", engine="pg", base="app", table="failed_jobs", queues=[], total=0)
    b = qw.helper_brokers(rep)[0]
    assert b["label"] == "Laravel failed_jobs" and b["source"] == "контейнер app-redis, Laravel failed_jobs, база app"
    assert b["queues"] == [] and b["failed"]["span"] == 4000 and qw.queues(rep) == []


# ---- мигание: перезапуск воркера, разбор без потребителей, наплыв, выдержка ----

def test_consumers_gone_for_a_minute_is_not_an_alert():
    # воркер перезапустился: две минуты без потребителей, очередь подросла и тут же разобрана
    series = [300 + (i % 5) * 10 for i in range(30)] + [340, 380] + [300 + (i % 5) * 10 for i in range(30)]
    cons = [3] * 30 + [0, 0] + [3] * 30
    assert _watch(series, cons)[1] is None
    # и девять минут без потребителей - еще не алерт
    series = [300] * 20 + [300 + 20 * i for i in range(1, 10)] + [300] * 10
    cons = [3] * 20 + [0] * 9 + [3] * 10
    assert _watch(series, cons)[1] is None


def test_consumers_gone_for_good():
    # потребителей нет с 10-й минуты, сообщения копятся по 10 в минуту: условие с 11-й, алерт через
    # 10 минут выдержки - на 21-й, текст про потребителей
    series = [300] * 10 + [300 + 10 * i for i in range(30)]
    cons = [3] * 10 + [0] * 30
    st, fired = _watch(series, cons)
    assert fired is not None and fired[0] == 21
    p = fired[1]
    assert p["why"] == "nocons" and p["age"] == 660 and p["d0"] == 300
    assert p["text"] == f"очередь mail ({SRC}) без потребителей 11 мин, сообщения копятся: 300 -> 410 сообщений"
    # стоит без потребителей и не растет - никто не пишет, не судим
    assert _watch([300] * 40, [0] * 40)[1] is None


def test_queue_consumed_without_consumers():
    # воркер берет сообщения опросом (basic.get): потребителей всегда ноль, очередь копится 45 минут
    # и уходит за минуту. Первый раз это не отличить от умершего воркера, дальше - видно
    cycle = [6 * i for i in range(45)] + [0] * 5
    st, fired = _watch(cycle, [0] * 50)
    assert fired is not None and fired[1]["why"] == "nocons"
    e = st[f"{SRC}|//mail"]
    assert qw.pull(e, int(NOW.timestamp()))
    st, fired = _watch(cycle * 3, [0] * 150, st=st, end=NOW + timedelta(minutes=150))
    assert fired is None
    # второй и дальше заходы до 264 не выше обычного: тот самый ход, который уже разбирали
    assert qw.usual(st[f"{SRC}|//mail"], int(NOW.timestamp()) + 150 * 60) == 264
    # через неделю без таких разборов отсутствие потребителей снова судим
    later = int(e["zc"]) + qw.PULL_KEEP + 60
    assert not qw.pull(e, later)


def _history(peak, days=6, name="mail", consumers=15):
    """Неделя истории: каждые сутки очередь доходила до peak, и ее разбирали (провалы в отчетах между
    сутками ход не держат, а пики разобранных ходов помнятся)"""
    st: dict = {}
    for k in range(days, 0, -1):
        for i, d in enumerate((peak // 2, peak, 0)):
            st = qw.update(st, _rep(consumers=consumers, **{name: d}), NOW - timedelta(days=k) + timedelta(minutes=i))
    return st


def test_growth_within_the_usual_level_is_not_an_alert():
    # потребители живы и забирают, приток временно больше: 50 -> 473 за 40 минут. Днем очередь
    # ходит до 1 200, это наплыв, а не "не разбирается"
    burst = [50 + 423 * i // 40 for i in range(41)]
    st, fired = _watch(burst, [15] * 41, st=_history(1200))
    assert fired is None
    assert qw.usual(st[f"{SRC}|//mail"], int(NOW.timestamp())) == 1200
    # а 5 000 при обычных 1 200 - уже алерт, с обычным уровнем в тексте
    big = [50 + 5000 * i // 40 for i in range(41)]
    st, fired = _watch(big, [15] * 41, st=_history(1200))
    assert fired is not None and fired[1]["why"] == "grow" and fired[1]["base"] == 1200
    assert fired[1]["text"].endswith(", обычно до 1 200")
    # ни одного разобранного хода за неделю - обычного не знаем, правило только по росту
    assert qw.usual({"dm": {"1": 5}}, int(NOW.timestamp())) is None
    assert qw.usual(_history(1200, days=9)[f"{SRC}|//mail"], int(NOW.timestamp())) == 1200


def test_alert_waits_ten_minutes():
    # рос полчаса и через девять минут разобран: раньше алерт и отбой через 9 минут, теперь тишина
    series = [50 + 14 * i for i in range(39)] + [40] * 5
    assert _watch(series, [15] * 44)[1] is None
    # растет и дальше - алерт на 40-й минуте (условие с 30-й плюс выдержка)
    st, fired = _watch([50 + 14 * i for i in range(50)], [15] * 50)
    assert fired is not None and fired[0] == 40 and fired[1]["why"] == "grow"


def test_standing_queue_with_consumers():
    # потребители есть, но за три часа не отдали ни одного сообщения: очередь стоит
    series = [500 + i // 20 for i in range(200)]
    st, fired = _watch(series, [2] * 200)
    assert fired is not None and fired[0] == 190 and fired[1]["why"] == "slow"
    assert "не отдает ни одного сообщения" in fired[1]["text"]
    # хоть одно отдала - тишина
    taken = series[:100] + [series[99] - 1] + series[101:]
    assert _watch(taken, [2] * 200)[1] is None
