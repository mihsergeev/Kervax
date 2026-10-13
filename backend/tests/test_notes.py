"""Заметки к серверам и мониторам: видны в карточке и приезжают строкой в алертах."""
from app import alerts
from app.alerts import Msg
from app.models import Check, Server


def _factory(client):
    return client._transport.app.state.session_factory  # noqa: SLF001


async def _server(client) -> int:
    async with _factory(client)() as s:
        srv = Server(name="backup-a", token_hash="x" * 64)
        s.add(srv)
        await s.commit()
        return srv.id


async def test_server_note_set_and_clear(client, auth_headers):
    sid = await _server(client)
    r = await client.put(f"/api/servers/{sid}/note", json={"text": "  диск sda под замену  "},
                         headers=auth_headers)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["note"] == "диск sda под замену" and j["note_by"] and j["note_at"]
    r = await client.put(f"/api/servers/{sid}/note", json={"text": ""}, headers=auth_headers)
    j = r.json()
    assert j["note"] == "" and j["note_by"] == "" and j["note_at"] is None


async def test_check_note(client, auth_headers):
    r = await client.post("/api/checks", json={"name": "site", "type": "http", "target": "example.com"},
                          headers=auth_headers)
    cid = r.json()["id"]
    r = await client.put(f"/api/checks/{cid}/note", json={"text": "чинят у подрядчика"},
                         headers=auth_headers)
    assert r.status_code == 200 and r.json()["note"] == "чинят у подрядчика"
    assert (await client.get(f"/api/checks/{cid}", headers=auth_headers)).json()["note"] == "чинят у подрядчика"


async def test_note_too_long(client, auth_headers):
    sid = await _server(client)
    r = await client.put(f"/api/servers/{sid}/note", json={"text": "x" * 2001}, headers=auth_headers)
    assert r.status_code == 422


async def test_alert_gets_note_line(client):
    f = _factory(client)
    async with f() as s:
        srv = Server(name="backup-a", token_hash="y" * 64, note="диск <sda> под замену, тикет у хостера")
        a = Check(name="site-a", type="http", target="a.example", note="чинят")
        b = Check(name="site-b", type="http", target="b.example", note="чинят")
        s.add_all([srv, a, b])
        await s.commit()
        sid, ia, ib = srv.id, a.id, b.id
    msgs = [
        Msg("🔥 backup-a — диск", "servers", "", kind="disk_health", target="backup-a", ref=f"srv:{sid}"),
        Msg("✅ backup-a — снова в норме", "servers", "", kind="disk_health", target="backup-a",
            recovery=True, ref=f"srv:{sid}"),
        Msg("🔴 site-a и site-b", "sites", "", kind="down", target="a.example", ref=f"chk:{ia},{ib}"),
        Msg("без ссылки", "servers", ""),
    ]
    out = await alerts.with_notes(f, msgs)
    # html экранирован, заметка курсивом отдельной строкой, атрибуты Msg сохранены
    assert out[0] == "🔥 backup-a — диск\n📝 <i>диск &lt;sda&gt; под замену, тикет у хостера</i>"
    assert out[0].kind == "disk_health" and out[0].ref == f"srv:{sid}" and out[0].section == "servers"
    # отбою заметка не нужна
    assert out[1] == msgs[1]
    # одинаковые заметки свернутых мониторов - один раз
    assert out[2].endswith("\n📝 <i>чинят</i>")
    assert out[3] == "без ссылки"


async def test_alert_note_trimmed(client):
    f = _factory(client)
    async with f() as s:
        srv = Server(name="backup-a", token_hash="z" * 64, note="слово " * 200)
        s.add(srv)
        await s.commit()
        sid = srv.id
    out = await alerts.with_notes(f, [Msg("алерт", "servers", "", ref=f"srv:{sid}")])
    line = out[0].split("\n📝 ", 1)[1]
    assert line.endswith("...</i>") and len(line) < 320


async def test_note_read_failure_sends_as_is():
    def broken():
        raise RuntimeError("db down")

    msgs = [Msg("алерт", "servers", "", ref="srv:1")]
    assert await alerts.with_notes(broken, msgs) == msgs


async def test_overview_hides_secrets_and_respects_groups(client, auth_headers):
    """Обзор мониторов отдавал пароль сайта, заголовки и токен push всем ролям и всем группам."""
    for name, grp in (("own", "web"), ("foreign", "other")):
        r = await client.post("/api/checks", json={
            "name": name, "type": "http", "target": f"{name}.example", "group_name": grp,
            "auth_method": "basic", "auth_user": "u", "auth_pass": "secret-pass",
            "http_headers": '{"Authorization": "Bearer t"}',
        }, headers=auth_headers)
        assert r.status_code == 201, r.text
    r = await client.post("/api/users", headers=auth_headers, json={
        "username": "watcher", "password": "watcher-pass-2026", "role": "viewer", "site_groups": ["web"],
    })
    assert r.status_code in (200, 201), r.text
    login = await client.post("/api/auth/login",
                              json={"username": "watcher", "password": "watcher-pass-2026"})
    vh = {"Authorization": f"Bearer {login.json()['access_token']}"}
    ov = (await client.get("/api/checks/overview", headers=vh)).json()
    assert [c["name"] for c in ov["checks"]] == ["own"] and ov["total"] == 1
    assert ov["checks"][0]["auth_pass"] == "" and ov["checks"][0]["http_headers"] == ""
    # админ видит все и с секретами, и маскировка выше не стерла их в базе
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert {c["name"] for c in ov["checks"]} == {"own", "foreign"}
    assert all(c["auth_pass"] == "secret-pass" for c in ov["checks"])
