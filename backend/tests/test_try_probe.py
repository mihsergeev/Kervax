"""Пробная проверка в форме нового монитора: до сохранения видно, откроется ли адрес и откуда.

Раньше форма заводила монитор вслепую: увидит ли панель сайт, человек узнавал от красного
монитора и алерта, а "откуда проверять" выбирал наугад.
"""

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select

from app import checks as checks_exec
from app.models import Check, Location, ProbeRequest

REPORT = {
    "hostname": "node", "os": "Ubuntu 24.04", "agent_version": "2.8",
    "cpu_percent": 1.0, "mem_used": 1, "mem_total": 2,
    "load": [0.1, 0.1, 0.1], "disks": [{"mount": "/", "used": 1, "total": 2}],
    "web_services": [{"kind": "nginx", "sites": ["admin.example.com"]}],
}

FORM = {"type": "http", "target": "https://admin.example.com"}


def _factory(client: httpx.AsyncClient):
    return client._transport.app.state.session_factory  # noqa: SLF001


async def _node(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "web-a"}, headers=auth_headers)
    ah = {"Authorization": f"Bearer {r.json()['token']}"}
    assert (await client.post("/api/agent/report", json=REPORT, headers=ah)).status_code == 200
    return r.json()["server"]["id"], ah


async def test_panel_part_is_one_attempt_and_sees_every_address(client, auth_headers, monkeypatch):
    seen = []

    async def fake_run(check):
        seen.append(check)
        return checks_exec.CheckOutcome(
            "down", 30, message="2/3 адресов OK; 203.0.113.12 - ошибка сервера (HTTP 502)",
            ip_results=[{"ip": "203.0.113.10", "status": "up", "latency_ms": 30, "message": "HTTP 200"},
                        {"ip": "203.0.113.12", "status": "down", "latency_ms": 28, "message": "HTTP 502"}])

    monkeypatch.setattr("app.checks.run_check", fake_run)
    async with _factory(client)() as s:
        s.add(Check(name="магазин", type="http", target="https://shop.example.com", group_name="Магазин"))
        # тот же домен, другой путь - это другой монитор, не дубль
        s.add(Check(name="api админки", type="http", target="https://admin.example.com/api"))
        s.add(Check(name="админка", type="http", target="admin.example.com/", group_name="Магазин"))
        await s.commit()
    r = await client.post("/api/checks/try", headers=auth_headers, json=dict(FORM, part="panel"))
    assert r.status_code == 200, r.text
    out = r.json()
    # взгляд перед сохранением: одна попытка, адреса домена - всегда по отдельности
    assert seen[0].retries == 0 and seen[0].check_all_ips is True
    assert out["panel"]["status"] == "down" and out["panel"]["code"] == 502
    assert len(out["panel"]["ip_results"]) == 2
    # группа - как у мониторов того же домена; этот адрес уже смотрит другой монитор
    assert out["group"] == "Магазин"
    assert out["existing_name"] == "админка"
    # правка самой "админки": она себе не дубль
    r = await client.post("/api/checks/try", headers=auth_headers,
                          json=dict(FORM, part="panel", check_id=out["existing_id"]))
    assert r.json()["existing_id"] is None
    # ничего не заведено
    async with _factory(client)() as s:
        assert await s.scalar(select(func.count()).select_from(Check)) == 3


async def test_panel_part_carries_the_form_settings(client, auth_headers, monkeypatch):
    """Сайт просит вход: "проверить снова" идет с логином, заголовком и кодом из формы."""
    seen = []

    async def fake_run(check):
        seen.append(check)
        return checks_exec.CheckOutcome("up", 90, message="HTTP 200 · 90 мс")

    monkeypatch.setattr("app.checks.run_check", fake_run)
    r = await client.post("/api/checks/try", headers=auth_headers, json=dict(
        FORM, part="panel", auth_method="basic", auth_user="ops", auth_pass="secret-1",
        http_headers='{"X-Token": "t-1"}', expected_status="200-299", keyword_up="ok",
        extra_paths=["/health"]))
    assert r.status_code == 200, r.text
    c = seen[0]
    assert (c.auth_method, c.auth_user, c.auth_pass) == ("basic", "ops", "secret-1")
    assert c.http_headers == '{"X-Token": "t-1"}' and c.expected_status == "200-299"
    assert c.keyword_up == "ok" and c.extra_paths == ["/health"]


