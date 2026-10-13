"""Сервер задыхается: процессы ждут память или диск (PSI), зависли в D-state, кончаются файловые
дескрипторы. Алерт с выдержкой, как у CPU и RAM, и строка в "Что сломано"."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector
from app.models import Server

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
CALM = {"psi": {"cpu_some_10": 3.0, "cpu_some_60": 2.5, "mem_some_60": 0.4, "mem_full_60": 0.1,
                "io_some_60": 7.0, "io_full_60": 2.0},
        "dstate": {"n": 1, "top": [{"c": "jbd2", "n": 1}]}, "fds": {"used": 9000, "max": 1000000}}


def _srv(rep, state=None, **kw):
    base = dict(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                alert_state=state or {}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                web_5xx_alert_percent=0, kube_expiry_warn_days=[])
    base.update(kw)
    return Server(**base)


def test_calm_server_has_no_pressure():
    assert collector.pressure_problems(CALM) == []
    assert collector.pressure_problems({}) == []  # агент старше 2.26


def test_each_cause_and_level():
    rep = {"psi": {"mem_full_60": 14.2, "io_some_60": 85.0, "io_full_60": 20.0},
           "dstate": {"n": 7, "top": [{"c": "nfsd", "n": 5}, {"c": "jbd2", "n": 2}]},
           "fds": {"used": 850, "max": 1000}}
    assert collector.pressure_problems(rep) == [
        (2, "mem", "не хватает памяти: все процессы разом стоят 14% времени"),
        (2, "dstate", "зависли в ожидании диска или сети 7 процессов (D-state): nfsd (5), jbd2 (2)"),
        (1, "io", "диск не успевает: процессы ждут диск 85% времени"),
        (1, "fds", "файловые дескрипторы заняты на 85% (850 из 1000)"),
    ]
    rep = {"psi": {"mem_some_60": 45.0, "io_full_60": 61.0}, "fds": {"used": 95, "max": 100}}
    assert collector.pressure_problems(rep) == [
        (2, "io", "диск не успевает: все процессы разом ждут диск 61% времени"),
        (2, "fds", "файловые дескрипторы заняты на 95% (95 из 100)"),
        (1, "mem", "не хватает памяти: процессы ждут память 45% времени"),
    ]
    # мусор в отчете не роняет алерты
    assert collector.pressure_problems({"psi": {"mem_full_60": "x"}, "dstate": "y", "fds": {"max": 0}}) == []


def test_io_cause_names_the_busiest_disk():
    rep = {"disk_devs": [{"dev": "sda", "util": 99.4, "await": 180.2}, {"dev": "sdb", "util": 12.0}]}
    assert collector.io_cause(rep) == " - sda занят на 99%, задержка 180 мс"
    assert collector.io_cause({}) == ""


def test_sustain_and_home_row():
    bad = dict(CALM, psi=dict(CALM["psi"], io_full_60=70.0))
    s = _srv(bad)
    lvl, ctx = collector._server_conditions(s, NOW)["pressure"]
    assert lvl == 0 and ctx["since"] == NOW.isoformat() and ctx["res"] == "io"
    # пока держится меньше выдержки - ни алерта, ни строки на главной
    s.alert_state = {"pressure_since": (NOW - timedelta(minutes=5)).isoformat()}
    assert collector._server_conditions(s, NOW)["pressure"][0] == 0
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] == "pressure"]
    s.alert_state = {"pressure_since": (NOW - timedelta(minutes=16)).isoformat()}
    lvl, ctx = collector._server_conditions(s, NOW)["pressure"]
    assert lvl == 2 and ctx["detail"] == "диск не успевает: все процессы разом ждут диск 70% времени"
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "pressure"]
    assert [(p["level"], p["sec"], p["mute"]) for p in rows] == [(2, "pressure", "pressure")]
    # отпустило - сразу ноль, отметка начала сбрасывается
    s.last_report = CALM
    lvl, ctx = collector._server_conditions(s, NOW)["pressure"]
    assert lvl == 0 and ctx["since"] is None
    # агент без новых полей - ключа нет вовсе, висящий алерт не закрывается молча
    s.last_report = {"cpu_percent": 5}
    assert "pressure" not in collector._server_conditions(s, NOW)


async def test_pressure_alert_lifecycle(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    heavy = dict(CALM, psi=dict(CALM["psi"], mem_full_60=22.0),
                 top_mem=[{"pid": 10, "comm": "java", "rss": 6 << 30}], mem_total=8 << 30)
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report=heavy))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(minutes, rep=None):
        nonlocal now
        now = now + timedelta(minutes=minutes)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            if rep is not None:
                srv.last_report = rep
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)

    await tick(0)
    await tick(10)
    assert sent == []
    await tick(6)
    assert len(sent) == 1 and sent[0].startswith("🫁 "), sent
    assert "не хватает памяти: все процессы разом стоят 22% времени" in sent[0]
    assert "java" in sent[0]  # кто ест память
    # отбой только после часа спокойствия: ночной дамп давал три пары "сработало - отбой" за два часа
    await tick(1, CALM)
    await tick(30)
    await tick(5, heavy)                    # снова пошло внутри часа - та же история
    await tick(5, CALM)
    await tick(50)
    assert len(sent) == 1, sent
    await tick(15)
    assert len(sent) == 2 and sent[1].startswith("✅") and "сервер снова дышит" in sent[1], sent
    await engine.dispose()
