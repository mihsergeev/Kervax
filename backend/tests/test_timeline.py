"""Хронология сервера (app/timeline.py): события из отчетов, лента в карточке и строка "перед
этим" в алертах."""
from datetime import datetime, timedelta, timezone

from app import collector, timeline
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent, AuditLog, Check, MaintenanceWindow, Server, ServerEvent


def test_uptime_parse():
    assert timeline.uptime("Up 2 hours") == 7200
    assert timeline.uptime("Up About a minute") == 60
    assert timeline.uptime("Up About an hour (healthy)") == 3600
    assert timeline.uptime("Up Less than a second") == 0
    assert timeline.uptime("Up 3 weeks") == 3 * 604800
    assert timeline.uptime("Exited (0) 3 hours ago") is None
    assert timeline.uptime(None) is None


def _snap(ts, c=None, p=None, **kw):
    base = {"ts": ts, "c": c, "p": p, "boot": "", "k": "", "kn": "", "os": "", "dk": "", "ag": "",
            "upd": None, "upd_ts": 0, "rb_ts": 0, "rb_pkgs": [], "ua": 0}
    base.update(kw)
    return base


def _c(image="nginx:1.18", up=7200, r=0, s="running"):
    return {"i": image, "u": up, "r": r, "s": s}


def test_diff_containers_and_debounce():
    pend: dict = {}
    first = _snap("t0", {"web": _c(), "db": _c("postgres:16")}, {"80/tcp": "nginx"})
    ev, snap = timeline.diff(None, first, pend)
    assert ev == [] and snap == first  # первый снимок - точка отсчета

    # пересоздание: контейнер пропал на один отчет и вернулся с маленьким аптаймом - "пересоздан"
    ev, snap = timeline.diff(snap, _snap("t1", {"db": _c("postgres:16", 7260)}, {"80/tcp": "nginx"}), pend)
    assert ev == [] and "web" in snap["c"]
    ev, snap = timeline.diff(snap, _snap("t2", {"web": _c(up=30), "db": _c("postgres:16", 7320)},
                                         {"80/tcp": "nginx"}), pend)
    assert [(e["kind"], e["obj"]) for e in ev] == [("restart", "web")]
    assert pend["c-"] == {}

    # пропал два отчета подряд - удален, со временем, когда пропал впервые
    ev, snap = timeline.diff(snap, _snap("t3", {"db": _c("postgres:16", 7380)}, {"80/tcp": "nginx"}), pend)
    assert ev == []
    ev, snap = timeline.diff(snap, _snap("t4", {"db": _c("postgres:16", 7440)}, {"80/tcp": "nginx"}), pend)
    assert [(e["kind"], e["ts"]) for e in ev] == [("gone", "t3")] and "web" not in snap["c"]

    # новый образ, падение по restart-политике, остановка
    ev, snap = timeline.diff(snap, _snap("t5", {"db": _c("postgres:17", 10), "job": _c("app:1")}, {}), pend)
    assert [e["kind"] for e in ev] == ["image"] and "postgres:16 -> postgres:17" in ev[0]["text"]
    ev, snap = timeline.diff(snap, _snap("t6", {"db": _c("postgres:17", 5, r=2), "job": _c("app:1")}, {}), pend)
    kinds = sorted(e["kind"] for e in ev)
    assert kinds == ["close", "crash", "new"]  # порт 80 пропал второй раз подряд, job подтвердился
    crash = next(e for e in ev if e["kind"] == "crash")
    assert crash["n"] == 2
    ev, snap = timeline.diff(snap, _snap("t7", {"db": _c("postgres:17", None, r=2, s="exited"),
                                                "job": _c("app:1")}, {}), pend)
    assert [(e["kind"], e["cat"]) for e in ev] == [("stop", "state")]

    # агент не прислал контейнеры - не "все удалены"
    ev, snap = timeline.diff(snap, _snap("t8", None, {}), pend)
    assert ev == [] and set(snap["c"]) == {"db", "job"}


