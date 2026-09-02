import asyncio
import types

import httpx
import pytest

from app import checks as checks_exec


# --- чистая логика ---

def test_status_matches():
    assert checks_exec.status_matches(200, "200-399")
    assert checks_exec.status_matches(200, "")  # дефолт 200-399
    assert not checks_exec.status_matches(404, "200-399")
    assert checks_exec.status_matches(301, "200-299,301")
    assert not checks_exec.status_matches(500, "200-299,301")
    assert checks_exec.status_matches(204, "204")


def test_cert_days_left():
    import ssl
    from datetime import datetime, timedelta, timezone

    future = datetime.now(timezone.utc) + timedelta(days=30)
    not_after = future.strftime("%b %d %H:%M:%S %Y GMT")
    days = checks_exec._cert_days_left(not_after)
    assert 28 < days < 31


# --- tcp_port исполнитель против локального сервера ---

async def test_tcp_port_up():
    async def handle(reader, writer):
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    check = types.SimpleNamespace(
        target="127.0.0.1", port=port, timeout_ms=3000, degraded_ms=2000
    )
    outcome = await checks_exec._run_tcp_port(check)
    server.close()
    await server.wait_closed()
    assert outcome.status in ("up", "degraded")
    assert outcome.latency_ms is not None


async def test_tcp_port_down():
    check = types.SimpleNamespace(
        target="127.0.0.1", port=1, timeout_ms=1500, degraded_ms=2000
    )
    outcome = await checks_exec._run_tcp_port(check)
    assert outcome.status == "down"


async def test_run_check_unknown_type():
    check = types.SimpleNamespace(type="nope")
    outcome = await checks_exec.run_check(check)
    assert outcome.status == "down"


async def test_run_http_https_fallback_to_http(monkeypatch):
    # домен без схемы → пробуем https, при ошибке подключения фолбэк на http
    calls: list[str] = []

    class FakeResp:
        status_code = 200
        text = ""

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            calls.append(url)
            if url.startswith("https://"):
                # реальный кейс http-only сайта: TCP есть, но https рвёт соединение
                raise httpx.RemoteProtocolError("Server disconnected")
            return FakeResp()

    monkeypatch.setattr("app.checks.httpx.AsyncClient", FakeClient)
    check = types.SimpleNamespace(
        target="play-win.casino", timeout_ms=3000, degraded_ms=2000,
        method="GET", keyword_up="", keyword_down="", expected_status="200-399",
    )
    outcome = await checks_exec._run_http(check)
    # HTTPS не поднялся, отвечает только HTTP → это degraded (а не «всё ок»)
    assert outcome.status == "degraded"
    assert "HTTPS" in outcome.message
    assert calls == ["https://play-win.casino", "http://play-win.casino"]

    # но если схему указали явно (http://) — фолбэка нет, это осознанный http → up
    calls.clear()
    check_http = types.SimpleNamespace(
        target="http://play-win.casino", timeout_ms=3000, degraded_ms=2000,
        method="GET", keyword_up="", keyword_down="", expected_status="200-399",
    )
    outcome = await checks_exec._run_http(check_http)
    assert outcome.status == "up"
    assert calls == ["http://play-win.casino"]


async def test_run_http_basic_auth_passed(monkeypatch):
    """auth_method=basic → в httpx.AsyncClient уходит BasicAuth с логином/паролем
    (сайт за 401 начинает отвечать 200)."""
    seen: dict = {}

    class FakeResp:
        status_code = 200
        text = ""

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            return FakeResp()

    monkeypatch.setattr("app.checks.httpx.AsyncClient", FakeClient)
    check = types.SimpleNamespace(
        target="https://db.example.com", timeout_ms=3000, degraded_ms=2000,
        method="GET", keyword_up="", keyword_down="", expected_status="200-399",
        auth_method="basic", auth_user="probe", auth_pass="secretpass",
    )
    outcome = await checks_exec._run_http(check)
    assert outcome.status == "up"
    auth = seen.get("auth")
    assert isinstance(auth, httpx.BasicAuth)
    # BasicAuth хранит заголовок Authorization: Basic base64(user:pass)
    import base64
    token = base64.b64encode(b"probe:secretpass").decode()
    assert auth._auth_header == f"Basic {token}"

    # без auth_method — auth не передаётся (None)
    seen.clear()
    check2 = types.SimpleNamespace(
        target="https://x", timeout_ms=3000, degraded_ms=2000,
        method="GET", keyword_up="", keyword_down="", expected_status="200-399",
        auth_method="", auth_user="", auth_pass="",
    )
    await checks_exec._run_http(check2)
    assert seen.get("auth") is None


