"""Дребезг тревога-отбой (collector._SETTLE_S, _AGAIN_S): отбой только после устойчивой нормы, повтор
того же после отбоя - продолжение прежней тревоги, а не новая пара сообщений.

Волны - из истории алертов трех панелей за 18-24.10.2026 (имена и адреса выдуманные): свой бэкап
с часовым расписанием пять раз за день "давно не отрабатывал" - "снова в норме", юнит с таймаутом
старта дважды за час, отбой и тревога в одну минуту. Время в тестах - минуты от начала волны."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import collector
from app.alerts import full_text
from app.models import Server

pytestmark = pytest.mark.calm


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
        s.add(Server(name="cold-copy-1", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=datetime.now(timezone.utc), cpu_alert_percent=0, mem_alert_percent=0,
                     last_report={"uptime_seconds": 100000}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    return engine, factory, Settings(alert_webhook="http://hook"), sent


async def _tick(factory, cfg, t, rep):
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.last_seen = t
        srv.last_report = {"uptime_seconds": 100000, "clock_unix": t.timestamp(), **rep}
        await s.commit()
    await collector.evaluate_servers(factory, cfg, t)


async def _state(factory) -> dict:
    async with factory() as s:
        return (await s.execute(select(Server))).scalars().one().alert_state or {}


def _job(t, name, ok_ago_h, sched="*-*-* *:40:00", enabled=True):
    """Таймер своего бэкапа, итог - из его метрик: последний успешный ok_ago_h часов назад"""
    ts = int(t.timestamp())
    return {"id": f"systemd:{name}.timer", "kind": "systemd", "name": name, "desc": "", "schedule": sched,
            "engines": ["PostgreSQL"], "files": False, "containers": [], "unit": f"{name}.service",
            "ok": -1, "ok_ts": ts - int(ok_ago_h * 3600), "run_ts": ts - 600, "result": "success",
            "running": False, "next_ts": ts + 1800, "prev_ts": 0, "enabled": enabled}


def _own(t, *jobs):
    return {"extras": {"custom-backups": {"ts": int(t.timestamp()), "jobs": list(jobs)}}}


def _alerts(sent):
    return [x for x in sent if not x.startswith("✅")]


def _oks(sent):
    return [x for x in sent if x.startswith("✅")]


async def _replay(factory, cfg, t0, plan, step_min=2):
    """plan: [(минута начала, отчет(t))] - отчет действует до следующей строки; тики раз в step_min"""
    end = plan[-1][0]
    m = 0
    while m <= end:
        rep = next(f for start, f in reversed(plan) if start <= m)
        t = t0 + timedelta(minutes=m)
        await _tick(factory, cfg, t, rep(t))
        m += step_min


async def test_backup_waves_one_alert_one_recovery(tmp_path, monkeypatch):
    """Метрики копии WAL то показывали последний успешный 2-4 часа назад, то свежий: 14:18 -
    16:36, 18:32 - 18:42, 19:30 - 19:42, 19:54 - 20:42, 21:06 - 21:10. Было пять пар сообщений,
    стала одна тревога и один отбой."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime(2026, 10, 25, 11, 18, tzinfo=timezone.utc)  # 14:18 по Москве
    stale = lambda t: _own(t, _job(t, "wal-copy", 3))  # noqa: E731
    fresh = lambda t: _own(t, _job(t, "wal-copy", 0.5))  # noqa: E731
    at = lambda hh, mm: (hh - 14) * 60 + mm - 18  # noqa: E731
    plan = [(0, stale), (at(16, 36), fresh), (at(18, 32), stale), (at(18, 42), fresh), (at(19, 30), stale),
            (at(19, 42), fresh), (at(19, 54), stale), (at(20, 42), fresh), (at(21, 6), stale), (at(21, 10), fresh),
            (at(22, 0), fresh)]
    await _replay(factory, cfg, t0, plan)
    assert len(_alerts(sent)) == 1 and "wal-copy" in sent[0], sent
    assert len(_oks(sent)) == 1, sent
    # отбой не сразу, а после 15 минут нормы
    st = await _state(factory)
    assert st["backup_custom"] == 0 and not st.get("backup_custom_hush") and not st.get("backup_custom_calm")
    assert st["backup_custom_was"]["sig"] == ["wal-copy"]
    await engine.dispose()


