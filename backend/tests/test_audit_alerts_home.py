"""Находки аудита 24.10 по алертам, Telegram и главной: что каждая чинит, проверено здесь.

Имена в тестах вымышленные: node-a, node-b, example.com."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app import collector, heartbeat, home_mute, kube_cron, mem_trend, panel_errors, tg_bot, tg_buttons
from app.alerts import full_text
from app.models import AlertEvent, Check, PanelError, Server, TgAlert, User


def _factory(path):
    from app.db import create_engine_and_factory

    return create_engine_and_factory(f"sqlite+aiosqlite:///{path.as_posix()}")


async def _env(tmp_path, monkeypatch, names=("node-a",), **kw):
    from app.config import Settings
    from app.db import Base

    engine, factory = _factory(tmp_path / "s.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        for n in names:
            s.add(Server(name=n, token_hash="x", enabled=True, backup_not_required=True,
                         last_seen=datetime.now(timezone.utc), cpu_alert_percent=0, mem_alert_percent=0,
                         last_report={"uptime_seconds": 100000}, **kw))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    return engine, factory, Settings(alert_webhook="http://hook"), sent


async def _tick(factory, cfg, t, rep, name=None, **fields):
    async with factory() as s:
        for srv in (await s.execute(select(Server))).scalars().all():
            if name is not None and srv.name != name:
                continue
            srv.last_seen = t
            srv.last_report = {"uptime_seconds": 100000, "clock_unix": t.timestamp(), **rep}
            for k, v in fields.items():
                setattr(srv, k, v)
        await s.commit()
    await collector.evaluate_servers(factory, cfg, t)


def _workers(t, fatal=()):
    ts = int(t.timestamp())
    progs = [{"name": n, "state": "FATAL", "since": ts - 600, "up": 0, "pid": 0, "exit": 1, "r15": 0, "r60": 0}
             for n in fatal]
    progs.append({"name": "queue-listen", "state": "RUNNING", "since": ts - 3600, "up": 3600, "pid": 8, "exit": 0,
                  "r15": 0, "r60": 0})
    return {"extras": {"workers": {"v": 1, "ts": ts, "skipped": [],
                                   "daemons": [{"kind": "supervisor", "where": "container node-a-workers-1",
                                                "progs": progs}]}}}


async def _server(factory, name):
    async with factory() as s:
        return (await s.execute(select(Server).where(Server.name == name))).scalars().one()


# --- Алерты 1, Бэкенд 2-3: исключение на одной ноде, журнал ошибок, пульс --------------------------

async def test_one_broken_server_does_not_silence_the_fleet(tmp_path, monkeypatch, caplog):
    heartbeat.reset()
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch, names=("node-a", "node-bad"))
    orig = collector._server_conditions

    def boom(s, *a, **k):
        if s.name == "node-bad":
            raise KeyError("неожиданное поле")
        return orig(s, *a, **k)

    monkeypatch.setattr(collector, "_server_conditions", boom)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    with caplog.at_level(logging.ERROR, logger="kervax.collector"):
        await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    assert len(sent) == 1 and "node-a" in full_text(sent[0])
    assert any("node-bad" in r.getMessage() for r in caplog.records)
    heartbeat.stage_done("серверные алерты", True)
    assert heartbeat.stage_fail_line() == "1 серверные алерты (node-bad)"
    # состояние сломанной ноды не тронуто, у живой записано
    assert not (await _server(factory, "node-bad")).alert_state
    assert (await _server(factory, "node-a")).alert_state.get("workers") == 2
    heartbeat.stage_done("серверные алерты", True)
    assert heartbeat.stage_fail_line() == ""
    await engine.dispose()


async def test_scheduler_errors_go_to_panel_errors(tmp_path):
    from app.db import Base

    engine, factory = _factory(tmp_path / "e.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    lg = logging.getLogger("kervax.collector")
    h = heartbeat.SchedulerErrors(factory)
    lg.addHandler(h)
    try:
        # вне планировщика (HTTP-запрос в одиночном режиме) - не пишем: там свой middleware
        try:
            raise NameError("x")
        except NameError:
            lg.exception("стадия планировщика «%s» упала", "серверные алерты")
        await panel_errors.drain()
        tok = heartbeat.IN_SCHEDULER.set(True)
        try:
            for _ in range(2):
                try:
                    raise NameError("name 'foo' is not defined")
                except NameError:
                    lg.exception("стадия планировщика «%s» упала", "серверные алерты")
                await panel_errors.drain()
        finally:
            heartbeat.IN_SCHEDULER.reset(tok)
    finally:
        lg.removeHandler(h)
        panel_errors.reset()
    async with factory() as s:
        rows = (await s.execute(select(PanelError))).scalars().all()
    assert len(rows) == 1
    assert rows[0].kind == "server" and rows[0].action == "планировщик"
    assert "серверные алерты" in rows[0].message and "NameError" in rows[0].message
    await engine.dispose()


async def test_heartbeat_carries_tick_and_failing_stage(tmp_path):
    from app.config import Settings

    heartbeat.reset()
    st = Settings(data_dir=str(tmp_path), scheduler_tick=30)
    heartbeat.tick_started(1_800_000_000)
    for _ in range(3):
        heartbeat.stage_done("прогноз дисков", False)
    await heartbeat.write_heartbeat(None, st, {}, True)
    text = (tmp_path / "heartbeat").read_text(encoding="utf-8")
    assert "tick=1800000000\n" in text and "tick_max=600\n" in text
    assert "stage_fail=3 прогноз дисков\n" in text
    heartbeat.reset()


async def test_next_tick_sleeps_to_the_nearest_due_monitor(tmp_path):
    from app.config import Settings
    from app.db import Base

    engine, factory = _factory(tmp_path / "t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Check(name="a", type="http", target="https://example.com", interval_seconds=60,
                    last_checked_at=now - timedelta(seconds=40)))
        await s.commit()
    st = Settings(scheduler_tick=30)
    left = await collector._next_tick_in(factory, st)
    assert 15 <= left <= 21  # до срока ~20 с, не меньше полутика
    c = SimpleNamespace(last_checked_at=now - timedelta(seconds=59.5), interval_seconds=60)
    assert collector._is_due(c, now)
    await engine.dispose()


async def test_sigterm_finishes_the_tick_and_exits(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base

    engine, factory = _factory(tmp_path / "l.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    stop = asyncio.Event()
    stop.set()  # SIGTERM пришел, пока шел тик
    await asyncio.wait_for(collector.collector_loop(factory, Settings(scheduler_tick=30), stop), 120)
    await engine.dispose()


# --- Алерты 2: идущий прогон CronJob держит прошлый вердикт ---------------------------------------

def test_running_cronjob_keeps_the_failed_verdict():
    now = datetime(2026, 10, 11, 1, 0, 30, tzinfo=timezone.utc)
    ts = int(now.timestamp())
    failed = {"ns": "app", "name": "geo-update", "schedule": "0 1 * * *", "last_schedule": ts - 86400,
              "last_success": ts - 5 * 86400, "last": {"st": "failed", "reason": "BackoffLimitExceeded"}}
    rep = {"kube": {"access": True, "cronjobs": [failed]}}
    st = kube_cron.track(None, rep, now - timedelta(minutes=1))
    assert st["app/geo-update"]["bad"]["kind"] == "fail"
    running = {**failed, "active": 1, "active_job": "geo-update-29000000", "last_schedule": ts - 30}
    it = kube_cron.issue({"kube": {"access": True, "cronjobs": [running]}}, running, now,
                         {kube_cron.STATE_KEY: st}, False)
    assert it is not None and it["kind"] == "fail" and "идет новый прогон" in it["why"]
    # успешный прогон - вердикт снят
    ok = {**failed, "last_schedule": ts - 30, "last_success": ts - 10, "last": {"st": "succeeded"}}
    st2 = kube_cron.track(st, {"kube": {"access": True, "cronjobs": [ok]}}, now)
    assert "bad" not in st2["app/geo-update"]


# --- Алерты 3: отбой монитора под снузом и при сбое доставки ----------------------------------------

async def test_monitor_recovery_survives_snooze_and_failed_delivery(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base

    engine, factory = _factory(tmp_path / "m.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        c = Check(name="shop", type="http", target="https://example.com", last_status="up",
                  snooze_until=now + timedelta(hours=20))
        s.add(c)
        await s.commit()
        cid = c.id
    sent: list[str] = []
    ok = {"v": False}

    async def fake_send(cfg, text, parse_mode=None):
        if not ok["v"]:
            return ["Telegram: timeout"]
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    monkeypatch.setattr(collector, "_REC_RETRY", {})
    st = Settings(alert_webhook="http://hook")
    rec = collector.Pending("recovery", "shop", "up", "HTTP 200", None, cid, None, "")
    await collector._send_alerts(factory, st, [rec], now)
    assert sent == [] and cid in collector._REC_RETRY
    # следующая отправка (любая) подмешивает недоставленный отбой, хоть монитор и под снузом
    ok["v"] = True
    await collector._send_alerts(factory, st, [], now + timedelta(minutes=1))
    assert len(sent) == 1 and "shop" in full_text(sent[0]) or "example.com" in full_text(sent[0])
    assert collector._REC_RETRY == {}
    # а падение под снузом по-прежнему молчит
    sent.clear()
    bad = collector.Pending("bad", "shop", "down", "HTTP 503", None, cid, None, "")
    await collector._send_alerts(factory, st, [bad], now)
    assert sent == []
    await engine.dispose()


# --- Алерты 5, 10, 11: тексты отбоя, правило вместо мьюта, медленное - днем --------------------------

def test_restarts_recovery_names_the_day_count():
    now = datetime.now(timezone.utc)
    ev = [[(now - timedelta(minutes=m)).isoformat(), 5] for m in (10, 30, 50)]
    st = {"kube_restarts_told": ["pod:tech/dispatch"], "restarts": {"pod:tech/dispatch-5c6cc79ff-x7k2q": {"ev": ev}}}
    assert collector._recovery_short("kube_restarts", st, {}) == "поды больше не перезапускаются (за сутки было 15)"


def test_rule_closure_is_told_human_mute_is_not():
    srv = SimpleNamespace(alert_mutes=[], alert_snoozes={}, alert_state={})
    now = datetime.now(timezone.utc)
    assert collector._sig_rule_why(srv, "units", {"kervax-changes.service"}, now)
    srv.alert_mutes = ["unit:kervax-changes.service"]
    assert collector._sig_rule_why(srv, "units", {"kervax-changes.service"}, now) == ""


def test_mem_trend_day_is_panel_local_and_slow_signs_wait_for_morning():
    t = datetime(2026, 10, 19, 22, 30, tzinfo=timezone.utc)  # 01:30 по Москве - уже 20.10
    assert mem_trend.step(None, 100, t, "Europe/Moscow")["d"] == "2026-10-20"
    assert mem_trend.step(None, 100, t)["d"] == "2026-10-19"
    assert not collector._daytime(datetime(2026, 10, 19, 0, 1, tzinfo=timezone.utc), "Europe/Moscow")
    assert collector._daytime(datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc), "Europe/Moscow")


# --- Алерты 6, 7: массовое молчание и ноды за ретранслятором -----------------------------------------

def test_mass_silence_is_one_panel_problem():
    now = datetime.now(timezone.utc)
    old = now - timedelta(minutes=10)

    def srv(seen, **kw):
        return SimpleNamespace(last_seen=seen, offline_after_seconds=120, alert_state={}, relay_via_id=None, **kw)

    quiet = [srv(old) for _ in range(6)] + [srv(now) for _ in range(2)]
    assert collector.ingest_gap(quiet, now) == (6, 8)
    # замолчали три из десяти - это беда трех серверов
    assert collector.ingest_gap([srv(old) for _ in range(3)] + [srv(now) for _ in range(7)], now)[0] == 0
    assert collector.offline_extra(srv(now)) + 180 == collector.offline_extra(
        SimpleNamespace(relay_via_id=7))


# --- Алерты 8: очередь по порогу с выдержкой и линией снятия ----------------------------------------

async def test_queue_waits_and_recovers_below_the_line(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch, queue_alert_depth=1000)

    def rep(n):
        return {"services": [{"kind": "rabbitmq", "source": "rabbit", "queues": [
            {"name": "jobs", "vhost": "/", "ready": n, "unacked": 0, "consumers": 1}]}]}

    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, rep(1200))
    assert sent == []  # выше порога, выдержка идет
    await _tick(factory, cfg, t0 + timedelta(minutes=11), rep(1100))
    assert len(sent) == 1 and "jobs" in full_text(sent[0])
    await _tick(factory, cfg, t0 + timedelta(minutes=12), rep(900))
    assert len(sent) == 1  # ниже порога, но выше линии снятия
    await _tick(factory, cfg, t0 + timedelta(minutes=13), rep(500))
    assert len(sent) == 2 and "снова в норме" in full_text(sent[1])
    await engine.dispose()


# --- Алерты 9: свернутые по домену мониторы в истории ----------------------------------------------

async def test_folded_monitors_get_their_own_history_rows(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base

    engine, factory = _factory(tmp_path / "h.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        a = Check(name="example.com", type="http", target="https://example.com", last_status="down")
        b = Check(name="www.example.com", type="http", target="https://www.example.com", last_status="down")
        s.add_all([a, b])
        await s.commit()
        ids = (a.id, b.id)

    async def fake_send(cfg, text, parse_mode=None):
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    now = datetime.now(timezone.utc)
    pend = [collector.Pending("bad", "example.com", "down", "HTTP 503", None, ids[0], None, ""),
            collector.Pending("bad", "www.example.com", "down", "HTTP 503", None, ids[1], None, "")]
    await collector._send_alerts(factory, Settings(alert_webhook="http://hook"), pend, now)
    async with factory() as s:
        rows = (await s.execute(select(AlertEvent))).scalars().all()
    assert sorted(r.target for r in rows) == ["example.com", "www.example.com"]
    await engine.dispose()


# --- Алерты 13: кнопка глушит вещь, а не вид ------------------------------------------------------

def test_mute_keys_name_the_items_of_the_alert():
    assert collector.tg_mute_keys("units", {"sig": ["nginx.service"]}) == ["unit:nginx.service"]
    # алерт про новое при открытом: только новое
    assert collector.tg_mute_keys("workers", {"sig": ["a|x", "a|y"], "fresh": {"a|y"}}) == ["worker:a|y"]
    assert collector.tg_mute_keys("kube_cron", {"items": [{"name": "app/geo-update"}]}) == ["kube_cron:app/geo-update"]
    assert collector.tg_mute_keys("kube_pod", {"items": [{"ns": "app", "name": "web-7d9c8b7f6d-x2x9q"}]}) == [
        "kube_pod:app/web"]
    assert collector.tg_mute_keys("kube_job", {"items": [{"ns": "app", "name": "report-29012345"}]}) == [
        "kube_job:app/report"]
    assert collector.tg_mute_keys("docker_down", {"container": "db-a"}) == ["docker_down:db-a"]
    assert collector.tg_mute_keys("listen_gone", {"sig": ["tcp:8080"]}) == ["listen:8080"]
    assert collector.tg_mute_keys("cpu", {"value": 99}) == []


async def test_button_mutes_one_cronjob_not_the_whole_cluster(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    srv = await _server(factory, "node-a")
    async with factory() as s:
        s.add(User(username="ops", password_hash="x", role="operator", tg_chat_id="555"))
        row = TgAlert(ref=f"srv:{srv.id}", kind="kube_cron", target="node-a", text="x",
                      messages=[{"chat": "-100", "id": 7}], undo={"keys": ["kube_cron:app/geo-update"]})
        s.add(row)
        await s.commit()
        aid = row.id

    async def nop(*a, **k):
        return None

    monkeypatch.setattr("app.alerts.tg_edit", nop)
    monkeypatch.setattr(tg_bot, "_call", lambda *a, **k: nop())
    cfg_tg = {"telegram_token": "T", "telegram_chat": "-100", "tz": "Europe/Moscow"}
    now = datetime.now(timezone.utc)

    async def press(op):
        await tg_bot.handle_callback(factory, cfg_tg, {"id": "q", "data": f"kx:{aid}:{op}", "from": {"id": 555},
                                                       "message": {"chat": {"id": -100}}}, now)

    await press("pm")
    srv = await _server(factory, "node-a")
    assert srv.alert_mutes == ["kube_cron:app/geo-update"]
    await press("un")
    assert not (await _server(factory, "node-a")).alert_mutes
    await press("h1")
    snz = (await _server(factory, "node-a")).alert_snoozes
    assert list(snz) == ["kube_cron:app/geo-update"]
    await engine.dispose()


def test_item_muted_cronjob_leaves_the_rest_alerting():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    ts = int(now.timestamp())

    def cj(name):
        return {"ns": "app", "name": name, "schedule": "0 1 * * *", "last_schedule": ts - 3600,
                "last_success": ts - 5 * 86400, "last": {"st": "failed", "reason": "BackoffLimitExceeded"}}

    rep = {"kube": {"access": True, "cronjobs": [cj("geo-update"), cj("cleanup")]}, "clock_unix": now.timestamp()}
    s = Server(name="node-a", token_hash="x", enabled=True, last_seen=now, last_report=rep, alert_state={},
               alert_mutes=["kube_cron:app/geo-update"], offline_after_seconds=120)
    lvl, ctx = collector._server_conditions(s, now)["kube_cron"]
    assert lvl == 1 and [x["name"] for x in ctx["items"]] == ["app/cleanup"]
    row = [p for p in collector.server_problems(s, now) if p["kind"] == "kube_cron"]
    assert len(row) == 1 and "cleanup" in row[0]["text"] and "geo-update" not in row[0]["text"]


# --- Алерты 14: под мьютом и снузом ушедшее не висит, отбой объявленного приходит ------------------

async def test_kind_mute_keeps_recovery_and_server_snooze_tracks_state(tmp_path, monkeypatch):
    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    await _tick(factory, cfg, t0, _workers(t0, fatal=["mailer"]))
    assert len(sent) == 1
    await _tick(factory, cfg, t0 + timedelta(minutes=1), _workers(t0, fatal=["mailer"]), alert_mutes=["workers"])
    await _tick(factory, cfg, t0 + timedelta(minutes=2), _workers(t0 + timedelta(minutes=2)))
    assert len(sent) == 2 and "✅" in str(sent[1])  # объявленное ушло - отбой и под мьютом
    assert int((await _server(factory, "node-a")).alert_state.get("workers") or 0) == 0
    # снуз сервера: новое не шлем и не пишем, после снуза придет
    t1 = t0 + timedelta(hours=1)
    await _tick(factory, cfg, t1, _workers(t1, fatal=["mailer"]), alert_mutes=[],
                snooze_until=t1 + timedelta(hours=1))
    assert len(sent) == 2 and not int((await _server(factory, "node-a")).alert_state.get("workers") or 0)
    await engine.dispose()


# --- Алерты 15, 17: кубер и уровни на главной как у алерта ----------------------------------------

def test_home_shows_bad_pods_by_name_with_alert_levels():
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    pods = [{"ns": "app", "name": "db-0", "owner": "StatefulSet", "phase": "Pending", "reason": "Unschedulable"}]
    rep = {"kube": {"access": True, "pods": pods}, "clock_unix": now.timestamp()}
    st = {"kube_pod_since": (now - timedelta(hours=1)).isoformat()}
    s = Server(name="node-a", token_hash="x", enabled=True, last_seen=now, last_report=rep, alert_state=st,
               alert_mutes=[], offline_after_seconds=120)
    row = [p for p in collector.server_problems(s, now) if p["kind"] == "kube_pod"]
    assert len(row) == 1 and row[0]["level"] == 2 and row[0]["section"] == "kuber"
    assert "app/db-0 (Unschedulable)" in row[0]["text"]
    s.alert_mutes = ["kube_pod:app/db"]
    assert not [p for p in collector.server_problems(s, now) if p["kind"] == "kube_pod"]
    assert collector.srv_short.level("kube_job", 1) == 2 and collector.srv_short.level("kube_cron", 1) == 1


# --- Алерты 16: заглушка "до нового" при сужении набора --------------------------------------------

def test_home_mute_follows_a_narrowed_set():
    now = datetime.now(timezone.utc)
    s = SimpleNamespace(alert_mutes=[])
    sigs = {"v": "db1,db2"}
    old = home_mute.SIGS["dbdump"]
    home_mute.SIGS["dbdump"] = lambda *_a: sigs["v"]
    try:
        k12 = home_mute.key("dbdump", "db1,db2")
        s.alert_mutes = [k12]
        lift, swap, keep = home_mute.review(s, now)
        assert not lift and not swap and keep == {k12: home_mute._items("db1,db2")}
        sigs["v"] = "db2"  # db1 начали дампить
        lift, swap, keep = home_mute.review(s, now, None, keep)
        k2 = home_mute.key("dbdump", "db2")
        assert not lift and swap == [(k12, k2)]
        s.alert_mutes = [k2]
        sigs["v"] = "db2,db3"  # появилась новая база
        lift, swap, _keep = home_mute.review(s, now, None, keep)
        assert swap == [] and lift and "появилось новое" in lift[0][1]
    finally:
        home_mute.SIGS["dbdump"] = old


# --- Алерты 19: упавший контейнер под мьютом виден на главной --------------------------------------

async def test_muted_container_down_is_still_known(tmp_path, monkeypatch):
    from app.api.servers import _docker_alerts

    engine, factory, cfg, sent = await _env(tmp_path, monkeypatch, alert_mutes=["docker_down:db-a"],
                                            alert_sustain_seconds=0)
    t0 = datetime.now(timezone.utc) + timedelta(days=1)
    dk = {"docker": {"access": True, "present": True, "containers": [
        {"name": "db-a", "state": "exited", "policy": "always", "restarts": 0, "exit_code": 137}]}}
    await _tick(factory, cfg, t0, dk)
    await _tick(factory, cfg, t0 + timedelta(minutes=1), dk)
    assert sent == []
    assert _docker_alerts(await _server(factory, "node-a")) == {"db-a": "down"}
    await engine.dispose()


# --- Веб 12, 16, 17b: кто жмет кнопки, привязка, обрезка -------------------------------------------

async def test_unlinked_guest_can_only_take(tmp_path):
    from app.db import Base

    engine, factory = _factory(tmp_path / "b.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        row = TgAlert(ref="srv:1", kind="access_change", target="node-a", text="x")
        s.add(row)
        await s.commit()
        toast, act = await tg_bot.apply(s, row, tg_buttons.OP_US, "@guest", None, now, "UTC")
        assert act == "" and "Привяжите" in toast
        toast, act = await tg_bot.apply(s, row, tg_buttons.OP_ACK, "@guest", None, now, "UTC")
        assert act == "tg_ack"
    await engine.dispose()


async def test_link_code_expires_and_needs_private_chat(tmp_path):
    from app.db import Base

    engine, factory = _factory(tmp_path / "k.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(User(username="ivan", password_hash="x", role="operator",
                   tg_link_code=tg_bot.link_code_store("ABCD1234", now - timedelta(minutes=11))))
        await s.commit()
        assert await tg_bot._link(s, {}, {"chat": {"id": 1, "type": "private"}}, "ABCD1234", now) is None
        u = (await s.execute(select(User))).scalars().one()
        u.tg_link_code = tg_bot.link_code_store("ABCD1234", now)
        await s.commit()
        msg = await tg_bot._link(s, {}, {"chat": {"id": -5, "type": "group"}}, "ABCD1234", now)
        assert msg and "личном" in msg
        assert "Привязано" in await tg_bot._link(s, {}, {"chat": {"id": 1, "type": "private"}}, "ABCD1234", now)
    await engine.dispose()


async def test_chat_id_only_by_link_and_token_format(client, auth_headers):
    r = await client.patch("/api/auth/telegram", headers=auth_headers, json={"chat_id": "12345"})
    assert r.status_code == 400
    r = await client.patch("/api/auth/telegram", headers=auth_headers, json={"token": "x/../../evil"})
    assert r.status_code == 400
    r = await client.patch("/api/auth/telegram", headers=auth_headers,
                           json={"token": "123456:" + "a" * 35, "chat_id": ""})
    assert r.status_code == 200


def test_edit_text_is_cut_without_breaking_tags():
    row = SimpleNamespace(acked_by="ops", acked_at=datetime.now(timezone.utc), undo={}, muted="",
                          resolved_at=None, text="<b>node-a</b>\n" + ("<a href=\"https://example.com\">x</a> " * 400))
    out = tg_buttons.row_text(row, "UTC")
    assert len(out) <= 4000 and "✋ взял ops" in out
    body = out.split("\n\n")[0]
    assert body.count("<a ") == body.count("</a>")
