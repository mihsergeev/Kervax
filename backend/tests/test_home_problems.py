"""Все, о чем шлют алерты, видно и в "Что сломано" на главной: часы, DNS, коннекты СУБД, 5xx,
очереди, сроки кластера и Flux, дампы и проверки бэкапов. Повод - 08.10.2026: про сдвиг часов
узнали, только открыв карточку сервера, а про DNS - из разговора, главная молчала."""
from datetime import datetime, timedelta, timezone

from app import collector


def _srv(now, rep, state=None, **kw):
    from app.models import Server

    base = dict(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                alert_state=state or {}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                web_5xx_alert_percent=0, kube_expiry_warn_days=[])
    base.update(kw)
    return Server(**base)


def _probs(s, now, kind, web_err=None):
    return [p for p in collector.server_problems(s, now, web_err=web_err) if p["kind"] == kind]


def test_clock_skew_is_cleaned_of_the_report_delay():
    t = 1_000_000.0
    w = None
    # часы точные, а часть отчетов шла секунды: перед каждым агент резолвит имя панели
    for i, raw in enumerate([0, 6, 5, 0, 7, 1]):
        w = collector.clock_skew_window(w, raw, t + 15 * i)
        assert w["win"][0][1] == 0
    # медленные отчеты подряд дольше окна - тогда и минимум вырастет (окно 5 минут)
    for i in range(25):
        w = collector.clock_skew_window(w, 6, t + 100 + 15 * i)
    assert w["win"][0][1] == 6
    # нода отстает на 40 с, ее поправили - видно сразу: минимум упал
    w = None
    for i in range(5):
        w = collector.clock_skew_window(w, 40, t + 15 * i)
    w = collector.clock_skew_window(w, 0, t + 90)
    assert w["win"][0][1] == 0
    # нода спешит на 40 с, ее поправили: один отчет еще может быть задержкой (до 15 с),
    # второй подряд - уже часы, окно с начала
    w = None
    for i in range(5):
        w = collector.clock_skew_window(w, -40, t + 15 * i)
    w = collector.clock_skew_window(w, 0, t + 90)
    assert w["win"][0][1] == -40
    w = collector.clock_skew_window(w, 1, t + 105)
    assert w["win"][0][1] == 1
    # мусор из старой записи не роняет прием отчета
    assert collector.clock_skew_window({"win": [["x"], 5], "last": "y"}, 3, t)["win"] == [[int(t), 3]]


