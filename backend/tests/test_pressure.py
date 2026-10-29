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
           "dstate": {"n": 7, "top": [{"c": "nfsd", "n": 5}, {"c": "jbd2", "n": 2}],
                      "stuck": [{"pid": 811, "comm": "nfsd", "wchan": "rpc_wait_bit_killable", "sec": 430}]},
           "fds": {"used": 850, "max": 1000}}
    assert collector.pressure_problems(rep) == [
        (2, "mem", "не хватает памяти: все процессы разом стоят 14% времени"),
        (2, "dstate", "завис nfsd (PID 811) в ожидании диска или сети 7 мин, wchan rpc_wait_bit_killable"),
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
    from app.alerts import full_text

    assert len(sent) == 1 and sent[0].startswith("🔴🫁 "), sent
    assert "<b>Серверу не хватает памяти</b>" in sent[0]
    assert "Ест: java (6гб)" in sent[0]  # кто ест память
    assert "не хватает памяти: все процессы разом стоят 22% времени" in full_text(sent[0])
    # отбой только после часа спокойствия: ночной дамп давал три пары "сработало - отбой" за два часа
    await tick(1, CALM)
    await tick(30)
    await tick(5, heavy)                    # снова пошло внутри часа - та же история
    await tick(5, CALM)
    await tick(50)
    assert len(sent) == 1, sent
    await tick(15)
    assert len(sent) == 2 and sent[1].startswith("✅") and "сервер больше не задыхается" in sent[1], sent
    await engine.dispose()


# Зависшие процессы (агент 2.40): под тяжелым чтением в D все время кто-то есть, но каждый на
# миллисекунды. Беда - только процесс, который ядро сочло бы зависшим (dstate.stuck), или рост
# счетчика hung_task. Само число - справка в карточке.
PG = {"pid": 4242, "comm": "postgres", "wchan": "folio_wait_bit_common"}


def _ds(n: int, stuck: list | None = None, **kw) -> dict:
    top = [{"c": "postgres", "n": max(n - 1, 0)}, {"c": "systemd", "n": 1}] if n else []
    return {"n": n, "top": top, "stuck": stuck if stuck is not None else [], **kw}


def test_mass_dstate_without_stuck_is_not_a_problem():
    for n in (2, 8, 23):
        assert collector.pressure_problems(dict(CALM, dstate=_ds(n))) == []
    # агент старше 2.40 зависших не считает: по числу D не алертим и у него
    old = dict(CALM, dstate={"n": 20, "top": [{"c": "postgres", "n": 19}, {"c": "systemd", "n": 1}]})
    assert collector.pressure_problems(old) == []
    s = _srv(old, {"pressure_since": (NOW - timedelta(hours=3)).isoformat()})
    lvl, ctx = collector._server_conditions(s, NOW)["pressure"]
    assert lvl == 0 and ctx["since"] is None
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] == "pressure"]


def test_stuck_process_is_named():
    # тот же pid три отчета подряд: проблема с первого, текст называет процесс
    for sec in (130, 145, 160):
        rep = dict(CALM, dstate=_ds(6, [dict(PG, sec=sec)]))
        assert collector.pressure_problems(rep) == [
            (2, "dstate", "завис postgres (PID 4242) в ожидании диска или сети 2 мин, wchan folio_wait_bit_common")]
    assert collector.pressure_hint(rep) == (
        "процесс дольше 2 мин в D и ни разу не проснулся: так висят на умирающем диске, подвисшем NFS или "
        "хранилище. На чем висит: sudo cat /proc/4242/stack и dmesg")
    s = _srv(rep, {"pressure_since": (NOW - timedelta(minutes=16)).isoformat()})
    assert collector._server_conditions(s, NOW)["pressure"][0] == 2
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "pressure"]
    assert [(p["level"], p["text"]) for p in rows] == [
        (2, "завис postgres (PID 4242) в ожидании диска или сети 2 мин, wchan folio_wait_bit_common")]
    # несколько: дольше всех первыми, коротко, wchan только где ядро его отдало
    many = [{"pid": 3, "comm": "nfsd", "sec": 200}, {"pid": 1, "comm": "postgres", "sec": 3700},
            {"pid": 4, "comm": "gzip", "sec": 130},
            {"pid": 2, "comm": "jbd2/sda1-8", "wchan": "jbd2_journal_commit_transaction", "sec": 400}]
    assert collector.pressure_problems({"dstate": _ds(9, many)}) == [(2, "dstate", (
        "зависли в ожидании диска или сети 4 процесса: postgres (PID 1) 1 ч 1 мин, "
        "jbd2/sda1-8 (PID 2, wchan jbd2_journal_commit_transaction) 6 мин, nfsd (PID 3) 3 мин и еще 1"))]
    hint = collector.pressure_hint({"dstate": _ds(9, many)})
    assert hint.startswith("процессы дольше 2 мин в D и ни разу не проснулись:") and "/proc/1/stack" in hint
    # мусор не роняет
    assert collector.pressure_problems({"dstate": {"n": 3, "stuck": "x"}}) == []
    assert collector.pressure_problems({"dstate": {"n": 3, "stuck": ["x", 5]}}) == []


