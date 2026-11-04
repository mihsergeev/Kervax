"""Повтор без отбоя: пока алерт открыт, одно и то же второй раз не уходит. Ни на следующем цикле, ни
после перезапуска панели (состояние в alert_state, а не в памяти), ни когда к беде добавилось новое
(сообщение начинается с нового, а не с уже объявленного), ни когда уровень качнулся вниз и обратно."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector
from app.alerts import full_text
from app.models import Server

SRC = "Supervisor, контейнер web-a-workers-1"


def _factory(path):
    from app.db import create_engine_and_factory

    return create_engine_and_factory(f"sqlite+aiosqlite:///{path.as_posix()}")


async def _env(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base

    engine, factory = _factory(tmp_path / "s.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Server(name="web-a", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=datetime.now(timezone.utc), cpu_alert_percent=0, mem_alert_percent=0,
                     last_report={"uptime_seconds": 100000}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    return engine, factory, Settings(alert_webhook="http://hook"), sent


async def _tick(factory, cfg, t, rep, state=None):
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.last_seen = t
        srv.last_report = {"uptime_seconds": 100000, "clock_unix": t.timestamp(), **rep}
        if state:
            srv.alert_state = {**(srv.alert_state or {}), **state}
        await s.commit()
    await collector.evaluate_servers(factory, cfg, t)


def _workers(t, fatal=(), looping=()):
    ts = int(t.timestamp())
    progs = [{"name": n, "state": "FATAL", "since": ts - 600, "up": 0, "pid": 0, "exit": 1, "r15": 0, "r60": 0}
             for n in fatal]
    progs += [{"name": n, "state": "RUNNING", "since": ts - 3600, "up": 60, "pid": 7, "exit": 0, "r15": 5,
               "r60": 5, "p60": 0} for n in looping]
    progs.append({"name": "queue-listen", "state": "RUNNING", "since": ts - 3600, "up": 3600, "pid": 8, "exit": 0,
                  "r15": 0, "r60": 0})
    return {"extras": {"workers": {"v": 1, "ts": ts, "skipped": [],
                                   "daemons": [{"kind": "supervisor", "where": "container web-a-workers-1",
                                                "progs": progs}]}}}


def _first_line(msg: str) -> str:
    """Первая строка полного текста (история алертов): в чат уходит короткий, подробности там"""
    return full_text(msg).split("\n", 1)[0]


def _short(msg: str) -> list[str]:
    """Короткий текст в чате без строки с эмодзи и именем: что случилось и конкретика"""
    return str(msg).split("\n")[1:]


async def test_sent_next_tick_and_restart_do_not_repeat(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    assert len(sent) == 1 and "воркер mailer" in full_text(sent[0])
    assert _short(sent[0]) == ["<b>Воркер не запускается</b>", "mailer в web-a-workers-1"]
    t = t0 + timedelta(minutes=1)
    await _tick(factory, cfg, t, _workers(t, fatal=["mailer"]))
    assert len(sent) == 1
    # перезапуск панели: новый движок на той же базе, в памяти ничего не осталось
    await engine.dispose()
    engine, factory = _factory(tmp_path / "s.db")
    for i in range(2, 5):
        t = t0 + timedelta(minutes=i)
        await _tick(factory, cfg, t, _workers(t, fatal=["mailer"]))
    assert len(sent) == 1
    async with factory() as s:
        st = (await s.execute(select(Server))).scalars().one().alert_state
    assert st["workers"] == 2 and st["workers_told"] == [f"{SRC}|mailer"] and st["workers_told_lvl"] == 2
    await engine.dispose()


async def test_new_trouble_in_an_open_alert_leads_the_message(tmp_path, monkeypatch):
    """Как было: о логе A сказали, потом по одному добавлялись B, C, D, и каждое сообщение
    начиналось с A - пять одинаковых с виду сообщений без отбоя, а D вообще ушел в "и еще 1"."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["billing", "cleanup", "digest"]))
    assert len(sent) == 1 and "воркер billing" in _first_line(sent[0])
    t = t0 + timedelta(minutes=1)
    await _tick(factory, cfg, t, _workers(t, fatal=["billing", "cleanup", "digest", "mailer"]))
    assert len(sent) == 2
    assert "воркер mailer" in _first_line(sent[1])
    full = full_text(sent[1])
    assert "billing" not in full and "cleanup" not in full and "digest" not in full
    assert "и еще 3, о них уже сообщали" in full
    assert _first_line(sent[1]) != _first_line(sent[0])
    # в чате новое тоже первым: конкретика начинается с mailer, а не с уже объявленного billing
    assert _short(sent[0])[1].startswith("billing") and _short(sent[1])[1].startswith("mailer")
    # дальше то же самое - тишина, и после перезапуска тоже
    await engine.dispose()
    engine, factory = _factory(tmp_path / "s.db")
    t = t0 + timedelta(minutes=2)
    await _tick(factory, cfg, t, _workers(t, fatal=["billing", "cleanup", "digest", "mailer"]))
    assert len(sent) == 2
    await engine.dispose()


