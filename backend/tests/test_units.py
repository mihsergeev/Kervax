"""Упавшие юниты systemd: блок helper'а units-setup, алерт "Systemd: упал юнит", команда
unit_fix (перезапустить или сбросить отметку)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.collector import unit_level, unit_log_line, unit_why, units_text

CERTBOT = {"unit": "certbot.service", "desc": "Certbot", "type": "oneshot", "result": "exit-code",
           "status": 1, "since": 0, "restarts": 0, "log": [
               "All renewals failed. The following certificates could not be renewed:",
               "1 renew failure(s), 0 parse failure(s)"]}
NGINX = {"unit": "nginx.service", "desc": "nginx", "type": "forking", "result": "signal",
         "status": 9, "since": 0, "restarts": 3, "log": ["worker process exited on signal 9"]}


def test_levels_and_reasons():
    assert unit_level(CERTBOT) == 1  # задание по расписанию не сделано - предупреждение
    assert unit_level(NGINX) == 2  # демон лежит - проблема
    assert unit_level({"unit": "data.mount"}) == 2
    assert unit_level({"unit": "backup.timer"}) == 1
    assert unit_why(CERTBOT) == "код 1"
    assert unit_why(NGINX) == "убит сигналом KILL"
    assert unit_why({"result": "start-limit-hit"}) == "слишком часто перезапускался"


def test_texts():
    one = dict(CERTBOT, since=100_000 - 3 * 3600)
    assert units_text([one], 100_000) == (
        "упал certbot.service: код 1, 3 ч назад\n↳ 1 renew failure(s), 0 parse failure(s)")
    both = units_text([CERTBOT, NGINX], 10_000)
    # серьезный (лежащий демон) первым, строка лога - его
    assert both == ("упали юниты: nginx.service (убит сигналом KILL), certbot.service (код 1)\n"
                    "↳ nginx.service: worker process exited on signal 9")


def test_log_line_skips_other_units():
    """Журнал user@N.service - это и сообщения менеджера сессии про свои юниты: под "упал
    user@1001.service" приходила строка про launchpadlib-cache-clean.service. Чужие строки и рутину
    systemd не берем, лучше без строки."""
    user = {"unit": "user@1001.service", "type": "notify-reload", "result": "signal", "status": 9, "since": 0,
            "log": ["Reached target basic.target - Basic System.", "Startup finished in 144ms.",
                    "launchpadlib-cache-clean.service - Clean up old files in the Launchpadlib cache was skipped "
                    "because of an unmet condition check (ConditionPathExists=/home/idv/.launchpadlib/api.launchpad.net/cache)."]}
    assert unit_log_line(user) == ""
    assert units_text([user], 200) == "упал user@1001.service: убит сигналом KILL"
    own = dict(user, log=["user@1001.service: A process of this unit has been killed by the OOM killer.",
                          "dbus.socket: Failed to listen", "Listening on dbus.socket - D-Bus User Message Bus Socket."])
    assert unit_log_line(own) == "user@1001.service: A process of this unit has been killed by the OOM killer."
    assert unit_log_line(CERTBOT) == "1 renew failure(s), 0 parse failure(s)"


async def test_units_alert_lifecycle(tmp_path, monkeypatch):
    """Упал certbot -> предупреждение; упал еще nginx -> новое сообщение (уровень вырос);
    третий юнит при том же уровне - тоже новое; "не алертить" по юниту убирает его из счета;
    все поднялось - отбой. Только что упавший юнит (меньше двух минут) не алертит."""
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'u.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def report(units):
        ts = int(now.timestamp())
        return {"clock_unix": ts, "uptime_seconds": 100000,
                "extras": {"units": {"v": 1, "ts": ts - 30, "units": units, "fix": True}}}

    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, cpu_alert_percent=0, mem_alert_percent=0, last_report=report([])))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(units, mutes=None):
        nonlocal now
        now = now + timedelta(minutes=2)
        old = int(now.timestamp()) - 600
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.last_report = report([dict(u, since=u.get("since") or old) for u in units])
            if mutes is not None:
                srv.alert_mutes = mutes
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)

    await tick([dict(CERTBOT, since=int(now.timestamp()) + 60)])  # упал только что - ждем
    assert sent == []
    await tick([CERTBOT])
    assert len(sent) == 1 and sent[0].startswith("⚠️⚙️ ") and "упал certbot.service: код 1" in sent[0]
    await tick([CERTBOT])
    assert len(sent) == 1
    await tick([CERTBOT, NGINX])
    assert len(sent) == 2 and sent[1].startswith("🔴⚙️ ") and "nginx.service (убит сигналом KILL)" in sent[1]
    await tick([CERTBOT, NGINX, dict(NGINX, unit="php-fpm.service")])
    assert len(sent) == 3 and "php-fpm.service" in sent[2]
    # "не алертить" по certbot и php-fpm: остается nginx, нового нет
    await tick([CERTBOT, NGINX, dict(NGINX, unit="php-fpm.service")],
               mutes=["unit:certbot.service", "unit:php-fpm.service"])
    assert len(sent) == 3
    # nginx поднялся, certbot заглушен: отбой не сразу, пропавший юнит держится 20 минут
    for _ in range(10):
        await tick([CERTBOT])
    assert len(sent) == 3
    await tick([CERTBOT])
    assert len(sent) == 4 and sent[3].startswith("✅") and "упавших юнитов больше нет" in sent[3]
    await engine.dispose()


async def test_unit_fix_command(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "u"}, headers=auth_headers)
    sid = r.json()["server"]["id"]
    url = f"/api/servers/{sid}/backup/command"
    ok = await client.post(url, json={"action": "unit_fix", "mode": "restart", "unit": "certbot.service"},
                           headers=auth_headers)
    assert ok.status_code == 200 and ok.json()["action"] == "unit_fix"
    for body in ({"action": "unit_fix", "mode": "restart"},  # без юнита
                 {"action": "unit_fix", "mode": "run", "unit": "certbot.service"},  # чужой режим
                 {"action": "run_now", "mode": "restart"}):  # режим только у unit_fix
        assert (await client.post(url, json=body, headers=auth_headers)).status_code == 400
    bad = await client.post(url, json={"action": "unit_fix", "mode": "reset", "unit": "x.service;reboot"},
                            headers=auth_headers)
    assert bad.status_code == 422
    # глушение одного юнита на время
    r = await client.post(f"/api/servers/{sid}/snooze-alert", json={"kind": "unit:certbot.service", "hours": 24},
                          headers=auth_headers)
    assert r.status_code == 200 and "unit:certbot.service" in (r.json()["alert_snoozes"] or {})


async def test_unit_journal_command(client, auth_headers):
    """Журнал юнита, всей системы или ядра: команда уходит агенту с периодом, лимитом строк и
    приоритетом; текст журнала возвращается результатом команды."""
    r = await client.post("/api/servers", json={"name": "j"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}
    url = f"/api/servers/{sid}/backup/command"
    r = await client.post(url, json={"action": "unit_journal", "unit": "nginx.service", "since_min": 360,
                                     "lines": 500, "prio": "warning"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    cid = r.json()["id"]
    k = await client.post(url, json={"action": "unit_journal", "unit": "_kernel"}, headers=auth_headers)
    assert k.status_code == 200
    rep = {"agent_version": "2.29", "mem_used": 1, "mem_total": 2, "disks": [{"mount": "/", "used": 1, "total": 2}]}
    cmds = (await client.post("/api/agent/report", json=rep, headers=ah)).json()["backup_commands"]
    assert cmds[0] == {"id": cid, "action": "unit_journal", "mode": "exclude", "paths": [], "schedule": "",
                       "name": "nginx.service", "since": 360, "lines": 500, "prio": "warning"}
    assert cmds[1]["name"] == "_kernel" and cmds[1]["since"] == 60 and cmds[1]["prio"] == "info"
    text = "2026-10-14T12:00:00+0300 web-a nginx[1]: started\n" * 1500
    assert (await client.post("/api/agent/backup-result", json={"id": cid, "ok": True, "output": text},
                              headers=ah)).status_code == 200
    st = (await client.get(f"{url}/{cid}", headers=auth_headers)).json()
    assert st["status"] == "done" and st["result"] == text
    for body in ({"action": "unit_journal"},  # без юнита
                 {"action": "unit_journal", "unit": "_root"},
                 {"action": "unit_journal", "unit": "nginx.service", "since_min": 20000},
                 {"action": "unit_journal", "unit": "nginx.service", "lines": 5000},
                 {"action": "unit_journal", "unit": "nginx.service", "prio": "debug"},
                 {"action": "unit_journal", "unit": "x.service;reboot"}):
        assert (await client.post(url, json=body, headers=auth_headers)).status_code in (400, 422), body
    # перезапускать "весь журнал" нельзя - имя только для чтения
    bad = await client.post(url, json={"action": "unit_fix", "mode": "restart", "unit": "_all"}, headers=auth_headers)
    assert bad.status_code == 400


WORKER = {"unit": "worker.service", "active": "activating", "sub": "auto-restart", "result": "exit-code",
          "status": 3, "started": 0, "log": ["boom: config not found"]}


def test_unit_loop_counts_only_growth():
    from app.collector import unit_loop_hits, unit_loop_step

    now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    cs = unit_loop_step(None, 40, now)  # первый отчет: неизвестно, когда набежало
    assert cs["hits"] == [] and cs["n"] == 40
    cs = unit_loop_step(cs, 42, now + timedelta(minutes=1))
    assert [h[1] for h in cs["hits"]] == [2]
    cs = unit_loop_step(cs, 0, now + timedelta(minutes=2))  # ручной restart обнулил счетчик
    assert [h[1] for h in cs["hits"]] == [2] and cs["n"] == 0
    # старше окна (15 минут) - не в счет
    assert unit_loop_hits(cs, now + timedelta(minutes=17)) == []


async def test_unit_loop_alert_lifecycle(tmp_path, monkeypatch):
    """Сервис падает и поднимается сам: три прироста счетчика за 15 минут - алерт и строка на
    главной; 15 минут без перезапусков - отбой. Заглушенный по имени юнит не алертит."""
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'l.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def report(n):
        ts = int(now.timestamp())
        return {"clock_unix": ts, "uptime_seconds": 100000,
                "extras": {"units": {"v": 1, "ts": ts - 30, "units": [], "fix": True,
                                     "restarting": [dict(WORKER, n=n)]}}}

    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, cpu_alert_percent=0, mem_alert_percent=0, last_report=report(1)))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(n, minutes=2, mutes=None):
        nonlocal now
        now = now + timedelta(minutes=minutes)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.last_report = report(n)
            if mutes is not None:
                srv.alert_mutes = mutes
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            return [p for p in collector.server_problems(srv, now) if p["kind"] == "unit_loop"]

    assert await tick(1) == []
    await tick(3)
    await tick(5)
    assert sent == []
    rows = await tick(8)
    assert len(sent) == 1 and sent[0].startswith("🔁⚙️ "), sent
    assert "worker.service перезапускается в цикле: 7 перезапусков за 15 мин, последний раз: код 3" in sent[0]
    assert [(p["level"], p["unit"], p["mute"], p["log"]) for p in rows] == [
        (2, "worker.service", "unit:worker.service", ["boom: config not found"])]
    assert rows[0]["text"] == "worker.service перезапускается в цикле: 7 перезапусков за 15 минут, последний раз: код 3"
    await tick(8, minutes=8)
    assert len(sent) == 1  # еще в окне: приросты были 8 минут назад
    rows = await tick(8, minutes=9)
    assert len(sent) == 2 and sent[1].startswith("✅") and "worker.service: перезапуски прекратились" in sent[1]
    assert rows == []
    # заглушенный по имени юнит: цикл снова идет, а алерта нет
    for n in (10, 12, 14):
        await tick(n, mutes=["unit:worker.service"])
    assert len(sent) == 2
    await engine.dispose()


async def test_timer_job_failing_every_run_does_not_flap(tmp_path, monkeypatch):
    """Задание по таймеру падает на каждом запуске и между падениями пару минут работает (в
    failed не числится): одно сообщение, без "больше нет" и нового "упал" каждые 10 минут.
    Свой helper панели (kervax-changes) не алертит вовсе, но виден в "Что сломано"; алерт,
    объявленный по нему старой панелью, гасится без отбоя."""
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'f.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    helper = dict(CERTBOT, unit="kervax-changes.service", result="timeout", status=0)

    def report(units):
        ts = int(now.timestamp())
        return {"clock_unix": ts, "uptime_seconds": 100000,
                "extras": {"units": {"v": 1, "ts": ts - 30, "units": units, "fix": True}}}

    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, cpu_alert_percent=0, mem_alert_percent=0, last_report=report([])))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(units, state=None):
        nonlocal now
        now = now + timedelta(minutes=2)
        old = int(now.timestamp()) - 600
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.last_report = report([dict(u, since=u.get("since") or old) for u in units])
            if state is not None:
                srv.alert_state = state
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)
        async with factory() as s:
            return (await s.execute(select(Server))).scalars().one()

    await tick([CERTBOT])
    assert len(sent) == 1 and "certbot.service" in sent[0]
    for _ in range(4):  # три цикла таймера: 8 минут лежит, 2 минуты снова работает
        await tick([CERTBOT])
        await tick([CERTBOT])
        await tick([CERTBOT])
        await tick([CERTBOT])
        await tick([])
    assert len(sent) == 1

    # свой helper: ни алерта, ни отбоя, но в "Что сломано" он есть
    for _ in range(3):
        srv = await tick([CERTBOT, helper])
    assert len(sent) == 1
    assert any(p["kind"] == "units" and "kervax-changes" in p["text"]
               for p in collector.server_problems(srv, now))
    st = dict(srv.alert_state)
    st.update({"units": 1, "units_sig": ["kervax-changes.service"], "units_hold": {}})
    await tick([helper], state=st)  # объявила старая панель: гасим молча
    srv = await tick([helper])
    assert len(sent) == 1 and int(srv.alert_state.get("units", 0)) == 0
    await engine.dispose()


def test_every_kervax_unit_is_either_own_helper_or_alerting():
    """Новый helper со своим юнитом: решить, алертит ли его падение (kervax-agent и дампы баз
    для бэкапа - да, снимки для отчета и обработчики кнопок - нет)."""
    import re
    from pathlib import Path

    from app.collector import own_helper_unit
    agent = Path(__file__).resolve().parents[2] / "agent"
    names = set()
    for f in agent.glob("*.sh"):
        names |= set(re.findall(r"kervax-[a-z0-9-]+\.service", f.read_text(encoding="utf-8")))
    alerting = {"kervax-agent.service", "kervax-dumps.service"}
    assert names - alerting and all(own_helper_unit(n) for n in names - alerting), sorted(names)
    assert not any(own_helper_unit(n) for n in alerting)


async def test_backup_unit_and_new_server_stay_quiet(tmp_path, monkeypatch):
    """Упавший юнит бэкапа - уже алерт "последний бэкап завершился с ошибкой", второго по юниту
    нет (и по своим бэкапам ноды). Только что добавленный сервер первые 15 минут молчит: helper'ы
    ставятся, отчеты неполные; что осталось, приходит после тишины."""
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'b.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    rest = dict(CERTBOT, unit="systemd-rest.service")
    custom = dict(CERTBOT, unit="pg-dump.service")

    def report(units):
        ts = int(now.timestamp())
        old = ts - 600
        return {"clock_unix": ts, "uptime_seconds": 100000, "backup": {"present": True, "configured": True},
                "extras": {"units": {"v": 1, "ts": ts - 30, "units": [dict(u, since=old) for u in units], "fix": True},
                           "custom-backups": {"v": 1, "ts": ts, "jobs": [{"id": "systemd:pg-dump.timer", "unit": "pg-dump.service"}]}}}

    from tests.test_reboot_access import _srv
    srv = _srv(report([rest, custom]))
    srv.last_seen = now
    assert collector.backup_units(srv, srv.last_report) >= {"systemd-rest.service", "pg-dump.service"}
    assert collector._server_conditions(srv, now)["units"][0] == 0
    srv.last_report = report([rest, custom, CERTBOT])
    assert collector._server_conditions(srv, now)["units"][1]["sig"] == ["certbot.service"]

    monkeypatch.setattr(collector, "_NEW_SERVER_QUIET", 900)
    async with factory() as s:
        s.add(Server(name="fresh", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     created_at=now, cpu_alert_percent=0, mem_alert_percent=0, last_report=report([CERTBOT])))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")
    for minutes in (2, 10, 14):
        now = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        async with factory() as s:
            row = (await s.execute(select(Server))).scalars().one()
            row.last_seen, row.last_report = now, report([CERTBOT])
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)
    assert sent == []
    now = datetime.now(timezone.utc) + timedelta(minutes=16)
    async with factory() as s:
        row = (await s.execute(select(Server))).scalars().one()
        row.last_seen, row.last_report = now, report([CERTBOT])
        await s.commit()
    await collector.evaluate_servers(factory, cfg, now)
    assert len(sent) == 1 and "certbot.service" in sent[0]
    await engine.dispose()


async def test_benign_distro_timers_do_not_alert(tmp_path, monkeypatch):
    """fwupd-refresh на ноде, откуда не открывается cdn.fwupd.org, падает на каждом прогоне, и
    никто на это не реагирует: ни алерта, ни строки в "Что сломано"; алерт, объявленный по нему
    раньше, гасится без отбоя."""
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'b.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    fw = dict(CERTBOT, unit="fwupd-refresh.service", result="exit-code", status=1)
    ts = int(now.timestamp())
    rep = {"clock_unix": ts, "uptime_seconds": 100000,
           "extras": {"units": {"v": 1, "ts": ts - 30, "units": [dict(fw, since=ts - 600)], "fix": True}}}
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report=rep,
                     alert_state={"units": 1, "units_sig": ["fwupd-refresh.service"], "units_hold": {}}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
    assert sent == [] and int(srv.alert_state.get("units", 0)) == 0
    assert not any(p["kind"] == "units" for p in collector.server_problems(srv, now))
    await engine.dispose()
