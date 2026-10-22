"""Мастер «Домены, найденные на серверах»: разовая проверка перед постановкой на мониторинг.

Живой случай (15.09.2026, example.com): мастер предлагал 11 доменов stand-a, а
открываются ли они панели, было не узнать, пока монитор не покраснеет. Часть сайтов
закрыта белым списком — их надо проверять изнутри сервера, а мастер заводил всё как
обычные внешние мониторы.
"""

from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select

from app import checks as checks_exec
from app import domain_probe
from app.models import Check, CheckIncident, DomainProbe, ProbeRequest, Server

REPORT = {
    "hostname": "node", "os": "Ubuntu 24.04", "agent_version": "2.8",
    "cpu_percent": 1.0, "mem_used": 1, "mem_total": 2,
    "load": [0.1, 0.1, 0.1], "disks": [{"mount": "/", "used": 1, "total": 2}],
    "web_services": [{"kind": "caddy", "sites": [
        "open.example", "closed.example", "*.mask.example", "watched.example",
    ]}],
}


def _factory(client: httpx.AsyncClient):
    return client._transport.app.state.session_factory  # noqa: SLF001


async def _setup(client, auth_headers, monkeypatch, version="2.8"):
    """Нода с веб-сервером на связи; open.example открывается снаружи, closed.example —
    нет (белый список рвёт соединение); watched.example уже под мониторингом."""
    r = await client.post("/api/servers", json={"name": "stand-a"}, headers=auth_headers)
    sid = r.json()["server"]["id"]
    ah = {"Authorization": f"Bearer {r.json()['token']}"}
    r = await client.post("/api/agent/report", json=dict(REPORT, agent_version=version),
                          headers=ah)
    assert r.status_code == 200

    calls: list[str] = []

    async def fake_run(check):
        calls.append(check.target)
        assert check.retries == 0 and check.timeout_ms == 10000
        if check.target == "https://open.example":
            return checks_exec.CheckOutcome("up", latency_ms=85, message="HTTP 200 · 85 мс")
        return checks_exec.CheckOutcome(
            "down", message="сервер оборвал соединение (Connection reset by peer)")

    monkeypatch.setattr("app.checks.run_check", fake_run)
    async with _factory(client)() as s:
        s.add(Check(name="watched", type="http", target="https://watched.example"))
        await s.commit()
    return sid, ah, calls


def _by_domain(items: list[dict]) -> dict[str, dict]:
    return {i["domain"]: i for i in items}


async def test_probe_outside_and_inside(client, auth_headers, monkeypatch):
    sid, ah, calls = await _setup(client, auth_headers, monkeypatch)
    r = await client.post("/api/checks/discovered/probe", headers=auth_headers, json={
        "domains": ["open.example", "Closed.Example.", "*.mask.example", "watched.example",
                    "elsewhere.example"],
    })
    assert r.status_code == 200, r.text
    items = _by_domain(r.json()["items"])
    # маску проверять нечем, мониторящийся домен не нужен, чужой панель не трогает
    assert set(items) == {"open.example", "closed.example"}
    assert all(i["local_status"] == "pending" and i["local_server"] == "stand-a"
               for i in items.values())
    assert sorted(calls) == ["https://closed.example", "https://open.example"]

    # снаружи — фоном, итог уже в базе
    items = _by_domain((await client.get("/api/checks/discovered/probes",
                                         headers=auth_headers)).json()["items"])
    assert items["open.example"]["ext_status"] == "up"
    assert items["open.example"]["ext_latency_ms"] == 85
    closed = items["closed.example"]
    assert closed["ext_status"] == "down" and closed["ext_kind"] == "reset"
    assert "оборвал" in closed["ext_message"]

    # изнутри — агент забирает задание быстрым опросом: только localhost, имя из адреса
    tasks = (await client.get("/api/agent/commands", headers=ah)).json()["site_probes"]
    assert sorted(t["url"] for t in tasks) == ["https://closed.example", "https://open.example"]
    assert all(t["id"] < 0 and t["expected_status"] == "200-399" and t["timeout_ms"] == 10000
               and not t["auth_pass"] and not t["headers"] for t in tasks)
    assert (await client.get("/api/agent/commands", headers=ah)).json()["site_probes"] == []

    ids = {t["url"]: t["id"] for t in tasks}
    r = await client.post("/api/agent/site-probe-result", headers=ah, json={"site_probes": [
        {"id": ids["https://open.example"], "code": 200, "latency_ms": 4, "kw_up_found": True},
        {"id": ids["https://closed.example"], "code": 200, "latency_ms": 3, "kw_up_found": True},
    ]})
    assert r.status_code == 200, r.text
    items = _by_domain((await client.get("/api/checks/discovered/probes",
                                         headers=auth_headers)).json()["items"])
    assert items["closed.example"]["local_status"] == "up"
    assert items["closed.example"]["local_message"] == "HTTP 200 · 3 мс"
    assert items["open.example"]["local_status"] == "up"

    # это разовый взгляд, а не мониторинг: ни мониторов, ни инцидентов
    async with _factory(client)() as s:
        assert await s.scalar(select(func.count()).select_from(Check)) == 1
        assert (await s.scalars(select(CheckIncident))).all() == []


