"""Прирост бэкапа за прогон (helper backup-setup 0.32): скачок в разы больше обычного - алерт и
пункт в "Что сломано", обычный - тишина."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import backup_growth, collector
from app.alerts import full_text
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


@pytest.mark.parametrize("acknowledge", [False, True])
async def test_growth_alert_lifecycle(tmp_path, monkeypatch, acknowledge):
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

    async def tick(sts, end=None):
        nonlocal now
        now = now + timedelta(minutes=2)
        end = end if end is not None else int(now.timestamp()) - 3600
        runs = [{"ts": int(end - (len(sts) - 1 - i) * 86400), "add": st * GB, "st": st * GB, "proc": GB,
                 "files": 1, "fnew": 0, "fchg": 0, "dur": 60} for i, st in enumerate(sts)]
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.last_report = {"clock_unix": int(now.timestamp()), "uptime_seconds": 10**6,
                               "extras": {"backup-runs": {"v": 1, "ts": int(now.timestamp()), "runs": runs}}}
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)
        return end

    def growth() -> list:
        return [m for m in sent if getattr(m, "kind", "") == "backup_growth"]

    first = await tick([20] * 14 + [192])
    msgs = growth()
    assert len(msgs) == 1 and "бэкап добавил 192 ГБ за прогон" in full_text(msgs[0]), sent
    # в чате: на сколько вырос и сколько обычно, размеры по-человечески
    assert msgs[0] == "⚠️💾 feed-a\n<b>Бэкап вырос на 192гб за раз</b>\nобычно 20гб", msgs[0]
    await tick([20] * 14 + [192], first)
    assert len(growth()) == 1
    if acknowledge:
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.backup_growth_ack_ts = first
            assert backup_growth.jump(srv, now) is None
            await s.commit()
        # Следующий скачок приходит до тика, который закрыл бы подтвержденный алерт.
        second = await tick([20] * 13 + [192, 250])
        assert len(growth()) == 2 and "250гб" in growth()[1], sent
        await tick([20] * 13 + [192, 250], second)
        assert len(growth()) == 2
    # следующий прогон обычный: алерт закрывается молча, "снова обычный" в телеграм не идет
    await tick([20] * 13 + [192, 21])
    assert not [m for m in sent if m.startswith("✅")], sent
    async with factory() as s:
        assert not (await s.execute(select(Server))).scalars().one().alert_state.get("backup_growth")
    # и новый скачок снова алертит
    await tick([20] * 12 + [192, 21, 250])
    assert len(growth()) == (3 if acknowledge else 2) and "250гб" in growth()[-1], sent
    await engine.dispose()


async def test_acknowledge_growth_for_exact_run(client, auth_headers):
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    shift = int(now.timestamp() - NOW.timestamp())
    runs = [{**run, "ts": run["ts"] + shift} for run in _runs([20] * 14 + [192])]
    ts = runs[-1]["ts"]
    async with factory() as session:
        server = _srv(runs)
        server.last_seen = now
        session.add(server)
        await session.commit()
        path = f"/api/servers/{server.id}"

    async def ack(run_ts):
        return await client.post(path + "/backup/growth-ack", headers=auth_headers, json={"ts": run_ts})

    before = await client.get(path, headers=auth_headers)
    assert before.json()["backup_growth"]["ts"] == ts
    assert (await ack(ts - 1)).status_code == 409
    for _ in range(2):
        response = await ack(ts)
        assert response.status_code == 200, response.text
        assert response.json()["backup_growth"] is None
        assert response.json()["backup_growth_ack_ts"] == ts
    refreshed = (await client.get(path, headers=auth_headers)).json()
    assert refreshed["backup_growth"] is None
    async with factory() as session:
        server = (await session.execute(select(Server))).scalars().one()
        assert backup_growth.runs(server) == runs
        server.last_report = {
            "extras": {"backup-runs": {"v": 1, "runs": [*runs, {**runs[-1], "ts": ts + 60, "st": 250 * GB}]}},
        }
        await session.commit()
    assert (await ack(ts)).json()["backup_growth"]["ts"] == ts + 60