async def test_run_http_all_ips_aggregates(monkeypatch):
    """check_all_ips: проверяются все A-адреса; один мёртвый бэкенд → монитор down
    с указанием IP, при этом Host/SNI = домен (TLS по домену)."""
    seen: list[dict] = []

    class FakeResp:
        def __init__(self, code):
            self.status_code = code
            self.text = ""

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            seen.append({"url": url, "ext": kw.get("extensions")})
            # 10.0.0.2 — мёртвый бэкенд (500), остальные 200
            return FakeResp(500 if "10.0.0.2" in url else 200)

    async def fake_resolve(host, port):
        return ["10.0.0.1", "10.0.0.2"]

    monkeypatch.setattr("app.checks.httpx.AsyncClient", FakeClient)
    monkeypatch.setattr("app.checks._resolve_ips", fake_resolve)
    check = types.SimpleNamespace(
        target="https://site.example", timeout_ms=3000, degraded_ms=2000,
        method="GET", keyword_up="", keyword_down="", expected_status="200-399",
        auth_method="", auth_user="", auth_pass="", check_all_ips=True,
    )
    outcome = await checks_exec._run_http(check)
    assert outcome.status == "down"
    assert "10.0.0.2" in outcome.message and "1/2" in outcome.message
    # оба адреса опрошены, каждый с SNI = домен
    assert {s["url"] for s in seen} == {"https://10.0.0.1", "https://10.0.0.2"}
    assert all(s["ext"] == {"sni_hostname": "site.example"} for s in seen)

    # все живы → up с числом адресов
    monkeypatch.setattr(
        "app.checks._resolve_ips",
        lambda host, port: _aiolist(["10.0.0.1", "10.0.0.3"]),
    )
    seen.clear()
    outcome = await checks_exec._run_http(check)
    assert outcome.status == "up" and "2" in outcome.message

    # один адрес → None-ветка → обычная одиночная проверка (не падаем)
    monkeypatch.setattr("app.checks._resolve_ips", lambda host, port: _aiolist(["10.0.0.1"]))
    outcome = await checks_exec._run_http(check)
    assert outcome.status == "up"


async def _aiolist(v):
    return v


async def test_gather_capped_limits_concurrency():
    """_gather_capped не запускает больше limit одновременно и сохраняет порядок."""
    from app import collector

    active = 0
    peak = 0

    async def job(i):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return i

    res = await collector._gather_capped([job(i) for i in range(20)], limit=4, jitter_s=0)
    assert res == list(range(20))  # порядок сохранён
    assert peak <= 4  # потолок соблюдён


# --- API CRUD (исполнитель замокан, без сети) ---

@pytest.fixture
def _fake_runner(monkeypatch):
    async def fake(check):
        return checks_exec.CheckOutcome("up", latency_ms=42, value=None, message="ok")

    monkeypatch.setattr("app.checks.run_check", fake)


