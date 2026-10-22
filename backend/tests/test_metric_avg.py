"""CPU, RAM, температуры и conntrack: в "Что сломано" и в алерте - то, что держит алерт, а не значение
последнего отчета. Повод - 22.10.2026: нода часами сидела выше порога (load 54 на 12 ядрах), в
минуту провала главная показывала "CPU 52%" рядом со "сломано", и строка выглядела ложной."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Server, ServerMetric


def _srv(now, rep, state=None, **kw):
    base = dict(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                alert_state=state or {}, alert_mutes=[], cpu_alert_percent=90, mem_alert_percent=90,
                disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=85,
                conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                web_5xx_alert_percent=0, kube_expiry_warn_days=[])
    base.update(kw)
    return Server(**base)


def _rows(s, now, kind):
    return [p["text"] for p in collector.server_problems(s, now) if p["kind"] == kind]


def test_row_names_the_average_and_when_the_alert_goes_away():
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=7)).isoformat()
    held = {"cpu": 1, "cpu_since": since, "cpu_from": since, "cpu_avg": {"v": 85, "min": 30}}
    # просела ниже линии снятия 3 минуты назад: среднее, сейчас и сколько осталось до отбоя
    s = _srv(now, {"cpu_percent": 52}, {**held, "cpu_cool": (now - timedelta(minutes=3)).isoformat()})
    assert _rows(s, now, "cpu") == ["CPU 85% за 30 мин, сейчас 52% - снимется, если продержится ниже 80% еще 12 мин"]
    # ниже линии, а цикл алертов еще не отметил начало: вся выдержка впереди
    s = _srv(now, {"cpu_percent": 52}, held)
    assert _rows(s, now, "cpu") == ["CPU 85% за 30 мин, сейчас 52% - снимется, если продержится ниже 80% 15 мин"]
    # между линией и порогом алерт держится: тоже сказано, когда снимется
    s = _srv(now, {"cpu_percent": 86}, held)
    assert _rows(s, now, "cpu") == ["CPU 86% - снимется, если продержится ниже 80% 15 мин"]
    # выше порога и близко к среднему - одно число, как раньше
    s = _srv(now, {"cpu_percent": 93}, {**held, "cpu_avg": {"v": 95, "min": 30}})
    assert _rows(s, now, "cpu") == ["CPU 93%"]
    # среднего еще нет (история не набралась) - текущее значение
    s = _srv(now, {"cpu_percent": 97}, {"cpu": 1, "cpu_since": since})
    assert _rows(s, now, "cpu") == ["CPU 97%"]


def test_row_for_temperature_has_no_release_line():
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=1)).isoformat()
    s = _srv(now, {"cpu_temp": 86}, {"temp": 1, "temp_since": since, "temp_avg": {"v": 93, "min": 30}})
    assert _rows(s, now, "temp") == ["CPU 93°C за 30 мин, сейчас 86°C"]


def test_average_starts_with_the_breach_and_looks_back_half_an_hour():
    now = datetime.now(timezone.utc)
    t = now.timestamp()
    hist = [(t - 50 * 60, {"cpu": 10.0}), (t - 20 * 60, {"cpu": 41.0}), (t - 10 * 60, {"cpu": 96.0}),
            (t - 5 * 60, {"cpu": 92.0}), (t, {"cpu": 94.0, "mem": 70.0})]
    # превышение 12 минут: среднее только по нему
    assert collector.metric_avg(hist, "cpu", (now - timedelta(minutes=12)).isoformat(), now) == {"v": 94, "min": 12}
    # превышение часами: последние полчаса
    assert collector.metric_avg(hist, "cpu", (now - timedelta(hours=7)).isoformat(), now) == {"v": 81, "min": 30}
    assert collector.metric_avg(hist, "cpu", None, now) is None
    assert collector.metric_avg([], "cpu", (now - timedelta(hours=1)).isoformat(), now) is None


async def test_alert_tells_the_average_over_the_hold(tmp_path, monkeypatch):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'avg.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     last_report={"cpu_percent": 99}, cpu_alert_percent=90, mem_alert_percent=0,
                     disk_alert_percent=0, alert_sustain_seconds=900, offline_after_seconds=120,
                     alert_state={"cpu_since": (now - timedelta(minutes=16)).isoformat()}))
        await s.commit()
        for m, v in ((40, 20), (15, 92), (10, 94), (5, 93), (1, 93)):
            s.add(ServerMetric(server_id=1, ts=now - timedelta(minutes=m), cpu_percent=v))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    assert len(sent) == 1 and "<b>CPU загружен на 93% в среднем за 16 мин</b>" in sent[0], sent
    async with factory() as s:
        st = (await s.scalars(select(Server))).one().alert_state
    # среднее - и в состоянии для главной, и как объявленное значение для "было N" в отбое
    assert st["cpu_avg"] == {"v": 93, "min": 16} and st["cpu_val"] == 93
    await engine.dispose()