def test_diff_system():
    pend: dict = {}
    prev = _snap("t0", {}, {}, boot="2026-10-01T00:00:00+00:00", k="6.8.0-40", os="Ubuntu 22.04.4 LTS",
                 dk="27.3.1", ag="2.26", upd=40, upd_ts=1000, ua=500)
    cur = _snap("t1", {}, {}, boot="2026-10-14T10:00:00+00:00", k="6.8.0-45", os="Ubuntu 22.04.5 LTS",
                dk="27.3.1", ag="2.27", upd=3, upd_ts=4600, ua=2000, kn="6.8.0-47", rb_ts=2100,
                rb_pkgs=["libc6"])
    ev, _ = timeline.diff(prev, cur, pend)
    by = {e["kind"]: e for e in ev}
    assert set(by) == {"reboot", "kernel", "os", "agent", "kernel_new", "packages", "reboot_req"}
    assert by["reboot"]["ts"] == "2026-10-14T10:00:00+00:00"
    # пакеты ставили автообновления: время - их прогон, а не следующий подсчет
    assert by["packages"]["ts"] == datetime.fromtimestamp(2000, timezone.utc).isoformat()
    assert "40" in by["packages"]["text"] and "3" in by["packages"]["text"]
    assert "libc6" in by["reboot_req"]["text"]


def test_before_text():
    end = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    start = end - timedelta(minutes=60)

    def at(m):
        return (end - timedelta(minutes=m)).isoformat()

    ev = [
        {"ts": at(10), "cat": "deploy", "kind": "restart", "obj": f"app-{i}", "text": ""} for i in range(5)
    ] + [
        {"ts": at(20), "cat": "system", "kind": "packages", "obj": "", "text": "обновлены пакеты: ждали обновления 9, осталось 0"},
        {"ts": at(5), "cat": "crash", "kind": "crash", "obj": "app-1", "text": "app-1: упал"},
        {"ts": at(90), "cat": "system", "kind": "reboot", "obj": "", "text": "перезагрузка"},
        {"ts": at(3), "cat": "state", "kind": "stop", "obj": "db", "text": "db: остановлен"},
    ]
    t = timeline.before_text(ev, [{"ts": at(30), "kind": "cron", "text": "изменен крон"}], start, end)
    assert t.startswith("↳ за час до этого: ")
    assert "перезапущен или пересоздан: app-0, app-1, app-2 и еще 2" in t
    assert "обновлены пакеты" in t and "изменились кроны" in t and "остановлен: db" in t
    assert "перезагрузка" not in t and "упал" not in t  # вне окна и симптом
    # события самого упавшего контейнера в его алерте не нужны
    assert "остановлен" not in timeline.before_text(ev, [], start, end, frozenset({"db"}))
    assert timeline.before_text([], [], start, end) == ""


def _report(up="Up 2 hours", image="nginx:1.18", restarts=0, ports=(80,)):
    return {"docker": {"version": "27.3.1", "containers": [
        {"name": "shop-web-1", "image": image, "status": up, "restarts": restarts, "state": "running"},
        {"name": "happy_turing", "image": "alpine", "status": "Up 5 seconds", "state": "running"},
    ]}, "listen": [{"p": "tcp", "ip": "0.0.0.0", "port": p} for p in ports] + [
        {"p": "tcp", "ip": "0.0.0.0", "port": 40000}],
        "extras": {"changes": {"listen": {"80": "docker-proxy", "8080": "java"}}},
        "os_upd": {"kernel": "6.8.0-45", "updates": 5}}


async def _panel(tmp_path):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, factory


async def _tick(factory, sid, now, rep):
    async with factory() as s:
        srv = await s.get(Server, sid)
        srv.last_report = rep
        srv.last_seen = now
        await s.commit()
    timeline._last_track = None
    return await timeline.track(factory, now)


