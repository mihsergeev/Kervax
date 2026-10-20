"""Алерт заполнения диска: выдержка, гистерезис отбоя, быстрое заполнение и мигание (app/fill_alert.py)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, fill_alert
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Server, ServerMetric

NOW = datetime(2026, 10, 20, 21, 0, tzinfo=timezone.utc)


def _pts(start: datetime, values: list[float]) -> list[tuple[float, float]]:
    """История раз в минуту, последняя точка - минуту назад от start + len."""
    return [((start + timedelta(minutes=i)).timestamp(), v) for i, v in enumerate(values)]


def test_fast_fill_needs_steady_growth_not_one_jump():
    t0 = NOW - timedelta(minutes=10)
    # копирование в бэкап: +3.5% в минуту все десять минут, до 100% три минуты
    steady = _pts(t0, [53 + 3.5 * i for i in range(10)])
    f = fill_alert.fast_fill(steady, 88.0, NOW)
    assert f and f["rate"] == 3.5 and f["eta_min"] == 3
    # образ распаковался: ровно, ровно, рывок на 5% за минуту - это не авария
    jump = _pts(t0, [81.5] * 8 + [82.6, 86.5])
    assert fill_alert.fast_fill(jump, 87.0, NOW) is None
    # рост есть, но до 100% больше получаса
    slow = _pts(t0, [80 + 0.1 * i for i in range(10)])
    assert fill_alert.fast_fill(slow, 81.0, NOW) is None
    # дыра в отчетах: две точки на краях - не повод
    assert fill_alert.fast_fill([(t0.timestamp(), 60.0)], 90.0, NOW) is None
    assert fill_alert.fast_fill(None, 90.0, NOW) is None


def test_step_holds_then_alerts_and_releases_with_hysteresis():
    st: dict = {}

    def tick(minutes: float, value: float, raw: int) -> int:
        lvl, ex = fill_alert.step(st, "disk", value, raw, 85.0, NOW + timedelta(minutes=minutes))
        st.update({"disk_since": ex["since"], "disk_cool": ex["cool"]})
        return lvl

    assert tick(0, 86, 1) == 0
    assert tick(29, 86, 1) == 0
    assert tick(30, 86, 1) == 1
    st["disk"] = 1  # алерт ушел
    # ниже порога, но выше линии отбоя (85 - 3): все еще та же беда, отбоя нет
    assert tick(31, 84.9, 0) == 1
    assert tick(90, 82.5, 0) == 1
    # 92% - эскалация сразу, обратно 86% - уровень не падает
    assert tick(91, 92, 2) == 2
    st["disk"] = 2
    assert tick(92, 86, 1) == 2
    # ниже линии отбоя: держится полчаса - отбой; заход выше линии сбрасывает отсчет
    assert tick(100, 70, 0) == 2
    assert tick(120, 83, 0) == 2
    assert tick(121, 70, 0) == 2
    assert tick(150, 70, 0) == 2
    assert tick(151, 70, 0) == 0


def test_step_dip_below_threshold_restarts_hold_and_crit_is_immediate():
    st: dict = {}

    def tick(minutes: float, value: float, raw: int) -> int:
        lvl, ex = fill_alert.step(st, "disk", value, raw, 85.0, NOW + timedelta(minutes=minutes))
        st.update({"disk_since": ex["since"], "disk_cool": ex["cool"]})
        return lvl

    assert tick(0, 85.2, 1) == 0
    assert tick(20, 84.8, 0) == 0  # нырнул ниже порога - отсчет заново
    assert tick(21, 85.1, 1) == 0
    assert tick(50, 85.1, 1) == 0
    assert tick(51, 85.3, 1) == 1
    assert fill_alert.step({}, "disk", 96, 3, 85.0, NOW)[0] == 3  # критично - без выдержки


async def _panel(tmp_path, **srv):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'fill.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=NOW, cpu_alert_percent=0, mem_alert_percent=0, offline_after_seconds=120,
                     disk_warn_percent=85, disk_alert_percent=90, disk_crit_percent=95, **srv))
        await s.commit()
    return engine, factory


def _sender(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    return sent


async def _tick(factory, at: datetime, pct: float) -> None:
    """Отчет агента: худший раздел pct, точка истории - как пишет ingest."""
    async with factory() as s:
        srv = await s.scalar(select(Server))
        srv.last_seen = at
        srv.last_report = {"uptime_seconds": 100000,
                           "disks": [{"mount": "/", "used": pct, "total": 100}]}
        s.add(ServerMetric(server_id=srv.id, ts=at, disk_percent=pct,
                           disks=[{"mount": "/", "pct": pct}]))
        await s.commit()
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), at)


async def test_short_spike_sends_nothing(tmp_path, monkeypatch):
    """Нода разработки: 82% -> 87% на две минуты (pull образа) -> 82%. Раньше пара сообщений."""
    engine, factory = await _panel(tmp_path)
    sent = _sender(monkeypatch)
    for i, v in enumerate([81.5] * 10 + [82.6, 86.5, 87.0, 81.5] + [81.5] * 40):
        await _tick(factory, NOW + timedelta(minutes=i), v)
    assert sent == []
    await engine.dispose()


async def test_hover_at_threshold_one_alert_and_no_false_recovery(tmp_path, monkeypatch):
    """Сидит на 85.2-85.3: алерт после выдержки, а "снова в норме: 85% (было 85%)" не приходит."""
    engine, factory = await _panel(tmp_path)
    sent = _sender(monkeypatch)
    for i in range(40):
        await _tick(factory, NOW + timedelta(minutes=i), 85.2 + 0.1 * (i % 2))
    assert len(sent) == 1 and "диск 85% ≥ 85%" in sent[0]
    for i in range(40, 100):
        await _tick(factory, NOW + timedelta(minutes=i), 84.8 + 0.5 * (i % 2))
    assert len(sent) == 1
    await engine.dispose()


async def test_fast_fill_alerts_at_once(tmp_path, monkeypatch):
    """Ночное копирование забивает раздел по 3.5% в минуту: алерт сразу при заходе за 85%."""
    engine, factory = await _panel(tmp_path)
    sent = _sender(monkeypatch)
    vals = [46.4] * 5 + [49.1 + 3.9 * i for i in range(11)]  # до 88.1
    for i, v in enumerate(vals):
        await _tick(factory, NOW + timedelta(minutes=i), v)
    assert len(sent) == 1, sent
    assert "диск 88% ≥ 85%" in sent[0] and "заполняется быстро: +3.9% в минуту, до 100% ~3 мин" in sent[0]
    await engine.dispose()


async def test_disk_swinging_twice_is_one_message_and_one_recovery(tmp_path, monkeypatch):
    """Два захода по часу за 6 ч: второй - одно сообщение "скачет", отбой один, когда затихнет."""
    engine, factory = await _panel(tmp_path)
    sent = _sender(monkeypatch)
    m = 0

    async def run(minutes: int, pct: float) -> list[str]:
        nonlocal m
        sent.clear()
        for _ in range(minutes):
            await _tick(factory, NOW + timedelta(minutes=m), pct)
            m += 1
        return list(sent)

    first = await run(35, 88)
    assert len(first) == 1 and "диск 88%" in first[0]
    rec = await run(35, 61)
    assert len(rec) == 1 and "снова в норме" in rec[0]
    swing = await run(35, 88)
    assert len(swing) == 1 and "диск скачет 61-88%: 2-й заход за порог за 6 ч" in swing[0]
    assert await run(35, 61) == []
    # затихло: шесть часов без заходов - один отбой
    calm = await run(6 * 60, 61)
    assert len(calm) == 1 and "снова в норме" in calm[0]
    await engine.dispose()


async def test_crit_comes_even_while_swinging_is_quiet(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path, alert_state={"flap_disk_muted": 1, "flap_disk_count": 2,
                                                          "flap_disk_since": NOW.isoformat()})
    sent = _sender(monkeypatch)
    await _tick(factory, NOW, 97)
    assert len(sent) == 1 and "диск 97% ≥ 95%" in sent[0]
    await engine.dispose()


def test_home_shows_disk_only_after_hold():
    """Строка "Диск 87%" на главной - с той же выдержкой, что алерт, и до отбоя."""
    rep = {"disks": [{"mount": "/", "used": 87, "total": 100}]}

    def srv(state):
        return Server(name="node", token_hash="x", enabled=True, last_seen=NOW, offline_after_seconds=120,
                      disk_warn_percent=85, disk_alert_percent=90, disk_crit_percent=95, last_report=rep,
                      alert_mutes=[], alert_snoozes={}, alert_state=state)

    def disk(state):
        return [(p["level"], p["text"], p["mute"]) for p in collector.server_problems(srv(state), NOW)
                if p["kind"] == "disk"]

    assert disk({"disk_since": NOW.isoformat()}) == []
    assert disk({"disk_since": (NOW - timedelta(minutes=31)).isoformat()}) == [(1, "Диск 87%", "disk@1")]
    rep["disks"][0]["used"] = 83
    assert disk({"disk": 2}) == [(2, "Диск 83%", "disk@2")]  # объявленный - до отбоя
    rep["disks"][0]["used"] = 97
    assert disk({}) == [(3, "Диск 97% 🚨", "disk")]