async def test_probe_is_one_off(client, auth_headers, monkeypatch):
    """Повторное открытие мастера показывает прошлый итог, а не гоняет сайты заново."""
    sid, ah, calls = await _setup(client, auth_headers, monkeypatch)
    body = {"domains": ["open.example"]}
    await client.post("/api/checks/discovered/probe", headers=auth_headers, json=body)
    assert len(calls) == 1
    r = await client.post("/api/checks/discovered/probe", headers=auth_headers, json=body)
    assert len(calls) == 1 and r.json()["items"][0]["ext_status"] == "up"
    # «перепроверить» дважды подряд — второй раз ничего не перезапускает
    await client.post("/api/checks/discovered/probe", headers=auth_headers,
                      json=dict(body, force=True))
    assert len(calls) == 1

    async with _factory(client)() as s:
        row = await s.get(DomainProbe, "open.example")
        old_req = row.local_request_id
        row.started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        await s.commit()
    r = await client.post("/api/checks/discovered/probe", headers=auth_headers,
                          json=dict(body, force=True))
    assert len(calls) == 2
    async with _factory(client)() as s:
        row = await s.get(DomainProbe, "open.example")
        assert row.local_request_id != old_req

    # опоздавший ответ на прежний запрос новый итог не трогает
    await client.post("/api/agent/site-probe-result", headers=ah, json={"site_probes": [
        {"id": -old_req, "code": 0, "error": 'Get "https://open.example": EOF'},
    ]})
    item = (await client.get("/api/checks/discovered/probes", headers=auth_headers)).json()
    assert item["items"][0]["local_status"] == "pending"

    # итог старше 12 часов мастер не показывает и при открытии проверяет заново
    async with _factory(client)() as s:
        row = await s.get(DomainProbe, "open.example")
        row.started_at = datetime.now(timezone.utc) - timedelta(hours=13)
        await s.commit()
    assert (await client.get("/api/checks/discovered/probes",
                             headers=auth_headers)).json()["items"] == []
    await client.post("/api/checks/discovered/probe", headers=auth_headers, json=body)
    assert len(calls) == 3