async def test_checks_crud(client: httpx.AsyncClient, auth_headers, _fake_runner):
    # create
    body = {"name": "Example", "type": "http", "target": "https://example.com"}
    r = await client.post("/api/checks", json=body, headers=auth_headers)
    assert r.status_code == 201
    c = r.json()
    cid = c["id"]
    # первая проверка теперь в фоне (чтобы создание не висело) → прогоним явно
    r = await client.post(f"/api/checks/{cid}/run", headers=auth_headers)
    assert r.json()["last_status"] == "up" and r.json()["last_latency_ms"] == 42

    # list + overview
    r = await client.get("/api/checks", headers=auth_headers)
    assert r.status_code == 200 and len(r.json()) == 1
    r = await client.get("/api/checks/overview", headers=auth_headers)
    ov = r.json()
    assert ov["total"] == 1 and ov["up"] == 1

    # get
    r = await client.get(f"/api/checks/{cid}", headers=auth_headers)
    assert r.status_code == 200 and r.json()["name"] == "Example"

    # patch
    r = await client.patch(
        f"/api/checks/{cid}", json={"name": "Renamed", "interval_seconds": 120},
        headers=auth_headers,
    )
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    assert r.json()["interval_seconds"] == 120

    # run now → пишет ещё один снимок
    r = await client.post(f"/api/checks/{cid}/run", headers=auth_headers)
    assert r.status_code == 200 and r.json()["last_status"] == "up"

    # history (снимки бинируются по времени → create+run попадают в один бакет)
    r = await client.get(f"/api/checks/{cid}/history", headers=auth_headers)
    assert r.status_code == 200
    assert len(r.json()["points"]) >= 1
    assert r.json()["points"][-1]["status"] == "up"

    # delete
    r = await client.delete(f"/api/checks/{cid}", headers=auth_headers)
    assert r.status_code == 204
    r = await client.get("/api/checks", headers=auth_headers)
    assert r.json() == []


async def test_overview_counts_disabled_separately(
    client: httpx.AsyncClient, auth_headers, _fake_runner
):
    # включённый + выключенный: статусные счётчики видят только включённый,
    # выключенный попадает в отдельный counter disabled
    r = await client.post(
        "/api/checks",
        json={"name": "on", "type": "http", "target": "https://on.com"},
        headers=auth_headers,
    )
    on_id = r.json()["id"]
    await client.post(f"/api/checks/{on_id}/run", headers=auth_headers)
    await client.post(
        "/api/checks",
        json={"name": "off", "type": "http", "target": "https://off.com",
              "enabled": False},
        headers=auth_headers,
    )
    ov = (await client.get("/api/checks/overview", headers=auth_headers)).json()
    assert ov["total"] == 2
    assert ov["disabled"] == 1
    assert ov["up"] == 1 and ov["unknown"] == 0  # выключенный не в статусных


async def test_bulk_delete(client: httpx.AsyncClient, auth_headers, _fake_runner):
    ids = []
    for name in ("d1", "d2", "keep"):
        r = await client.post(
            "/api/checks",
            json={"name": name, "type": "http", "target": f"https://{name}.com"},
            headers=auth_headers,
        )
        ids.append(r.json()["id"])
    r = await client.post(
        "/api/checks/bulk-delete", json={"ids": ids[:2]}, headers=auth_headers
    )
    assert r.status_code == 200 and r.json()["updated"] == 2
    left = (await client.get("/api/checks", headers=auth_headers)).json()
    assert [c["name"] for c in left] == ["keep"]
    # несуществующие id — просто 0, не ошибка
    r = await client.post(
        "/api/checks/bulk-delete", json={"ids": [99999]}, headers=auth_headers
    )
    assert r.status_code == 200 and r.json()["updated"] == 0


async def test_domain_days_cached_dedup(monkeypatch):
    # 100 мониторов одного registrable-домена → ОДИН фактический запрос к
    # регистратуре (кэш+очередь), а не шквал, за который rdap.org отвечает 429
    import asyncio
    calls = []

    async def fake(domain, timeout):
        calls.append(domain)
        return 250, "ok"

    monkeypatch.setattr(checks_exec, "_domain_days", fake)
    monkeypatch.setattr(checks_exec, "_DOMAIN_GAP_S", 0.0)  # без пауз в тесте
    checks_exec._DOMAIN_CACHE.clear()
    results = await asyncio.gather(
        *[checks_exec._domain_days_cached("example.com", 10.0) for _ in range(100)]
    )
    assert all(r == (250, "ok") for r in results)
    assert len(calls) == 1  # один реальный запрос на всех

    # ошибка кэшируется (негативный кэш) — повторный вызов не долбит регистратуру
    async def fail(domain, timeout):
        calls.append(domain)
        return None, "RDAP HTTP 429"

    monkeypatch.setattr(checks_exec, "_domain_days", fail)
    checks_exec._DOMAIN_CACHE.clear()
    await checks_exec._domain_days_cached("x.com", 10.0)
    await checks_exec._domain_days_cached("x.com", 10.0)
    assert calls.count("x.com") == 1
    checks_exec._DOMAIN_CACHE.clear()


