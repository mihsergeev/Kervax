"""Карта зависимостей (api/depmap.py): связи серверов и мониторов, как их видит учетка."""
from datetime import datetime, timedelta, timezone

from app.models import Check, Server


async def _fill(client) -> dict:
    f = client._transport.app.state.session_factory  # noqa: SLF001
    now = datetime.now(timezone.utc)
    async with f() as s:
        gw = Server(name="gw-a", token_hash="t-gw", group_name="net", last_seen=now, offline_after_seconds=120)
        s.add(gw)
        await s.commit()
        node = Server(name="node-b", token_hash="t-node", group_name="shop", parent_id=gw.id,
                      last_seen=now - timedelta(hours=1), offline_after_seconds=120)
        web = Server(name="web-c", token_hash="t-web", group_name="shop", last_seen=now, offline_after_seconds=120,
                     last_report={"web_services": [{"sites": ["a.example"]}]})
        off = Server(name="old-d", token_hash="t-old", group_name="shop", enabled=False)
        s.add_all([node, web, off])
        await s.commit()
        s.add_all([
            Check(name="site-a", type="http", target="https://a.example/", group_name="shop", last_status="down"),
            Check(name="site-x", type="http", target="https://x.example/", group_name="shop", last_status="up"),
            Check(name="api-b", type="tcp_port", target="api.internal:443", group_name="shop",
                  parent_mode="server", parent_server_id=node.id),
            Check(name="off-z", type="http", target="https://a.example/z", enabled=False),
            Check(name="free-y", type="http", target="https://y.example/", parent_mode="off"),
        ])
        await s.commit()
        return {"gw": gw.id, "node": node.id, "web": web.id, "old": off.id}


async def test_depmap(client, auth_headers):
    ids = await _fill(client)
    j = (await client.get("/api/depmap", headers=auth_headers)).json()
    srv = {x["name"]: x for x in j["servers"]}
    assert srv["node-b"]["parent_id"] == ids["gw"] and not srv["node-b"]["online"]
    assert srv["gw-a"]["online"] and srv["gw-a"]["parent_id"] is None
    assert srv["old-d"]["enabled"] is False and srv["old-d"]["online"] is False
    chk = {x["name"]: x for x in j["checks"]}
    assert "off-z" not in chk  # выключенные мониторы на карте не нужны
    assert chk["site-a"]["parents"] == [{"id": ids["web"], "why": "site"}] and chk["site-a"]["status"] == "down"
    assert chk["api-b"]["parents"] == [{"id": ids["node"], "why": "manual"}]
    assert chk["site-x"]["parents"] == [] and chk["site-x"]["mode"] == "auto"
    # отвязанный руками отличается от того, чей сервер панель не нашла
    assert chk["free-y"]["parents"] == [] and chk["free-y"]["mode"] == "off"
    assert chk["api-b"]["target"] == "api.internal:443" and chk["api-b"]["type"] == "tcp_port"
    assert srv["gw-a"]["last_seen"] and srv["old-d"]["last_seen"] is None


async def test_depmap_respects_access(client, auth_headers):
    ids = await _fill(client)
    await client.post("/api/users", headers=auth_headers, json={
        "username": "mapviewer", "password": "mapviewer-pass-2026", "role": "viewer",
        "server_groups": ["shop"], "sections": ["servers"]})
    login = await client.post("/api/auth/login", json={"username": "mapviewer", "password": "mapviewer-pass-2026"})
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    j = (await client.get("/api/depmap", headers=h)).json()
    srv = {x["name"]: x for x in j["servers"]}
    # шлюз из чужой группы не виден, и node-b для этой учетки ни за кем не стоит
    assert "gw-a" not in srv and srv["node-b"]["parent_id"] is None
    assert j["checks"] == []  # раздела "Сайты" у учетки нет
    assert ids["web"] in {x["id"] for x in j["servers"]}
