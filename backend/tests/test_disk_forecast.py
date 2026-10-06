"""Прогноз заполнения дисков и inode, алерты "Диск: inode" и "Диск: скоро заполнится"."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import disk_forecast as dfc

GB = 1024 ** 3
NOW = 1_800_000_000.0


def series(fn, days=3.0, step=60):
    """Минутные точки (ts, pct) за последние days суток: fn(сутки назад) -> pct."""
    n = int(days * 86400 / step)
    return [(NOW - (n - i) * step, fn((n - i) * step / 86400)) for i in range(n)]


def test_steady_growth_gives_rate():
    pts = series(lambda ago: 70 + 3 * (3 - ago))
    assert abs(dfc.growth_rate(pts, NOW) - 3) < 0.05


def test_daily_sawtooth_is_not_growth():
    """Ночной дамп пишется и удаляется (минимум суток один и тот же) - роста нет."""
    pts = series(lambda ago: 60 + (8 if (ago % 1) < 0.1 else 0))
    assert dfc.growth_rate(pts, NOW) is None


def test_one_time_jump_is_not_growth():
    """Вчера скопировали архив: минимум подскочил один раз - это не тренд."""
    pts = series(lambda ago: 60 + (10 if ago < 1.5 else 0))
    assert dfc.growth_rate(pts, NOW) is None


def test_jump_next_to_noise_is_not_growth():
    """admin-a 05.10: сборка образов разом +7.1% к /app, накануне +0.1% шума. Оба дня
    "росли", но это скачок: прогноз обещал заполнение через неделю при ровном графике."""
    def fn(ago):
        if ago >= 2:
            return 62.6
        return 62.7 if ago >= 1.2 else 69.8
    assert dfc.growth_rate(series(fn), NOW) is None


def test_growth_must_be_comparable_and_above_noise():
    # ускорение в пределах STEP_RATIO - все еще тренд
    pts = series(lambda ago: 70 + (1 * (3 - ago) if ago >= 1 else 2 + 3 * (1 - ago)))
    assert dfc.growth_rate(pts, NOW) is not None
    # ровный рост ниже порога шума - не прогноз (до заполнения все равно месяцы)
    assert dfc.growth_rate(series(lambda ago: 40 + 0.2 * (3 - ago)), NOW) is None


def test_gap_in_history_gives_nothing():
    pts = [p for p in series(lambda ago: 50 + 2 * (3 - ago)) if not (1.1 < (NOW - p[0]) / 86400 < 1.9)]
    assert dfc.growth_rate(pts, NOW) is None


def _rows(fn, ifn=None):
    out = []
    for t, v in series(fn):
        d = {"mount": "/hdd", "pct": round(v, 1)}
        if ifn:
            d["ipct"] = round(ifn((NOW - t) / 86400), 1)
        out.append((datetime.fromtimestamp(t, tz=timezone.utc), [d]))
    return out


def test_build_uses_avail_and_inodes():
    rows = _rows(lambda ago: 72 + 3 * (3 - ago), lambda ago: 60 + 10 * (3 - ago))
    rep = {"disks": [{"mount": "/hdd", "total": 100 * GB, "used": 81 * GB, "avail": 13 * GB,
                      "inodes": 1000, "inodes_used": 900}]}
    items = dfc.build(rows, rep, NOW)
    by = {i["kind"]: i for i in items}
    # место: 13% доступно обычным процессам (резерв ext4 не в счет), +3% в сутки -> ~104 ч
    assert by["space"]["pct"] == 81.0 and abs(by["space"]["rate"] - 3) < 0.05
    assert 100 < by["space"]["eta_h"] < 108
    # inode: 10% свободно, +10% в сутки -> сутки
    assert 23 < by["inode"]["eta_h"] < 25
    assert items[0]["kind"] == "inode"  # самое скорое - первым
    # без avail (агент до 2.15) - до 100%
    rep["disks"][0].pop("avail")
    assert 145 < dfc.build(rows, rep, NOW)[1]["eta_h"] < 155


def test_build_skips_far_future_and_gone_mounts():
    rows = _rows(lambda ago: 10 + 0.1 * (3 - ago))
    assert dfc.build(rows, {"disks": [{"mount": "/hdd", "total": GB, "used": GB // 10}]}, NOW) == []
    assert dfc.build(_rows(lambda ago: 72 + 3 * (3 - ago)), {"disks": []}, NOW) == []


def test_texts():
    assert dfc.eta_text(14.4) == "через 14 ч"
    assert dfc.eta_text(80) == "через 3 дн"
    assert dfc.eta_text(0.3) == "меньше чем через час"
    items = [{"mount": "/hdd", "kind": "space", "pct": 91.0, "rate": 3.0, "eta_h": 70},
             {"mount": "/", "kind": "inode", "pct": 88.4, "rate": 0.45, "eta_h": 50.0},
             {"mount": "/data", "kind": "space", "pct": 50, "rate": 1, "eta_h": 500}]
    items.sort(key=lambda i: i["eta_h"])
    assert dfc.forecast_text(items, 72) == (
        "inode на / закончатся примерно через 2 дн: сейчас 88%, растет на ~0.5% в сутки; "
        "еще /hdd через 3 дн")


def test_fresh_items_and_mount_eta():
    now = datetime.fromtimestamp(NOW, tz=timezone.utc)
    fc = {"ts": int(NOW) - 600, "items": [{"mount": "/", "kind": "space", "pct": 90, "rate": 2, "eta_h": 40}]}
    assert dfc.mount_eta(fc, "/", "space", now) == 40
    assert dfc.mount_eta(fc, "/", "inode", now) is None
    assert dfc.fresh_items({"ts": int(NOW) - 3 * 3600, "items": []}, now) is None


async def _setup(tmp_path, name, **srv):
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / f'{name}.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name=name, token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, cpu_alert_percent=0, mem_alert_percent=0,
                     disk_warn_percent=85, disk_alert_percent=90, disk_crit_percent=95, **srv))
        await s.commit()
    return engine, factory, now


def _capture(monkeypatch) -> list[str]:
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    return sent


async def test_forecast_alert_levels_and_recovery(tmp_path, monkeypatch):
    from app import collector
    from app.config import Settings
    from app.models import Server

    rep = {"clock_unix": 0, "uptime_seconds": 100000,
           "disks": [{"mount": "/hdd", "used": 80, "total": 100}]}
    engine, factory, now = await _setup(tmp_path, "fc", last_report=rep)
    sent = _capture(monkeypatch)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(eta_h, minutes=2):
        nonlocal now
        now = now + timedelta(minutes=minutes)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            items = [] if eta_h is None else [
                {"mount": "/hdd", "kind": "space", "pct": 80.0, "rate": 3.0, "eta_h": eta_h}]
            srv.disk_forecast = {"ts": int(now.timestamp()), "items": items}
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)

    await tick(200)  # неделя - не повод
    assert sent == []
    await tick(60)
    assert len(sent) == 1 and sent[0].startswith("⚠️📈 ")
    assert "диск /hdd заполнится примерно через 2 дн: сейчас 80%, растет на ~3% в сутки" in sent[0]
    await tick(80)  # гистерезис: в пределах запаса алерт держится
    assert len(sent) == 1
    await tick(20)
    assert len(sent) == 2 and sent[1].startswith("🔴📈 ") and "через 20 ч" in sent[1]
    await tick(28)  # запас и у второго уровня
    assert len(sent) == 2
    await tick(None)  # рост прекратился
    assert len(sent) == 3 and sent[2].startswith("✅") and "больше не грозят заполниться" in sent[2]
    await engine.dispose()


async def test_forecast_alert_comes_five_days_ahead(tmp_path, monkeypatch):
    """Предупреждение за пять суток - с того же срока, что пункт в "Что сломано"."""
    from app import collector
    from app.config import Settings
    from app.models import Server

    rep = {"clock_unix": 0, "uptime_seconds": 100000, "disks": [{"mount": "/hdd", "used": 80, "total": 100}]}
    engine, factory, now = await _setup(tmp_path, "fc5", last_report=rep)
    sent = _capture(monkeypatch)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(eta_h):
        nonlocal now
        now = now + timedelta(minutes=2)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.disk_forecast = {"ts": int(now.timestamp()), "items": [
                {"mount": "/hdd", "kind": "space", "pct": 80.0, "rate": 3.0, "eta_h": eta_h}]}
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)

    await tick(130)  # больше пяти суток - рано
    assert sent == []
    await tick(115)
    assert len(sent) == 1 and sent[0].startswith("⚠️📈 ") and "через 5 дн" in sent[0]
    await tick(150)  # запас гистерезиса
    assert len(sent) == 1
    await engine.dispose()


async def test_analyze_request_for_forecast_mount(tmp_path):
    """Раздел ниже 75% с прогнозом: helper сам его не разбирает - панель просит разбор. Не чаще
    раза в 12 ч, только у нод с агентом 2.24 и diskusage-setup 0.6, не тот, что уже разобран."""
    from app import collector
    from app.models import BackupCommand, Server

    rep = {"agent_version": "2.24", "setup_versions": {"diskusage-setup": "0.6"},
           "disks": [{"mount": "/app", "used": 70, "total": 100}, {"mount": "/hdd", "used": 90, "total": 100}],
           "extras": {"disk-usage": {"v": 1, "ts": 1, "items": [], "fs": [{"mount": "/hdd", "pct": 90}]}}}
    engine, factory, now = await _setup(tmp_path, "an", last_report=rep)
    fc = {"ts": int(now.timestamp()), "items": [
        {"mount": "/app", "kind": "space", "pct": 70.0, "rate": 6.0, "eta_h": 100.0},
        {"mount": "/hdd", "kind": "space", "pct": 90.0, "rate": 3.0, "eta_h": 60.0}]}
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.disk_forecast = fc
        await s.commit()
    assert await collector.request_disk_analysis(factory, now) == 1
    async with factory() as s:
        cmds = list(await s.scalars(select(BackupCommand)))
        assert [(c.action, c.mode, c.payload["name"], c.payload["mount"], c.origin) for c in cmds] == [
            ("disk_fix", "run", "analyze", "/app", "auto")]
        cmds[0].status = "done"
        await s.commit()
    # повтор - только через 12 часов
    assert await collector.request_disk_analysis(factory, now + timedelta(hours=1)) == 0
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.disk_forecast = {**fc, "ts": int((now + timedelta(hours=13)).timestamp())}
        srv.last_seen = now + timedelta(hours=13)
        await s.commit()
    assert await collector.request_disk_analysis(factory, now + timedelta(hours=13)) == 1
    # старый агент - не просим
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.last_report = {**rep, "agent_version": "2.23"}
        await s.commit()
    assert not collector.disk_analyze_ok({**rep, "agent_version": "2.23"})
    assert not collector.disk_analyze_ok({**rep, "setup_versions": {"diskusage-setup": "0.5"}})
    await engine.dispose()


async def test_analyze_api(client, auth_headers):
    from tests.test_users import _mk_viewer

    r = await client.post("/api/servers", json={"name": "an"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    rep = {"hostname": "an", "os": "U", "agent_version": "2.24", "cpu_percent": 5, "mem_used": 1, "mem_total": 2,
           "setup_versions": {"diskusage-setup": "0.6"},
           "disks": [{"mount": "/app", "used": 70, "total": 100}]}
    r = await client.post("/api/agent/report", json=rep, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    srv = [x for x in (await client.get("/api/servers", headers=auth_headers)).json() if x["id"] == sid][0]
    assert srv["disk_analyze"] is True
    url = f"/api/servers/{sid}/backup/command"
    body = {"action": "disk_fix", "mode": "run", "fix": "analyze", "mount": "/app"}
    r = await client.post(url, json=body, headers=auth_headers)
    assert r.status_code == 200, r.text
    # раздела нет у ноды, preview, путь с обходом - отказ
    for bad in ({**body, "mount": "/etc"}, {**body, "mode": "preview"}, {**body, "mount": "/app/../etc"}):
        assert (await client.post(url, json=bad, headers=auth_headers)).status_code in (400, 422)
    viewer = await _mk_viewer(client, auth_headers)
    assert (await client.post(url, json=body, headers=viewer)).status_code == 403
    # агент получает раздел в команде
    r = await client.get("/api/agent/commands", headers={"Authorization": f"Bearer {token}"})
    cmd = [c for c in r.json()["backup_commands"] if c["action"] == "disk_fix"][0]
    assert cmd["name"] == "analyze" and cmd["mount"] == "/app" and cmd["mode"] == "run"


async def test_inode_alert_with_cause_and_disk_eta(tmp_path, monkeypatch):
    from app import collector
    from app.config import Settings

    now0 = datetime.now(timezone.utc)
    rep = {"clock_unix": int(now0.timestamp()), "uptime_seconds": 100000,
           "disks": [{"mount": "/", "used": 91, "total": 100, "avail": 4,
                      "inodes": 6_000_000, "inodes_used": 5_580_000}],
           "extras": {"disk-usage": {"v": 1, "ts": int(now0.timestamp()) - 300, "items": [], "fs": [
               {"mount": "/", "size": 100, "used": 91, "avail": 4, "pct": 96, "inodes": 5_580_000,
                "top_state": "ok", "top_ts": 1, "top": [], "ipct": 93, "iused": 5_580_000,
                "itotal": 6_000_000, "itop_state": "ok", "itop_ts": 1, "itop": [
                    {"path": "/var", "files": 4_300_000}, {"path": "/var/lib", "files": 4_200_000},
                    {"path": "/var/lib/php/sessions", "files": 4_100_000},
                    {"path": "/home/www", "files": 700_000}]}]}}}
    fc = {"ts": int(now0.timestamp()), "items": [
        {"mount": "/", "kind": "inode", "pct": 93.0, "rate": 2.0, "eta_h": 84.0},
        {"mount": "/", "kind": "space", "pct": 91.0, "rate": 1.0, "eta_h": 96.0}]}
    engine, factory, now = await _setup(tmp_path, "ino", last_report=rep, disk_forecast=fc)
    sent = _capture(monkeypatch)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    ino = next(m for m in sent if "inode на /" in m)
    assert ino.startswith("🔴 ") and "занято 93% ≥ 90%" in ino
    assert "кончатся примерно через 4 дн" in ino
    assert "больше всего файлов: /var/lib/php/sessions 4.1 млн, /home/www 700 тыс." in ino
    disk = next(m for m in sent if "диск 91%" in m)
    assert "заполнится примерно через 4 дн" in disk
    await engine.dispose()


async def test_forecast_stage_reads_history(tmp_path):
    from app.models import Server, ServerMetric

    rep = {"disks": [{"mount": "/hdd", "used": 81 * GB, "total": 100 * GB, "avail": 13 * GB}]}
    engine, factory, now = await _setup(tmp_path, "stage", last_report=rep)
    async with factory() as s:
        sid = (await s.execute(select(Server.id))).scalar_one()
        for i in range(0, 3 * 1440, 2):  # раз в 2 минуты за трое суток
            t = now - timedelta(minutes=3 * 1440 - i)
            ago = (now - t).total_seconds() / 86400
            s.add(ServerMetric(server_id=sid, ts=t, disks=[{"mount": "/hdd", "pct": round(72 + 3 * (3 - ago), 1)}]))
        await s.commit()
    orig = dfc.MIN_POINTS
    dfc.MIN_POINTS = 300  # точек вдвое реже, чем у живого агента
    try:
        assert await dfc.update_disk_forecasts(factory, now) == 1
        assert await dfc.update_disk_forecasts(factory, now + timedelta(minutes=5)) == 0  # свежий
    finally:
        dfc.MIN_POINTS = orig
    async with factory() as s:
        fc = (await s.execute(select(Server.disk_forecast))).scalar_one()
    assert fc["items"][0]["mount"] == "/hdd" and 95 < fc["items"][0]["eta_h"] < 115
    await engine.dispose()


async def test_ingest_keeps_inode_history(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "ino"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    report = {"hostname": "h", "os": "U", "agent_version": "2.15", "cpu_percent": 5,
              "mem_used": 1, "mem_total": 2,
              "disks": [{"mount": "/", "used": 50, "total": 100, "avail": 45,
                         "inodes": 1000, "inodes_used": 123},
                        {"mount": "/data", "used": 1, "total": 100, "avail": 99}]}
    r = await client.post("/api/agent/report", json=report, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    m = (await client.get(f"/api/servers/{sid}/metrics?hours=1", headers=auth_headers)).json()
    disks = m[-1]["disks"] if isinstance(m, list) else m["points"][-1]["disks"]
    assert disks == [{"mount": "/", "pct": 50.0, "ipct": 12.3}, {"mount": "/data", "pct": 1.0}]
