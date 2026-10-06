"""Прирост бэкапа за прогон (helper backup-setup 0.32): скачок в разы больше обычного - алерт и
пункт в "Что сломано", обычный - тишина."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import backup_growth, collector
from app.models import Server

GB = 2**30
NOW = datetime(2026, 10, 7, 6, 0, tzinfo=timezone.utc)


def _runs(sts_gb, last_age_h=3):
    """Ночные прогоны: последний - last_age_h часов назад, остальные - по суткам раньше."""
    end = NOW.timestamp() - last_age_h * 3600
    n = len(sts_gb)
    return [{"ts": int(end - (n - 1 - i) * 86400), "add": st * 2.3 * GB, "st": st * GB,
             "proc": 700 * GB, "files": 300000, "fnew": 100, "fchg": 10, "dur": 2700}
            for i, st in enumerate(sts_gb)]


def _srv(runs, **kw):
    rep = {"clock_unix": int(NOW.timestamp()), "extras": {"backup-runs": {"v": 1, "ts": int(NOW.timestamp()),
                                                                         "unit": "systemd-rest.service", "runs": runs}}}
    return Server(name="feed-a", token_hash="x", enabled=True, last_seen=NOW, last_report=rep,
                  offline_after_seconds=120, **kw)


def test_jump_and_quiet_cases():
    g = backup_growth.jump(_srv(_runs([20] * 14 + [192])), NOW)
    assert g and g["level"] == 1 and g["text"] == "бэкап добавил 192 ГБ за прогон (обычно 20.0 ГБ)"
    # полтора раза больше - обычные колебания
    assert backup_growth.jump(_srv(_runs([20] * 14 + [30])), NOW) is None
    # в четыре раза, но с 100 МБ до 400 МБ - не повод
    assert backup_growth.jump(_srv(_runs([0.1] * 14 + [0.4])), NOW) is None
    # последний прогон двое суток назад - бэкап сломан иначе, про это другой алерт
    assert backup_growth.jump(_srv(_runs([20] * 14 + [192], last_age_h=48)), NOW) is None
    # мало истории - не с чем сравнивать
    assert backup_growth.jump(_srv(_runs([20] * 3 + [192])), NOW) is None
    assert backup_growth.jump(Server(name="x", token_hash="x", last_report={}), NOW) is None


def test_condition_present_only_with_helper():
    lvl, ctx = collector._server_conditions(_srv(_runs([20] * 14 + [192])), NOW)["backup_growth"]
    assert lvl == 1 and "192 ГБ" in ctx["detail"]
    assert collector._server_conditions(_srv(_runs([20] * 15)), NOW)["backup_growth"][0] == 0
    no_helper = Server(name="x", token_hash="x", enabled=True, last_seen=NOW, offline_after_seconds=120,
                       last_report={"clock_unix": int(NOW.timestamp())})
    assert "backup_growth" not in collector._server_conditions(no_helper, NOW)


async def test_growth_alert_lifecycle(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'g.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name="feed-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report={}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(sts):
        nonlocal now
        now = now + timedelta(minutes=2)
        end = now.timestamp() - 3600
        runs = [{"ts": int(end - (len(sts) - 1 - i) * 86400), "add": st * GB, "st": st * GB, "proc": GB,
                 "files": 1, "fnew": 0, "fchg": 0, "dur": 60} for i, st in enumerate(sts)]
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.last_report = {"clock_unix": int(now.timestamp()), "uptime_seconds": 10**6,
                               "extras": {"backup-runs": {"v": 1, "ts": int(now.timestamp()), "runs": runs}}}
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)

    await tick([20] * 14 + [192])
    msgs = [m for m in sent if "бэкап добавил" in m]
    assert len(msgs) == 1 and "💾📈" in msgs[0] and "192 ГБ" in msgs[0], sent
    await tick([20] * 14 + [192])
    assert len([m for m in sent if "бэкап добавил" in m]) == 1
    await tick([20] * 13 + [192, 21])
    assert any(m.startswith("✅") and "прирост бэкапа снова обычный" in m for m in sent), sent
    await engine.dispose()
