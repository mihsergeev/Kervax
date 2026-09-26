"""«Почему» в пороговом алерте и гаситель мигания.

Живой случай 18.09.2026, fi-hz-aff: рекламная сеть залила 2.3 млн переходов в сутки
вместо обычных шести тысяч. Пришёл алерт «CPU 92%», и разбор занял вечер, хотя всё
нужное панель уже знала: топ процессов лежит в том же отчёте, а трафик и соединения
в минутной истории. Плюс наплыв держится сутками, CPU ходит вокруг порога, и на
каждый заход прилетала пара «сработало - отбой».
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Server, ServerMetric

TOP_CPU = [
    {"comm": "php-fpm", "cpu": 42.0, "rss": 300 * 1024 ** 2},
    {"comm": "php-fpm", "cpu": 31.0, "rss": 280 * 1024 ** 2},
    {"comm": "queue-worker", "cpu": 28.0, "rss": 120 * 1024 ** 2},
    {"comm": "nginx", "cpu": 9.0, "rss": 40 * 1024 ** 2},
    {"comm": "sshd", "cpu": 0.7, "rss": 8 * 1024 ** 2},
]


def _report(cpu: float, net: float, conn: float) -> dict:
    return {
        "cpu_percent": cpu, "mem_total": 16 * 1024 ** 3, "mem_used": 4 * 1024 ** 3,
        "net_rx": net, "net_tx": net / 10, "sock_tcp": conn, "top_cpu": TOP_CPU,
        "top_mem": [{"comm": "postgres", "rss": 3 * 1024 ** 3, "shared": 2 * 1024 ** 3},
                    {"comm": "clickhouse", "rss": 900 * 1024 ** 2}],
    }


def test_top_eaters_sums_processes_of_one_name():
    # у php-fpm полсотни воркеров: по отдельности каждый мелкий, вместе - вся нагрузка
    assert collector.top_eaters(_report(90, 0, 0), "cpu") == "php-fpm 73%, queue-worker 28%, nginx 9%"
    # мелочь ниже порога не упоминаем, sshd с его 0.7% в список не попал
    assert "sshd" not in collector.top_eaters(_report(90, 0, 0), "cpu")
    # память считаем приватную: shared_buffers постгреса это не его личные гигабайты
    assert collector.top_eaters(_report(90, 0, 0), "mem") == "postgres 1.0 ГБ, clickhouse 900 МБ"
    assert collector.top_eaters({}, "cpu") == ""


def test_surge_needs_a_real_stream():
    base = {"net": 200_000.0, "conn": 300.0}
    # трафик в 40 раз выше нормы этого часа
    assert "трафик x44" in collector.cause_text(_report(92, 8_000_000, 300), "cpu", base)
    # рост есть, но поток сам по себе мелкий: ночная нода, проснувшаяся под бэкап
    assert "трафик" not in collector.cause_text(_report(92, 700_000, 300), "cpu", {"net": 1000.0})
    # нормы нет (истории мало) - хвост только про процессы
    assert collector.cause_text(_report(92, 8_000_000, 300), "cpu", {}) == (
        " - сверху php-fpm 73%, queue-worker 28%, nginx 9%"
    )


async def _panel(tmp_path, name="srv.db"):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


async def test_cpu_alert_says_who_eats_and_that_it_is_a_surge(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(
            name="fi-hz-aff", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now, last_report=_report(92, 9_000_000, 4000),
            cpu_alert_percent=90, mem_alert_percent=0, disk_alert_percent=0,
            alert_sustain_seconds=0, offline_after_seconds=120,
        ))
        await s.commit()
        # обычный день этого же часа: трафик 200 КБ/с, соединений 300
        for d in range(1, 8):
            for m in (-20, 0, 20):
                s.add(ServerMetric(
                    server_id=1, ts=now - timedelta(days=d) + timedelta(minutes=m),
                    cpu_percent=30, net_rx=180_000, net_tx=20_000, sock_tcp=300,
                ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    assert len(sent) == 1, sent
    assert "CPU 92% ≥ 90%" in sent[0]
    assert "сверху php-fpm 73%, queue-worker 28%" in sent[0]
    assert "трафик x50 и соединений x13 к обычному для этого часа" in sent[0]
    await engine.dispose()


async def test_cpu_alert_without_history_looks_as_before(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path, "srv2.db")
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now, last_report={"cpu_percent": 95}, cpu_alert_percent=90,
            mem_alert_percent=0, disk_alert_percent=0, alert_sustain_seconds=0,
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    # ни процессов, ни истории: хвоста нет, «{cause}» в текст не протекает
    assert len(sent) == 1 and sent[0].endswith("CPU 95% ≥ 90%") and "cause" not in sent[0]
    await engine.dispose()


async def test_metric_walking_around_threshold_goes_quiet(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path, "flap.db")
    start = datetime.now(timezone.utc) - timedelta(hours=5)
    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=start, last_report={"cpu_percent": 95}, cpu_alert_percent=90,
            mem_alert_percent=0, disk_alert_percent=0, alert_sustain_seconds=0,
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    async def tick(minutes: int, cpu: float) -> list[str]:
        sent.clear()
        now = start + timedelta(minutes=minutes)
        async with factory() as s:
            srv = await s.scalar(select(Server))
            srv.last_seen = now
            srv.last_report = {"cpu_percent": cpu}
            await s.commit()
        await collector.evaluate_servers(factory, settings, now)
        return list(sent)

    # два захода за порог и обратно: пишем как раньше, по паре сообщений
    assert any("CPU 95%" in x for x in await tick(0, 95))
    assert any("вернулся" in x or "снова" in x for x in await tick(30, 40))
    assert any("CPU 95%" in x for x in await tick(60, 95))
    assert await tick(90, 40) != []
    # третий заход за те же 6 часов - одно сообщение про мигание, дальше тишина
    third = await tick(120, 95)
    assert len(third) == 1 and "ходит вокруг порога" in third[0]
    assert await tick(150, 40) == []
    assert await tick(180, 95) == []
    # окно кончилось, метрика в норме - короткий отбой, и снова можно алертить
    lift = await tick(60 * 7, 40)
    assert len(lift) == 1 and "CPU снова в норме" in lift[0]
    assert any("CPU 95%" in x for x in await tick(60 * 7 + 30, 95))
    await engine.dispose()


def test_web_rate_block_is_taken_only_while_fresh():
    now = datetime.now(timezone.utc)
    fresh = {"web-rate": {"ts": now.timestamp() - 30, "rpm": 6042, "logs": []}}
    assert collector.web_rate_total(fresh, now) == 6042
    # хелпер встал: вчерашний поток не должен выглядеть как сегодняшний
    old = {"web-rate": {"ts": now.timestamp() - 3600, "rpm": 6042}}
    assert collector.web_rate_total(old, now) is None
    assert collector.web_rate_total(None, now) is None
    assert collector.web_rate_total({"web-rate": {"rpm": 10}}, now) is None


async def test_requests_are_preferred_over_bytes(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path, "rpm.db")
    now = datetime.now(timezone.utc)
    rep = _report(92, 300_000, 900)
    rep["extras"] = {"web-rate": {"ts": now.timestamp() - 20, "rpm": 6000, "logs": [
        {"log": "/var/log/nginx/winbet.access.log", "rpm": 5800, "sites": ["play-winbet.top"]}]}}
    async with factory() as s:
        s.add(Server(
            name="fi-hz-aff", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now, last_report=rep, cpu_alert_percent=90, mem_alert_percent=0,
            disk_alert_percent=0, alert_sustain_seconds=0,
        ))
        await s.commit()
        for d in range(1, 8):
            for m in (-20, 0, 20):
                s.add(ServerMetric(
                    server_id=1, ts=now - timedelta(days=d) + timedelta(minutes=m),
                    cpu_percent=30, net_rx=180_000, net_tx=20_000, sock_tcp=300, web_rpm=150,
                ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    # редиректы по 300 байт канал почти не шевелят, поэтому о байтах молчим, а запросы
    # выросли в 40 раз - о них и говорим
    assert len(sent) == 1 and "запросов к веб-серверу x40 к обычному для этого часа" in sent[0]
    assert "трафик" not in sent[0]
    await engine.dispose()


async def test_report_stores_requests_per_minute(client, auth_headers):
    """Блок хелпера приезжает в extras как есть, панель кладёт сумму в тайм-серию:
    иначе не с чем сравнивать наплыв и нечего рисовать на графике."""
    import time

    r = await client.post("/api/servers", json={"name": "web1"}, headers=auth_headers)
    token = r.json()["token"]
    sid = r.json()["server"]["id"]
    report = {
        "hostname": "h1", "os": "Ubuntu 24.04", "agent_version": "2.9",
        "cpu_percent": 12.5, "mem_used": 50, "mem_total": 100,
        "extras": {"web-rate": {"ts": int(time.time()), "rpm": 6042, "e5": 12, "logs": [
            {"log": "/var/log/nginx/winbet.access.log", "rpm": 6000, "e5": 12,
             "sites": ["play-winbet.top"]},
            {"log": "/var/log/pods/ingress-nginx_ingress-nginx-controller-abc_uid/controller/0.log",
             "name": "ingress-nginx/ingress-nginx-controller-abc", "rpm": 42, "e5": 0}]}},
    }
    r = await client.post("/api/agent/report", json=report,
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    r = await client.get(f"/api/servers/{sid}/metrics", headers=auth_headers)
    # ошибки едут той же строкой: 0.1-0.3% 5xx синтетический монитор не увидит, а
    # счётчик по логам - да (ради этого и считаем коды ответа)
    assert r.json()[0]["web_rpm"] == 6042 and r.json()[0]["web_5xx"] == 12


async def test_dead_pod_of_a_controller_alerts_and_jobs_do_not(tmp_path, monkeypatch):
    """uz-air-op-dg, 23.09.2026: ClickHouse и PostgreSQL лежали по семь часов (не нашлись
    секрет и сертификат), и панель не сказала об этом ни разу. Поды Job'ов при этом
    алертить нельзя: на одной ноде их набралось 19 штук с ImagePullBackOff."""
    engine, factory = await _panel(tmp_path, "pods.db")
    now = datetime.now(timezone.utc)
    pods = [
        {"ns": "default", "name": "postgres-0", "phase": "Running", "ready": False,
         "owner": "StatefulSet", "reason": "CrashLoopBackOff", "restarts": 83},
        {"ns": "tech1", "name": "cron-aggregate-29661745-758l6", "phase": "Pending",
         "ready": False, "owner": "Job", "reason": "ImagePullBackOff"},
        {"ns": "default", "name": "envoy-gateway-fc4", "phase": "Running", "ready": True,
         "owner": "ReplicaSet"},
    ]
    async with factory() as s:
        s.add(Server(
            name="uz-air-op-dg", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now, cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0,
            alert_sustain_seconds=0,
            last_report={"cpu_percent": 5, "kube": {"present": True, "access": True, "pods": pods}},
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1, sent
    assert "default/postgres-0 (CrashLoopBackOff, 83 рестарта)" in sent[0]
    assert "cron-aggregate" not in sent[0]  # Job - не наша забота

    # повторно молчим
    sent.clear()
    await collector.evaluate_servers(factory, settings, now + timedelta(minutes=5))
    assert sent == []

    # под поднялся - отбой
    async with factory() as s:
        srv = await s.scalar(select(Server))
        ok = [dict(p) for p in pods]
        ok[0].update(ready=True, reason="")
        srv.last_report = {"cpu_percent": 5, "kube": {"present": True, "access": True, "pods": ok}}
        srv.last_seen = now + timedelta(minutes=10)
        await s.commit()
    await collector.evaluate_servers(factory, settings, now + timedelta(minutes=10))
    assert len(sent) == 1 and "снова в норме" in sent[0]
    await engine.dispose()


def test_web_error_rule_needs_a_real_count_and_a_real_share():
    # балансер из разбора: 0.2% ошибок при большом потоке - повод
    assert collector.web_error_level(total=500_000, errs=1_000, points=15, threshold=0.05)
    # одна случайная 503 на маленькой ноде - 0.09%, но будить из-за неё нельзя
    assert not collector.web_error_level(total=1_115, errs=1, points=15, threshold=0.05)
    # ошибок много, но на таком потоке это шум: 0.001%
    assert not collector.web_error_level(total=3_000_000, errs=30, points=15, threshold=0.05)
    # окно не набралось (только что раскатили хелпер) - молчим
    assert not collector.web_error_level(total=50_000, errs=500, points=3, threshold=0.05)
    # правило выключено
    assert not collector.web_error_level(total=50_000, errs=500, points=15, threshold=0)


async def test_5xx_alert_names_where_the_errors_are(tmp_path, monkeypatch):
    """Кейс 23.09: ingress-nginx на балансере отдавал 503 на 0.1-0.28% запросов, и
    заметили это по жалобе. Теперь панель говорит сама и показывает, в каком логе."""
    engine, factory = await _panel(tmp_path, "e5.db")
    now = datetime.now(timezone.utc)
    rep = {"cpu_percent": 20, "extras": {"web-rate": {"ts": now.timestamp(), "rpm": 40000, "e5": 80, "logs": [
        {"log": "/var/log/pods/ingress-nginx_ingress-nginx-controller-569b_uid/controller/0.log",
         "name": "ingress-nginx/ingress-nginx-controller-569b", "rpm": 39000, "e5": 80},
        {"log": "/var/log/nginx/access.log", "sites": ["my.advcake.com"], "rpm": 1000, "e5": 0},
    ]}}}
    async with factory() as s:
        s.add(Server(
            name="ru-vk-abalancer-wn9", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now, last_report=rep, cpu_alert_percent=0, mem_alert_percent=0,
            disk_alert_percent=0, alert_sustain_seconds=900, web_5xx_alert_percent=0.05,
        ))
        await s.commit()
        # 15 минут потока: 40 тысяч запросов в минуту, из них 80 - 503 (0.2%)
        for m in range(15):
            s.add(ServerMetric(server_id=1, ts=now - timedelta(minutes=m), cpu_percent=20,
                               web_rpm=40000, web_5xx=80))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    assert len(sent) == 1, sent
    assert "ошибки 5xx: 0.20% за 15 минут (1200 из 600000)" in sent[0]
    assert "сверху ingress-nginx/ingress-nginx-controller-569b 80/мин" in sent[0]
    await engine.dispose()