async def test_recovery_and_refire_in_one_minute_is_nothing(tmp_path, monkeypatch):
    """Отбой и та же тревога в одну минуту (так было у GitLab после перезапуска панели): норма
    продержалась меньше выдержки - тревога все та же, сообщений нет."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
    stale = lambda t: _own(t, _job(t, "nightly-dump", 3))  # noqa: E731
    fresh = lambda t: _own(t, _job(t, "nightly-dump", 0.5))  # noqa: E731
    await _replay(factory, cfg, t0, [(0, stale), (60, fresh), (62, stale), (100, fresh), (110, stale),
                                     (120, stale)], step_min=1)
    assert len(sent) == 1 and not sent[0].startswith("✅"), sent
    st = await _state(factory)
    assert st["backup_custom"] == 1 and not st.get("backup_custom_calm")
    await engine.dispose()


async def test_other_job_after_recovery_is_news(tmp_path, monkeypatch):
    """После отбоя по одному заданию сломалось другое - это новая тревога, не тихий повтор
    (22.10: сначала выключили таймер проверки восстановления, через шесть часов - зеркала)."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
    ok = lambda t: _own(t, _job(t, "restore-check", 0.5), _job(t, "mirror-copy", 0.5))  # noqa: E731
    a = lambda t: _own(t, _job(t, "restore-check", 3), _job(t, "mirror-copy", 0.5))  # noqa: E731
    b = lambda t: _own(t, _job(t, "restore-check", 0.5), _job(t, "mirror-copy", 3))  # noqa: E731
    await _replay(factory, cfg, t0, [(0, a), (36, ok), (90, b), (130, ok), (160, ok)])
    alerts = _alerts(sent)
    assert len(alerts) == 2 and "restore-check" in alerts[0] and "mirror-copy" in alerts[1], sent
    assert len(_oks(sent)) == 2
    # а повтор mirror-copy через полчаса - уже та же волна
    await _replay(factory, cfg, t0 + timedelta(minutes=170), [(0, b), (20, ok), (60, ok)])
    assert len(_alerts(sent)) == 2 and len(_oks(sent)) == 2, sent
    await engine.dispose()


def test_worse_or_other_repeat_is_news():
    """Повтор после отбоя, но хуже, чем было (уровень выше) или с новым в перечне - не волна;
    волна - только в окне _AGAIN_S после отбоя и не дольше _AGAIN_MAX от первого отбоя цепочки."""
    t0 = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
    was = {"ts": t0.isoformat(), "first": t0.isoformat(), "lvl": 1, "sig": ["a"]}
    st = {"k_was": was}
    later = t0 + timedelta(hours=2)
    assert collector._again_of(st, "k", 1, ["a"], later) == was
    assert collector._again_of(st, "k", 2, ["a"], later) is None
    assert collector._again_of(st, "k", 1, ["a", "b"], later) is None
    assert collector._again_of(st, "k", 1, ["a"], t0 + timedelta(hours=7)) is None
    old = {**was, "first": (t0 - timedelta(hours=23)).isoformat()}
    assert collector._again_of({"k_was": old}, "k", 1, ["a"], later) is None
    # о ком тревога у видов без перечня
    assert collector._who({"sig": ["x", "x"]}) == ["x"] and collector._who({"who": ["b", "a"]}) == ["a", "b"]
    assert collector._who({"mount": "/data"}) == ["/data"] and collector._who({}) == []


