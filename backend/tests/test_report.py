"""Отчет за период (app/api/report.py): простой сайтов и серверов, шум, бэкапы, окна обслуживания."""
from datetime import datetime, timedelta, timezone

from app.models import AlertEvent, Check, CheckIncident, CheckSample, MaintenanceWindow, Server, ServerMetric


async def _fill(client) -> dict:
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        srv = [Server(name=n, token_hash="t-" + n, enabled=True, last_seen=now, offline_after_seconds=120)
               for n in ("web-a", "web-b", "web-c", "web-d")]
        s.add_all(srv)
        await s.commit()
        for x in srv:
            for m in range(180, 0, -1):
                if 40 < m <= 50:
                    continue  # десять минут молчали все разом: лежала панель
                if x.name == "web-a" and 70 < m <= 90:
                    continue  # двадцать минут молчал только web-a
                s.add(ServerMetric(server_id=x.id, ts=now - timedelta(minutes=m), cpu_percent=5))
        a = Check(name="site-a", type="http", target="a.example", group_name="")
        b = Check(name="site-b", type="http", target="b.example", group_name="")
        s.add_all([a, b])
        await s.commit()
        for i in range(100):
            s.add(CheckSample(check_id=a.id, ts=now - timedelta(minutes=100 - i), status="down" if i < 10 else "up"))
            s.add(CheckSample(check_id=b.id, ts=now - timedelta(minutes=100 - i), status="up"))
        s.add(CheckIncident(check_id=a.id, status="down", started_at=now - timedelta(minutes=30),
                            ended_at=now - timedelta(minutes=20)))
        for _ in range(3):
            s.add(AlertEvent(ts=now - timedelta(hours=2), kind="disk", target="web-a", section="servers"))
        s.add(AlertEvent(ts=now - timedelta(hours=1), kind="disk", target="web-a", section="servers", recovery=True))
        for _ in range(2):
            s.add(AlertEvent(ts=now - timedelta(hours=3), kind="backup_failed", target="web-b", section="backups"))
        s.add(AlertEvent(ts=now - timedelta(days=8), kind="disk", target="web-a", section="servers"))
        s.add(MaintenanceWindow(title="ядро", server_ids=[srv[0].id], starts_at=now - timedelta(hours=2),
                                ends_at=now - timedelta(hours=1), repeat=""))
        await s.commit()
        return {"a": a.id, "web_a": srv[0].id}


async def test_report_week(client, auth_headers):
    ids = await _fill(client)
    r = await client.get("/api/report?days=7", headers=auth_headers)
    assert r.status_code == 200, r.text
    j = r.json()
    sites = j["sites"]
    assert [x["name"] for x in sites["items"]] == ["site-a"]
    a = sites["items"][0]
    assert a["id"] == ids["a"] and a["incidents"] == 1 and abs(a["downtime_s"] - 600) < 5 and a["uptime"] == 90.0
    assert sites["total"] == 2 and sites["incidents"] == 1 and sites["uptime"] == 95.0
    srv = j["servers"]
    # общий пропуск у всех четырех - это панель, у web-a свои двадцать минут
    assert srv["panel_gaps"] == 1 and abs(srv["panel_s"] - 660) < 70
    web_a = next(x for x in srv["items"] if x["name"] == "web-a")
    assert web_a["gaps"] == 1 and abs(web_a["offline_s"] - 1260) < 70
    assert web_a["alerts"] == 3 and web_a["things"][0]["n"] == 3
    assert srv["with_gaps"] == 1
    noise = j["noise"]
    assert noise["fires"] == 5 and noise["prev_fires"] == 1
    # без текста алерта "что" - название вида; ссылка - на карточку сервера
    top = noise["items"][0]
    assert (top["target"], top["kind"], top["n"], top["srv_id"]) == ("web-a", "disk", 3, ids["web_a"])
    bk = next(x for x in noise["items"] if x["kind"] == "backup_failed")
    assert bk["target"] == "web-b" and bk["n"] == 2 and bk["days"] == 1
    assert [m["title"] for m in j["maint"]] == ["ядро"]
    # по дням: все случилось за последние часы - сегодня (или вчера, если сутки только начались)
    days = j["day_labels"]
    assert len(days) == 8 and len(noise["per_day"]) == 8 == len(sites["per_day"]) == len(srv["per_day"])
    assert sum(noise["per_day"]) == 5 and sum(noise["per_day"][-2:]) == 5
    assert abs(sum(sites["per_day"]) - 600) < 5 and abs(sum(srv["per_day"]) - 1260) < 70


async def test_report_respects_groups(client, auth_headers):
    await _fill(client)
    r = await client.post("/api/users", headers=auth_headers, json={
        "username": "webviewer", "password": "webviewer-pass-2026", "role": "viewer",
        "server_groups": ["nope"], "site_groups": ["nope"]})
    assert r.status_code in (200, 201), r.text
    login = await client.post("/api/auth/login", json={"username": "webviewer", "password": "webviewer-pass-2026"})
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    j = (await client.get("/api/report?days=1", headers=h)).json()
    assert j["sites"]["total"] == 0 and j["servers"]["total"] == 0
    assert j["noise"]["fires"] == 0 and j["maint"] == []


async def test_report_noise_says_what_exactly(client, auth_headers):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    unit = "⚠️⚙️ web-a — упал app-sync.service: код 1, {m} мин назад\n↳ ModuleNotFoundError: No module named 'requests'"
    pods = ("🔄☸️ web-a — под shop/worker-7fb484cbcb-wvkcj перезапускался 5 раз за сутки; "
            "под shop/worker-7fb484cbcb-xmszd перезапускался 6 раз за сутки")
    async with f() as s:
        s.add(Server(name="web-a", token_hash="t-web-a", enabled=True, last_seen=now))
        for h, m in ((30, 3), (5, 40)):
            s.add(AlertEvent(ts=now - timedelta(hours=h), kind="units", target="web-a", section="servers",
                             text=unit.format(m=m)))
        s.add(AlertEvent(ts=now - timedelta(hours=1), kind="kube_restarts", target="web-a", section="kuber", text=pods))
        await s.commit()
    j = (await client.get("/api/report?days=7", headers=auth_headers)).json()
    rows = {x["what"]: x for x in j["noise"]["items"]}
    u = rows["упал app-sync.service"]
    assert u["n"] == 2 and u["days"] == 2 and u["note"] == "код 1: ModuleNotFoundError: No module named 'requests'"
    # два пода одного deployment - одна беда
    assert rows["перезапускается под shop/worker"]["n"] == 1
    web_a = next(x for x in j["servers"]["items"] if x["name"] == "web-a")
    assert [t["what"] for t in web_a["things"]] == ["упал app-sync.service", "перезапускается под shop/worker"]
