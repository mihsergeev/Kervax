"""Авто-очистка диска: галочка сервера (по умолчанию выключена), только безопасные действия
"Освободить" и только разрешенные нодой, одно за раз, пауза между повторами, уведомление о
результате. Включает только админ. Плюс соседние мелочи: долгий disk_fix не закрывается
как зависший, а алерт по памяти называет контейнеры."""

import time
from datetime import datetime, timedelta, timezone

G = 1024 ** 3


def _rep(pct: int = 91, agent: str = "2.13", allow: list[str] | None = None) -> dict:
    ts = int(time.time())
    items = [
        {"id": "journal", "bytes": 4 * G, "free": int(3.8 * G), "mount": "/",
         "path": "/var/log/journal", "level": "safe", "fix": "journalctl --vacuum-size=200M"},
        {"id": "rotated-logs", "bytes": int(0.6 * G), "free": int(0.6 * G), "mount": "/",
         "path": "/var/log", "level": "safe", "fix": "find /var/log ... -delete"},
        {"id": "container-log", "bytes": 9 * G, "free": 9 * G, "mount": "/", "name": "app",
         "path": "/var/lib/docker/containers/x/x-json.log", "level": "careful", "fix": "truncate"},
        {"id": "docker-images", "bytes": 20 * G, "free": 20 * G, "mount": "/",
         "path": "/var/lib/docker", "level": "careful", "fix": "docker image prune -af"},
    ]
    return {
        "agent_version": agent, "mem_used": 1, "mem_total": 2, "clock_unix": ts,
        "disks": [{"mount": "/", "used": pct, "total": 100}],
        "extras": {"disk-usage": {
            "v": 1, "ts": ts - 60, "items": items,
            "fs": [{"mount": "/", "size": 100 * G, "used": pct * G, "avail": (100 - pct) * G,
                    "pct": pct, "inodes": 1, "top_state": "ok", "top_ts": ts, "top": []}],
            "fix": {"v": 1, "allow": allow if allow is not None
                    else ["journal", "rotated-logs", "container-log"]},
        }},
    }


async def _server(client, auth_headers, name="afx"):
    r = await client.post("/api/servers", json={"name": name}, headers=auth_headers)
    return r.json()["token"], r.json()["server"]["id"]


