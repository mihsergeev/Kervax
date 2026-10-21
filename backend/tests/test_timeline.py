"""Хронология сервера (app/timeline.py): события из отчетов, лента в карточке и строка "перед
этим" в алертах."""
from datetime import datetime, timedelta, timezone

from app import alerts, collector, timeline
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
    # остановка - со второго отчета подряд, со временем первого
    ev, snap = timeline.diff(snap, _snap("t7", {"db": _c("postgres:17", None, r=2, s="exited"),
                                                "job": _c("app:1")}, {}), pend)
    assert ev == []
    ev, snap = timeline.diff(snap, _snap("t7b", {"db": _c("postgres:17", None, r=2, s="exited"),
                                                 "job": _c("app:1")}, {}), pend)
    assert [(e["kind"], e["cat"], e["ts"]) for e in ev] == [("stop", "state", "t7")]

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
    # в чат - коротко, что менялось за час до сбоя - в полном тексте (история, панель)
    assert sent[0].startswith("🔴📡 web-a\n<b>Не на связи</b>") and "за час до этого" not in sent[0]
    full = alerts.full_text(sent[0])
    assert "↳ за час до этого: обновлены пакеты: ждали обновления 12, осталось 0" in full
    assert "перезагрузка" not in full
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
    by = {m.target: alerts.full_text(m) for m in sent}
    assert "↳ за час до этого: новый образ: shop-web-1" in by["site-a"]
    assert "↳" not in by["site-b"]  # у сайта без сервера в панели строки нет
    # в чат подробность не идет: там что случилось и причина
    chat = {m.target: str(m) for m in sent}
    assert "<b>Не отвечает</b>" in chat["site-a"] and "за час до этого" not in chat["site-a"]


def test_udp_sessions_and_flapping_ports_stay_out():
    """На двух VPN-нодах xray открывал UDP-сокет на случайном порту на каждую сессию клиента:
    52 тысячи строк "открыт/закрыт порт" за 10 дней. А 123/udp открывался и закрывался 159 раз."""
    t0 = datetime(2026, 10, 16, tzinfo=timezone.utc)

    def at(m):
        return (t0 + timedelta(minutes=m)).isoformat()

    pend: dict = {}
    base = {"22/tcp": "sshd", "443/tcp": "xray"}
    # снимок, сохраненный до правила, с сессией внутри: она уходит без строки "закрыт порт"
    _, snap = timeline.diff(None, _snap(at(0), {}, dict(base, **{"17123/udp6": ""})), pend)
    got = []
    for i in range(1, 13):
        ports = dict(base, **{f"{20000 + i}/udp6": ""})
        if (i - 1) // 2 % 2 == 0:
            ports["123/udp"] = ""  # NTP без имени процесса, но порт ниже 1024 - это сервис
        ev, snap = timeline.diff(snap, _snap(at(i), {}, ports), pend)
        got += [(e["kind"], e["obj"]) for e in ev]
    assert all(not o.endswith("udp6") for _, o in got), got
    assert [k for k, o in got if o == "123/udp"] == ["open", "close", "open", "flap"], got
    # через сутки тишины порт снова пишется
    for m in (60 * 25, 60 * 25 + 1):
        ev, snap = timeline.diff(snap, _snap(at(m), {}, dict(base, **{"123/udp": ""})), pend)
    assert [(e["kind"], e["obj"]) for e in ev] == [("open", "123/udp")], ev
    # UDP с известным процессом - событие
    for m in (60 * 26, 60 * 26 + 1):
        ev, snap = timeline.diff(snap, _snap(at(m), {}, dict(base, **{"123/udp": "", "5353/udp": "avahi-daemon"})), pend)
    assert [(e["kind"], e["obj"]) for e in ev] == [("open", "5353/udp")], ev


def _at(m):
    return (datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=m)).isoformat()


def test_recreate_is_one_restart():
    """Выкладка через compose: контейнер на отчет стоит created или exited. Раньше это давало
    "остановлен", "запущен" и "перезапущен" на одну выкладку."""
    pend: dict = {}
    _, snap = timeline.diff(None, _snap(_at(0), {"app": _c(up=7200), "cron": _c(up=7200)}, {}), pend)
    got = []
    for m, app, cron in ((1, _c(up=None, s="created"), _c(up=None, s="exited")),
                         (2, _c(up=5), _c(up=20))):
        ev, snap = timeline.diff(snap, _snap(_at(m), {"app": app, "cron": cron}, {}), pend)
        got += [(e["kind"], e["obj"]) for e in ev]
    assert sorted(got) == [("restart", "app"), ("restart", "cron")], got
    assert pend["cstop"] == {}

    # новый образ в отчете, где контейнер еще created: только "образ", без "перезапущен"
    ev, snap = timeline.diff(snap, _snap(_at(3), {"app": _c("app:2", None, s="created"), "cron": _c(up=80)}, {}), pend)
    assert [e["kind"] for e in ev] == ["image"]
    ev, snap = timeline.diff(snap, _snap(_at(4), {"app": _c("app:2", 10), "cron": _c(up=140)}, {}), pend)
    assert ev == []

    # created дольше 15 минут - остановлен, со временем, когда заметили
    for m in (5, 10, 19):
        ev, snap = timeline.diff(snap, _snap(_at(m), {"app": _c("app:2", None, s="created"),
                                                      "cron": _c(up=600)}, {}), pend)
        assert ev == []
    ev, snap = timeline.diff(snap, _snap(_at(21), {"app": _c("app:2", None, s="created"), "cron": _c(up=900)}, {}), pend)
    assert [(e["kind"], e["ts"]) for e in ev] == [("stop", _at(5))]
    ev, snap = timeline.diff(snap, _snap(_at(22), {"app": _c("app:2", 5), "cron": _c(up=960)}, {}), pend)
    assert [e["kind"] for e in ev] == ["start"]


