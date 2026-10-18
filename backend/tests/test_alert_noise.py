"""Антишум в цикле алертов: одна беда - одно сообщение (что уже сказано, пока алерт открыт, не
новость, даже если ушло и вернулось), копии воркера - одна беда, распорядок (то же давление в то же
время каждый день) не алертит."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector
from app.models import Server


async def _env(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 's.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True,
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


def _workers(t, *bad):
    progs = [{"name": f"listen:listen_{i:02d}", "state": "RUNNING", "since": int(t.timestamp()) - 3600, "up": 60,
              "pid": 7, "exit": 0, "r15": 5 if f"{i:02d}" in bad else 0, "r60": 5, "p60": 0} for i in range(15)]
    if "mailer" in bad:
        progs.append({"name": "mailer", "state": "FATAL", "since": int(t.timestamp()) - 600, "up": 0, "pid": 0,
                      "exit": 1, "r15": 0, "r60": 0})
    return {"extras": {"workers": {"v": 1, "ts": int(t.timestamp()), "skipped": [],
                                   "daemons": [{"kind": "supervisor", "where": "container feed-supervisor", "progs": progs}]}}}


async def test_copies_of_a_worker_coming_back_one_by_one_are_one_message(tmp_path, monkeypatch):
    """Пятнадцать копий слушателя падали по очереди: каждая вернувшаяся в беду давала новое
    сообщение, десять за полтора часа."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, "00", "01"))
    assert len(sent) == 1 and "воркер listen (Supervisor, контейнер feed-supervisor) падает и поднимается" in sent[0]
    assert "так 2 копии из 15" in sent[0]
    for i, bad in enumerate([("02",), ("00", "02"), ("05",), ("00", "01", "02", "03")]):
        t = t0 + timedelta(minutes=2 * (i + 1))
        await _tick(factory, cfg, t, _workers(t, *bad))
    assert len(sent) == 1
    # другая программа сломалась - это новость
    t = t0 + timedelta(minutes=12)
    await _tick(factory, cfg, t, _workers(t, "00", "mailer"))
    assert len(sent) == 2 and "mailer" in sent[1]
    await engine.dispose()


async def test_old_copy_keys_in_an_open_alert_are_not_news(tmp_path, monkeypatch):
    """Алерт открыт панелью до 1.4.273, sig по копиям: тот же воркер под ключом программы - не новость"""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, "03", "07"), state={
        "workers": 1, "workers_sig": ["Supervisor, контейнер feed-supervisor|listen:listen_03"]})
    assert sent == []
    await engine.dispose()


def _logs(t, *names):
    return {"extras": {"logerr": {"v": 1, "ts": int(t.timestamp()), "logs": 9, "read": 1000, "skipped": 0,
                                  "sources": [{"where": f"container {n}", "container": "", "h1": 5000, "d1": 5000,
                                               "lines1": 9000, "span": 1800, "last_ts": int(t.timestamp()) - 60,
                                               "last": "ERROR boom"} for n in names]}}}


async def test_what_was_told_and_came_back_is_not_news(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    steps = [("a",), ("a", "b"), ("a",), ("a", "b"), ("a",), ("a", "b")]
    counts = []
    for i, names in enumerate(steps):
        t = t0 + timedelta(minutes=2 * i)
        await _tick(factory, cfg, t, _logs(t, *names))
        counts.append(len(sent))
    # a - алерт, b - новое, дальше b уходит и возвращается: молчим
    assert counts == [1, 2, 2, 2, 2, 2]
    # отбой, и после него b - снова новость
    t = t0 + timedelta(minutes=20)
    await _tick(factory, cfg, t, _logs(t))
    assert len(sent) == 3 and "✅" in sent[2]
    t = t0 + timedelta(minutes=22)
    await _tick(factory, cfg, t, _logs(t, "b"))
    assert len(sent) == 4
    await engine.dispose()


def test_routine_of():
    day = 86400
    now = 10 * day + 13 * 3600 + 13 * 60  # день 10, 13:13
    h = [[now - day - 300, now - day + 6000], [now - 2 * day + 600, now - 2 * day + 6600]]
    a, b = collector.routine_of(h, now)
    assert a == now - 300 and b == now + 6600
    # только вчера - еще не распорядок
    assert collector.routine_of(h[:1], now) is None
    # позавчера в другое время
    assert collector.routine_of([h[0], [now - 2 * day + 3 * 3600, now - 2 * day + 4 * 3600]], now) is None
    # не прошло само (нет конца) или шло дольше 6 часов - не плановая задача
    assert collector.routine_of([h[0], [now - 2 * day, None]], now) is None
    assert collector.routine_of([h[0], [now - 2 * day, now - 2 * day + 7 * 3600]], now) is None
    # история: новый эпизод, конец последнего, старше 8 дней уходит
    hs = collector.routine_start([[now - 9 * day, now - 9 * day + 60]] + h, now)
    assert hs[-1] == [now, None] and len(hs) == 3
    assert collector.routine_end(hs, now + 60)[-1] == [now, now + 60]


def _psi(on):
    return {"psi": {"io_full_60": 60 if on else 0, "io_some_60": 60 if on else 0}}


async def test_pressure_at_the_same_time_every_day_is_routine(tmp_path, monkeypatch):
    """На сервере бэкапов "диск не успевает" приходил каждый день в 13:13 и отбивался к 15:00"""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    base = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=10, minute=13, second=0, microsecond=0)
    calm = timedelta(seconds=collector._CALM_S + 60)

    async def episode(day, start_min, minutes, extra=()):
        t = base + timedelta(days=day, minutes=start_min)
        await _tick(factory, cfg, t, _psi(True), state={"pressure_since": (t - timedelta(minutes=20)).isoformat()})
        for m in extra:
            tt = t + timedelta(minutes=m)
            await _tick(factory, cfg, tt, _psi(True))
        end = t + timedelta(minutes=minutes)
        await _tick(factory, cfg, end, _psi(False))
        await _tick(factory, cfg, end + calm, _psi(False))

    await episode(0, 0, 100)
    assert len(sent) == 2 and "✅" in sent[1]          # первый раз - алерт и отбой
    await episode(1, 5, 100)
    assert len(sent) == 4                               # второй день - тоже (одного дня мало)
    await episode(2, -10, 100)
    assert len(sent) == 4                               # третий день в то же время - распорядок, тишина
    # четвертый день: в то же время, но держится на час дольше обычного конца - алерт с пометкой
    await episode(3, 0, 300, extra=(60, 200))
    assert len(sent) == 6
    assert "сегодня держится дольше" in sent[4] and "✅" in sent[5]
    await engine.dispose()


def test_io_cause_does_not_blame_an_idle_disk():
    """Процессы ждут диск, а sda занят на 0%: ждут не его (сетевая ФС, хранилище гипервизора)"""
    assert collector.io_cause({"disk_devs": [{"dev": "sda", "util": 0.3, "await": 1.8}]}) == (
        " - при этом локальные диски почти свободны (самый занятый sda на 0%)")
    assert collector.io_cause({"disk_devs": [{"dev": "sdb", "util": 91, "await": 40}, {"dev": "sda", "util": 5}]}) == (
        " - sdb занят на 91%, задержка 40 мс")