async def test_many_new_at_once_show_count_of_told(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["aaa"]))
    t = t0 + timedelta(minutes=1)
    await _tick(factory, cfg, t, _workers(t, fatal=["aaa", "bbb", "ccc", "ddd", "eee"]))
    assert len(sent) == 2
    assert "воркер bbb" in _first_line(sent[1]) and "aaa" not in full_text(sent[1])
    assert "и еще 2, о 1 из них уже сообщали" in full_text(sent[1])
    assert _short(sent[1])[1].startswith("bbb") and "aaa" not in sent[1]
    await engine.dispose()


async def test_level_back_and_forth_is_one_message(tmp_path, monkeypatch):
    """Тот же воркер: не запускается (проблема) - падает и поднимается (предупреждение, молча) -
    снова не запускается. Это та же беда, о ней уже сказали"""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    assert len(sent) == 1
    for i in range(1, 7):
        t = t0 + timedelta(minutes=i)
        await _tick(factory, cfg, t, _workers(t, fatal=["mailer"]) if i % 2 == 0 else _workers(t, looping=["mailer"]))
    assert len(sent) == 1
    # отбой, и после него та же беда - снова новость
    t = t0 + timedelta(minutes=10)
    await _tick(factory, cfg, t, _workers(t))
    assert len(sent) == 2 and "✅" in sent[1]
    t = t0 + timedelta(minutes=11)
    await _tick(factory, cfg, t, _workers(t, fatal=["mailer"]))
    assert len(sent) == 3 and "воркер mailer" in full_text(sent[2])
    assert _short(sent[2]) == ["<b>Воркер не запускается</b>", "mailer в web-a-workers-1"]
    await engine.dispose()


async def test_escalation_from_warning_is_still_news(tmp_path, monkeypatch):
    """Предупреждение, потом впервые проблема - это хуже, чем было объявлено: сообщение уходит"""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    # падает и поднимается уже дольше выдержки (workers.LOOP_HOLD)
    held = {"workers_loop": {"Supervisor, контейнер web-a-workers-1|mailer": [int(t0.timestamp()) - 1900, int(t0.timestamp())]}}
    await _tick(factory, cfg, t0, _workers(t0, looping=["mailer"]), state=held)
    assert len(sent) == 1 and "падает и поднимается" in full_text(sent[0])
    assert _short(sent[0])[0] == "<b>Воркер падает и поднимается</b>"
    t = t0 + timedelta(minutes=1)
    await _tick(factory, cfg, t, _workers(t, fatal=["mailer"]))
    assert len(sent) == 2 and "не запускается" in full_text(sent[1])
    assert _short(sent[1])[0] == "<b>Воркер не запускается</b>"
    for i in range(2, 6):
        t = t0 + timedelta(minutes=i)
        await _tick(factory, cfg, t, _workers(t, fatal=["mailer"]) if i % 2 == 0 else _workers(t, looping=["mailer"]))
    assert len(sent) == 2
    await engine.dispose()


