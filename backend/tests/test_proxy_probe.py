"""Сайт за белым списком caddy или nginx - проверка изнутри без правок на сервере (агент 2.59).

Живой случай (29.10.2026, web-a): у сервера два адреса, 443 первого держит VPN, caddy опубликован
только на втором. Проверка изнутри стучалась на localhost и видела "порт закрыт", а снаружи
панель рвал белый список caddy - поставить сайт на мониторинг без правки вайтлиста было нельзя.
Агент 2.59 пробует тот же порт на своих адресах ("addr:<ip>"), а если и там обрыв или 403 -
спрашивает бэкенд из конфига прокси напрямую ("proxy:caddy container app-1:80"). Панель говорит,
каким путем проверено.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from app import checks as checks_exec
from app import srv_short
from tests.test_domain_probe import _by_domain, _setup

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
CHECK = SimpleNamespace(interval_seconds=60, expected_status="200-399", keyword_up="",
                        keyword_down="", probe_server_id=1, target="https://panel.example.com")


def _probe(**kw):
    base = dict(ts=NOW, code=0, latency_ms=0, error="", kw_up_found=True, kw_down_found=False,
                via="proxy:caddy container panel-frontend-1:80")
    return SimpleNamespace(**(base | kw))


def test_via_text():
    assert checks_exec.via_text("") == ""
    assert checks_exec.via_text("addr:203.0.113.6") == "через адрес 203.0.113.6"
    assert checks_exec.via_text("proxy:caddy container app-1:80") == "контейнер app-1:80 в обход caddy"
    assert checks_exec.via_text("proxy:nginx addr 127.0.0.1:8080") == "адрес 127.0.0.1:8080 в обход nginx"
    assert checks_exec.via_text("cluster:default/web:80") == "сервис default/web:80 в обход шлюза"


def test_backend_answers_past_the_proxy():
    out = checks_exec.outcome_from_agent(CHECK, _probe(code=200, latency_ms=3), NOW, 2000)
    assert out.status == "up"
    assert out.message == "HTTP 200 · 3 мс · контейнер panel-frontend-1:80 в обход caddy"
    # код бэкенда оценивается как обычно
    out = checks_exec.outcome_from_agent(CHECK, _probe(code=502, latency_ms=3), NOW, 2000)
    assert out.status == "down" and out.message.endswith("в обход caddy")


def test_proxy_not_listening_is_down_even_if_the_backend_answers():
    err = "front 203.0.113.6:443: dial tcp 203.0.113.6:443: connect: connection refused"
    out = checks_exec.outcome_from_agent(CHECK, _probe(code=0, error=err), NOW, 2000)
    assert out.status == "down" and out.message.startswith("прокси 203.0.113.6:443 не принимает соединения")


def test_backend_error_names_the_backend():
    err = 'Get "http://172.18.0.3:80/": dial tcp 172.18.0.3:80: connect: connection refused'
    out = checks_exec.outcome_from_agent(CHECK, _probe(error=err), NOW, 2000)
    assert out.status == "down" and out.message.endswith("контейнер panel-frontend-1:80 в обход caddy")
    assert "localhost" not in out.message
    # в чат - причина и путь проверки
    emoji, what, facts = srv_short.site("bad", "down", out.message)
    assert what == "Не отвечает"
    assert facts == ["сервер отклонил соединение", "контейнер panel-frontend-1:80 в обход caddy"]


def test_own_address_says_so():
    out = checks_exec.outcome_from_agent(CHECK, _probe(code=200, latency_ms=4, via="addr:203.0.113.6"),
                                         NOW, 2000)
    assert out.status == "up" and out.message == "HTTP 200 · 4 мс · через адрес 203.0.113.6"
    # обрыв с адреса сервера, бэкенд в конфиге прокси не нашелся: в белый список - этот адрес
    err = 'Get "https://panel.example.com": EOF'
    out = checks_exec.outcome_from_agent(CHECK, _probe(error=err, via="addr:203.0.113.6"), NOW, 2000)
    assert out.status == "down"
    assert "добавьте в него адрес сервера 203.0.113.6" in out.message and "127.0.0.1" not in out.message
    assert srv_short.site("bad", "down", out.message)[2][-1] == "через адрес 203.0.113.6"


def test_localhost_wording_unchanged():
    err = 'Get "https://panel.example.com": EOF'
    out = checks_exec.outcome_from_agent(CHECK, _probe(error=err, via=""), NOW, 2000)
    assert "127.0.0.1/32" in out.message and "через адрес" not in out.message


async def test_wizard_shows_how_it_was_probed(client, auth_headers, monkeypatch):
    """Мастер: изнутри работает, и под доменом видно, мимо чего прошла проверка."""
    _, ah, _ = await _setup(client, auth_headers, monkeypatch, version="2.59")
    r = await client.post("/api/checks/discovered/probe", headers=auth_headers,
                          json={"domains": ["closed.example", "open.example"]})
    assert r.status_code == 200, r.text
    tasks = (await client.get("/api/agent/commands", headers=ah)).json()["site_probes"]
    ids = {t["url"]: t["id"] for t in tasks}
    r = await client.post("/api/agent/site-probe-result", headers=ah, json={"site_probes": [
        {"id": ids["https://closed.example"], "code": 200, "latency_ms": 3, "kw_up_found": True,
         "via": "proxy:caddy container panel-frontend-1:80"},
        {"id": ids["https://open.example"], "code": 200, "latency_ms": 2, "kw_up_found": True},
    ]})
    assert r.status_code == 200, r.text
    items = _by_domain((await client.get("/api/checks/discovered/probes",
                                         headers=auth_headers)).json()["items"])
    closed = items["closed.example"]
    assert closed["local_status"] == "up" and closed["ext_status"] == "down"
    assert closed["local_via"] == "контейнер panel-frontend-1:80 в обход caddy"
    assert items["open.example"]["local_via"] == ""
