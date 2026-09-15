"""Мастер «Домены, найденные на серверах»: разовая проверка перед постановкой на мониторинг.

Живой случай (15.09.2026, msergeev.ru): мастер предлагал 11 доменов fi-hz-aff, а
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
    r = await client.post("/api/servers", json={"name": "fi-hz-aff"}, headers=auth_headers)
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
    assert all(i["local_status"] == "pending" and i["local_server"] == "fi-hz-aff"
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
    assert "fi-hz-aff" in item["local_message"]

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
    assert k("down", "сервер оборвал соединение изнутри: белый список сайта не пускает "
                     "локальный запрос — добавьте в него 127.0.0.1/32 (Get: EOF)")[0] == "reset"
    assert k("down", "изнутри сервера порт 443 закрыт: веб-сервер не слушает localhost "
                     "(так бывает у Kubernetes) (dial tcp 127.0.0.1:443: connect: "
                     "connection refused)")[0] == "refused"
    assert k("down", "что-то непонятное")[0] == "other"
