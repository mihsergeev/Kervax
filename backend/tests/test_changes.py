"""Изменения на сервере: снимки helper'а changes-setup, лента, алерт на изменения доступа и
отметка "это мы"."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import changes, collector
from app.models import Server

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())
KEY_A = {"u": "root", "t": "ED25519", "fp": "SHA256:aaaa", "c": "admin@laptop", "f": "/root/.ssh/authorized_keys"}
KEY_B = {"u": "deploy", "t": "RSA", "fp": "SHA256:bbbb", "c": "", "f": "/home/deploy/.ssh/authorized_keys"}


def _snap(ts, **kw):
    base = {"v": 1, "ts": ts, "keys": [KEY_A, KEY_A], "users": [{"n": "root", "uid": 0, "sh": "/bin/bash"}],
            "sudo": {"groups": {"sudo": ["ops"]}, "files": {"/etc/sudoers": "1111"}},
            "cron": {"/etc/crontab": "c1"}, "units": ["ssh.service"]}
    base.update(kw)
    return base


def test_diff_every_kind():
    old = _snap(T)
    new = _snap(T + 600, keys=[KEY_B],
                users=[{"n": "root", "uid": 0, "sh": "/bin/bash"}, {"n": "deploy", "uid": 1001, "sh": "/bin/bash"}],
                sudo={"groups": {"sudo": ["deploy"]}, "files": {"/etc/sudoers": "2222", "/etc/sudoers.d/deploy": "3333"}},
                cron={"/etc/crontab": "c2", "/etc/cron.d/miner": "c3"}, units=["ssh.service", "x.timer"])
    ev = changes.diff(old, new)
    assert [(e["kind"], e["act"], e["alert"], e["text"]) for e in ev] == [
        ("key", "add", True, "добавлен SSH-ключ пользователю deploy: RSA SHA256:bbbb"),
        ("key", "del", False, "удален SSH-ключ пользователя root: ED25519 SHA256:aaaa (admin@laptop)"),
        ("user", "add", True, "новый пользователь deploy (uid 1001, /bin/bash)"),
        ("sudo", "add", True, "deploy добавлен в группу sudo"),
        ("sudo", "del", False, "ops убран из группы sudo"),
        ("sudo", "mod", True, "изменен /etc/sudoers"),
        ("sudo", "add", True, "добавлен /etc/sudoers.d/deploy"),
        ("cron", "mod", False, "изменен крон /etc/crontab"),
        ("cron", "add", False, "добавлен крон /etc/cron.d/miner"),
        ("unit", "add", False, "включен x.timer"),
    ]
    # uid 0 у существующего пользователя - тоже доступ
    ev = changes.diff(_snap(T, users=[{"n": "svc", "uid": 998, "sh": "/bin/sh"}]),
                      _snap(T + 1, users=[{"n": "svc", "uid": 0, "sh": "/bin/sh"}]))
    assert [(e["text"], e["alert"]) for e in ev] == [("пользователь svc получил uid 0", True)]


def test_baseline_feed_and_ack():
    st = changes.apply_snapshot(None, _snap(T), NOW)
    assert st["log"] == [] and "alert_ts" not in st  # первый снимок - точка отсчета
    assert changes.apply_snapshot(st, _snap(T), NOW) == st  # тот же снимок - ничего
    st = changes.apply_snapshot(st, _snap(T + 600, cron={"/etc/crontab": "c9"}), NOW)
    assert [e["text"] for e in st["log"]] == ["изменен крон /etc/crontab"] and "alert_ts" not in st
    st = changes.apply_snapshot(st, _snap(T + 1200, cron={"/etc/crontab": "c9"}, keys=[KEY_A, KEY_B]), NOW)
    stamp = datetime.fromtimestamp(T + 1200, timezone.utc).isoformat()
    assert st["alert_ts"] == stamp and st["log"][0]["ts"] == stamp
    srv = Server(name="n", token_hash="x", changes=st)
    assert [e["text"] for e in changes.unacked_alerts(srv, NOW + timedelta(minutes=30))] == [
        "добавлен SSH-ключ пользователю deploy: RSA SHA256:bbbb"]
    srv.changes = dict(st, ack_ts=(NOW + timedelta(minutes=40)).isoformat())
    assert changes.unacked_alerts(srv, NOW + timedelta(minutes=41)) == []
    # старое уходит из ленты через 90 дней
    later = changes.apply_snapshot(st, _snap(T + 9_000_000, keys=[KEY_A, KEY_B], cron={"/etc/crontab": "c9"}),
                                   NOW + timedelta(days=100))
    assert later["log"] == []
    assert changes.view(srv)["keys"] == 2


def test_added_then_removed_is_marked():
    """Ключ положили и сняли: в ленте и на главной строка остается, но с пометкой."""
    st = changes.apply_snapshot(None, _snap(T - 1800), NOW)
    st = changes.apply_snapshot(st, _snap(T - 1200, keys=[KEY_A, KEY_B],
                                         sudo={"groups": {"sudo": ["ops", "deploy"]}, "files": {"/etc/sudoers": "1111"}}), NOW)
    st = changes.apply_snapshot(st, _snap(T - 600, keys=[KEY_A]), NOW)
    srv = Server(name="n", token_hash="x", changes=st)
    assert [e["text"] for e in changes.unacked_alerts(srv, NOW)] == [
        "добавлен SSH-ключ пользователю deploy: RSA SHA256:bbbb (позже удален)",
        "deploy добавлен в группу sudo (позже убран)"]
    assert changes.view(srv)["log"][2]["text"].endswith("(позже удален)")
    assert not st["log"][2]["text"].endswith(")")  # в базе текст как был
    # положили снова - новое добавление без пометки, старое с пометкой
    st = changes.apply_snapshot(st, _snap(T - 60, keys=[KEY_A, KEY_B]), NOW)
    srv.changes = st
    assert [e["text"] for e in changes.unacked_alerts(srv, NOW) if e["kind"] == "key"] == [
        "добавлен SSH-ключ пользователю deploy: RSA SHA256:bbbb",
        "добавлен SSH-ключ пользователю deploy: RSA SHA256:bbbb (позже удален)"]


def _srv_row(st):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report={},
                  alert_state={}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[], changes=st)


def test_home_row_until_ack():
    st = changes.apply_snapshot(changes.apply_snapshot(None, _snap(T - 600), NOW),
                                _snap(T - 60, keys=[KEY_A, KEY_B]), NOW)
    s = _srv_row(st)
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "access_change"]
    assert [(p["level"], p["sec"], p["text"]) for p in rows] == [
        (2, "changes", "изменился доступ: добавлен SSH-ключ пользователю deploy: RSA SHA256:bbbb")]
    s.changes = dict(st, ack_ts=NOW.isoformat())
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] == "access_change"]


async def test_access_alert_once_and_ack_api(client, auth_headers, tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'c.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    t0 = int(now.timestamp())
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0,
                     last_report={"uptime_seconds": 100000, "extras": {"changes": _snap(t0 - 600)}}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    monkeypatch.setattr(changes, "_last_track", None)
    cfg = Settings(alert_webhook="http://hook")
    await changes.track(factory, now)
    await collector.evaluate_servers(factory, cfg, now)
    assert not [m for m in sent if m.startswith("🔑")]
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.last_report = {"uptime_seconds": 100000, "extras": {"changes": _snap(t0, keys=[KEY_A, KEY_B])}}
        await s.commit()
    monkeypatch.setattr(changes, "_last_track", None)
    await changes.track(factory, now + timedelta(minutes=2))
    await collector.evaluate_servers(factory, cfg, now + timedelta(minutes=2))
    keys = [m for m in sent if m.startswith("🔑")]
    assert len(keys) == 1 and "добавлен SSH-ключ пользователю deploy" in keys[0], sent
    await collector.evaluate_servers(factory, cfg, now + timedelta(minutes=3))
    assert len([m for m in sent if m.startswith("🔑")]) == 1
    await engine.dispose()

    # отметка "это мы" через API
    r = await client.post("/api/servers", json={"name": "ack-node"}, headers=auth_headers)
    sid = r.json()["server"]["id"]
    r = await client.post(f"/api/servers/{sid}/changes/ack", headers=auth_headers)
    assert r.status_code == 200 and r.json()["changes"]["ack_ts"]


def test_password_login_from_a_new_address():
    from app.changes import apply_snapshot
    t0 = datetime(2026, 10, 13, 10, 0, tzinfo=timezone.utc)

    def snap(ts, *logins):
        return {"v": 1, "ts": int(ts.timestamp()), "keys": [], "users": [], "sudo": {}, "cron": {}, "units": [],
                "ssh": {"logins": [{"m": m, "u": u, "ip": ip} for m, u, ip in logins]}}
    # первый снимок с входами - точка отсчета, даже вход по паролю без алерта
    st = apply_snapshot(None, snap(t0, ("password", "ops", "198.51.100.1")), t0)
    assert st.get("log", []) == [] and "ops|198.51.100.1" in st["ssh_seen"]
    t1 = t0 + timedelta(minutes=10)
    st = apply_snapshot(st, snap(t1, ("password", "ops", "198.51.100.1"), ("publickey", "ms", "198.51.100.7"),
                                 ("password", "ops", "203.0.113.66")), t1)
    texts = [e["text"] for e in st["log"]]
    assert texts == ["вход по паролю: ops с 203.0.113.66, раньше с этого адреса не входил"]
    assert st["log"][0]["alert"] is True and st["alert_ts"] == t1.isoformat()
    t2 = t1 + timedelta(minutes=10)
    st = apply_snapshot(st, snap(t2, ("password", "ops", "203.0.113.66")), t2)
    assert len(st["log"]) == 1  # с этого адреса уже входил
    # кто последний раз входил по паролю: по ключу не в счет
    assert st["ssh_pw"] == {"ops": t2.isoformat()}


def test_key_known_in_the_fleet_goes_to_the_feed_only():
    """Наш деплой разносит свой ключ по серверам: 9 из 13 сообщений "добавлен SSH-ключ" за две недели
    были нашими. Ключ, который парк уже видел, - событие в ленте с пометкой, без алерта и без строки
    на главной; незнакомый - алерт, как раньше."""
    other = {"snap": _snap(T - 3600, keys=[KEY_B]), "log": []}
    assert changes.known_fps([other, None, {"log": [{"kind": "key", "act": "add", "ts": "x",
                                                     "text": "добавлен SSH-ключ пользователю ci: ED25519 SHA256:cccc (ci)"}]}]) \
        == {"SHA256:bbbb", "SHA256:cccc"}
    known = changes.known_fps([other])
    st = changes.apply_snapshot(changes.apply_snapshot(None, _snap(T - 600), NOW, known),
                                _snap(T - 60, keys=[KEY_A, KEY_B]), NOW, known)
    assert [(e["text"], e["alert"], e.get("known")) for e in st["log"]] == [
        ("добавлен SSH-ключ пользователю deploy: RSA SHA256:bbbb", False, True)]
    assert "alert_ts" not in st
    s = _srv_row(st)
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] == "access_change"]
    # в ленте карточки и хронологии - с пометкой; снятие парой находится как раньше
    assert changes.view(s)["log"][0]["text"] == ("добавлен SSH-ключ пользователю deploy: RSA SHA256:bbbb"
                                                 " (ключ уже был на серверах парка)")
    # незнакомый ключ рядом с новым пользователем: алертит ключ и пользователь, знакомого в алерте нет
    key_c = {"u": "deploy", "t": "ED25519", "fp": "SHA256:cccc", "c": "", "f": "/home/deploy/.ssh/authorized_keys"}
    st = changes.apply_snapshot(st, _snap(T, keys=[KEY_A, KEY_B, key_c],
                                          users=[{"n": "root", "uid": 0, "sh": "/bin/bash"},
                                                 {"n": "deploy", "uid": 1001, "sh": "/bin/bash"}]), NOW, known)
    assert changes.events_text(changes.alert_events(_srv_row(st), None)) == (
        "добавлен SSH-ключ пользователю deploy: ED25519 SHA256:cccc; новый пользователь deploy (uid 1001, /bin/bash)")


async def test_fleet_key_is_silent_unknown_key_alerts(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'k.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    t0 = int(now.timestamp())
    key_x = {"u": "deploy", "t": "ED25519", "fp": "SHA256:xxxx", "c": "", "f": "/home/deploy/.ssh/authorized_keys"}

    def row(name, keys):
        return Server(name=name, token_hash=name, enabled=True, backup_not_required=True, last_seen=now,
                      cpu_alert_percent=0, mem_alert_percent=0,
                      last_report={"uptime_seconds": 100000, "extras": {"changes": _snap(t0 - 600, keys=keys)}})

    async with factory() as s:
        s.add_all([row("web-a", [KEY_A, KEY_B]), row("web-b", [KEY_A]), row("web-c", [KEY_A])])
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    monkeypatch.setattr(changes, "_last_track", None)
    cfg = Settings(alert_webhook="http://hook")
    await changes.track(factory, now)
    async with factory() as s:
        for srv in (await s.execute(select(Server))).scalars():
            # web-b: ключ, который лежит на web-a; web-c: ключ, которого парк не видел
            keys = {"web-a": [KEY_A, KEY_B], "web-b": [KEY_A, KEY_B], "web-c": [KEY_A, key_x]}[srv.name]
            srv.last_report = {"uptime_seconds": 100000, "extras": {"changes": _snap(t0, keys=keys)}}
        await s.commit()
    monkeypatch.setattr(changes, "_last_track", None)
    await changes.track(factory, now + timedelta(minutes=2))
    await collector.evaluate_servers(factory, cfg, now + timedelta(minutes=2))
    keys = [m for m in sent if m.startswith("🔑")]
    assert len(keys) == 1 and "web-c" in keys[0] and "SHA256:xxxx" in keys[0], sent
    async with factory() as s:
        b = (await s.execute(select(Server).where(Server.name == "web-b"))).scalar_one()
        assert [(e["alert"], e.get("known")) for e in b.changes["log"]] == [(False, True)]
    # тот же незнакомый ключ на следующем сервере - уже знакомый
    async with factory() as s:
        a = (await s.execute(select(Server).where(Server.name == "web-a"))).scalar_one()
        a.last_report = {"uptime_seconds": 100000, "extras": {"changes": _snap(t0 + 600, keys=[KEY_A, KEY_B, key_x])}}
        await s.commit()
    monkeypatch.setattr(changes, "_last_track", None)
    await changes.track(factory, now + timedelta(minutes=12))
    await collector.evaluate_servers(factory, cfg, now + timedelta(minutes=12))
    assert len([m for m in sent if m.startswith("🔑")]) == 1, sent
    await engine.dispose()