def test_hung_task_growth_is_a_problem():
    rep = {"dstate": _ds(3, hung=14, hung_new=2)}
    assert collector.pressure_problems(rep) == [
        (2, "dstate", "ядро видит зависшие задачи: hung_task сработал 2 раза за 10 мин")]
    assert "dmesg" in collector.pressure_hint(rep)
    # счетчик с загрузки не растет: старые зависания - не новость
    assert collector.pressure_problems({"dstate": _ds(3, hung=14)}) == []
    assert collector.pressure_hint({"dstate": _ds(3, hung=14)}) == ""


async def _life(tmp_path, monkeypatch, rep: dict, state: dict | None = None):
    """Сервер db-a в базе и тик цикла алертов: tick(минут, отчет) -> отправленные сообщения."""
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'd.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    clock = {"now": datetime.now(timezone.utc)}
    st = dict(state or {})
    for k, v in list(st.items()):
        if k.endswith(("_since", "_from")) and isinstance(v, timedelta):
            st[k] = (clock["now"] - v).isoformat()
    async with factory() as s:
        s.add(Server(name="db-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=clock["now"],
                     cpu_alert_percent=0, mem_alert_percent=0, last_report=rep, alert_state=st))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(minutes: float, rep: dict | None = None) -> list[str]:
        clock["now"] += timedelta(minutes=minutes)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = clock["now"]
            if rep is not None:
                srv.last_report = rep
            await s.commit()
        await collector.evaluate_servers(factory, cfg, clock["now"])
        return sent

    return tick, sent, engine


async def test_mass_dstate_for_hours_no_alert(tmp_path, monkeypatch):
    # число D скачет 2/8 часами, зависших нет: ни алерта, ни строки
    tick, sent, engine = await _life(tmp_path, monkeypatch, dict(CALM, dstate=_ds(8)))
    for i in range(36):
        await tick(5, dict(CALM, dstate=_ds(8 if i % 2 else 2)))
    assert sent == []
    await engine.dispose()


async def test_stuck_alert_names_process(tmp_path, monkeypatch):
    from app import alert_what

    tick, sent, engine = await _life(tmp_path, monkeypatch, dict(CALM, dstate=_ds(6, [dict(PG, sec=130)])))
    await tick(0)
    await tick(5, dict(CALM, dstate=_ds(7, [dict(PG, sec=430)])))
    await tick(5, dict(CALM, dstate=_ds(5, [dict(PG, sec=730)])))
    assert sent == []  # выдержка, как у всего давления
    await tick(6, dict(CALM, dstate=_ds(6, [dict(PG, sec=1090)])))
    from app.alerts import full_text

    assert len(sent) == 1 and sent[0].startswith("🔴🫁 "), sent
    # в чате - что случилось и кто завис, без PID, wchan и подсказки
    assert "<b>Процессы зависли</b>" in sent[0]
    assert "postgres" in sent[0], "не назван зависший процесс"
    assert "PID" not in sent[0] and "wchan" not in sent[0] and "/proc/" not in sent[0]
    # полный текст в истории - как раньше
    head, hint = full_text(sent[0]).split("\n")
    assert "db-a" in head and head.endswith(
        " — завис postgres (PID 4242) в ожидании диска или сети 18 мин, wchan folio_wait_bit_common")
    assert hint.startswith("↳ процесс дольше 2 мин в D") and "/proc/4242/stack" in hint
    assert alert_what.items("pressure", full_text(sent[0])) == [
        ("завис postgres", "18 мин, wchan folio_wait_bit_common")]
    # отпустило: отбой после часа спокойствия, одним сообщением
    for _ in range(14):
        await tick(5, dict(CALM, dstate=_ds(6)))
    assert len(sent) == 2 and sent[1].startswith("✅") and "сервер больше не задыхается" in sent[1], sent
    await engine.dispose()