async def test_checks_require_auth(client: httpx.AsyncClient):
    assert (await client.get("/api/checks")).status_code == 401
    assert (await client.post("/api/checks", json={})).status_code == 401


async def test_check_validation(client: httpx.AsyncClient, auth_headers, _fake_runner):
    # неизвестный тип → 422
    r = await client.post(
        "/api/checks", json={"name": "x", "type": "ping", "target": "a"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    # порт вне диапазона → 422
    r = await client.post(
        "/api/checks",
        json={"name": "x", "type": "tcp_port", "target": "a", "port": 99999},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_checks_reorder(client: httpx.AsyncClient, auth_headers, _fake_runner):
    # создаём три монитора; новые добавляются в конец (sort_order по возрастанию)
    ids = []
    for name in ("A", "B", "C"):
        r = await client.post(
            "/api/checks",
            json={"name": name, "type": "http", "target": f"https://{name}.com"},
            headers=auth_headers,
        )
        assert r.status_code == 201
        ids.append(r.json()["id"])

    # порядок по умолчанию — как создавали
    r = await client.get("/api/checks", headers=auth_headers)
    assert [c["id"] for c in r.json()] == ids

    # переставляем: C, A, B
    new_order = [ids[2], ids[0], ids[1]]
    r = await client.post(
        "/api/checks/reorder",
        json={"order": [{"id": i} for i in new_order]},
        headers=auth_headers,
    )
    assert r.status_code == 200 and r.json()["updated"] >= 1

    # и list, и overview отдают новый порядок
    r = await client.get("/api/checks", headers=auth_headers)
    assert [c["id"] for c in r.json()] == new_order
    r = await client.get("/api/checks/overview", headers=auth_headers)
    assert [c["id"] for c in r.json()["checks"]] == new_order

    # новый монитор добавляется в конец нового порядка, а не наверх
    r = await client.post(
        "/api/checks",
        json={"name": "D", "type": "http", "target": "https://d.com"},
        headers=auth_headers,
    )
    did = r.json()["id"]
    r = await client.get("/api/checks", headers=auth_headers)
    assert [c["id"] for c in r.json()] == new_order + [did]


async def test_reorder_moves_between_groups(
    client: httpx.AsyncClient, auth_headers, _fake_runner
):
    # два монитора в группе «Прод»
    ids = []
    for name in ("X", "Y"):
        r = await client.post(
            "/api/checks",
            json={
                "name": name,
                "type": "http",
                "target": f"https://{name}.com",
                "group_name": "Прод",
            },
            headers=auth_headers,
        )
        ids.append(r.json()["id"])

    # переносим Y в группу «Дев» одним reorder-запросом
    r = await client.post(
        "/api/checks/reorder",
        json={
            "order": [
                {"id": ids[0], "group_name": "Прод"},
                {"id": ids[1], "group_name": "Дев"},
            ]
        },
        headers=auth_headers,
    )
    assert r.status_code == 200

    r = await client.get("/api/checks", headers=auth_headers)
    by_id = {c["id"]: c for c in r.json()}
    assert by_id[ids[0]]["group_name"] == "Прод"
    assert by_id[ids[1]]["group_name"] == "Дев"


async def test_reorder_requires_auth(client: httpx.AsyncClient):
    body = {"order": [{"id": 1}]}
    assert (await client.post("/api/checks/reorder", json=body)).status_code == 401


async def test_history_by_ip(client, auth_headers, monkeypatch):
    """Ручной прогон монитора с check_all_ips пишет сэмплы по IP, а
    /history?ip=<ip> отдаёт тайм-серию именно этого адреса."""
    async def fake_run(check):
        return checks_exec.CheckOutcome(
            "up", latency_ms=50, message="все 2 адреса OK",
            ip_results=[
                {"ip": "10.0.0.1", "status": "up", "latency_ms": 50, "message": "HTTP 200"},
                {"ip": "10.0.0.2", "status": "up", "latency_ms": 90, "message": "HTTP 200"},
            ],
        )

    monkeypatch.setattr("app.checks.run_check", fake_run)
    r = await client.post(
        "/api/checks", json={"name": "m", "type": "http", "target": "https://x",
                             "check_all_ips": True}, headers=auth_headers,
    )
    cid = r.json()["id"]
    assert r.json()["check_all_ips"] is True
    await client.post(f"/api/checks/{cid}/run", headers=auth_headers)

    r = await client.get(f"/api/checks/{cid}/history?ip=10.0.0.1", headers=auth_headers)
    assert r.status_code == 200
    pts = r.json()["points"]
    assert len(pts) == 1 and pts[0]["latency_ms"] == 50  # только 10.0.0.1
    # другой IP — своя серия
    r = await client.get(f"/api/checks/{cid}/history?ip=10.0.0.2", headers=auth_headers)
    assert r.json()["points"][0]["latency_ms"] == 90


async def test_bulk_toggle_check_locations(client, auth_headers, _fake_runner):
    """PATCH /checks/bulk с check_locations=false выключает локации у всех мониторов
    (массовый переключатель). Новый монитор по умолчанию — без локаций."""
    # новый монитор: check_locations по умолчанию False
    r = await client.post(
        "/api/checks", json={"name": "a", "type": "http", "target": "https://a"},
        headers=auth_headers,
    )
    assert r.json()["check_locations"] is False
    # включим у одного вручную
    await client.patch(f"/api/checks/{r.json()['id']}",
                       json={"check_locations": True}, headers=auth_headers)
    await client.post(
        "/api/checks", json={"name": "b", "type": "http", "target": "https://b",
                             "check_locations": True}, headers=auth_headers,
    )
    # массово выключим локации у всех
    r = await client.patch("/api/checks/bulk",
                           json={"check_locations": False}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["updated"] >= 2
    checks = (await client.get("/api/checks", headers=auth_headers)).json()
    assert all(c["check_locations"] is False for c in checks)


async def test_bulk_apply_to_selected_ids(client, auth_headers, _fake_runner):
    """PATCH /checks/bulk с ids=[…] применяет настройки ТОЛЬКО к выбранным."""
    made = []
    for n in ("a", "b", "c"):
        r = await client.post(
            "/api/checks", json={"name": n, "type": "http", "target": f"https://{n}"},
            headers=auth_headers,
        )
        made.append(r.json()["id"])
    # включим check_all_ips только у первых двух
    r = await client.patch(
        "/api/checks/bulk",
        json={"ids": made[:2], "check_all_ips": True, "interval_seconds": 300},
        headers=auth_headers,
    )
    assert r.status_code == 200 and r.json()["updated"] == 2
    checks = {c["id"]: c for c in (await client.get("/api/checks", headers=auth_headers)).json()}
    assert checks[made[0]]["check_all_ips"] is True and checks[made[0]]["interval_seconds"] == 300
    assert checks[made[1]]["check_all_ips"] is True
    assert checks[made[2]]["check_all_ips"] is False  # третий не тронут
    assert checks[made[2]]["interval_seconds"] == 60


async def test_run_http_custom_headers(monkeypatch):
    """http_headers (JSON) → уходят в запрос (напр. x-application-token для 401)."""
    seen: dict = {}

    class FakeResp:
        status_code = 200
        text = ""

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kw):
            return FakeResp()

    monkeypatch.setattr("app.checks.httpx.AsyncClient", FakeClient)
    check = types.SimpleNamespace(
        target="https://app.example.com", timeout_ms=3000, degraded_ms=2000,
        method="GET", keyword_up="", keyword_down="", expected_status="200-399",
        auth_method="", auth_user="", auth_pass="", check_all_ips=False,
        http_headers='{"x-application-token": "TOK123"}',
    )
    outcome = await checks_exec._run_http(check)
    assert outcome.status == "up"
    assert seen["headers"].get("x-application-token") == "TOK123"
    assert seen["headers"].get("User-Agent")  # базовый UA сохранён

    # невалидный JSON → просто игнорируется (не роняет проверку)
    assert checks_exec._parse_headers("не json") == {}
    assert checks_exec._parse_headers('["a","b"]') == {}
    assert checks_exec._parse_headers('{"k": 7}') == {"k": "7"}


async def test_cert_monitor_fills_ssl_days(tmp_path, monkeypatch):
    """У монитора типа «сертификат» срок должен доезжать до ssl_days.

    Дни приходят в value самой проверки, а отдельный проход по срокам ходит
    только к http-мониторам — из-за этого чип срока в списке, блок «истекает»
    на главной и группировка по домену для cert-мониторов молчали, хотя данные
    были посчитаны.
    """
    from sqlalchemy import select

    from app import checks as checks_exec
    from app import collector
    from app.checks import CheckOutcome
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Check

    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{(tmp_path / 'cert.db').as_posix()}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Check(name="cert", type="cert", target="example.com"))
        await s.commit()

    async def fake_run(check):
        return CheckOutcome("up", value=42.7, message="сертификат: 42 дн. до истечения")

    monkeypatch.setattr(checks_exec, "run_check", fake_run)
    await collector.run_due_checks(factory, Settings())

    async with factory() as s:
        row = await s.scalar(select(Check))
        assert row.ssl_days == 42, f"ssl_days={row.ssl_days}, last_value={row.last_value}"
        assert row.expiry_checked_at is not None
    await engine.dispose()


async def test_agent_probe_outcome():
    """Оценка локальной проверки: те же правила, что и у обычной.

    Сайт за белым списком панель проверить не может — снаружи соединение рвут,
    и монитор вечно «недоступен», хотя сайт жив. Результат снимает агент на самом
    сервере; здесь проверяется, что вердикт по нему выносится тем же кодом.
    """
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from app import checks as checks_exec

    now = datetime.now(timezone.utc)
    check = SimpleNamespace(
        expected_status="200-399", keyword_up="", keyword_down="",
        interval_seconds=60, degraded_ms=2000,
    )
    probe = SimpleNamespace(ts=now, code=200, latency_ms=12, error="",
                            kw_up_found=True, kw_down_found=False)

    assert checks_exec.outcome_from_agent(check, probe, now, 2000).status == "up"

    # ошибка с ноды переводится тем же словарём, что и своя
    probe.error = "dial tcp 127.0.0.1:443: connect: connection refused"
    out = checks_exec.outcome_from_agent(check, probe, now, 2000)
    assert out.status == "down" and "172.16.0.0/12" in out.message  # подсказываем, что добавить

    # молчащий агент — это отказ проверки, а не «сайт работает»
    probe.error = ""
    stale = SimpleNamespace(ts=now - timedelta(minutes=30), code=200, latency_ms=10,
                            error="", kw_up_found=True, kw_down_found=False)
    out = checks_exec.outcome_from_agent(check, stale, now, 2000)
    assert out.status == "down" and "не присылает" in out.message

    # результата ещё не было — тоже не «up»
    check.probe_server_id = 7
    assert checks_exec.outcome_from_agent(check, None, now, 2000).status == "down"

    # проверять некому (домена нет ни на одной ноде) — это ДРУГАЯ беда, и лечится
    # она не на ноде, а установкой агента или снятием галочки
    check.probe_server_id = None
    out = checks_exec.outcome_from_agent(check, None, now, 2000)
    assert out.status == "down" and "некому" in out.message

    # ключевое слово ищет агент, панель верит флагу
    check.keyword_up = "Добро пожаловать"
    probe.kw_up_found = False
    assert checks_exec.outcome_from_agent(check, probe, now, 2000).status == "down"
    probe.kw_up_found = True
    assert checks_exec.outcome_from_agent(check, probe, now, 2000).status == "up"

    # 403 снаружи — норма для таких сайтов, но ИЗНУТРИ это всё-таки отказ:
    # правило кодов общее, никаких поблажек локальной проверке
    check.keyword_up = ""
    probe.code = 403
    assert checks_exec.outcome_from_agent(check, probe, now, 2000).status == "down"


async def test_local_probe_rebinding(tmp_path):
    """Галочку ставит человек, ноду находит панель — и находит заново при переезде.

    Выбор конкретной ноды был бы лишней работой: панель и так знает, чьи веб-серверы
    держат домен. А жёсткая привязка молча устаревала бы, когда сайт переезжает.
    """
    from app import collector
    from app.db import create_engine_and_factory
    from app.models import Base, Check, Server
    from sqlalchemy import select

    db = (tmp_path / "lp.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add(Check(name="панель", type="http", target="https://panel.example.ru",
                    enabled=True, probe_local=True))
        s.add(Server(name="node-a", token_hash="a", enabled=True,
                     last_report={"web_services": [{"kind": "nginx", "sites": ["panel.example.ru"]}]}))
        s.add(Server(name="node-b", token_hash="b", enabled=True, last_report={}))
        await s.commit()

    assert await collector.rebind_local_probes(factory) == 1
    async with factory() as s:
        chk = (await s.scalars(select(Check))).one()
        node_a = (await s.scalars(select(Server).where(Server.name == "node-a"))).one()
        assert chk.probe_server_id == node_a.id

    # сайт переехал на другую ноду — привязка обязана переехать следом
    async with factory() as s:
        a = (await s.scalars(select(Server).where(Server.name == "node-a"))).one()
        b = (await s.scalars(select(Server).where(Server.name == "node-b"))).one()
        a.last_report = {}
        b.last_report = {"web_services": [{"kind": "nginx", "sites": ["panel.example.ru"]}]}
        await s.commit()
        b_id = b.id
    assert await collector.rebind_local_probes(factory) == 1
    async with factory() as s:
        assert (await s.scalars(select(Check))).one().probe_server_id == b_id

    # домена нет нигде — привязки нет, и монитор честно скажет, что проверять некому
    async with factory() as s:
        b = (await s.scalars(select(Server).where(Server.name == "node-b"))).one()
        b.last_report = {}
        await s.commit()
    assert await collector.rebind_local_probes(factory) == 1
    async with factory() as s:
        assert (await s.scalars(select(Check))).one().probe_server_id is None
    await engine.dispose()


async def test_bulk_enable_local_probe(client, auth_headers):
    """Локальную проверку можно включить сразу пачке мониторов.

    Сайтов за белым списком обычно не один, а десяток: автодискавери заводит их
    вместе, и все вместе они оказываются красными. Включать по одному — работа,
    которую никто не будет делать.
    """
    ids = []
    for host in ("a.example.ru", "b.example.ru"):
        r = await client.post("/api/checks", headers=auth_headers,
                              json={"name": host, "type": "http", "target": f"https://{host}"})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])

    r = await client.patch("/api/checks/bulk", headers=auth_headers,
                           json={"ids": ids, "probe_local": True})
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == 2

    for cid in ids:
        got = await client.get(f"/api/checks/{cid}", headers=auth_headers)
        assert got.json()["probe_local"] is True

    # и выключить тоже пачкой — иначе из режима не выйти без ручного обхода
    r = await client.patch("/api/checks/bulk", headers=auth_headers,
                           json={"ids": ids, "probe_local": False})
    assert r.status_code == 200
    for cid in ids:
        got = await client.get(f"/api/checks/{cid}", headers=auth_headers)
        assert got.json()["probe_local"] is False


