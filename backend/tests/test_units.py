"""Упавшие юниты systemd: блок helper'а units-setup, алерт "Systemd: упал юнит", команда
unit_fix (перезапустить или сбросить отметку)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.collector import unit_level, unit_why, units_text

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
    await tick([CERTBOT])  # nginx поднялся, certbot заглушен
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