async def test_old_count_alert_closes_with_one_recovery(tmp_path, monkeypatch):
    # алерт, объявленный по числу D еще до 2.40, висит три дня; агент на ноде старый, число скачет 2/8
    old = lambda n: dict(CALM, dstate={"n": n, "top": [{"c": "postgres", "n": n}]})  # noqa: E731
    tick, sent, engine = await _life(tmp_path, monkeypatch, old(8), {
        "pressure": 2, "pressure_since": timedelta(days=3), "pressure_from": timedelta(days=3), "pressure_res": "dstate"})
    for i in range(11):  # 55 минут
        await tick(5, old(2 if i % 2 else 8))
    assert sent == []
    for i in range(20):  # и еще почти два часа: отбой один раз, без новых срабатываний
        await tick(5, old(8 if i % 2 else 2))
    assert len(sent) == 1 and sent[0].startswith("✅") and "сервер больше не задыхается" in sent[0], sent
    await engine.dispose()


# Сервер бэкапов: давление только по диску - его работа (prune и check репозиториев, прием бэкапов).
# На двух HDD каждый день 17 минут ждал диск 55-60% времени во время prune одного
# репозитория: алерт в 16:13 и отбой в 18:00, делать нечего. Алертит, только если держится дольше 6 ч
BSRV = {"present": True}
BUSY = dict(CALM, psi=dict(CALM["psi"], io_some_60=63.0, io_full_60=57.0), backup_server=BSRV)


def test_backup_server_io_hold():
    pr = collector.pressure_problems(BUSY)
    assert collector.pressure_hold_s(BUSY, pr) == 6 * 3600
    # обычный сервер, зависший процесс или память - общая выдержка
    assert collector.pressure_hold_s(dict(BUSY, backup_server={}), pr) is None
    stuck = dict(BUSY, dstate=_ds(6, [dict(PG, sec=430)]))
    assert collector.pressure_hold_s(stuck, collector.pressure_problems(stuck)) is None
    mem = dict(BUSY, psi=dict(BUSY["psi"], mem_full_60=22.0))
    assert collector.pressure_hold_s(mem, collector.pressure_problems(mem)) is None
    assert collector.pressure_hold_s(CALM, []) is None

    s = _srv(BUSY, {"pressure_since": (NOW - timedelta(minutes=40)).isoformat()})
    assert collector._server_conditions(s, NOW)["pressure"][0] == 0
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] == "pressure"]
    s.alert_state = {"pressure_since": (NOW - timedelta(hours=6, minutes=1)).isoformat()}
    lvl, ctx = collector._server_conditions(s, NOW)["pressure"]
    assert lvl == 2 and ctx["_note"] == "на сервере бэкапов так бывает во время prune и check, но не дольше 6 ч"
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "pressure"]
    assert [p["text"] for p in rows] == ["диск не успевает: все процессы разом ждут диск 57% времени "
                                         "(на сервере бэкапов так бывает во время prune и check, но не дольше 6 ч)"]
    # обычный сервер с тем же давлением - как раньше, через 15 минут
    s = _srv(dict(BUSY, backup_server={}), {"pressure_since": (NOW - timedelta(minutes=16)).isoformat()})
    assert collector._server_conditions(s, NOW)["pressure"][0] == 2


async def test_backup_server_prune_no_alert(tmp_path, monkeypatch):
    tick, sent, engine = await _life(tmp_path, monkeypatch, CALM)
    for _ in range(5):  # prune: 20 минут давления, потом тихо
        await tick(5, BUSY)
    for _ in range(20):
        await tick(5, dict(CALM, backup_server=BSRV))
    assert [m for m in sent if "🫁" in m or "дышит" in m] == []
    # затянулось на 6 ч - алерт с пояснением
    for _ in range(74):
        await tick(5, BUSY)
    from app.alerts import full_text

    got = [m for m in sent if m.startswith("🔴🫁 ")]
    assert len(got) == 1 and "<b>Сервер ждет диск</b>" in got[0], sent
    # пояснение про сервер бэкапов - подробность для истории, в чат не идет
    assert "prune" not in got[0]
    assert full_text(got[0]).endswith(
        "\n↳ на сервере бэкапов так бывает во время prune и check, но не дольше 6 ч"), sent
    await engine.dispose()