async def test_report_keeps_the_minimum_skew(client, auth_headers):
    import time

    r = await client.post("/api/servers", json={"name": "clk"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]

    async def report(lag: int):
        rep = {"agent_version": "2.25", "mem_used": 1, "mem_total": 2, "clock_unix": int(time.time()) - lag}
        r = await client.post("/api/agent/report", json=rep, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    await report(0)
    await report(6)  # отчет шел 6 с: медленный DNS, а часы в порядке
    r = await client.get("/api/servers", headers=auth_headers)
    srv = next(x for x in r.json() if x["id"] == sid)
    assert abs(srv["last_report"]["clock_skew_sec"]) <= 1
    assert not [p for p in srv["problems"] if p["kind"] == "clock"]


def test_clock_skew_levels():
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    ts = now.timestamp()
    s = _srv(now, {"clock_unix": ts, "clock_skew_sec": 3})
    assert _probs(s, now, "clock") == []
    s.last_report = {"clock_unix": ts, "clock_skew_sec": -7}
    s.alert_state = {"clock_since": (now - timedelta(minutes=2)).isoformat()}
    got = _probs(s, now, "clock")
    assert [(p["level"], p["mute"], p["sec"]) for p in got] == [(1, "clock@1", "clock")]
    assert got[0]["text"] == "часы разошлись с панелью на 7 с"
    assert got[0]["since"] == s.alert_state["clock_since"]
    s.last_report = {"clock_unix": ts, "clock_skew_sec": 45}
    assert [(p["level"], p["mute"]) for p in _probs(s, now, "clock")] == [(2, "clock@2")]
    s.last_report = {"clock_unix": ts, "clock_skew_sec": 400}
    got = _probs(s, now, "clock")
    assert [(p["level"], p["mute"], p["text"]) for p in got] == [(3, "clock", "часы разошлись с панелью на 6 мин")]


def test_db_connections_and_rabbitmq_queues():
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    rep = {"clock_unix": now.timestamp(),
           "db_stats": [{"engine": "postgres", "container": "pg", "conn_used": 95, "conn_max": 100},
                        {"engine": "mysql", "conn_used": 1, "conn_max": 100}],
           "services": [{"kind": "rabbitmq", "source": "rmq", "queues": [
               {"name": "jobs", "vhost": "/", "ready": 5000, "unacked": 10},
               {"name": "mail", "vhost": "/", "ready": 3, "unacked": 0}]}]}
    s = _srv(now, rep, db_conn_alert_percent=90, queue_alert_depth=1000)
    got = _probs(s, now, "db_conn")
    assert [(p["level"], p["section"], p["text"]) for p in got] == [
        (2, "services", "pg: занято 95 из 100 подключений (95%)")]
    got = _probs(s, now, "queue")
    assert [(p["level"], p["section"], p["text"]) for p in got] == [
        (2, "services", "переполнены очереди RabbitMQ: jobs (5010)")]
    # порог 0 - не сторожим
    s.db_conn_alert_percent, s.queue_alert_depth = 0, 0
    assert _probs(s, now, "db_conn") == [] and _probs(s, now, "queue") == []


def test_web_5xx_while_errors_go():
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    s = _srv(now, {"clock_unix": now.timestamp()}, web_5xx_alert_percent=5)
    per_log = {"nginx": {1: 40, 2: 30, 3: 30}}
    got = _probs(s, now, "web_5xx", web_err=(1000.0, 100.0, 15, per_log, 0.0))
    assert [(p["level"], p["sec"], p["text"]) for p in got] == [
        (2, "web", "ошибки 5xx: 10.0% запросов за 15 минут")]
    # ответы сканерам не в счет, без окна (одиночный сервер) - молчим
    assert _probs(s, now, "web_5xx", web_err=(1000.0, 100.0, 15, {}, 100.0)) == []
    assert _probs(s, now, "web_5xx") == []


def test_kube_expiry_and_flux():
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    ts = now.timestamp()
    day = 86400
    rep = {"clock_unix": ts, "kube_expiry": [
        {"kind": "flux-token", "where": "flux-system/flux", "expires": ts + 5 * day},
        {"kind": "cluster-cert", "where": "/pki/server.crt", "expires": ts + 300 * day}]}
    s = _srv(now, rep, kube_expiry_warn_days=[14, 7, 1])
    got = _probs(s, now, "kube_expiry")
    assert [(p["level"], p["section"], p["sec"], p["text"]) for p in got] == [
        (1, "kuber", "expiry", "токен Flux в секрете flux-system/flux: истекает через 5 дн.")]
    rep["kube_expiry"][0]["expires"] = ts + day / 2
    assert [p["level"] for p in _probs(s, now, "kube_expiry")] == [2]
    rep["kube_expiry"][0]["expires"] = ts - 2 * day
    assert [p["level"] for p in _probs(s, now, "kube_expiry")] == [3]
    s.kube_expiry_warn_days = []
    assert _probs(s, now, "kube_expiry") == []

    s.last_report = {"clock_unix": ts, "flux": [
        {"kind": "GitRepository", "where": "flux-system/flux-system", "ready": False,
         "reason": "GitOperationFailed", "message": "authentication required"}]}
    # пока идет выкат, зависимые минутами не готовы: до выдержки алерта не показываем
    assert _probs(s, now, "flux_down") == []
    since = (now - timedelta(minutes=20)).isoformat()
    s.alert_state = {"flux_down_since": since, "flux_down_from": now.isoformat()}
    got = _probs(s, now, "flux_down")
    assert [(p["level"], p["section"], p["sec"], p["since"]) for p in got] == [(2, "kuber", "flux", since)]
    assert got[0]["text"].startswith("Flux: доставка встала, GitRepository flux-system/flux-system - ")


def test_db_dumps_cronjobs_and_repo_checks():
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    ts = now.timestamp()
    bk = {"present": True, "configured": True, "metric_present": True, "success": 1,
          "last_backup_ts": ts - 3600,
          "dumps": [{"engine": "postgres", "container": "pg", "files": 0, "last_ts": 0, "enabled_ts": ts - 5 * 86400},
                    {"engine": "mysql", "files": 3, "last_ts": ts - 7200, "skipped": True, "skip_free_pct": 4}]}
    s = _srv(now, {"clock_unix": ts, "backup": bk})
    # сломанный дамп - после выдержки алерта, пропущенный из-за места - сразу
    assert _probs(s, now, "backup_dump") == []
    got = _probs(s, now, "backup_dump_space")
    assert [(p["section"], p["srv"], p["text"]) for p in got] == [
        ("backups", False, "дамп пропущен, мало места (свободно 4%): mysql")]
    s.alert_state = {"backup_dump_from": now.isoformat()}
    got = _probs(s, now, "backup_dump")
    assert [(p["level"], p["section"], p["srv"], p["text"]) for p in got] == [
        (2, "backups", False, "дамп СУБД не обновляется: postgres@pg")]
    # сам бэкап упал - про дампы молчим, это строка "бэкап завершился с ошибкой"
    bk["success"] = 0
    assert _probs(s, now, "backup_dump") == [] and _probs(s, now, "backup_dump_space") == []

    s.last_report = {"clock_unix": ts, "kube": {"access": True, "cronjobs": [
        {"ns": "db", "name": "pg-backup", "image": "postgres:16", "suspend": True}]}}
    got = _probs(s, now, "backup_cron")
    assert [(p["section"], p["srv"], p["text"]) for p in got] == [
        ("backups", False, "дамп-CronJob не отрабатывает: db/pg-backup (приостановлен)")]

    extra = {"v": 1, "ts": ts - 60, "repos": {
        "r1": {"check_ts": ts - 86400, "check_ok": 0}, "r2": {"check_ts": ts - 86400, "check_ok": 1},
        "r3": {"check_ts": ts - 86400, "check_ok": 0}}}
    s.last_report = {"clock_unix": ts, "extras": {"backup-server": extra},
                     "backup_server": {"present": True, "running": True,
                                       "repos": [{"name": "r1"}, {"name": "r2"}, {"name": "r3"}]}}
    s.backup_repo_mutes = ["r3"]
    got = _probs(s, now, "backup_check")
    assert [(p["section"], p["srv"], p["text"]) for p in got] == [
        ("backups", True, "не прошла проверка целостности: r1")]


def test_offline_node_shows_nothing_but_offline():
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    s = _srv(now - timedelta(hours=1), {"clock_unix": now.timestamp(), "clock_skew_sec": 400})
    assert collector.server_problems(s, now) == []