async def test_tcp_port_is_checked_from_the_panel_only(client, auth_headers, monkeypatch):
    async def fake_run(check):
        assert check.type == "tcp_port" and check.port == 5432 and not check.check_all_ips
        return checks_exec.CheckOutcome("up", 3, message="порт 5432 открыт · 3 мс")

    monkeypatch.setattr("app.checks.run_check", fake_run)
    body = {"type": "tcp_port", "target": "db.example.com", "port": 5432}
    r = await client.post("/api/checks/try", headers=auth_headers, json=dict(body, part="panel"))
    assert r.json()["panel"]["status"] == "up"
    # агент изнутри и локации умеют только сайты
    for part in ("inside", "locations", "expiry"):
        r = await client.post("/api/checks/try", headers=auth_headers, json=dict(body, part=part))
        assert r.status_code == 400


async def test_dns_shows_the_answer_before_saving(client, auth_headers, monkeypatch):
    """DNS-монитор: до сохранения видно, что резолвер отвечает сейчас. Проба ничего не
    запоминает, а в правке сравнивает с ответом, который монитор уже запомнил."""
    seen = []

    async def fake_run(check):
        seen.append(check)
        if check.dns_baseline:
            return checks_exec.CheckOutcome(
                "down", 12, message="ответ изменился: было 203.0.113.10, стало 203.0.113.20",
                answers=["203.0.113.20"])
        return checks_exec.CheckOutcome("up", 12, message="A: 203.0.113.20 (запомнили)",
                                        answers=["203.0.113.20"])

    monkeypatch.setattr("app.checks.run_check", fake_run)
    body = {"type": "dns", "target": "https://Example.com/path", "part": "panel"}
    r = await client.post("/api/checks/try", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    p = r.json()["panel"]
    # имя нормализовано как у сохраненного монитора, тип по умолчанию A, одна попытка
    assert seen[0].target == "example.com" and seen[0].dns_type == "A" and seen[0].retries == 0
    assert p["answers"] == ["203.0.113.20"] and p["baseline"] == "new"
    assert p["message"] == "A: 203.0.113.20"

    async with _factory(client)() as s:
        mon = Check(name="dns", type="dns", target="example.com", dns_type="A",
                    dns_baseline=["203.0.113.10"])
        s.add(mon)
        await s.commit()
        mid = mon.id
    # новый монитор на ту же запись - дубль
    r = await client.post("/api/checks/try", headers=auth_headers, json=body)
    assert r.json()["existing_id"] == mid
    # правка этого монитора: сравнили с запомненным ответом
    r = await client.post("/api/checks/try", headers=auth_headers, json=dict(body, check_id=mid))
    p = r.json()["panel"]
    assert seen[-1].dns_baseline == ["203.0.113.10"]
    assert p["status"] == "down" and p["baseline"] == "kept" and r.json()["existing_id"] is None
    # запись другого типа - уже не та, с чем сравнивать нечего
    r = await client.post("/api/checks/try", headers=auth_headers,
                          json=dict(body, check_id=mid, dns_type="MX"))
    assert seen[-1].dns_baseline == [] and r.json()["panel"]["baseline"] == "new"

    r = await client.post("/api/checks/try", headers=auth_headers,
                          json={"type": "dns", "target": "example.com", "part": "panel",
                                "dns_server": "dns.example.com"})
    assert r.status_code == 400
    r = await client.post("/api/checks/try", headers=auth_headers,
                          json={"type": "dns", "target": "example.com", "part": "inside"})
    assert r.status_code == 400


async def test_inside_goes_to_the_agent_with_the_form_settings(client, auth_headers):
    """Сайт за белым списком: панель видит 403, а агент сервера сайта проверяет его на
    localhost с теми же кодом, заголовком и входом, что заведут в монитор."""
    sid, ah = await _node(client, auth_headers)
    body = dict(FORM, part="inside", expected_status="200-399,401", auth_method="basic",
                auth_user="ops", auth_pass="secret-1", http_headers='{"X-Token": "t-1"}')
    r = await client.post("/api/checks/try", headers=auth_headers, json=body)
    assert r.status_code == 200, r.text
    inside = r.json()["inside"]
    assert inside["status"] == "pending" and inside["server_name"] == "web-a"
    assert inside["web"] == "nginx" and inside["fast"] is True
    rid = inside["request_id"]

    tasks = (await client.get("/api/agent/commands", headers=ah)).json()["site_probes"]
    assert len(tasks) == 1
    t = tasks[0]
    assert t["id"] == -rid and t["url"] == "https://admin.example.com"
    assert t["expected_status"] == "200-399,401"
    assert t["auth_user"] == "ops" and t["auth_pass"] == "secret-1"
    assert t["headers"] == '{"X-Token": "t-1"}'

    r = await client.get(f"/api/checks/try/{rid}", headers=auth_headers)
    assert r.json()["inside"]["status"] == "pending" and r.json()["inside"]["web"] == "nginx"

    r = await client.post("/api/agent/site-probe-result", headers=ah, json={"site_probes": [
        {"id": -rid, "code": 401, "latency_ms": 2, "kw_up_found": True},
    ]})
    assert r.status_code == 200, r.text
    got = (await client.get(f"/api/checks/try/{rid}", headers=auth_headers)).json()["inside"]
    # 401 в форме объявлен нормой - и агентский ответ оценен по нему же
    assert got["status"] == "up" and got["code"] == 401
    async with _factory(client)() as s:
        req = await s.get(ProbeRequest, rid)
        # пароль и заголовок не лежат в базе после ответа
        assert "auth_pass" not in req.spec and "http_headers" not in req.spec
        assert req.spec["expected_status"] == "200-399,401"
        assert await s.scalar(select(func.count()).select_from(Check)) == 0


async def test_inside_without_a_serving_server_or_answer(client, auth_headers):
    sid, ah = await _node(client, auth_headers)
    r = await client.post("/api/checks/try", headers=auth_headers,
                          json={"type": "http", "target": "other.example.com", "part": "inside"})
    assert r.json()["inside"] == {**r.json()["inside"], "status": "none", "kind": "no_server"}

    r = await client.post("/api/checks/try", headers=auth_headers,
                          json=dict(FORM, part="inside", auth_method="basic", auth_pass="p"))
    rid = r.json()["inside"]["request_id"]
    async with _factory(client)() as s:
        req = await s.get(ProbeRequest, rid)
        req.created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        await s.commit()
    got = (await client.get(f"/api/checks/try/{rid}", headers=auth_headers)).json()["inside"]
    assert got["status"] == "none" and got["kind"] == "no_answer" and got["deadline"] > 0
    async with _factory(client)() as s:
        assert "auth_pass" not in (await s.get(ProbeRequest, rid)).spec


async def test_poll_answers_only_its_own_requests(client, auth_headers):
    sid, ah = await _node(client, auth_headers)
    async with _factory(client)() as s:
        req = ProbeRequest(check_id=0, server_id=sid, url="https://admin.example.com",
                           created_at=datetime.now(timezone.utc))
        s.add(req)
        await s.commit()
        rid = req.id
    # запрос мастера найденных доменов - не пробная проверка формы
    assert (await client.get(f"/api/checks/try/{rid}", headers=auth_headers)).status_code == 404
    assert (await client.get("/api/checks/try/99999", headers=auth_headers)).status_code == 404


async def test_locations_one_attempt_each_without_direct(client, auth_headers, monkeypatch):
    calls = []

    async def fake_http(check, proxy=None, degraded_ms=None, timeout_ms=None):
        calls.append(proxy)
        if "kz" in proxy:
            return checks_exec.CheckOutcome("down", message="доступ запрещён (HTTP 403)")
        return checks_exec.CheckOutcome("up", 40, message="HTTP 200 · 40 мс")

    monkeypatch.setattr("app.checks._run_http", fake_http)
    async with _factory(client)() as s:
        s.add(Location(name="Москва", url="http://proxy.msk:3128", enabled=True))
        s.add(Location(name="Алматы", url="http://proxy.kz:3128", enabled=True))
        s.add(Location(name="напрямую", url="", enabled=True))
        s.add(Location(name="выкл", url="http://proxy.off:3128", enabled=False))
        await s.commit()
    r = await client.post("/api/checks/try", headers=auth_headers, json=dict(FORM, part="locations"))
    locs = {x["name"]: x for x in r.json()["locations"]}
    assert set(locs) == {"Москва", "Алматы"}
    assert sorted(calls) == ["http://proxy.kz:3128", "http://proxy.msk:3128"]
    assert locs["Алматы"]["status"] == "down" and locs["Алматы"]["code"] == 403
    assert locs["Москва"]["latency_ms"] == 40


async def test_viewer_cannot_try(client, auth_headers):
    r = await client.post("/api/users", headers=auth_headers, json={
        "username": "watcher", "password": "watcher-pass-2026", "role": "viewer",
    })
    assert r.status_code in (200, 201), r.text
    login = await client.post("/api/auth/login",
                              json={"username": "watcher", "password": "watcher-pass-2026"})
    vh = {"Authorization": f"Bearer {login.json()['access_token']}"}
    r = await client.post("/api/checks/try", headers=vh, json=dict(FORM, part="panel"))
    assert r.status_code == 403