async def test_backup_server_stuck_still_alerts(tmp_path, monkeypatch):
    # тот же prune, но процесс завис в D: общая выдержка
    rep = lambda sec: dict(BUSY, dstate=_ds(6, [dict(PG, sec=sec)]))  # noqa: E731
    tick, sent, engine = await _life(tmp_path, monkeypatch, rep(130))
    await tick(0)
    for i in range(4):
        await tick(5, rep(430 + i * 300))
    from app.alerts import full_text

    got = [m for m in sent if m.startswith("🔴🫁 ")]
    assert len(got) == 1 and "завис postgres" in full_text(got[0]), sent
    await engine.dispose()


T0 = int(NOW.timestamp())


def _jobs(*jobs):
    return {"v": 1, "ts": T0, "jobs": list(jobs)}


def _job(name, run_ago, *, every=3600, kind="systemd", running=False, schedule=""):
    run = T0 - run_ago
    return {"id": f"{kind}:{name}", "name": name, "kind": kind, "run_ts": run, "next_ts": run + every,
            "running": running, "schedule": schedule}


# Холодная копия бэкапов на сетевом хранилище (не rest-server): свои задания каждый час тянут к себе
# бэкапы других серверов, процессы каждое утро по 3-4 часа ждали хранилище, локальные диски свободны.
# Красная тревога в 5:01 и отбой в 9:14 на его обычной работе
PULL = dict(BUSY, backup_server=None, clock_unix=T0,
            extras={"custom-backups": _jobs(_job("pull-copy", 40 * 60), _job("copy-metrics", 60, every=300))})


def test_io_wait_during_own_backup_is_held():
    pr = collector.pressure_problems(PULL)
    assert collector.backup_busy(PULL) == "pull-copy"
    assert collector.pressure_hold_s(PULL, pr) == 6 * 3600
    s = _srv(PULL, {"pressure_since": (NOW - timedelta(hours=4)).isoformat()})
    assert collector._server_conditions(s, NOW)["pressure"][0] == 0
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] == "pressure"]
    s.alert_state = {"pressure_since": (NOW - timedelta(hours=6, minutes=1)).isoformat()}
    lvl, ctx = collector._server_conditions(s, NOW)["pressure"]
    assert lvl == 2 and ctx["_note"] == "во время бэкапа (pull-copy) так бывает, но не дольше 6 ч"
    # задание раз в 5 минут (метрики) - не окно бэкапа, иначе окно было бы всегда
    only_metrics = dict(PULL, extras={"custom-backups": _jobs(_job("copy-metrics", 60, every=300))})
    assert collector.backup_busy(only_metrics) == ""
    assert collector.pressure_hold_s(only_metrics, pr) is None
    cron5 = dict(PULL, extras={"custom-backups": _jobs(_job("m", 60, kind="cron", schedule="*/5 * * * *"))})
    assert collector.backup_busy(cron5) == ""
    # бэкап кончился больше часа назад, а диск все ждут - выдержка обычная
    done = dict(PULL, extras={"custom-backups": _jobs(_job("pull-copy", 2 * 3600, every=86400))})
    assert collector.pressure_hold_s(done, pr) is None
    s = _srv(done, {"pressure_since": (NOW - timedelta(minutes=16)).isoformat()})
    assert collector._server_conditions(s, NOW)["pressure"][0] == 2
    # долгое задание, которое еще идет
    long = dict(PULL, extras={"custom-backups": _jobs(_job("pg-dump", 5 * 3600, every=86400, running=True))})
    assert collector.backup_busy(long) == "pg-dump"
    # дамп базы на сервере
    dump = dict(PULL, extras={"db-health": {"v": 1, "ts": T0, "items": [
        {"engine": "pg", "container": "pg", "xact": 1800, "xact_db": "app", "xact_app": "pg_dump"}]}})
    assert collector.backup_busy(dump) == "pg_dump"
    # зависший процесс - как у всех, несмотря на бэкап
    stuck = dict(PULL, dstate=_ds(6, [dict(PG, sec=430)]))
    assert collector.pressure_hold_s(stuck, collector.pressure_problems(stuck)) is None
    # резервный сервер бэкапов - роль, как rest-server
    mirror = _srv(dict(BUSY, backup_server=None), backup_mirror_role=True)
    assert collector.pressure_hold_s(mirror.last_report, pr, mirror) == 6 * 3600
