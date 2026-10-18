"""Очередь растет и не разбирается (queue_watch): ход очереди, правило без порога, алерт, "Что сломано",
карточка."""
from datetime import datetime, timedelta, timezone

from app import collector, queue_watch as qw
from app.models import Server

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
SRC = "контейнер app-rabbitmq-1"


def _rep(**depths):
    qs = [{"name": n, "vhost": "/", "ready": d, "unacked": 0} for n, d in depths.items()]
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": int(NOW.timestamp()),
            "services": [{"kind": "rabbitmq", "source": SRC, "queues": qs, "total": len(qs)}]}


def _srv(state=None, rep=None, mutes=None):
    return Server(id=1, name="app-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def _run(series, name="mail", step_s=60):
    """Прогон ряда глубин раз в step_s секунд, конец - в NOW: состояние и сколько очередь застряла"""
    st: dict = {}
    t0 = NOW - timedelta(seconds=step_s * (len(series) - 1))
    for i, d in enumerate(series):
        st = qw.update(st, _rep(**{name: d}), t0 + timedelta(seconds=step_s * i))
    return st, qw.stuck(st[f"{SRC}|//{name}"], series[-1], int(NOW.timestamp()))


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
    # пара писем в час, три часа ни одного не забрали
    rare = [i // 30 for i in range(181)]
    assert _run(rare)[1] == 180 * 60
    # хоть одно забрали - не мертвый
    taken = rare[:100] + [rare[100] - 1] + rare[101:]
    assert _run(taken)[1] == 0
    # стоит и не растет - не судим (очередь никто не пишет)
    assert _run([7] * 200)[1] == 0
    # без потребителей (агент 2.38) хватает получаса
    st, _a = _run([i // 5 for i in range(41)])
    e = st[f"{SRC}|//mail"]
    assert qw.stuck(e, 8, int(NOW.timestamp())) == 0
    assert qw.stuck(e, 8, int(NOW.timestamp()), consumers=0) == 2400
    assert qw.stuck(e, 8, int(NOW.timestamp()), consumers=2) == 0


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
    assert qw.view(s, NOW) == {f"{SRC}|//mail": {"age": 2400, "d0": 0, "muted": False}}
    # без потребителей (агент 2.38) - так и сказано
    rep["services"][0]["queues"][0]["consumers"] = 0
    assert "нет ни одного потребителя" in qw.problems(_srv({"qwatch": st}, rep), NOW)[0]["text"]
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
    bs = qw.redis_brokers(rep)
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
    rows = {p["kind"]: p for p in collector.server_problems(s, NOW)}
    assert rows["queue"]["text"] == "переполнены очереди: document_indexing (200)"


def test_queues_helper_is_needed_where_redis_is():
    from app import setup_scripts
    assert setup_scripts.setup_needed("queues-setup", {"db_engines": ["PostgreSQL", "Redis"]})
    assert setup_scripts.setup_needed("queues-setup", {"extras": {"queues": {"brokers": []}}})
    assert not setup_scripts.setup_needed("queues-setup", {"db_engines": ["PostgreSQL"]})


def test_snooze_accepts_a_queue_key():
    from app.schemas import AlertSnoozeIn
    k = "qstuck:под default/redis-0, Celery, db 1|//document_indexing"
    assert AlertSnoozeIn(kind=k, hours=24).kind == k


def test_helper_script():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "agent" / "queues-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.2" in src and "/bin/sh -c" in src and 'KERVAX_SETUP_WHEN="' in src
    assert "REDISCLI_AUTH" in src and "\r" not in src
    # пароль только через stdin, не в командной строке
    assert "redis-cli -a" not in src and "--pass" not in src and "--requirepass \"$" not in src
