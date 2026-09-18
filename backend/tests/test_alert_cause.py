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