async def test_disk_latency_is_one_notice_across_devices_and_thresholds(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    for minute, (sda, sdb) in enumerate([(650, 300), (260, 320), (280, 680), (700, 210)]):
        t = start + timedelta(minutes=minute)
        await _tick(factory, cfg, t, {"disk_devs": [
            {"dev": "sda", "await": sda, "util": 25}, {"dev": "sdb", "await": sdb, "util": 30},
        ]}, {"disk_slow_since": (start - timedelta(minutes=30)).isoformat()})
    assert [msg.kind for msg in sent] == ["disk_slow"]

    # Sparse ticks below stand in for a continuous sequence of healthy reports.
    monkeypatch.setattr(collector.report_progress, "MAX_SAME_SECONDS", 3600)
    for minute in (5, 65, 81):
        await _tick(factory, cfg, start + timedelta(minutes=minute),
                    {"disk_devs": [{"dev": "sdb", "await": 5}]})
    assert [(msg.kind, msg.recovery) for msg in sent] == [("disk_slow", False), ("disk_slow", True)]

    later = start + timedelta(hours=9)
    await _tick(factory, cfg, later, {"disk_devs": [{"dev": "sda", "await": 300}]},
                {"disk_slow_since": (later - timedelta(minutes=30)).isoformat()})
    assert [(msg.kind, msg.recovery) for msg in sent][-1] == ("disk_slow", False)
    await engine.dispose()


async def test_open_legacy_condition_keeps_highest_announced_level(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    levels = {"level": 1}

    def conditions(*_args, **_kwargs):
        return {"net_health": (levels["level"], {"detail": "ошибки сети"})}

    monkeypatch.setattr(collector, "_server_conditions", conditions)
    await _tick(factory, cfg, start, {}, {"net_health": 2})
    for minute, level in enumerate((2, 1, 2, 3, 2, 3), start=1):
        levels["level"] = level
        await _tick(factory, cfg, start + timedelta(minutes=minute), {})
    assert [msg.kind for msg in sent] == ["net_health"]
    async with factory() as s:
        state = (await s.execute(select(Server))).scalars().one().alert_state
    assert state["net_health"] == state["net_health_told_lvl"] == 3
    await engine.dispose()


async def test_new_critical_pressure_cause_can_alert_once(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    start = datetime.now(timezone.utc) + timedelta(days=1)
    condition = {}

    def conditions(*_args, **_kwargs):
        return {"pressure": condition["value"]}

    monkeypatch.setattr(collector, "_server_conditions", conditions)
    for minute, (level, cause) in enumerate(((2, "io"), (1, "io"), (2, "dstate"), (1, "io"), (2, "dstate"))):
        condition["value"] = (level, {"res": cause, "detail": cause, "items": [(level, cause, cause)]})
        await _tick(factory, cfg, start + timedelta(minutes=minute), {})
    assert [msg.kind for msg in sent] == ["pressure", "pressure"]
    await engine.dispose()


def test_news_first_keeps_text_without_old():
    ctx = {"detail": "a; b", "sig": ["a", "b"], "parts": [("a", "a"), ("b", "b")]}
    # текст тот же; что нового - едет дальше в короткий текст ("fresh"), чтобы поставить его первым
    assert collector._news_first(ctx, {"a", "b"}) == {**ctx, "fresh": {"a", "b"}}
    assert collector._news_first(ctx, set())["detail"] == "a; b"
    assert collector._news_first(ctx, {"b"})["detail"] == "b; и еще 1, о них уже сообщали"
    assert collector._news_first({"detail": "x", "sig": ["x"]}, {"x"})["detail"] == "x"