async def test_repeat_that_holds_for_hours_is_told(tmp_path, monkeypatch):
    """Тихий повтор, который не проходит: через _AGAIN_TELL это уже не волна - сообщение с пометкой,
    дальше обычный отбой."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc)
    stale = lambda t: _own(t, _job(t, "wal-copy", 3))  # noqa: E731
    fresh = lambda t: _own(t, _job(t, "wal-copy", 0.5))  # noqa: E731
    await _replay(factory, cfg, t0, [(0, stale), (30, fresh), (60, fresh), (120, stale), (120 + 170, stale)],
                  step_min=5)
    assert len(_alerts(sent)) == 1 and len(_oks(sent)) == 1, sent
    await _replay(factory, cfg, t0 + timedelta(minutes=295), [(0, stale), (10, stale)], step_min=5)
    alerts = _alerts(sent)
    assert len(alerts) == 2, sent
    assert "после отбоя вернулось и держится уже 3 ч" in full_text(alerts[1])
    st = await _state(factory)
    assert not st.get("backup_custom_hush")
    await _replay(factory, cfg, t0 + timedelta(minutes=310), [(0, fresh), (20, fresh)], step_min=5)
    assert len(_oks(sent)) == 2, sent
    await engine.dispose()


def test_offline_recovery_not_held():
    """Недоступен - вернулся: у offline свое правило мигания, отбой без выдержки"""
    assert "offline" in collector._NO_SETTLE and "backup_growth" in collector._NO_SETTLE
    assert collector._SETTLE_S == 15 * 60 and collector._AGAIN_S == 6 * 3600


GI = 2**30
CH = {"kind": "pod", "name": "default/olap-0", "u": "5e1fa0c2", "lim": 40 * GI, "cur": int(39.88 * GI),
      "anon": int(9.25 * GI), "file": int(30.5 * GI), "some": 0, "full": 0, "f10": 0, "mf": 0, "rf": 0}


async def test_mem_limit_evening_waves(tmp_path, monkeypatch):
    """Под базы у лимита 40Gi, refault волнами по 25 минут с перерывами 35-150 минут (вечер 24.10:
    четыре пары "уперся" - "больше не упирается"). Без давления памяти это кэш - ни одного
    сообщения; с давлением - одна тревога и один отбой на весь вечер."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime(2026, 10, 25, 13, 0, tzinfo=timezone.utc)

    def evening(extra):
        plan, m = [], 0
        for burst, quiet in ((26, 150), (24, 35), (25, 34), (26, 90)):
            plan.append((m, lambda t, e=extra: {"mem_limits": [{**CH, "rf": 9676, **e}]}))
            plan.append((m + burst, lambda t: {"mem_limits": [CH]}))
            m += burst + quiet
        plan.append((m, lambda t: {"mem_limits": [CH]}))
        return plan, m

    plan, end = evening({})
    await _replay(factory, cfg, t0, plan)
    assert sent == []
    plan, _ = evening({"some": 8.0, "full": 0.4})
    await _replay(factory, cfg, t0 + timedelta(minutes=end + 10), plan)
    assert len(_alerts(sent)) == 1 and "уперся в лимит памяти 40Gi" in sent[0], sent
    assert len(_oks(sent)) == 1, sent
    await engine.dispose()


async def test_unit_timeout_twice_an_hour(tmp_path, monkeypatch):
    """Юнит проверки не уложился в таймаут старта: 18:37 упал, 19:06 отбой, 19:37 упал снова, 20:01
    отбой (22.10). Второй раз - та же беда, одно сообщение и один отбой."""
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime(2026, 10, 25, 15, 35, tzinfo=timezone.utc)

    def units(failed: bool):
        def rep(t):
            ts = int(t.timestamp())
            u = [{"unit": "exposure-probe.service", "type": "simple", "result": "timeout", "status": 0,
                  "since": ts - 180, "log": ["exposure-probe.service: start operation timed out. Terminating."]}]
            return {"extras": {"units": {"v": 1, "ts": ts - 30, "units": u if failed else [], "fix": True}}}
        return rep

    await _replay(factory, cfg, t0, [(0, units(True)), (8, units(False)), (60, units(True)), (68, units(False)),
                                     (150, units(False))])
    assert len(_alerts(sent)) == 1 and "exposure-probe.service" in full_text(sent[0]), sent
    assert len(_oks(sent)) == 1, sent
    await engine.dispose()
