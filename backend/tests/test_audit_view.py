"""Журнал действий для показа: действия с сервером по номеру (srv=N) показывают имя сервера,
и такие действия видны в ленте событий самого сервера."""

from datetime import datetime, timedelta, timezone

from app.models import AuditLog, Server


async def test_srv_ref_shows_server_name(client, auth_headers):
    f = client._transport.app.state.session_factory  # noqa: SLF001
    async with f() as s:
        srv = Server(name="web-a", token_hash="t-a")
        s.add(srv)
        await s.commit()
        s.add_all([
            AuditLog(username="admin", action="docker_restart", target="shop-web-1", detail=f"srv={srv.id}"),
            AuditLog(username="авто", action="backup_disk_fix", target="journal:run", detail="srv=99999"),
            AuditLog(username="admin", action="server_snooze", target="web-a", detail="disk=2ч"),
        ])
        await s.commit()
    rows = (await client.get("/api/audit?limit=3", headers=auth_headers)).json()
    got = {r["action"]: r["detail"] for r in rows}
    assert got == {"docker_restart": "web-a", "backup_disk_fix": "#99999", "server_snooze": "disk=2ч"}
    assert all(r["ts"].endswith(("Z", "+00:00")) for r in rows)
    over = (await client.get("/api/users/overview", headers=auth_headers)).json()
    assert {a["action"]: a["detail"] for a in over["audit"]}["docker_restart"] == "web-a"


async def test_timeline_shows_actions_by_server_id(client, auth_headers):
    """Перезапуск контейнера пишется с объектом-контейнером и srv=N: раньше лента сервера искала
    только по имени сервера в объекте и таких действий не показывала."""
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        srv = Server(name="web-a", token_hash="t-a", last_seen=now)
        other = Server(name="web-b", token_hash="t-b", last_seen=now)
        s.add_all([srv, other])
        await s.commit()
        s.add_all([
            AuditLog(ts=now - timedelta(minutes=5), username="admin", action="docker_restart",
                     target="shop-web-1", detail=f"srv={srv.id}"),
            AuditLog(ts=now - timedelta(minutes=4), username="admin", action="docker_restart",
                     target="shop-web-2", detail=f"srv={other.id}"),
        ])
        await s.commit()
        sid = srv.id
    j = (await client.get(f"/api/servers/{sid}/timeline?days=1", headers=auth_headers)).json()
    panel = [(e["kind"], e["text"]) for e in j["events"] if e["cat"] == "panel"]
    assert panel == [("docker_restart", "shop-web-1")]
