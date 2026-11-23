"""Ключевое слово в проверке изнутри задает только админ (аудит агента 2.74).

Агент ищет слово в странице, которую видно только с ноды, и отдает "есть/нет". Меняя слово,
адрес или пути и глядя на статус монитора, редактор подбирал содержимое закрытого сайта."""

import pytest

from app import checks

PW = "fakepass-example"
SITE = {"name": "closed", "type": "http", "target": "https://closed.example", "probe_local": True}


@pytest.fixture(autouse=True)
def _no_panel_probe(monkeypatch):
    async def up(check):
        return checks.CheckOutcome("up", message="HTTP 200")

    monkeypatch.setattr(checks, "run_check", up)


async def _editor(client, auth_headers) -> dict:
    r = await client.post("/api/users", headers=auth_headers,
                          json={"username": "kw-editor", "password": PW, "role": "editor"})
    assert r.status_code == 201, r.text
    r = await client.post("/api/auth/login", json={"username": "kw-editor", "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_editor_cannot_set_keyword_of_local_probe(client, auth_headers):
    editor = await _editor(client, auth_headers)
    r = await client.post("/api/checks", headers=editor, json={**SITE, "keyword_up": "secret-a"})
    assert r.status_code == 403 and "администратор" in r.text, r.text
    r = await client.post("/api/checks/import", headers=editor,
                          json={"items": [{**SITE, "keyword_down": "secret-a"}]})
    assert r.status_code == 403, r.text
    # без слова - можно, как раньше; слово у монитора, который проверяет панель, - тоже
    r = await client.post("/api/checks", headers=editor, json=SITE)
    assert r.status_code == 201, r.text
    own = r.json()["id"]
    r = await client.post("/api/checks", headers=editor,
                          json={**SITE, "name": "public", "probe_local": False, "keyword_up": "Welcome"})
    assert r.status_code == 201, r.text
    public = r.json()["id"]
    r = await client.patch(f"/api/checks/{own}", headers=editor, json={"keyword_up": "secret-a"})
    assert r.status_code == 403, r.text
    # включить проверку изнутри у монитора со словом - тот же оракул
    r = await client.patch(f"/api/checks/{public}", headers=editor, json={"probe_local": True})
    assert r.status_code == 403, r.text
    r = await client.patch("/api/checks/bulk", headers=editor, json={"ids": [public], "probe_local": True})
    assert r.status_code == 403, r.text
    r = await client.get(f"/api/checks/{public}", headers=editor)
    assert r.json()["probe_local"] is False


async def test_editor_keeps_admin_keyword_monitor_but_cannot_retarget_it(client, auth_headers):
    editor = await _editor(client, auth_headers)
    r = await client.post("/api/checks", headers=auth_headers, json={**SITE, "keyword_up": "Welcome"})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # правка, которая не меняет запрос изнутри, проходит
    r = await client.patch(f"/api/checks/{cid}", headers=editor, json={"name": "closed site", "interval_seconds": 120})
    assert r.status_code == 200, r.text
    r = await client.patch("/api/checks/bulk", headers=editor, json={"ids": [cid], "retries": 2})
    assert r.status_code == 200, r.text
    # то же слово на другом адресе или пути - новый вопрос к закрытому сайту
    for change in ({"keyword_up": "Welcome2"}, {"keyword_down": "error"},
                   {"target": "https://closed.example/admin"}, {"extra_paths": ["/admin"]}):
        r = await client.patch(f"/api/checks/{cid}", headers=editor, json=change)
        assert r.status_code == 403, (change, r.text)
    r = await client.get(f"/api/checks/{cid}", headers=auth_headers)
    assert r.json()["target"] == SITE["target"] and r.json()["keyword_up"] == "Welcome"
    # убрать слово редактор может, админ - менять как угодно
    r = await client.patch(f"/api/checks/{cid}", headers=auth_headers, json={"keyword_up": "Hello", "extra_paths": ["/a"]})
    assert r.status_code == 200, r.text
    r = await client.patch(f"/api/checks/{cid}", headers=editor, json={"keyword_up": ""})
    assert r.status_code == 200, r.text


async def test_editor_cannot_redirect_admin_keyword_monitor(client, auth_headers):
    # заголовки (Host, Range), учетные данные, TLS и сервер-родитель уводят то же слово на другой
    # vhost или участок страницы
    editor = await _editor(client, auth_headers)
    r = await client.post("/api/servers", headers=auth_headers, json={"name": "node-kw"})
    sid = r.json()["server"]["id"]
    r = await client.post("/api/checks", headers=auth_headers, json={**SITE, "keyword_up": "Welcome"})
    cid = r.json()["id"]
    for change in ({"http_headers": '{"Host": "admin.closed.example"}'}, {"ignore_tls": True},
                   {"auth_method": "basic", "auth_user": "probe", "auth_pass": "fake-pass"},
                   {"parent_mode": "server", "parent_server_id": sid}):
        r = await client.patch(f"/api/checks/{cid}", headers=editor, json=change)
        assert r.status_code == 403, (change, r.text)
    r = await client.patch(f"/api/checks/{cid}", headers=auth_headers, json={"ignore_tls": True})
    assert r.status_code == 200, r.text


async def _serving_node(client, auth_headers) -> dict:
    from tests.test_manual_run import REPORT

    r = await client.post("/api/servers", headers=auth_headers, json={"name": "node-closed"})
    agent = {"Authorization": f"Bearer {r.json()['token']}"}
    rep = dict(REPORT, agent_version="2.74", web_services=[{"kind": "nginx", "sites": ["closed.example"]}])
    assert (await client.post("/api/agent/report", headers=agent, json=rep)).status_code == 200
    return agent


async def test_editor_try_inside_runs_without_keyword(client, auth_headers):
    # форма правки сама зовет пробу: не-админу - проба без слова и пометка, а не 403
    editor = await _editor(client, auth_headers)
    await _serving_node(client, auth_headers)
    body = {"part": "inside", "type": "http", "target": SITE["target"], "keyword_up": "secret-a"}
    r = await client.post("/api/checks/try", headers=editor, json=body)
    assert r.status_code == 200, r.text
    inside = r.json()["inside"]
    assert inside["status"] == "pending" and inside["keyword_skipped"] is True, inside
    factory = client._transport.app.state.session_factory
    async with factory() as s:
        from app.models import ProbeRequest

        req = await s.get(ProbeRequest, inside["request_id"])
        assert req.spec["keyword_up"] == "" and req.spec["keyword_down"] == ""
    r = await client.get(f"/api/checks/try/{inside['request_id']}", headers=editor)
    assert r.json()["inside"]["keyword_skipped"] is True
    # админу слово идет агенту как есть
    r = await client.post("/api/checks/try", headers=auth_headers, json=body)
    assert r.json()["inside"]["keyword_skipped"] is False


async def test_editor_local_probe_apply_skips_keyword_monitor(client, auth_headers):
    editor = await _editor(client, auth_headers)
    await _serving_node(client, auth_headers)
    r = await client.post("/api/checks", headers=editor,
                          json={**SITE, "probe_local": False, "keyword_up": "Welcome"})
    cid = r.json()["id"]
    r = await client.post("/api/checks/local-probe-apply", headers=editor, json={"check_ids": [cid]})
    assert r.status_code == 200 and r.json()["updated"] == 0, r.text
    assert (await client.get(f"/api/checks/{cid}", headers=editor)).json()["probe_local"] is False
    r = await client.post("/api/checks/local-probe-apply", headers=auth_headers, json={"check_ids": [cid]})
    assert r.json()["updated"] == 1


async def test_draining_agent_gets_no_commands(client, auth_headers):
    # агент 2.74 сливает команды перед exec в обновление: новые ждут в очереди, иначе слив
    # тянулся бы до таймаута и обрывал их
    from tests.test_manual_run import REPORT

    r = await client.post("/api/servers", headers=auth_headers, json={"name": "node-drain"})
    sid = r.json()["server"]["id"]
    agent = {"Authorization": f"Bearer {r.json()['token']}"}
    r = await client.post(f"/api/servers/{sid}/docker/command", headers=auth_headers,
                          json={"container": "web", "action": "restart"})
    assert r.status_code == 200, r.text
    rep = dict(REPORT, agent_version="2.74")
    r = await client.post("/api/agent/report", headers=agent, json={**rep, "draining": True})
    assert r.status_code == 200 and r.json()["docker_commands"] == [], r.text
    r = await client.post("/api/agent/report", headers=agent, json=rep)
    assert [c["container"] for c in r.json()["docker_commands"]] == ["web"]