async def test_suggestion_for_site_that_answers_403(client, auth_headers, monkeypatch):
    """Сайт, отвечающий 403, лечится не локальной проверкой, а принятием кода.

    Поймано на живом парке: сайты за Envoy Gateway отдают 403 снаружи, а изнутри
    ноды на localhost не слушает никто — проверять там нечего. Раз сайт ОТВЕЧАЕТ,
    он жив, и достаточно считать этот код нормой.
    """
    r = await client.post("/api/checks", headers=auth_headers,
                          json={"name": "closed", "type": "http",
                                "target": "https://closed.example.ru"})
    cid = r.json()["id"]

    # приводим монитор в то состояние, в каком его видит панель после проверки
    from sqlalchemy import select
    from app.models import Check

    # У фикстуры своё приложение с временной БД (см. conftest), поэтому фабрику
    # сессий берём у него, а не у импортированного модуля: иначе правка уедет
    # в другую базу и монитор останется нетронутым.
    factory = client._transport.app.state.session_factory  # noqa: SLF001
    async with factory() as s:
        row = (await s.scalars(select(Check).where(Check.id == cid))).one()
        row.last_status = "down"
        row.last_message = "доступ запрещён (HTTP 403)"
        await s.commit()

    r = await client.get("/api/checks/local-probe-suggestions", headers=auth_headers)
    items = [x for x in r.json()["items"] if x["check_id"] == cid]
    assert items, "панель не заметила сайт, отвечающий 403"
    assert items[0]["kind"] == "code" and items[0]["code"] == 403

    r = await client.post("/api/checks/local-probe-apply", headers=auth_headers,
                          json={"check_ids": [cid]})
    assert r.json()["updated"] == 1

    got = (await client.get(f"/api/checks/{cid}", headers=auth_headers)).json()
    assert "403" in got["expected_status"]
    # 200-399 остаётся в силе: сайт, который однажды откроется, зелёным быть не перестанет
    assert "200-399" in got["expected_status"]
    # локальную проверку при этом НЕ включаем: изнутри там проверять нечего
    assert got["probe_local"] is False

    # предложение исчезает: код уже принят
    r = await client.get("/api/checks/local-probe-suggestions", headers=auth_headers)
    assert not [x for x in r.json()["items"] if x["check_id"] == cid]