async def test_track_writes_events(tmp_path):
    engine, factory = await _panel(tmp_path)
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    async with factory() as s:
        srv = Server(name="web-a", token_hash="t", enabled=True, last_seen=now, last_report=_report(),
                     agent_version="2.27")
        s.add(srv)
        await s.commit()
        sid = srv.id
    timeline._last_track = None
    assert await timeline.track(factory, now) == 0  # точка отсчета
    assert await _tick(factory, sid, now + timedelta(minutes=1), _report(up="Up 2 minutes")) == 1
    # петля падений сливается в одно событие со счетом
    await _tick(factory, sid, now + timedelta(minutes=2), _report(up="Up 3 minutes", restarts=1))
    await _tick(factory, sid, now + timedelta(minutes=3), _report(up="Up 4 minutes", restarts=3))
    # порт 8080 открылся - подтверждается вторым отчетом; эфемерный 40000 и случайное имя не пишутся
    await _tick(factory, sid, now + timedelta(minutes=4), _report(up="Up 5 minutes", restarts=3, ports=(80, 8080)))
    await _tick(factory, sid, now + timedelta(minutes=5), _report(up="Up 6 minutes", restarts=3, ports=(80, 8080)))
    # нода молчит: отчет старый, сравнивать нечего
    async with factory() as s:
        (await s.get(Server, sid)).last_report = _report(image="nginx:1.27")
        await s.commit()
    timeline._last_track = None
    assert await timeline.track(factory, now + timedelta(minutes=30)) == 0
    async with factory() as s:
        rows = list(await s.scalars(select_events(sid)))
    got = [(e.kind, e.obj, e.n) for e in rows]
    assert got == [("restart", "shop-web-1", 1), ("crash", "shop-web-1", 3), ("open", "8080/tcp", 1)], got
    assert rows[1].text == "shop-web-1: падал и перезапускался, раз: 3"
    assert "java" in rows[2].text
    await engine.dispose()


def select_events(sid):
    from sqlalchemy import select
    return select(ServerEvent).where(ServerEvent.server_id == sid).order_by(ServerEvent.id)


async def test_timeline_api_merges_sources(client, auth_headers):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        srv = Server(name="web-a", token_hash="t-a", group_name="shop", last_seen=now,
                     note="диск под замену", note_by="admin", note_at=now - timedelta(hours=1),
                     changes={"log": [{"ts": (now - timedelta(hours=2)).isoformat(), "kind": "key", "act": "add",
                                       "text": "добавлен SSH-ключ пользователю deploy", "alert": True},
                                      {"ts": (now - timedelta(days=30)).isoformat(), "kind": "cron",
                                       "act": "mod", "text": "старое", "alert": False}]})
        s.add(srv)
        await s.commit()
        s.add_all([
            ServerEvent(server_id=srv.id, ts=now - timedelta(hours=3), cat="deploy", kind="image",
                        obj="shop-web-1", text="shop-web-1: образ nginx:1.18 -> nginx:1.27"),
            AlertEvent(ts=now - timedelta(minutes=50), kind="disk", target="web-a", section="servers",
                       text="💽 web-a — диск / занят на 91%\n🔗 ссылка"),
            AlertEvent(ts=now - timedelta(minutes=40), kind="down", target="web-a", section="sites",
                       text="монитор с тем же именем - не сервер"),
            AuditLog(ts=now - timedelta(minutes=30), username="admin", action="server_snooze", target="web-a",
                     detail="disk 2ч"),
            MaintenanceWindow(title="ядро", server_ids=[srv.id], starts_at=now - timedelta(hours=5),
                              ends_at=now - timedelta(hours=4), repeat=""),
        ])
        await s.commit()
        sid = srv.id
    r = await client.get(f"/api/servers/{sid}/timeline?days=7", headers=auth_headers)
    assert r.status_code == 200, r.text
    j = r.json()
    got = [(e["cat"], e["kind"]) for e in j["events"]]
    assert got == [("panel", "server_snooze"), ("alert", "disk"), ("panel", "note"), ("access", "key"),
                   ("deploy", "image"), ("panel", "maint")], got
    alert = next(e for e in j["events"] if e["cat"] == "alert")
    assert alert["text"] == "диск / занят на 91%"
    assert j["cats"]["panel"] == 3

    # viewer действий в панели не видит: журнал - только админу
    await client.post("/api/users", headers=auth_headers, json={
        "username": "tlviewer", "password": "tlviewer-pass-2026", "role": "viewer"})
    login = await client.post("/api/auth/login", json={"username": "tlviewer", "password": "tlviewer-pass-2026"})
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    j = (await client.get(f"/api/servers/{sid}/timeline", headers=h)).json()
    assert ("panel", "server_snooze") not in [(e["cat"], e["kind"]) for e in j["events"]]

    # удаление сервера уносит и его хронологию
    assert (await client.delete(f"/api/servers/{sid}", headers=auth_headers)).status_code == 204
    async with f() as s:
        assert list(await s.scalars(select_events(sid))) == []