async def test_probe_inside_when_agent_cannot_answer(client, auth_headers, monkeypatch):
    sid, ah, calls = await _setup(client, auth_headers, monkeypatch, version="2.6")
    await client.post("/api/checks/discovered/probe", headers=auth_headers,
                      json={"domains": ["closed.example"]})
    # старый агент получает задание отчётом — первым и с частыми отчётами
    cfg = (await client.post("/api/agent/report", json=dict(REPORT, agent_version="2.6"),
                             headers=ah)).json()
    assert cfg["site_probes"][0]["url"] == "https://closed.example"
    assert cfg["interval"] == 2

    # агент так и не ответил — «проверить не удалось», а не вечное ожидание
    async with _factory(client)() as s:
        row = await s.get(DomainProbe, "closed.example")
        row.started_at = datetime.now(timezone.utc) - timedelta(minutes=2)
        await s.commit()
    item = (await client.get("/api/checks/discovered/probes", headers=auth_headers)).json()
    item = item["items"][0]
    assert item["local_status"] == "none" and item["local_kind"] == "no_answer"
    assert "stand-a" in item["local_message"]

    # нода не на связи — изнутри проверять некому, и видно это сразу
    async with _factory(client)() as s:
        srv = await s.get(Server, sid)
        srv.last_seen = datetime.now(timezone.utc) - timedelta(hours=1)
        await s.commit()
    r = await client.post("/api/checks/discovered/probe", headers=auth_headers,
                          json={"domains": ["open.example"]})
    item = r.json()["items"][0]
    assert item["local_status"] == "none" and item["local_kind"] == "offline"
    async with _factory(client)() as s:
        reqs = (await s.scalars(select(ProbeRequest))).all()
        assert [q.url for q in reqs] == ["https://closed.example"]


async def test_adopt_inside_the_server(client, auth_headers, monkeypatch):
    """Вариант «изнутри» из мастера — монитор сразу проверяет агент ноды домена."""
    sid, ah, _ = await _setup(client, auth_headers, monkeypatch)
    r = await client.post("/api/checks/adopt", headers=auth_headers, json={
        "domains": ["open.example", "closed.example", "nowhere.example"],
        # nowhere.example не найден ни на одной ноде — проверять изнутри некому
        "local": ["closed.example", "nowhere.example"],
    })
    assert r.status_code == 201, r.text
    assert r.json()["created"] == 3 and r.json()["local"] == 1
    async with _factory(client)() as s:
        rows = {c.name: c for c in await s.scalars(select(Check))}
    closed = rows["closed.example"]
    assert closed.probe_local and closed.probe_server_id == sid
    assert closed.probe_bound_at is not None and closed.check_locations is False
    assert not rows["open.example"].probe_local
    assert not rows["nowhere.example"].probe_local and rows["nowhere.example"].probe_server_id is None
    # агент получает плановое задание по новому монитору
    tasks = (await client.post("/api/agent/report", json=REPORT, headers=ah)).json()["site_probes"]
    assert [t["id"] for t in tasks] == [closed.id]


