"""Сервер задыхается без памяти: свопится и встает, OOM-killer молчит (app/mem_starve.py).

Живой случай: две сборки Gradle на сервере с 23гб, swap забит еще до них, через семь минут сервер
встал. Алерт давления ждал 15 минут и не успел, "не на связи" пришел без слова о памяти."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, mem_starve, srv_short
from app.alerts import full_text
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Server

GB = 1 << 30
TOTAL = 25199378432  # 23.5гб
SWAP = 2970611712

# последний отчет перед пропажей: память и swap кончились, процессы стоят, ядро читает с диска
DYING = {"mem_total": TOTAL, "mem_used": int(TOTAL * 0.96), "swap_total": SWAP, "swap_used": int(SWAP * 0.999),
         "swap_in": 170_000, "swap_out": 30_000, "majflt": 1034.0, "load": [13.2, 6.0, 2.0], "cpu_cores": 6,
         "cpu_percent": 97.0, "uptime_seconds": 864000, "oom_total": 3,
         "psi": {"mem_some_10": 70.0, "mem_some_60": 48.0, "mem_full_10": 45.0, "mem_full_60": 31.0,
                 "io_some_60": 20.0, "io_full_60": 10.0},
         "top_mem": [
             {"pid": 11, "comm": "java", "rss": 9 * GB, "kind": "container", "owner": "runner-a-build",
              "cmdline": "/opt/java/bin/java -Xmx8g org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.10"},
             {"pid": 12, "comm": "java", "rss": 8 * GB, "kind": "container", "owner": "runner-b-build",
              "cmdline": "/opt/java/bin/java org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.10"},
             {"pid": 13, "comm": "ruby", "rss": 1 * GB, "shared": 200 << 20, "kind": "container", "owner": "gitlab"},
             {"pid": 14, "comm": "java", "rss": 2 * GB, "cmdline": "/usr/bin/java -jar /srv/app/graylog.jar"},
         ]}
CALM = {"mem_total": TOTAL, "mem_used": int(TOTAL * 0.5), "swap_total": SWAP, "swap_used": int(SWAP * 0.95),
        "swap_in": 0, "swap_out": 0, "majflt": 3.0, "load": [0.8, 1, 1], "cpu_cores": 6, "cpu_percent": 6.0,
        "uptime_seconds": 864600, "oom_total": 3,
        "psi": {"mem_some_10": 0, "mem_some_60": 0, "mem_full_10": 0, "mem_full_60": 0, "io_full_60": 0}}


def test_starving_needs_host_out_of_memory_and_stalls():
    assert mem_starve.starving(DYING)
    assert not mem_starve.starving(CALM)
    # контейнер у своего лимита: давление памяти есть, а у сервера памяти полно - не сюда
    at_limit = dict(DYING, mem_used=int(TOTAL * 0.6))
    assert not mem_starve.starving(at_limit)
    assert collector.pressure_problems(at_limit)[0][1] == "mem"
    # память занята кэшем и никто не стоит - тоже не сюда
    busy = dict(DYING, majflt=4.0, psi={"mem_some_60": 1.0, "mem_full_60": 0.2, "mem_full_10": 0.5})
    assert not mem_starve.starving(busy)
    # swap забит, своп стоит, но ядро читает код и кэш с диска: старый агент без major faults ловится PSI
    assert mem_starve.starving(dict(DYING, majflt=None))
    # агент без PSI, но major faults тысяча в секунду
    assert mem_starve.starving(dict(DYING, psi={}))
    # мусор в отчете не роняет
    assert not mem_starve.starving({"mem_total": "x"})
    assert not mem_starve.starving(None)


def test_episode_keeps_on_lower_thresholds():
    edge = dict(DYING, mem_used=int(TOTAL * 0.88), majflt=50.0,
                psi={"mem_some_60": 12.0, "mem_full_60": 3.0, "mem_full_10": 4.0})
    assert not mem_starve.starving(edge)
    assert mem_starve.starving(edge, going=True)


def test_alert_text_and_chat():
    pr = collector.pressure_problems(DYING)
    assert pr[0][:2] == (2, "thrash")
    assert pr[0][2] == ("кончилась память, сервер может встать: свободно 4% из 23гб, swap занят на 100%, "
                        "все процессы разом стоят 45% времени, с диска читается 1034 страниц/с")
    # обычного "не хватает памяти" рядом нет: это одна беда
    assert [r for _l, r, _t in pr].count("mem") == 0
    what, facts = srv_short._short("pressure", {"items": pr}, Server(name="n", last_report=DYING), 2)
    assert what == "Кончилась память, сервер свопится"
    assert facts == ["Едят: контейнер runner-a-build (java (Gradle)) 9гб, контейнер runner-b-build (java (Gradle)) 8гб",
                     "Свободно 4%, swap 100%, PSI full 45%"]


def test_eaters_from_old_agent():
    """Агент до 2.58 не подписывает процессы владельцем: тогда контейнеры по их памяти"""
    rep = dict(DYING, top_mem=[{"pid": 1, "comm": "java", "rss": 9 * GB}],
               docker={"containers": [{"name": "runner-a", "mem": 9 * GB}, {"name": "gitlab", "mem": 4 * GB}]})
    assert mem_starve.eaters_text(mem_starve.eaters(rep)) == "контейнер runner-a 9гб, контейнер gitlab 4гб"
    rep = dict(DYING, top_mem=[{"pid": 1, "comm": "java", "rss": 5 * GB, "cmdline": "java -jar /x/app.jar"},
                               {"pid": 2, "comm": "java", "rss": 4 * GB, "cmdline": "java -jar /x/app.jar"}])
    assert mem_starve.eaters_text(mem_starve.eaters(rep)) == "java (app.jar) 9гб"


def _srv(rep, state=None, **kw):
    base = dict(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True,
                last_seen=datetime(2026, 10, 29, 8, 46, tzinfo=timezone.utc),
                offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                alert_state=state or {}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                web_5xx_alert_percent=0, kube_expiry_warn_days=[])
    base.update(kw)
    return Server(**base)


def test_short_hold_not_the_server_sustain():
    now = datetime(2026, 10, 29, 8, 46, tzinfo=timezone.utc)
    # две минуты - короткий всплеск (сборка съела память и отпустила): не алертим
    s = _srv(DYING, {"pressure_since": (now - timedelta(minutes=2)).isoformat()}, last_seen=now)
    assert collector._server_conditions(s, now)["pressure"][0] == 0
    # три - уже не всплеск, хотя общая выдержка сервера 15 минут
    s.alert_state = {"pressure_since": (now - timedelta(minutes=3, seconds=5)).isoformat()}
    lvl, ctx = collector._server_conditions(s, now)["pressure"]
    assert lvl == 2 and ctx["res"] == "thrash"
    rows = [p for p in collector.server_problems(s, now) if p["kind"] == "pressure"]
    assert rows and rows[0]["text"].startswith("кончилась память, сервер может встать")
    # общая выдержка сервера короче - она и действует
    s.alert_sustain_seconds = 60
    s.alert_state = {"pressure_since": (now - timedelta(seconds=70)).isoformat()}
    assert collector._server_conditions(s, now)["pressure"][0] == 2


def test_offline_quick_when_memory_ran_out():
    """Агент замолчал без памяти: не ждем лишние три минуты, пока "канал перегружен" """
    seen = datetime(2026, 10, 29, 8, 46, 30, tzinfo=timezone.utc)
    now = seen + timedelta(seconds=150)
    assert collector._server_conditions(_srv(DYING, last_seen=seen), now)["offline"][0] == 1
    assert collector._server_conditions(_srv(CALM, last_seen=seen), now)["offline"][0] == 0


def test_snapshot_texts():
    seen = datetime(2026, 10, 29, 8, 46, 30, tzinfo=timezone.utc)
    sn = mem_starve.snapshot(DYING, seen)
    assert sn["starving"] and sn["oom"] == 3
    assert mem_starve.before_full(sn, "11:46") == (
        "перед пропажей (11:46) кончалась память: CPU 97%, память 96%, swap 100%, PSI full 45%, load 13 на 6 "
        "ядрах - похоже, сервер встал без памяти. Больше всех ели: контейнер runner-a-build (java (Gradle)) 9гб, "
        "контейнер runner-b-build (java (Gradle)) 8гб, java (graylog.jar) 2гб")
    assert mem_starve.before_short(sn) == \
        "Перед этим кончилась память: CPU 97%, память 96%, swap 100%, PSI full 45%, load 13 на 6 ядрах"
    assert mem_starve.before_home(sn).startswith("перед этим кончилась память")
    # все было спокойно - про цифры молчим (обрыв сети на здоровом сервере)
    assert mem_starve.snapshot(CALM, seen) is None
    # кончилась не память, а CPU: цифры без вывода про память
    cpu = dict(CALM, cpu_percent=100.0, load=[40.0, 30, 20])
    sn = mem_starve.snapshot(cpu, seen)
    assert not sn["starving"]
    assert mem_starve.before_short(sn) == "Перед этим: CPU 100%, память 50%, swap 95%, load 40 на 6 ядрах"
    # PSI еще не вырос, но ни памяти, ни swap не осталось - тоже без памяти
    edge = dict(DYING, majflt=None, psi={"mem_some_60": 2.0})
    assert mem_starve.snapshot(edge, seen)["starving"]


def test_back_lines():
    sn = mem_starve.snapshot(DYING, None)
    assert mem_starve.back_lines(sn, CALM) == [
        "Кончилась память, сервер встал в свопе, OOM не было, память отпустило само",
        "Ели память: контейнер runner-a-build (java (Gradle)) 9гб, контейнер runner-b-build (java (Gradle)) 8гб, "
        "java (graylog.jar) 2гб"]
    assert mem_starve.back_lines(sn, dict(CALM, oom_total=5))[0] == \
        "Кончилась память, сервер встал в свопе, OOM-killer убил 2 процесса"
    assert mem_starve.back_lines(sn, dict(CALM, uptime_seconds=300))[0] == \
        "Кончилась память, сервер встал в свопе, сервер перезагрузился"
    assert mem_starve.back_lines(None, CALM) == []


async def test_offline_and_back_tell_the_cause(tmp_path, monkeypatch):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    seen = now - timedelta(seconds=150)
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=seen,
                     cpu_alert_percent=0, mem_alert_percent=0, offline_after_seconds=120, last_report=DYING))
        await s.commit()
    sent: list = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    cfg = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, cfg, now)
    off = [m for m in sent if m.kind == "offline"]
    assert len(off) == 1, [str(m) for m in sent]
    assert "Перед этим кончилась память: CPU 97%, память 96%, swap 100%, PSI full 45%" in str(off[0])
    assert "похоже, сервер встал без памяти. Больше всех ели: контейнер runner-a-build" in full_text(off[0])
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        assert srv.alert_state["offline_before"]["starving"]
        # вернулся: память отпустило, OOM не было
        srv.last_seen = now + timedelta(minutes=10)
        srv.last_report = CALM
        await s.commit()
    sent.clear()
    await collector.evaluate_servers(factory, cfg, now + timedelta(minutes=10))
    back = [m for m in sent if m.kind == "offline"]
    assert len(back) == 1 and back[0].recovery, [str(m) for m in sent]
    assert "Кончилась память, сервер встал в свопе, OOM не было, память отпустило само" in str(back[0])
    assert "Ели память: контейнер runner-a-build" in str(back[0])
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        assert not srv.alert_state.get("offline_before")
    await engine.dispose()


async def test_daily_routine_does_not_hide_thrash(tmp_path, monkeypatch):
    """Распорядок давления (тот же эпизод в то же время каждый день) молчит про ночной дамп, но не про
    кончившуюся память: сервер встает и каждый день"""
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'r.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    ts = now.timestamp()
    hist = [[ts - 2 * 86400 - 60, ts - 2 * 86400 + 1800], [ts - 86400 - 60, ts - 86400 + 1800]]
    io = dict(CALM, psi=dict(CALM["psi"], io_full_60=70.0))
    async with factory() as s:
        for name, rep in (("io-node", io), ("mem-node", DYING)):
            s.add(Server(name=name, token_hash="t" + name, enabled=True, backup_not_required=True, last_seen=now,
                         cpu_alert_percent=0, mem_alert_percent=0, last_report=rep,
                         alert_state={"pressure_since": (now - timedelta(minutes=20)).isoformat(),
                                      "pressure_hist": hist}))
        await s.commit()
    sent: list = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    got = [m for m in sent if m.kind == "pressure"]
    assert [m.target for m in got] == ["mem-node"], [str(m) for m in sent]
    assert "Кончилась память, сервер свопится" in str(got[0])
    assert "больше всех едят контейнер runner-a-build" in full_text(got[0])
    await engine.dispose()