async def _report(client, token, rep):
    r = await client.post("/api/agent/report", json=rep, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    return r.json()


async def _fix_cmds(factory, sid):
    from sqlalchemy import select

    from app.models import BackupCommand

    async with factory() as s:
        return list(await s.scalars(select(BackupCommand).where(
            BackupCommand.server_id == sid, BackupCommand.action == "disk_fix").order_by(BackupCommand.id)))


async def test_autofix_flow(client, auth_headers, monkeypatch):
    """Самое крупное безопасное действие, одно за раз; результат - уведомлением; то же
    действие не повторяется 6 часов, следом идет следующее; осторожное не трогается."""
    from app import config
    from app.collector import autofix_disks

    monkeypatch.setenv("KERVAX_ALERT_WEBHOOK", "http://hook")
    config.get_settings.cache_clear()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    factory = client._transport.app.state.session_factory
    token, sid = await _server(client, auth_headers)
    r = await client.patch(f"/api/servers/{sid}", json={"disk_autofix": True}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["disk_autofix"] is True
    await _report(client, token, _rep(91))

    now = datetime.now(timezone.utc)
    assert await autofix_disks(factory, now) == 1
    cmds = await _fix_cmds(factory, sid)
    assert len(cmds) == 1 and cmds[0].mode == "run" and cmds[0].origin == "auto"
    assert cmds[0].payload == {"name": "journal", "container": ""}
    assert await autofix_disks(factory, now) == 0  # пока команда в работе - новых нет

    resp = await _report(client, token, _rep(91))
    assert resp["backup_commands"][0]["name"] == "journal"
    out = '{"action":"journal","mode":"run","bytes":4022337536,"count":0,"sample":[]}'
    r = await client.post("/api/agent/backup-result", json={"id": cmds[0].id, "ok": True, "output": out},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert len(sent) == 1 and "автоочистка диска: журнал systemd, освобождено 3.7 ГБ" in sent[0]

    # журнал на паузе - следом старые логи; потом все на паузе
    assert await autofix_disks(factory, now) == 1
    cmds = await _fix_cmds(factory, sid)
    assert cmds[-1].payload["name"] == "rotated-logs"
    await client.post("/api/agent/backup-result", json={"id": cmds[-1].id, "ok": False, "output": "boom"},
                      headers={"Authorization": f"Bearer {token}"})
    assert "автоочистка диска не удалась (старые ротированные логи): boom" in sent[-1]
    assert await autofix_disks(factory, now) == 0
    # через 6 часов журнал можно снова
    assert await autofix_disks(factory, now + timedelta(hours=6, minutes=1)) == 1
    config.get_settings.cache_clear()


async def test_autofix_skips(client, auth_headers):
    """Без галочки, ниже порога предупреждения, со старым агентом и с действием, которое
    нода не разрешила, - ничего не ставится или ставится разрешенное."""
    from app.collector import autofix_disks

    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    token, sid = await _server(client, auth_headers, "off")
    await _report(client, token, _rep(95))
    assert await autofix_disks(factory, now) == 0  # галочка выключена

    for name, rep in (("low", _rep(80)), ("old", _rep(95, agent="2.12"))):
        token, sid = await _server(client, auth_headers, name)
        await client.patch(f"/api/servers/{sid}", json={"disk_autofix": True}, headers=auth_headers)
        await _report(client, token, rep)
    assert await autofix_disks(factory, now) == 0

    token, sid = await _server(client, auth_headers, "nojournal")
    await client.patch(f"/api/servers/{sid}", json={"disk_autofix": True}, headers=auth_headers)
    await _report(client, token, _rep(95, allow=["rotated-logs"]))
    assert await autofix_disks(factory, now) == 1
    assert (await _fix_cmds(factory, sid))[0].payload["name"] == "rotated-logs"


async def test_autofix_toggle_admin_only(client, auth_headers):
    """Галочку меняет только админ; остальные поля редактор правит как раньше."""
    token, sid = await _server(client, auth_headers, "rbac")
    r = await client.post("/api/users", json={"username": "ed2", "password": "editorpass-002",
                                              "role": "editor"}, headers=auth_headers)
    assert r.status_code == 201
    tok = (await client.post("/api/auth/login",
                             json={"username": "ed2", "password": "editorpass-002"})).json()["access_token"]
    eh = {"Authorization": f"Bearer {tok}"}
    assert (await client.patch(f"/api/servers/{sid}", json={"disk_autofix": True},
                               headers=eh)).status_code == 403
    r = await client.patch(f"/api/servers/{sid}", json={"disk_autofix": False, "group_name": "x"},
                           headers=eh)
    assert r.status_code == 200 and r.json()["group_name"] == "x" and r.json()["disk_autofix"] is False


async def test_expire_keeps_long_disk_fix(client, auth_headers):
    """Обычная команда через 15 минут без ответа закрывается, disk_fix (builder prune идет
    до 15 минут) - нет."""
    from app.collector import _expire_commands
    from app.models import BackupCommand

    factory = client._transport.app.state.session_factory
    _, sid = await _server(client, auth_headers, "exp")
    old = datetime.now(timezone.utc) - timedelta(minutes=15)
    async with factory() as s:
        s.add_all([BackupCommand(server_id=sid, action="set_schedule", status="running", created_at=old),
                   BackupCommand(server_id=sid, action="disk_fix", mode="run", status="running",
                                 created_at=old)])
        await s.commit()
    await _expire_commands(factory)
    from sqlalchemy import select

    async with factory() as s:
        st = {c.action: c.status for c in await s.scalars(select(BackupCommand).where(
            BackupCommand.server_id == sid))}
    assert st == {"set_schedule": "error", "disk_fix": "running"}


def test_mem_cause_names_containers():
    """Алерт по памяти называет контейнеры, которые ее съели (агент 2.14+)."""
    from app.collector import cause_text

    rep = {"top_mem": [{"comm": "java", "rss": 3 * G}],
           "docker": {"containers": [{"name": "app", "mem": 3 * G}, {"name": "db", "mem": G},
                                     {"name": "tiny", "mem": 1}]}}
    txt = cause_text(rep, "mem", {})
    assert "больше всего памяти у java 3.0 ГБ" in txt and "контейнеры: app 3.0 ГБ, db 1.0 ГБ" in txt