def _capture(monkeypatch):
    sent: list = []

    async def fake_dispatch(cfg, can_send, texts, threshold, **kw):
        sent.extend(texts)
        return True

    monkeypatch.setattr("app.alerts.dispatch", fake_dispatch)
    return sent


async def test_offline_alert_says_what_changed_before(tmp_path, monkeypatch):
    engine, factory = await _panel(tmp_path)
    now = datetime.now(timezone.utc)
    gone = now - timedelta(minutes=10)
    async with factory() as s:
        srv = Server(name="web-a", token_hash="t-a", enabled=True, backup_not_required=True, last_seen=gone,
                     last_report={}, alert_state={}, cpu_alert_percent=0, mem_alert_percent=0,
                     disk_alert_percent=0, offline_after_seconds=120, alert_sustain_seconds=0)
        s.add(srv)
        await s.commit()
        s.add_all([
            ServerEvent(server_id=srv.id, ts=gone - timedelta(minutes=15), cat="system", kind="packages",
                        text="обновлены пакеты: ждали обновления 12, осталось 0"),
            ServerEvent(server_id=srv.id, ts=gone - timedelta(hours=3), cat="system", kind="reboot",
                        text="перезагрузка"),
        ])
        await s.commit()
    sent = _capture(monkeypatch)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook"), now)
    assert [m.kind for m in sent] == ["offline"]
    assert "↳ за час до этого: обновлены пакеты: ждали обновления 12, осталось 0" in sent[0]
    assert "перезагрузка" not in sent[0]
    await engine.dispose()


async def test_site_down_alert_says_what_changed_on_its_server(client, monkeypatch):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        srv = Server(name="web-a", token_hash="t-a", enabled=True, last_seen=now,
                     last_report={"web_services": [{"sites": ["a.example"]}]})
        a = Check(name="site-a", type="http", target="a.example")
        b = Check(name="site-b", type="http", target="b.example")
        s.add_all([srv, a, b])
        await s.commit()
        s.add(ServerEvent(server_id=srv.id, ts=now - timedelta(minutes=7), cat="deploy", kind="image",
                          obj="shop-web-1", text="shop-web-1: образ nginx:1.18 -> nginx:1.27"))
        await s.commit()
        cid, oid = a.id, b.id
    sent = _capture(monkeypatch)
    await collector._send_alerts(f, Settings(alert_webhook="http://hook"), [
        ("bad", "site-a", "down", "HTTP 502", None, cid, None),
        ("bad", "site-b", "down", "HTTP 502", None, oid, None),
    ], now)
    by = {m.target: str(m) for m in sent}
    assert "↳ за час до этого: новый образ: shop-web-1" in by["site-a"]
    assert "↳" not in by["site-b"]  # у сайта без сервера в панели строки нет