async def test_adopt_reports_each_domain(client, auth_headers, monkeypatch):
    """После «Поставить на мониторинг» мастер показывает итог по каждому домену: что
    заведено и как проверяется, что нет и почему. Живой случай (17.09.2026, corp): окно
    оставалось с «создано мониторов: 5» и пустым списком — что добавилось, не понять."""
    await _setup(client, auth_headers, monkeypatch)
    r = await client.post("/api/checks/adopt", headers=auth_headers, json={
        "domains": ["closed.example", "open.example", "watched.example", "*.mask.example",
                    "nowhere.example", "open.example"],
        "local": ["closed.example", "nowhere.example"],
        "group_name": "CORP",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["group_name"] == "CORP"
    async with _factory(client)() as s:
        ids = {c.target: c.id for c in await s.scalars(select(Check))}
    # порядок — как в запросе, повтор домена второй строки не даёт
    assert body["items"] == [
        {"domain": "closed.example", "check_id": ids["https://closed.example"], "local": True,
         "server": "stand-a", "reason": "", "problem": "", "path": "",
         "expected_status": ""},
        {"domain": "open.example", "check_id": ids["https://open.example"], "local": False,
         "server": "", "reason": "", "problem": "", "path": "",
         "expected_status": ""},
        {"domain": "watched.example", "check_id": ids["https://watched.example"],
         "local": False, "server": "", "reason": "monitored", "problem": "", "path": "",
         "expected_status": ""},
        {"domain": "*.mask.example", "check_id": 0, "local": False, "server": "",
         "reason": "invalid", "problem": "маска — нет конкретного хоста", "path": "",
         "expected_status": ""},
        # изнутри просили, но домен не найден ни на одной ноде — проверяет панель
        {"domain": "nowhere.example", "check_id": ids["https://nowhere.example"],
         "local": False, "server": "", "reason": "", "problem": "", "path": "",
         "expected_status": ""},
    ]


async def test_viewer_cannot_probe(client, auth_headers, monkeypatch):
    await _setup(client, auth_headers, monkeypatch)
    r = await client.post("/api/users", headers=auth_headers, json={
        "username": "watcher", "password": "watcher-pass-2026", "role": "viewer",
    })
    assert r.status_code in (200, 201), r.text
    login = await client.post("/api/auth/login",
                              json={"username": "watcher", "password": "watcher-pass-2026"})
    vh = {"Authorization": f"Bearer {login.json()['access_token']}"}
    r = await client.post("/api/checks/discovered/probe", headers=vh,
                          json={"domains": ["open.example"]})
    assert r.status_code == 403


def test_kind_names_the_problem():
    k = domain_probe._kind
    assert k("down", "доступ запрещён (HTTP 403)") == ("http", 403)
    assert k("up", "HTTP 200 · 85 мс") == ("", 200)
    assert k("down", "домен не находится в DNS ([Errno -2] Name or service not known)")[0] == "dns"
    assert k("down", "сервер не ответил на попытку соединения (ConnectTimeout)")[0] == "timeout"
    assert k("down", "TLS-сертификат истёк (certificate has expired)")[0] == "tls"
    assert k("down", "сервер отклонил соединение — порт закрыт (connection refused)")[0] == "refused"
    assert k("down", "не удалось подключиться ни по одному адресу (All connection attempts failed)")[0] == "connect"
    # изнутри: белый список и «на localhost никто не слушает» — разные беды
    assert k("down", "сервер оборвал запрос изнутри: похоже, белый список сайта — "
                     "добавьте в него 127.0.0.1/32 (Get: EOF)")[0] == "reset"
    assert k("down", "изнутри сервера порт 443 закрыт — веб-сервер не слушает localhost "
                     "(так бывает у Kubernetes), локальная проверка здесь не сработает "
                     "(dial tcp 127.0.0.1:443: connect: connection refused)")[0] == "refused"
    assert k("down", "что-то непонятное")[0] == "other"


# Живой случай (22.10.2026): MCP-сервер на найденном домене на "/" отвечает 404, мастер
# показывал его мертвым снаружи и изнутри. Сервис жив, главной у него просто нет.
SERVICES = {
    # главная 404, жив /api, а /mcp на GET отвечает 406
    "https://api.example.com": 404, "https://api.example.com/api": 200,
    "https://api.example.com/mcp": 406,
    # только MCP: GET на /mcp - 406, остальное 404
    "https://mcp.example.com": 404, "https://mcp.example.com/mcp": 406,
    # на все подряд 400 - это не MCP-сервер, а прокси, которому что-то не нравится
    "https://strict.example.com": 400, "https://strict.example.com/mcp": 400,
    # главной нет, ничего типового тоже
    "https://gone.example.com": 404,
}
SERVICE_SITES = ["open.example", "closed.example", "api.example.com", "mcp.example.com",
                 "strict.example.com", "gone.example.com"]


async def _setup_services(client, auth_headers, monkeypatch):
    sid, ah, _ = await _setup(client, auth_headers, monkeypatch)
    report = dict(REPORT, web_services=[{"kind": "caddy", "sites": SERVICE_SITES}])
    await client.post("/api/agent/report", json=report, headers=ah)
    calls: list[str] = []

    async def fake_run(check):
        calls.append(check.target)
        if check.target == "https://open.example":
            return checks_exec.CheckOutcome("up", latency_ms=85, message="HTTP 200 · 85 мс")
        if check.target.startswith("https://closed.example"):
            return checks_exec.CheckOutcome(
                "down", message="сервер оборвал соединение (Connection reset by peer)")
        root = "/".join(check.target.split("/")[:3])
        code = SERVICES.get(check.target, 400 if root.startswith("https://strict.") else 404)
        if check.target != root:
            # типовой путь: короткий таймаут, годен только 2xx
            assert check.timeout_ms == domain_probe.PATH_TIMEOUT_MS
            assert check.expected_status == "200-299"
        if checks_exec.status_matches(code, check.expected_status):
            return checks_exec.CheckOutcome("up", latency_ms=9, message=f"HTTP {code} · 9 мс")
        return checks_exec.CheckOutcome("down", latency_ms=9, message=f"HTTP {code}")

    monkeypatch.setattr("app.checks.run_check", fake_run)
    return sid, ah, calls


async def test_probe_suggests_typical_path_outside(client, auth_headers, monkeypatch):
    sid, ah, calls = await _setup_services(client, auth_headers, monkeypatch)
    r = await client.post("/api/checks/discovered/probe", headers=auth_headers,
                          json={"domains": SERVICE_SITES})
    assert r.status_code == 200, r.text
    items = _by_domain((await client.get("/api/checks/discovered/probes",
                                         headers=auth_headers)).json()["items"])
    api = items["api.example.com"]
    # главная по-прежнему "не открылась" - подсказка идет отдельно
    assert api["ext_status"] == "down" and api["ext_code"] == 404
    assert api["ext_hint"] == {"path": "/api", "code": 200, "expect": ""}
    assert items["mcp.example.com"]["ext_hint"] == {
        "path": "/mcp", "code": 406, "expect": domain_probe.MCP_EXPECT}
    assert items["strict.example.com"]["ext_hint"] is None
    assert items["gone.example.com"]["ext_hint"] is None
    assert items["open.example"]["ext_hint"] is None
    # пути пробуют только там, где сервер ответил, а главная не подошла
    assert not any(c.startswith(("https://open.example/", "https://closed.example/"))
                   for c in calls)
    assert sorted(c for c in calls if c.startswith("https://gone.example.com/")) == sorted(
        f"https://gone.example.com{p}" for p in domain_probe.PATHS)

    domains = ["api.example.com", "mcp.example.com", "gone.example.com", "open.example"]
    r = await client.post("/api/checks/adopt", headers=auth_headers,
                          json={"domains": domains, "paths": domains})
    assert r.status_code == 201, r.text
    got = {i["domain"]: (i["path"], i["expected_status"]) for i in r.json()["items"]}
    assert got == {
        "api.example.com": ("/api", ""),
        "mcp.example.com": ("/mcp", domain_probe.MCP_EXPECT),
        "gone.example.com": ("", ""),
        "open.example": ("", ""),
    }
    async with _factory(client)() as s:
        checks = {c.name: c for c in await s.scalars(select(Check))}
    assert checks["api.example.com"].target == "https://api.example.com/api"
    assert checks["api.example.com"].expected_status == "200-399"
    mcp = checks["mcp.example.com"]
    assert mcp.target == "https://mcp.example.com/mcp"
    assert mcp.expected_status == domain_probe.MCP_EXPECT
    # MCP-монитор: 406 - жив, 404 и 5xx - нет
    assert checks_exec.status_matches(406, mcp.expected_status)
    assert not checks_exec.status_matches(404, mcp.expected_status)
    assert not checks_exec.status_matches(502, mcp.expected_status)
    assert checks["gone.example.com"].target == "https://gone.example.com"
    # монитор с путем - все тот же домен: мастер больше его не предлагает
    hosts = (await client.get("/api/checks/known-hosts", headers=auth_headers)).json()["hosts"]
    assert hosts["api.example.com"] == checks["api.example.com"].id


async def test_adopt_without_paths_keeps_root(client, auth_headers, monkeypatch):
    """Путь подобран, но человек снял выбор - монитор на главной, как раньше."""
    await _setup_services(client, auth_headers, monkeypatch)
    await client.post("/api/checks/discovered/probe", headers=auth_headers,
                      json={"domains": ["api.example.com"]})
    r = await client.post("/api/checks/adopt", headers=auth_headers,
                          json={"domains": ["api.example.com"]})
    assert r.json()["items"][0]["path"] == ""
    async with _factory(client)() as s:
        chk = (await s.scalars(select(Check).where(Check.name == "api.example.com"))).one()
    assert chk.target == "https://api.example.com"


async def test_probe_suggests_typical_path_inside(client, auth_headers, monkeypatch):
    """Сайт за белым списком, у которого и изнутри главная 404: пути перебирает агент."""
    sid, ah, calls = await _setup_services(client, auth_headers, monkeypatch)
    await client.post("/api/checks/discovered/probe", headers=auth_headers,
                      json={"domains": ["closed.example"]})
    tasks = (await client.get("/api/agent/commands", headers=ah)).json()["site_probes"]
    assert [t["url"] for t in tasks] == ["https://closed.example"]
    await client.post("/api/agent/site-probe-result", headers=ah, json={"site_probes": [
        {"id": tasks[0]["id"], "code": 404, "latency_ms": 3},
    ]})
    item = (await client.get("/api/checks/discovered/probes", headers=auth_headers)).json()
    item = item["items"][0]
    assert item["local_status"] == "down" and item["local_code"] == 404
    assert item["local_hint"] is None and item["local_hint_pending"] is True

    tasks = (await client.get("/api/agent/commands", headers=ah)).json()["site_probes"]
    assert sorted(t["url"] for t in tasks) == sorted(
        f"https://closed.example{p}" for p in domain_probe.PATHS)
    assert all(t["timeout_ms"] == domain_probe.PATH_TIMEOUT_MS for t in tasks)
    codes = {"https://closed.example/healthz": 200, "https://closed.example/docs": 200}
    r = await client.post("/api/agent/site-probe-result", headers=ah, json={"site_probes": [
        {"id": t["id"], "code": codes.get(t["url"], 404), "latency_ms": 2} for t in tasks
    ]})
    assert r.status_code == 200, r.text
    item = (await client.get("/api/checks/discovered/probes", headers=auth_headers)).json()
    item = item["items"][0]
    assert item["local_hint"] == {"path": "/healthz", "code": 200, "expect": ""}
    assert item["local_hint_pending"] is False
    # ответ про путь - не вердикт домена
    assert item["local_status"] == "down"

    r = await client.post("/api/checks/adopt", headers=auth_headers, json={
        "domains": ["closed.example"], "local": ["closed.example"], "paths": ["closed.example"],
    })
    assert r.json()["items"][0]["path"] == "/healthz"
    async with _factory(client)() as s:
        chk = (await s.scalars(select(Check).where(Check.name == "closed.example"))).one()
    assert chk.target == "https://closed.example/healthz" and chk.probe_local


async def test_inside_paths_give_up_when_agent_is_silent(client, auth_headers, monkeypatch):
    sid, ah, _ = await _setup_services(client, auth_headers, monkeypatch)
    await client.post("/api/checks/discovered/probe", headers=auth_headers,
                      json={"domains": ["closed.example"]})
    tasks = (await client.get("/api/agent/commands", headers=ah)).json()["site_probes"]
    await client.post("/api/agent/site-probe-result", headers=ah, json={"site_probes": [
        {"id": tasks[0]["id"], "code": 404, "latency_ms": 3},
    ]})
    async with _factory(client)() as s:
        row = await s.get(DomainProbe, "closed.example")
        row.local_ts = datetime.now(timezone.utc) - timedelta(minutes=5)
        await s.commit()
    item = (await client.get("/api/checks/discovered/probes", headers=auth_headers)).json()
    item = item["items"][0]
    assert item["local_hint"] is None and item["local_hint_pending"] is False


def test_pick_prefers_health_over_mcp():
    pick = domain_probe.pick
    assert pick(404, {"/mcp": 406, "/docs": 200}) == {"path": "/docs", "code": 200, "expect": ""}
    assert pick(404, {"/health": 204, "/ping": 200})["path"] == "/health"
    # 400 на все подряд - не признак MCP-сервера
    assert pick(400, {"/mcp": 400}) == {}
    assert pick(404, {"/mcp": 401})["expect"] == domain_probe.MCP_EXPECT
    assert pick(404, {"/api": 301, "/mcp": 404}) == {}
    assert domain_probe.wants_paths("down", "не найдено (HTTP 404)")
    assert not domain_probe.wants_paths("down", "слишком много запросов (HTTP 429)")
    assert not domain_probe.wants_paths("down", "сервер не ответил (ConnectTimeout)")
    assert not domain_probe.wants_paths("up", "HTTP 200 · 5 мс")
