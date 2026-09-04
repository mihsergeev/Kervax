import httpx


async def test_server_enroll_report_flow(client: httpx.AsyncClient, auth_headers):
    # регистрация сервера → токен + команда установки
    r = await client.post("/api/servers", json={"name": "srv1"}, headers=auth_headers)
    assert r.status_code == 201
    body = r.json()
    token = body["token"]
    sid = body["server"]["id"]
    assert token and "install.sh" in body["install_cmd"]
    assert body["server"]["online"] is False

    # репорт агента по токену
    report = {
        "hostname": "h1", "os": "Ubuntu 24.04", "agent_version": "1.0",
        "cpu_percent": 12.5, "mem_used": 50, "mem_total": 100,
        "load": [0.5, 0.4, 0.3], "disks": [{"mount": "/", "used": 30, "total": 100}],
    }
    r = await client.post(
        "/api/agent/report", json=report,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200 and "interval" in r.json()

    # список → сервер online, снимок и хостнейм сохранены
    r = await client.get("/api/servers", headers=auth_headers)
    s = r.json()[0]
    assert s["online"] is True and s["hostname"] == "h1"
    assert s["last_report"]["cpu_percent"] == 12.5

    # метрики → есть точка с cpu
    r = await client.get(f"/api/servers/{sid}/metrics", headers=auth_headers)
    assert r.status_code == 200 and len(r.json()) >= 1
    assert r.json()[0]["cpu_percent"] == 12.5 and r.json()[0]["mem_percent"] == 50.0

    # неверный токен → 401
    r = await client.post(
        "/api/agent/report", json=report,
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


async def test_server_offline_and_recovery_alert(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "srv.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now - timedelta(seconds=600), offline_after_seconds=120,
            cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0,
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    # молчит 10 мин → оффлайн-алерт
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "недоступен" in sent[0]
    # повторно — без дубля
    sent.clear()
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []
    # снова на связи → recovery «снова доступен» (для offline — свой текст)
    async with factory() as s:
        srv = await s.scalar(__import__("sqlalchemy").select(Server))
        srv.last_seen = now
        await s.commit()
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "снова доступен" in sent[0]
    await engine.dispose()


async def test_server_disk_level_escalation(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "disk.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def rep(used):
        return {"uptime_seconds": 100, "disks": [{"mount": "/", "used": used, "total": 100}]}

    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
            cpu_alert_percent=0, mem_alert_percent=0,
            disk_warn_percent=85, disk_alert_percent=90, disk_crit_percent=95,
            last_report=rep(87),
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    async def set_disk(used):
        async with factory() as s:
            srv = await s.scalar(select(Server))
            srv.last_report = rep(used)
            await s.commit()

    # 87% → предупреждение. Уровень читается по ведущей иконке, не по слову
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and sent[0].startswith("⚠️") and "87%" in sent[0]
    assert "предупреждение" not in sent[0]
    # то же значение → без дубля
    sent.clear()
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []
    # 92% → проблема (эскалация)
    await set_disk(92)
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and sent[0].startswith("🔴") and "92%" in sent[0]
    # 97% → критично
    sent.clear()
    await set_disk(97)
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and sent[0].startswith("🚨") and "97%" in sent[0]
    # 80% → восстановление: видно, С КАКОГО значения вернулись (последнее объявленное)
    sent.clear()
    await set_disk(80)
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "снова в норме" in sent[0]
    assert "80%" in sent[0] and "было 97%" in sent[0]
    await engine.dispose()


async def test_server_reboot_alert(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "reboot.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
            cpu_alert_percent=0, mem_alert_percent=0,
            disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
            rebooted_at=now, last_report={"uptime_seconds": 10},
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "перезагружен" in sent[0]
    # повторно тот же rebooted_at → без дубля
    sent.clear()
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []
    await engine.dispose()


async def test_server_alert_rules_disable_scope_text(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import collector, settings_store
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "rules.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now - timedelta(seconds=600), offline_after_seconds=120,
            cpu_alert_percent=0, mem_alert_percent=0,
            disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    # offline-правило выключено → алерта нет
    async with factory() as s:
        await settings_store.set_server_alert_rules(s, {"offline": {"enabled": False}})
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []

    # включено, но область = конкретные серверы (не наш) → алерта нет
    async with factory() as s:
        await settings_store.set_server_alert_rules(
            s, {"offline": {"enabled": True, "scope_type": "servers", "scope": [999]}}
        )
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []

    # включено, кастомный текст, область = все → алерт по шаблону
    async with factory() as s:
        await settings_store.set_server_alert_rules(
            s, {"offline": {"enabled": True, "text": "УПАЛ {server}!", "scope_type": "all"}}
        )
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and sent[0].startswith("УПАЛ node!")
    await engine.dispose()


def test_agent_advice_from_caps():
    """По caps из отчёта агента панель советует фикс юнита; только на явный false."""
    from app.api.servers import _agent_advice
    from app.models import Server

    # агент сообщил, что kmsg НЕ читается → совет + команда с CAP_SYSLOG
    s = Server(name="n", token_hash="x", last_report={"caps": {"kmsg": False}})
    titles, cmd = _agent_advice(s)
    assert titles and "OOM" in titles[0]
    assert cmd and "CAP_SYSLOG" in cmd and "daemon-reload" in cmd

    # kmsg читается → ничего не советуем
    s.last_report = {"caps": {"kmsg": True}}
    assert _agent_advice(s) == ([], None)

    # старый агент без caps → тоже тихо (не мигаем ложно)
    s.last_report = {"cpu_percent": 5}
    assert _agent_advice(s) == ([], None)
    s.last_report = None
    assert _agent_advice(s) == ([], None)


async def test_oom_event_log(client, auth_headers):
    """Каждый отчёт агента с OOM-киллом пишет запись в журнал (когда + кого);
    эндпоинт отдаёт новые сверху."""
    r = await client.post("/api/servers", json={"name": "oomnode"}, headers=auth_headers)
    body = r.json()
    token, sid = body["token"], body["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}
    base = {"agent_version": "1.21", "mem_used": 1, "mem_total": 2,
            "disks": [{"mount": "/", "used": 1, "total": 2}]}

    await client.post("/api/agent/report", json=base, headers=ah)  # без килла
    r = await client.get(f"/api/servers/{sid}/oom-events", headers=auth_headers)
    assert r.json() == []

    # килл с именем жертвы → запись
    await client.post("/api/agent/report",
                      json=dict(base, oom_kill=2, oom_victim="php (pid 123)"), headers=ah)
    ev = (await client.get(f"/api/servers/{sid}/oom-events", headers=auth_headers)).json()
    assert len(ev) == 1 and ev[0]["victim"] == "php (pid 123)" and ev[0]["count"] == 2

    # килл без имени → вторая запись, новые сверху
    await client.post("/api/agent/report", json=dict(base, oom_kill=1), headers=ah)
    ev = (await client.get(f"/api/servers/{sid}/oom-events", headers=auth_headers)).json()
    assert len(ev) == 2 and ev[0]["victim"] == "" and ev[0]["count"] == 1


async def test_docker_command_flow(client, auth_headers):
    """Постановка docker-команды → агент забирает её в ответе (running, без повтора)
    → постит результат → статус done."""
    r = await client.post("/api/servers", json={"name": "dk"}, headers=auth_headers)
    body = r.json()
    token, sid = body["token"], body["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}

    r = await client.post(f"/api/servers/{sid}/docker/command",
                          json={"container": "web", "action": "restart"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    cid = r.json()["id"]

    rep = {"agent_version": "1.26", "mem_used": 1, "mem_total": 2,
           "disks": [{"mount": "/", "used": 1, "total": 2}]}
    resp = (await client.post("/api/agent/report", json=rep, headers=ah)).json()
    assert resp["docker_commands"] == [
        {"id": cid, "container": "web", "action": "restart", "tail": 200, "since": 0}
    ]
    st = (await client.get(f"/api/servers/{sid}/docker/command/{cid}", headers=auth_headers)).json()
    assert st["status"] == "running"

    # повторный отчёт команду НЕ пересылает
    resp2 = (await client.post("/api/agent/report", json=rep, headers=ah)).json()
    assert resp2["docker_commands"] == []

    # агент постит результат → done
    r = await client.post("/api/agent/docker-result",
                          json={"id": cid, "ok": True, "output": "OK"}, headers=ah)
    assert r.status_code == 200
    st = (await client.get(f"/api/servers/{sid}/docker/command/{cid}", headers=auth_headers)).json()
    assert st["status"] == "done" and st["ok"] is True and st["result"] == "OK"


async def test_agent_managed_update(client, auth_headers, monkeypatch):
    # enroll + первый репорт (агент на 1.5)
    r = await client.post("/api/servers", json={"name": "n1"}, headers=auth_headers)
    body = r.json()
    token, sid = body["token"], body["server"]["id"]
    rep = {"agent_version": "1.5", "mem_used": 1, "mem_total": 2,
           "disks": [{"mount": "/", "used": 1, "total": 2}]}
    ah = {"Authorization": f"Bearer {token}"}
    await client.post("/api/agent/report", json=rep, headers=ah)

    # нет подписанного релиза → 409
    r = await client.post("/api/servers/agent-update",
                          json={"version": "1.6", "server_ids": [sid]}, headers=auth_headers)
    assert r.status_code == 409

    # эмулируем наличие релиза 1.6
    monkeypatch.setattr("app.api.servers._available_agent_version", lambda: "1.6")

    # чужая версия к раскатке → 400
    r = await client.post("/api/servers/agent-update",
                          json={"version": "9.9", "server_ids": [sid]}, headers=auth_headers)
    assert r.status_code == 400

    # доступная версия видна
    r = await client.get("/api/servers/agent-release", headers=auth_headers)
    assert r.json()["version"] == "1.6"

    # canary на нашу ноду → target выставлен
    r = await client.post("/api/servers/agent-update",
                          json={"version": "1.6", "server_ids": [sid]}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()[0]["target_agent_version"] == "1.6"

    # репорт агента (всё ещё 1.5) → панель просит обновиться до 1.6
    r = await client.post("/api/agent/report", json=rep, headers=ah)
    assert r.json()["update"] == {"version": "1.6"}

    # агент обновился и отчитался 1.6 → сигнала обновления больше нет
    rep16 = dict(rep, agent_version="1.6")
    r = await client.post("/api/agent/report", json=rep16, headers=ah)
    assert r.json()["update"] is None

    # снять target
    r = await client.post("/api/servers/agent-update/cancel",
                          json={"server_ids": [sid]}, headers=auth_headers)
    assert r.status_code == 200
    r = await client.get(f"/api/servers/{sid}", headers=auth_headers)
    assert r.json()["target_agent_version"] == ""


async def test_server_temp_throttle_conditions():
    from datetime import datetime, timedelta, timezone

    from app import collector
    from app.models import Server

    now = datetime.now(timezone.utc)
    s = Server(
        name="n", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
        offline_after_seconds=120, temp_alert_c=80, alert_sustain_seconds=900,
        cpu_alert_percent=0, mem_alert_percent=0,
        disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
        last_report={"cpu_temp": 85, "cpu_throttle": 3},
    )
    cond = collector._server_conditions(s, now)
    # ПЕРВЫЙ интервал: температура горячая, но держится 0с < 15мин → уровень 0
    # (одиночные спайки не требуют вмешательства — не шумим); throttle: стрик 1 < 3
    assert cond["temp"][0] == 0 and cond["temp"][1]["value"] == 85
    assert cond["throttle"][0] == 0 and cond["throttle"][1]["streak"] == 1

    # температура держится дольше sustain (since 20мин назад) → уровень 1;
    # троттлинг на 3-м интервале подряд → устойчиво → уровень 1
    s.alert_state = {
        "temp_since": (now - timedelta(minutes=20)).isoformat(),
        "throttle_streak": 2,
    }
    cond = collector._server_conditions(s, now)
    assert cond["temp"][0] == 1
    assert cond["throttle"][0] == 1 and cond["throttle"][1]["streak"] == 3

    # «холодный» троттлинг (49°C) сбрасывает стрик даже после долгой серии → 0
    s.alert_state = {"throttle_streak": 5}
    s.last_report = {"cpu_temp": 49, "cpu_throttle": 1016}
    cond = collector._server_conditions(s, now)
    assert cond["throttle"][0] == 0 and cond["throttle"][1]["streak"] == 0

    # под порогом / без троттлинга → уровень 0 (в норме), since сброшен
    s.alert_state = None
    s.last_report = {"cpu_temp": 50, "cpu_throttle": 0}
    cond = collector._server_conditions(s, now)
    assert cond["temp"][0] == 0 and cond["temp"][1]["since"] is None and cond["throttle"][0] == 0

    # нет датчика (None) → условий нет вовсе (не алертим на VM без сенсоров)
    s.last_report = {"cpu_temp": None, "cpu_throttle": None}
    cond = collector._server_conditions(s, now)
    assert "temp" not in cond and "throttle" not in cond


async def test_server_conntrack_disktemp_conditions():
    from datetime import datetime, timedelta, timezone

    from app import collector
    from app.models import Server

    now = datetime.now(timezone.utc)
    s = Server(
        name="n", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
        offline_after_seconds=120, temp_alert_c=0, alert_sustain_seconds=900,
        cpu_alert_percent=0, mem_alert_percent=0,
        disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
        conntrack_alert_percent=90, disk_temp_alert_c=60,
        last_report={
            "conntrack_count": 95, "conntrack_max": 100,
            "disk_devs": [{"dev": "sda", "temp": 65}, {"dev": "sdb", "temp": 40}],
        },
    )
    cond = collector._server_conditions(s, now)
    # ПЕРВЫЙ интервал: превышения есть, но держатся 0с < 15мин → уровень 0 (гасим спайки)
    assert cond["conntrack"][0] == 0 and cond["conntrack"][1]["value"] == 95
    assert cond["disktemp"][0] == 0 and cond["disktemp"][1]["value"] == 65  # самый горячий (65≥60)

    # держатся дольше sustain (since 20мин назад) → уровень 1
    ago = (now - timedelta(minutes=20)).isoformat()
    s.alert_state = {"conntrack_since": ago, "disktemp_since": ago}
    cond = collector._server_conditions(s, now)
    assert cond["conntrack"][0] == 1 and cond["disktemp"][0] == 1

    # под порогами → уровень 0
    s.last_report = {
        "conntrack_count": 10, "conntrack_max": 100,
        "disk_devs": [{"dev": "sda", "temp": 40}],
    }
    cond = collector._server_conditions(s, now)
    assert cond["conntrack"][0] == 0 and cond["disktemp"][0] == 0

    # conntrack не загружен (max=0) и у дисков нет датчика (temp=None) → условий нет
    s.last_report = {
        "conntrack_count": 0, "conntrack_max": 0,
        "disk_devs": [{"dev": "sda", "temp": None}],
    }
    cond = collector._server_conditions(s, now)
    assert "conntrack" not in cond and "disktemp" not in cond


async def test_server_alert_flood_digest(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "flood.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        for i in range(8):  # 8 нод разом замолчали (напр. упал аплинк)
            s.add(Server(
                name=f"node{i}", token_hash=f"x{i}", enabled=True,
                last_seen=now - timedelta(seconds=600), offline_after_seconds=120,
                cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0,
            ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    # порог 6 → 8 срабатываний схлопываются в ОДИН дайджест
    settings = Settings(alert_webhook="http://hook", alert_flood_threshold=6)
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "🌊" in sent[0] and "8" in sent[0]

    # порог 0 = выкл → каждое сработавшее шлётся отдельно (8 сообщений)
    async with factory() as s:
        for srv in await s.scalars(__import__("sqlalchemy").select(Server)):
            srv.alert_state = None
        await s.commit()
    sent.clear()
    settings = Settings(alert_webhook="http://hook", alert_flood_threshold=0)
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 8
    await engine.dispose()


async def test_server_alert_mute(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "mute.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        # CPU выше порога, но «cpu» заглушён для этого сервера → алерта нет.
        # sustain=0 → без задержки (тест про МЬЮТ, дебаунс проверяется отдельно).
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
            offline_after_seconds=120, cpu_alert_percent=50, alert_sustain_seconds=0,
            mem_alert_percent=0, disk_alert_percent=0,
            alert_mutes=["cpu"], last_report={"cpu_percent": 95},
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook", alert_flood_threshold=0)
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []  # cpu заглушён → тишина

    # убрать мьют → алерт приходит (sustain=0 → сразу)
    async with factory() as s:
        srv = await s.scalar(__import__("sqlalchemy").select(Server))
        srv.alert_mutes = []
        await s.commit()
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "CPU" in sent[0]
    await engine.dispose()


async def test_server_alert_snooze_per_type(tmp_path, monkeypatch):
    """Точечный снуз ОДНОГО типа глушит только его: OOM заглушён, но offline
    (сервер замолчал) продолжает алертить."""
    from datetime import datetime, timedelta, timezone

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "snz.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=6)).isoformat()
    async with factory() as s:
        # сервер замолчал (offline) + был OOM-kill; oom заглушён точечно на 6ч
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True,
            last_seen=now - timedelta(seconds=600), offline_after_seconds=120,
            cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0,
            oom_total=1, oom_victim="php (pid 1)",
            alert_snoozes={"oom": future},
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook", alert_flood_threshold=0)
    await collector.evaluate_servers(factory, settings, now)
    # oom заглушён → нет; offline НЕ заглушён → есть ровно он
    assert len(sent) == 1
    assert "OOM" not in sent[0] and "недоступен" in sent[0]

    # снуз истёк → oom тоже прорывается
    async with factory() as s:
        srv = await s.scalar(__import__("sqlalchemy").select(Server))
        srv.alert_snoozes = {"oom": (now - timedelta(hours=1)).isoformat()}
        await s.commit()
    sent.clear()
    await collector.evaluate_servers(factory, settings, now)
    assert any("OOM" in m for m in sent)
    await engine.dispose()


async def test_agent_ip_allowlist_file(client: httpx.AsyncClient, auth_headers):
    """agent_ip сохраняется и попадает в data/agent_allow_ips (для хостового
    фаервол-синка); удаление сервера убирает адрес из файла."""
    import os
    from app.config import get_settings

    r = await client.post(
        "/api/servers",
        json={"name": "fw-node", "agent_ip": "203.0.113.77"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    sid = r.json()["server"]["id"]
    assert r.json()["server"]["agent_ip"] == "203.0.113.77"

    path = os.path.join(get_settings().data_dir, "agent_allow_ips")
    with open(path, encoding="utf-8") as fh:
        assert "203.0.113.77" in fh.read()

    # смена IP через PATCH обновляет файл
    r = await client.patch(
        f"/api/servers/{sid}", json={"agent_ip": "203.0.113.88"}, headers=auth_headers
    )
    assert r.status_code == 200
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    assert "203.0.113.88" in body and "203.0.113.77" not in body

    # удаление сервера убирает IP
    assert (
        await client.delete(f"/api/servers/{sid}", headers=auth_headers)
    ).status_code == 204
    with open(path, encoding="utf-8") as fh:
        assert "203.0.113.88" not in fh.read()


async def test_throttle_alerts_only_when_sustained_and_hot(tmp_path, monkeypatch):
    """Троттлинг: одиночные/холодные спайки счётчика — молчим (шум); алерт только
    когда процессор горячий И тормозит ≥3 интервала подряд (недоохлаждение)."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "thr.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def rep(throttle, temp):
        return {"uptime_seconds": 100, "cpu_throttle": throttle, "cpu_temp": temp}

    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
            cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0,
            last_report=rep(0, 45),
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    async def tick(throttle, temp):
        async with factory() as s:
            (await s.scalar(select(Server))).last_report = rep(throttle, temp)
            await s.commit()
        await collector.evaluate_servers(factory, settings, now)

    # холодный спайк (49°C) — не тепловой, молчим даже много раз подряд
    for _ in range(4):
        await tick(1016, 49)
    assert sent == []

    # горячий, но одиночный (сосед — 0) → стрик не набирается → молчим
    await tick(68, 95)
    await tick(0, 60)
    await tick(70, 95)
    assert sent == []

    # горячий троттлинг 3 интервала подряд → УСТОЙЧИВЫЙ → один алерт
    await tick(50, 95)
    await tick(60, 96)
    await tick(80, 97)
    assert len(sent) == 1 and "троттлинг" in sent[0].lower()
    # держится дальше → без дублей
    sent.clear()
    await tick(90, 98)
    assert sent == []
    # остыл/перестал → recovery
    await tick(0, 55)
    assert len(sent) == 1 and "снова в норме" in sent[0]
    await engine.dispose()


async def test_oom_kill_one_shot_alert(tmp_path, monkeypatch):
    """OOM (кумулятивный oom_total) → одноразовый алерт с числом и жертвой; повторный
    тик без роста счётчика не дублирует (high-water mark oom_seen); рост → снова."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "oom.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
            cpu_alert_percent=0, mem_alert_percent=0, disk_alert_percent=0,
            oom_total=3, oom_victim="mysqld (pid 1234)",
            last_report={"uptime_seconds": 100},
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    # oom_total=3 → алерт с числом и жертвой
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "OOM" in sent[0] and "3" in sent[0] and "mysqld" in sent[0]
    # счётчик не вырос → без дубля
    sent.clear()
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []
    # oom_total вырос (3→4) → снова алерт (дельта 1)
    async with factory() as s:
        (await s.scalar(select(Server))).oom_total = 4
        await s.commit()
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "1" in sent[0]
    await engine.dispose()


async def test_docker_container_crash_loop_alert(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "dloop.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def rep(rc):
        return {"uptime_seconds": 100, "docker": {"present": True, "access": True,
                "containers": [{"name": "web", "state": "running", "policy": "always", "restarts": rc}]}}

    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
            offline_after_seconds=120, cpu_alert_percent=0, mem_alert_percent=0,
            disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
            last_report=rep(0),
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    async def set_rc(rc):
        async with factory() as s:
            (await s.scalar(select(Server))).last_report = rep(rc)
            await s.commit()

    # baseline rc=0, затем три прироста подряд → на 3-м приросте (rc=3) crash-loop
    await collector.evaluate_servers(factory, settings, now)  # rc0 baseline
    assert sent == []
    for rc in (1, 2):
        await set_rc(rc)
        await collector.evaluate_servers(factory, settings, now)
    assert sent == []  # 2 прироста < порога 3
    await set_rc(3)
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "web" in sent[0] and "перезапус" in sent[0].lower()
    # тот же rc → без дубля
    sent.clear()
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []
    await engine.dispose()


async def test_docker_container_down_and_policy_guard(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "ddown.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    def rep(state, policy):
        return {"uptime_seconds": 100, "docker": {"present": True, "access": True,
                "containers": [{"name": "db", "state": state, "policy": policy, "restarts": 0}]}}

    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
            offline_after_seconds=120, alert_sustain_seconds=900,
            cpu_alert_percent=0, mem_alert_percent=0,
            disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
            last_report=rep("exited", "always"),
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    async def set_rep(state, policy):
        async with factory() as s:
            srv = await s.scalar(select(Server))
            srv.last_seen = now  # держим онлайн относительно текущего now
            srv.last_report = rep(state, policy)
            await s.commit()

    # exited+always, но держится 0с < sustain → пока молчим
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []
    # спустя >15 мин всё ещё exited → алерт «упал»
    later = now + timedelta(minutes=16)
    async with factory() as s:
        (await s.scalar(select(Server))).last_seen = later
        await s.commit()
    await collector.evaluate_servers(factory, settings, later)
    assert len(sent) == 1 and "db" in sent[0]
    # снова running → восстановление
    sent.clear()
    async with factory() as s:
        srv = await s.scalar(select(Server))
        srv.last_seen = later
        srv.last_report = rep("running", "always")
        await s.commit()
    await collector.evaluate_servers(factory, settings, later)
    assert len(sent) == 1 and "работает" in sent[0]
    # policy=no (намеренно остановлен/one-shot) → НЕ алертим даже спустя время
    sent.clear()
    much_later = later + timedelta(minutes=30)
    async with factory() as s:
        srv = await s.scalar(select(Server))
        srv.last_seen = much_later
        srv.last_report = rep("exited", "no")
        srv.alert_state = None
        await s.commit()
    await collector.evaluate_servers(factory, settings, much_later)
    await collector.evaluate_servers(factory, settings, much_later)
    assert sent == []
    await engine.dispose()


async def test_kube_command_flow(client, auth_headers):
    """Постановка kube-команды (rollout_restart) → агент забирает (running, без повтора)
    → постит результат → done. Плюс валидация имени/действия на входе."""
    r = await client.post("/api/servers", json={"name": "kb"}, headers=auth_headers)
    body = r.json()
    token, sid = body["token"], body["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}

    # невалидное действие / имя отвергаются схемой (422)
    bad = await client.post(f"/api/servers/{sid}/kube/command",
                            json={"ns": "default", "kind": "deployment", "name": "web", "action": "exec"},
                            headers=auth_headers)
    assert bad.status_code == 422
    bad2 = await client.post(f"/api/servers/{sid}/kube/command",
                             json={"ns": "default", "name": "Web_Bad", "action": "delete_pod"},
                             headers=auth_headers)
    assert bad2.status_code == 422

    r = await client.post(f"/api/servers/{sid}/kube/command",
                          json={"ns": "default", "kind": "deployment", "name": "backend",
                                "action": "rollout_restart"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    cid = r.json()["id"]

    rep = {"agent_version": "1.33", "mem_used": 1, "mem_total": 2,
           "disks": [{"mount": "/", "used": 1, "total": 2}]}
    resp = (await client.post("/api/agent/report", json=rep, headers=ah)).json()
    assert resp["kube_commands"] == [
        {"id": cid, "ns": "default", "kind": "deployment", "name": "backend",
         "action": "rollout_restart", "tail": 400, "since": 0}
    ]
    # повторный отчёт команду НЕ пересылает
    resp2 = (await client.post("/api/agent/report", json=rep, headers=ah)).json()
    assert resp2["kube_commands"] == []

    r = await client.post("/api/agent/kube-result",
                          json={"id": cid, "ok": True, "output": "OK"}, headers=ah)
    assert r.status_code == 200
    st = (await client.get(f"/api/servers/{sid}/kube/command/{cid}", headers=auth_headers)).json()
    assert st["status"] == "done" and st["ok"] is True


async def test_kube_report_stored(client, auth_headers):
    """Агент шлёт снимок kube → он сохраняется в last_report и виден в API сервера."""
    r = await client.post("/api/servers", json={"name": "kb2"}, headers=auth_headers)
    body = r.json()
    token, sid = body["token"], body["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}
    rep = {"agent_version": "1.33", "mem_used": 1, "mem_total": 2,
           "disks": [{"mount": "/", "used": 1, "total": 2}],
           "kube": {"present": True, "access": True, "flavor": "k0s", "version": "v1.35.5+k0s",
                    "namespaces": 7,
                    "nodes": [{"name": "n1", "ready": True, "roles": "control-plane", "version": "v1.35.5+k0s"}],
                    "pods": [{"ns": "default", "name": "web-1", "phase": "Running", "ready": True, "restarts": 0}]}}
    await client.post("/api/agent/report", json=rep, headers=ah)
    srv = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    assert srv["last_report"]["kube"]["flavor"] == "k0s"
    assert srv["last_report"]["kube"]["nodes"][0]["ready"] is True


async def test_backup_alerts(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "bk.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    nowts = int(now.timestamp())

    def rep(success, ts):
        return {"uptime_seconds": 100,
                "backup": {"present": True, "metric_present": True, "success": success, "last_backup_ts": ts}}

    async with factory() as s:
        s.add(Server(
            name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
            offline_after_seconds=120, cpu_alert_percent=0, mem_alert_percent=0,
            disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
            last_report=rep(1, nowts),
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    async def set_rep(success, ts):
        async with factory() as s:
            srv = await s.scalar(select(Server))
            srv.last_seen = now
            srv.last_report = rep(success, ts)
            await s.commit()

    # свежий успешный бэкап → тихо
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []
    # последний прогон упал → алерт «ошибка»
    await set_rep(0, nowts)
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "ошибк" in sent[0].lower()
    # снова успех → восстановление
    sent.clear()
    await set_rep(1, nowts)
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "норм" in sent[0].lower()
    # бэкап устарел на 3 дня → алерт «не свежий»
    sent.clear()
    await set_rep(1, nowts - 3 * 86400)
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and ("свеж" in sent[0].lower() or "устар" in sent[0].lower())
    await engine.dispose()


async def test_backup_command_flow(client, auth_headers):
    """Постановка backup-команды (set_schedule) → агент забирает → результат → done.
    Плюс валидация: кривой путь и кривое время отвергаются схемой (422)."""
    r = await client.post("/api/servers", json={"name": "bkc"}, headers=auth_headers)
    body = r.json()
    token, sid = body["token"], body["server"]["id"]
    ah = {"Authorization": f"Bearer {token}"}

    bad = await client.post(f"/api/servers/{sid}/backup/command",
                            json={"action": "set_paths", "mode": "include", "paths": ["../etc/passwd"]},
                            headers=auth_headers)
    assert bad.status_code == 422
    bad2 = await client.post(f"/api/servers/{sid}/backup/command",
                             json={"action": "set_schedule", "schedule": "99:99"}, headers=auth_headers)
    assert bad2.status_code == 422

    r = await client.post(f"/api/servers/{sid}/backup/command",
                          json={"action": "set_schedule", "schedule": "02:30"}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["status"] == "pending"
    cid = r.json()["id"]

    rep = {"agent_version": "1.35", "mem_used": 1, "mem_total": 2,
           "disks": [{"mount": "/", "used": 1, "total": 2}]}
    resp = (await client.post("/api/agent/report", json=rep, headers=ah)).json()
    assert resp["backup_commands"] == [
        {"id": cid, "action": "set_schedule", "mode": "exclude", "paths": [], "schedule": "02:30"}
    ]
    resp2 = (await client.post("/api/agent/report", json=rep, headers=ah)).json()
    assert resp2["backup_commands"] == []

    r = await client.post("/api/agent/backup-result",
                          json={"id": cid, "ok": True, "output": "OK"}, headers=ah)
    assert r.status_code == 200
    st = (await client.get(f"/api/servers/{sid}/backup/command/{cid}", headers=auth_headers)).json()
    assert st["status"] == "done" and st["ok"] is True


async def test_backup_repo_alert_and_mute(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "brepo.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    nowts = int(now.timestamp())

    def rep():
        return {"uptime_seconds": 100, "backup_server": {"present": True, "running": True, "version": "0.14",
                "repos": [
                    {"name": "fresh", "valid": True, "snapshots": 5, "last_activity": nowts, "locked": False},
                    {"name": "old-oneoff", "valid": True, "snapshots": 2, "last_activity": nowts - 900 * 86400, "locked": False},
                ]}}

    async with factory() as s:
        s.add(Server(
            name="bsrv", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
            offline_after_seconds=120, cpu_alert_percent=0, mem_alert_percent=0,
            disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
            last_report=rep(),
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    # old-oneoff устарел (900 дн) → алерт с его именем
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "old-oneoff" in sent[0]
    # тот же набор проблем → без дубля
    sent.clear()
    await collector.evaluate_servers(factory, settings, now)
    assert sent == []
    # заглушаем old-oneoff → проблем нет → recovery
    async with factory() as s:
        (await s.scalar(select(Server))).backup_repo_mutes = ["old-oneoff"]
        await s.commit()
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "норм" in sent[0].lower()
    await engine.dispose()


async def test_backup_missing_alert_and_optout(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "bmiss.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)

    async with factory() as s:
        # онлайн-сервер БЕЗ бэкапа и без флага «не требуется» → должен алертить.
        # Нода и состояние «бэкапа нет» — старше суток: первое срабатывание
        # намеренно отложено на _BACKUP_MISSING_GRACE (свежую ноду ещё настраивают).
        old_ts = now - timedelta(days=2)
        s.add(Server(
            name="node", token_hash="x", enabled=True, last_seen=now,
            offline_after_seconds=120, cpu_alert_percent=0, mem_alert_percent=0,
            disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
            last_report={"uptime_seconds": 100},
            created_at=old_ts,
            alert_state={"backup_missing_since": old_ts.isoformat()},
        ))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")

    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "не настроен" in sent[0]
    # ставим галочку «бэкап не требуется» → recovery
    sent.clear()
    async with factory() as s:
        (await s.scalar(select(Server))).backup_not_required = True
        await s.commit()
    await collector.evaluate_servers(factory, settings, now)
    assert len(sent) == 1 and "норм" in sent[0].lower()
    await engine.dispose()


async def test_metrics_written_no_more_often_than_interval(client, auth_headers, monkeypatch):
    """Частые отчёты не должны раздувать историю метрик.

    Агент шлёт отчёты часто — это нужно для «онлайн» и порогов. Но в
    server_metrics строка нужна раз в интервал: таблица была самой большой в
    базе (1,6 ГБ при десяти нодах). При этом СВЕЖЕСТЬ страдать не должна:
    last_report обновляется каждым отчётом."""
    from sqlalchemy import func, select

    from app.config import get_settings
    from app.models import Server, ServerMetric

    get_settings.cache_clear()
    monkeypatch.setenv("KERVAX_SERVER_METRIC_INTERVAL_SECONDS", "3600")
    get_settings.cache_clear()

    r = await client.post("/api/servers", json={"name": "rate"}, headers=auth_headers)
    token = r.json()["token"]
    head = {"Authorization": f"Bearer {token}"}

    async def report(cpu: float):
        return await client.post(
            "/api/agent/report",
            json={
                "hostname": "h", "os": "Ubuntu", "agent_version": "1.97",
                "cpu_percent": cpu, "mem_used": 50, "mem_total": 100,
                "load": [0.1, 0.1, 0.1], "disks": [],
            },
            headers=head,
        )

    for cpu in (10.0, 20.0, 30.0, 40.0):
        assert (await report(cpu)).status_code == 200

    app = client._transport.app  # type: ignore[attr-defined]
    async with app.state.session_factory() as s:
        n = await s.scalar(select(func.count()).select_from(ServerMetric))
        srv = await s.scalar(select(Server).where(Server.name == "rate"))
    # четыре отчёта, интервал час → в истории одна точка
    assert n == 1, f"ожидали одну строку метрик, получили {n}"
    # но оперативные данные — от ПОСЛЕДНЕГО отчёта
    assert srv.last_report["cpu_percent"] == 40.0
    assert srv.metric_written_at is not None

    get_settings.cache_clear()


async def test_server_db_connection_conditions():
    """Занятость слотов подключений СУБД: порог, дебаунс и самый нагруженный инстанс.

    Коннекты кончаются раньше, чем это станет заметно по CPU или памяти самой базы:
    она жива и отвечает, а приложение уже получает «sorry, too many clients already».
    """
    from datetime import datetime, timedelta, timezone

    from app import collector
    from app.models import Server

    now = datetime.now(timezone.utc)
    s = Server(
        name="n", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
        offline_after_seconds=120, temp_alert_c=0, alert_sustain_seconds=900,
        cpu_alert_percent=0, mem_alert_percent=0,
        disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
        conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=85,
        last_report={"db_stats": [
            {"engine": "pg", "container": "spare-db", "conn_used": 10, "conn_max": 100},
            {"engine": "pg", "container": "hot-db", "conn_used": 92, "conn_max": 100},
        ]},
    )
    cond = collector._server_conditions(s, now)
    # берём САМЫЙ нагруженный инстанс, а не первый попавшийся: алерт на сервер один
    assert cond["db_conn"][1]["value"] == 92
    assert cond["db_conn"][1]["engine"] == "hot-db"
    assert cond["db_conn"][1]["used"] == 92 and cond["db_conn"][1]["limit"] == 100
    # первый интервал: превышение есть, но не удержалось → уровень 0 (гасим всплески пулера)
    assert cond["db_conn"][0] == 0

    s.alert_state = {"db_conn_since": (now - timedelta(minutes=20)).isoformat()}
    assert collector._server_conditions(s, now)["db_conn"][0] == 1

    # ниже порога → условие есть, но уровень 0 (иначе прошлый алерт не закрылся бы)
    s.last_report = {"db_stats": [{"engine": "pg", "conn_used": 12, "conn_max": 100}]}
    cond = collector._server_conditions(s, now)
    assert cond["db_conn"][0] == 0 and cond["db_conn"][1]["value"] == 12

    # движок не отдал лимит (старый helper) → не выдумываем процент
    s.last_report = {"db_stats": [{"engine": "pg", "conn_used": 5, "conn_max": 0}]}
    assert "db_conn" not in collector._server_conditions(s, now)

    # порог 0 = проверка выключена для всей ноды
    s.db_conn_alert_percent = 0
    s.last_report = {"db_stats": [{"engine": "pg", "conn_used": 99, "conn_max": 100}]}
    assert "db_conn" not in collector._server_conditions(s, now)


def _kube_server(now, **kw):
    """Сервер без единого включённого порога: мешать проверке нечему."""
    from app.models import Server

    base = dict(
        name="k", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
        offline_after_seconds=120, temp_alert_c=0, alert_sustain_seconds=900,
        cpu_alert_percent=0, mem_alert_percent=0,
        disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0,
        conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
        kube_expiry_alert_days=14,
    )
    base.update(kw)
    return Server(**base)


async def test_server_kube_expiry_conditions():
    """Сроки кластера: порог в днях, самый ранний срок и счётчик остальных.

    Это отказ, который не виден ни одной метрикой: истёкший токен Flux не роняет
    подов и не грузит процессор — просто новое перестаёт приезжать.
    """
    from datetime import datetime, timedelta, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    day = 86400
    ts = int(now.timestamp())

    s = _kube_server(now, last_report={"kube_expiry": [
        {"kind": "cluster-cert", "where": "/var/lib/k0s/pki/server.crt", "expires": ts + 3600 * day},
        {"kind": "flux-token", "where": "flux-system/flux-system", "expires": ts + 5 * day, "note": "GitRepository"},
        {"kind": "secret-cert", "where": "default/api-tls", "expires": ts + 9 * day},
    ]})
    cond = collector._server_conditions(s, now)
    # самый ранний срок, а не первый в списке: алерт на сервер один
    # уточнение из хелпера дописано в скобках — по пути к секрету не видно, чей он
    assert "flux-system/flux-system (GitRepository)" == cond["kube_expiry"][1]["where"]
    assert "истекает через 5 дн." == cond["kube_expiry"][1]["value"]
    assert "токен Flux" in cond["kube_expiry"][1]["what"]
    # второй подпадающий под порог упомянут числом: длинного списка в алерте не будет
    assert cond["kube_expiry"][1]["more"] == " + ещё 1 на этом сервере"
    # дальний сертификат (10 лет) в счёт не идёт — иначе алерт был бы всегда
    assert cond["kube_expiry"][0] == 0  # первый интервал: дебаунс общий

    s.alert_state = {"kube_expiry_since": (now - timedelta(minutes=20)).isoformat()}
    assert collector._server_conditions(s, now)["kube_expiry"][0] == 1

    # уже истёкший: считаем дни назад, а не «через -2 дн.»
    s.last_report = {"kube_expiry": [
        {"kind": "flux-token", "where": "flux-system/flux-system", "expires": ts - 2 * day},
    ]}
    assert collector._server_conditions(s, now)["kube_expiry"][1]["value"] == "ИСТЁК 2 дн. назад"

    # всё далеко → условие есть, уровень 0: прошлый алерт должен закрыться
    s.last_report = {"kube_expiry": [
        {"kind": "cluster-cert", "where": "/x.crt", "expires": ts + 300 * day},
    ]}
    cond = collector._server_conditions(s, now)
    assert cond["kube_expiry"][0] == 0

    # порог 0 = проверка выключена для ноды
    s.kube_expiry_alert_days = 0
    s.last_report = {"kube_expiry": [{"kind": "flux-token", "where": "a/b", "expires": ts + day}]}
    assert "kube_expiry" not in collector._server_conditions(s, now)


async def test_server_flux_down_conditions():
    """Вставшая доставка Flux: отозванный токен предупредить о себе не успевает.

    Срок можно проспать, а токен — ещё и отозвать руками; тогда алертить надо по
    факту, а не по дате. Реконсиляция при этом поломкой не считается.
    """
    from datetime import datetime, timedelta, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    s = _kube_server(now, last_report={"flux": [
        {"kind": "Kustomization", "where": "flux-system/apps", "ready": True},
        {"kind": "GitRepository", "where": "flux-system/flux-system", "ready": False,
         "reason": "GitOperationFailed",
         "message": "authentication required: HTTP Basic: Access denied"},
    ]})
    cond = collector._server_conditions(s, now)
    assert cond["flux_down"][1]["where"] == "flux-system/flux-system"
    # причина по-русски, а сырой код Flux — в скобках: по нему гуглят
    assert "истёк или отозван токен" in cond["flux_down"][1]["reason"]
    assert "GitOperationFailed" in cond["flux_down"][1]["reason"]
    # преамбула Flux вырезана, суть осталась
    assert "Access denied" in cond["flux_down"][1]["message"]
    assert "failed to checkout" not in cond["flux_down"][1]["message"]
    assert cond["flux_down"][0] == 0  # первый интервал — дебаунс

    s.alert_state = {"flux_down_since": (now - timedelta(minutes=20)).isoformat()}
    assert collector._server_conditions(s, now)["flux_down"][0] == 1

    # реконсиляция — не поломка: иначе алерт срабатывал бы на каждой выкатке
    s.last_report = {"flux": [
        {"kind": "HelmRelease", "where": "apps/web", "ready": False, "reason": "Progressing"},
    ]}
    cond = collector._server_conditions(s, now)
    assert cond["flux_down"][0] == 0

    # Flux в кластере нет вовсе → условия нет (не «всё хорошо», а «нечего проверять»)
    s.last_report = {"kube_expiry": []}
    assert "flux_down" not in collector._server_conditions(s, now)

    # Flux читался и всё зелено → условие есть с уровнем 0, чтобы алерт закрылся
    s.last_report = {"flux": []}
    assert collector._server_conditions(s, now)["flux_down"][0] == 0


async def test_flux_alert_points_at_the_root_of_the_cascade():
    """Одна упавшая сборка тянет за собой зависимые — алертить надо о причине.

    Поймано на живом парке: в ленте висело «Kustomization op-dg-prod-backend —
    DependencyNotReady: dependency op-dg-prod-migrations is not ready», то есть
    следствие, выбранное по алфавиту. Инженеру приходилось самому идти искать,
    что же именно упало.
    """
    from datetime import datetime, timedelta, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    s = _kube_server(now, last_report={"flux": [
        # по алфавиту первый — и это следствие
        {"kind": "Kustomization", "where": "flux-system/op-dg-prod-backend",
         "ready": False, "reason": "DependencyNotReady",
         "message": "dependency 'flux-system/op-dg-prod-migrations' is not ready"},
        {"kind": "Kustomization", "where": "flux-system/op-dg-prod-migrations",
         "ready": False, "reason": "HealthCheckFailed",
         "message": "health check failed after 33.9ms: failed early due to stalled "
                    "resources: [Job/op-dg-prod/op-dg-migrations status: 'Failed']"},
        {"kind": "Kustomization", "where": "flux-system/op-dg-prod-processing",
         "ready": False, "reason": "DependencyNotReady",
         "message": "dependency 'flux-system/op-dg-prod-migrations' is not ready"},
    ]})
    s.alert_state = {"flux_down_since": (now - timedelta(minutes=20)).isoformat()}
    lvl, ctx = collector._server_conditions(s, now)["flux_down"]
    assert lvl == 1
    assert ctx["where"] == "flux-system/op-dg-prod-migrations", "показали следствие вместо причины"
    # ждущие посчитаны отдельно: масштаб виден, но список не раздут
    assert "2 ждут этого" in ctx["more"]
    # объект, который не поднялся, назван по-человечески
    assert ctx["message"] == "Job op-dg-migrations: Failed"
    # подсказка подставлена с реальными namespace и именем
    assert "kubectl -n flux-system describe kustomization op-dg-prod-migrations" in ctx["hint"]


async def test_expiry_alert_explains_what_to_do():
    """Дата без объяснения вызывает недоумение, а не действие.

    «ИСТЁК 49 дн. назад» на живом кластере — первый вопрос инженера «а почему
    тогда всё работает?». Ответ должен быть в самом сообщении.
    """
    from datetime import datetime, timedelta, timezone

    from app import collector

    now = datetime.now(timezone.utc)
    ts = int(now.timestamp())
    s = _kube_server(now, last_report={"kube_expiry": [
        {"kind": "secret-cert", "where": "tech1/backend-hosts-ssl",
         "expires": ts - 49 * 86400, "note": "tls.crt"},
    ]})
    s.alert_state = {"kube_expiry_since": (now - timedelta(minutes=20)).isoformat()}
    ctx = collector._server_conditions(s, now)["kube_expiry"][1]
    assert "при рестарте пода" in ctx["advice"]
    # «TLS-сертификат … (tls.crt)» — повтор самого себя, его быть не должно
    assert "tls.crt" not in ctx["where"]

    # ещё не истёк — совет другой: не «уберите», а «обновите до срока»
    s.last_report = {"kube_expiry": [
        {"kind": "flux-token", "where": "flux-system/flux-system", "expires": ts + 5 * 86400},
    ]}
    ctx = collector._server_conditions(s, now)["kube_expiry"][1]
    assert "перестанет забирать изменения" in ctx["advice"]

async def test_daily_uncovered_digest(tmp_path, monkeypatch):
    """На ноде появилось новое (СУБД, докер), покрытия под это нет — сводка раз в сутки.

    Панель узнаёт о таком сама, но раньше говорила только пунктом в «Требует
    действий»: пока туда не заглядывают, свежая база стоит без инвентаря.
    """
    from datetime import datetime, timedelta, timezone

    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    db = (tmp_path / "unc.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    rep = {
        "agent_version": "2.6",
        "db_engines": ["postgres"],
        "docker": {"present": True, "access": False},
        "setup_versions": {"timesync-setup": "0.2"},
    }
    async with factory() as s:
        s.add(Server(name="новая-нода", token_hash="a", enabled=True,
                     last_seen=now, offline_after_seconds=120, last_report=rep))
        # оффлайновую чинить не зовут: сначала её надо поднять, и об этом свой алерт
        s.add(Server(name="мёртвая-нода", token_hash="b", enabled=True,
                     last_seen=now - timedelta(hours=5), offline_after_seconds=120,
                     last_report=rep))
        await s.commit()

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    monkeypatch.setattr("app.collector.current_setup_versions",
                        lambda: {"dbstat-setup": "0.3", "timesync-setup": "0.2"})
    settings = Settings(alert_webhook="http://hook")

    await collector._daily_uncovered(factory, settings)
    assert len(sent) == 1, sent
    msg = sent[0]
    assert "новая-нода" in msg
    assert "инвентарь СУБД" in msg, "не сказано, чего не хватает"
    assert "Docker без доступа" in msg
    assert "kervax_helpers.yml" in msg, "нет готовой команды — сводка не действие, а новость"
    assert "мёртвая-нода" not in msg, "оффлайновую ноду звать чинить рано"
    assert "timesync-setup" not in msg, "установленный helper попал в сводку"

    # второй раз в те же сутки — молчим
    sent.clear()
    await collector._daily_uncovered(factory, settings)
    assert sent == []

    # дыру закрыли → сутки спустя сводки нет
    async with factory() as s:
        from sqlalchemy import select

        for row in await s.scalars(select(Server)):
            row.last_report = {**rep, "docker": {"present": True, "access": True},
                               "setup_versions": {"timesync-setup": "0.2",
                                                  "dbstat-setup": "0.3"}}
        await s.commit()
    from app import settings_store

    async with factory() as s:
        await settings_store.set_uncovered_sent(s, 0)
        await s.commit()
    await collector._daily_uncovered(factory, settings)
    assert sent == []