def test_kernel_and_reboot_once_per_boot():
    """На одной ноде за неделю 15 одинаковых "нужна перезагрузка" и "установлено ядро": каждый
    рестарт агента давал отчет без обновлений ОС, и следующий писал оба заново."""
    pend: dict = {}
    boot1 = "2026-10-01T00:00:00+00:00"
    upd = dict(boot=boot1, k="6.8.0-140", upd=0, upd_ts=1000)
    _, snap = timeline.diff(None, _snap(_at(0), {}, {}, **upd), pend)
    pending = dict(upd, kn="6.8.0-147", rb_ts=2000, rb_pkgs=["linux-image-6.8.0-147-generic"], upd_ts=2100)
    ev, snap = timeline.diff(snap, _snap(_at(1), {}, {}, **pending), pend)
    assert sorted(e["kind"] for e in ev) == ["kernel_new", "reboot_req"]

    got = []
    # рестарт агента: обновлений в отчете нет, потом снова есть
    for m in range(2, 6):
        ev, snap = timeline.diff(snap, _snap(_at(m), {}, {}, boot=boot1, ou=False), pend)
        got += ev
        ev, snap = timeline.diff(snap, _snap(_at(m) + "x", {}, {}, **pending), pend)
        got += ev
    # apt следующим прогоном переписал reboot-required: то же ожидание
    ev, snap = timeline.diff(snap, _snap(_at(7), {}, {}, **dict(pending, rb_ts=9000, upd_ts=9100)), pend)
    got += ev
    assert got == [], got
    assert snap["kn"] == "6.8.0-147" and snap["rb_ts"] == 9000

    # еще более новое ядро, пока ждем перезагрузку, - это новость
    ev, snap = timeline.diff(snap, _snap(_at(8), {}, {}, **dict(pending, kn="6.8.0-148", rb_ts=9000)), pend)
    assert [e["kind"] for e in ev] == ["kernel_new"] and "148" in ev[0]["text"]

    # перезагрузка: первый отчет агента без обновлений - ничего не повторяем
    boot2 = "2026-10-15T03:00:00+00:00"
    ev, snap = timeline.diff(snap, _snap(_at(9), {}, {}, boot=boot2, ou=False), pend)
    assert [e["kind"] for e in ev] == ["reboot"]
    ev, snap = timeline.diff(snap, _snap(_at(10), {}, {}, boot=boot2, k="6.8.0-148", upd=0, upd_ts=9500), pend)
    assert [e["kind"] for e in ev] == ["kernel"]
    # следующее обновление после перезагрузки - снова по разу
    ev, snap = timeline.diff(snap, _snap(_at(11), {}, {}, boot=boot2, k="6.8.0-148", upd=0, upd_ts=9900,
                                         kn="6.8.0-150", rb_ts=9800), pend)
    assert sorted(e["kind"] for e in ev) == ["kernel_new", "reboot_req"]


def test_state_without_said_starts_from_snapshot():
    """Снимки, сохраненные до отметки "что уже сказано": ожидание из прошлого снимка не повторяем."""
    prev = _snap(_at(0), {}, {}, boot="2026-10-01T00:00:00+00:00", kn="6.8.0-147", rb_ts=2000)
    ev, _ = timeline.diff(prev, _snap(_at(1), {}, {}, boot="2026-10-01T00:00:00+00:00", kn="6.8.0-147",
                                      rb_ts=2500), {})
    assert ev == []


def _migration():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0126_timeline_dups.py"
    spec = importlib.util.spec_from_file_location("m0126", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_migration_drops_repeats_only():
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    eng = sa.create_engine("sqlite://")
    rb = "после обновления нужна перезагрузка: libc6"
    kn = "установлено ядро 6.8.0-147, заработает после перезагрузки"
    rows = [(1, 1, "kernel_new", kn), (2, 1, "reboot_req", rb), (3, 1, "agent", "агент Kervax: 2.27 -> 2.28"),
            (4, 1, "kernel_new", kn), (5, 1, "reboot_req", rb),
            (6, 1, "reboot_req", "после обновления нужна перезагрузка: libc6, openssl"),
            (7, 2, "kernel_new", kn),  # другой сервер
            (8, 1, "reboot", "плановая перезагрузка"),
            (9, 1, "kernel_new", kn), (10, 1, "reboot_req", rb), (11, 1, "kernel_new", kn)]
    with eng.begin() as conn:
        conn.execute(sa.text("CREATE TABLE server_events (id INTEGER PRIMARY KEY, server_id INTEGER, kind TEXT, text TEXT)"))
        for r in rows:
            conn.execute(sa.text("INSERT INTO server_events VALUES (:a, :b, :c, :d)"), dict(zip("abcd", r)))
        with Operations.context(MigrationContext.configure(conn)):
            _migration().upgrade()
        left = [r[0] for r in conn.execute(sa.text("SELECT id FROM server_events ORDER BY id"))]
    # после перезагрузки то же ядро ждет снова - это новость, один раз
    assert left == [1, 2, 3, 6, 7, 8, 9, 10], left