async def test_agent_probe_tls_hint_points_at_http():
    """Изнутри «unknown authority» почти всегда значит «у сайта нет TLS».

    Веб-сервер отдаёт локальному запросу свой внутренний сертификат, и голая
    ошибка x509 отправляет искать проблему в сертификатах, а дело в схеме.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app import checks as checks_exec

    now = datetime.now(timezone.utc)
    check = SimpleNamespace(expected_status="200-399", keyword_up="", keyword_down="",
                            interval_seconds=60, degraded_ms=2000, probe_server_id=1)
    probe = SimpleNamespace(ts=now, code=0, latency_ms=3,
                            error='Get "https://ai.example.ru": tls: failed to verify '
                                  "certificate: x509: certificate signed by unknown authority",
                            kw_up_found=True, kw_down_found=False)
    out = checks_exec.outcome_from_agent(check, probe, now, 2000)
    assert out.status == "down"
    assert "http://" in out.message, "не подсказали, что дело в схеме"


async def test_cert_expiry_from_agent():
    """Срок сертификата для закрытого сайта берётся у агента.

    Панель до такого сайта не дотягивается, и её собственная проверка упирается в
    тот же обрыв: в карточке висело «сайт недоступен (таймаут)» при живом
    сертификате. Агент видит его на том же соединении, которым проверяет сайт.
    """
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    from app import checks as checks_exec

    now = datetime.now(timezone.utc)
    check = SimpleNamespace(check_ssl=True, check_domain=False,
                            target="https://closed.example.ru", timeout_ms=10000)
    probe = SimpleNamespace(cert_expires=int((now + timedelta(days=42)).timestamp()),
                            cert_issuer="Let's Encrypt")

    info = checks_exec.expiry_from_agent(check, probe, now)
    assert info.ssl_days == 41 or info.ssl_days == 42, info.ssl_days
    # в сообщении видно, что срок снят изнутри: снаружи посетитель может получить
    # другой сертификат, если перед сайтом стоит ещё один прокси
    assert "с сервера" in info.ssl_message

    # сайт по HTTP — сертификата нет и быть не должно, это не ошибка
    check.target = "http://plain.example.ru"
    assert checks_exec.expiry_from_agent(check, probe, now).ssl_message == "не HTTPS"

    # агент ещё не присылал — говорим об этом, а не молчим с пустым сроком
    check.target = "https://closed.example.ru"
    info = checks_exec.expiry_from_agent(check, None, now)
    assert info.ssl_days is None and "не присылал" in info.ssl_message

    # слежение за сертификатом выключено — ничего не выдумываем
    check.check_ssl = False
    empty = checks_exec.expiry_from_agent(check, probe, now)
    assert empty.ssl_days is None and empty.ssl_message == ""
