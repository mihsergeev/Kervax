"""Общий лог нескольких сайтов, разложенный по доменам (helper webserver-setup 0.21): полосы
графика по сайтам, ошибки 5xx по доменам в карточке и в алерте."""
import time
from datetime import datetime, timezone

from app import collector

SHARED = {"log": "/var/log/nginx/access.log", "rpm": 100, "e5": 12,
          "sites": ["anketa.shop.example", "mobile.shop.example", "mobile-web.shop.example"],
          "c5": {"500": 12}, "p5": [{"p": "/route/approved", "n": 10}],
          "hosts": [{"h": "mobile.shop.example", "rpm": 70, "e5": 11},
                    {"h": "anketa.shop.example", "rpm": 25, "e5": 1},
                    {"h": "-", "rpm": 5, "e5": 0}]}


def test_chart_bands_split_shared_log_by_domain():
    now = datetime.now(timezone.utc)
    block = {"ts": int(now.timestamp()), "rpm": 100, "e5": 12, "logs": [SHARED]}
    tops, _ = collector.web_breakdown({"web-rate": block}, now)
    by = {x["k"]: (x["r"], x["e"]) for x in tops}
    assert by["mobile.shop.example"] == (70, 11) and by["anketa.shop.example"] == (25, 1)
    # неразобранное (домен не вытащился) остается под подписью лога, а не теряется
    rest = [k for k in by if k not in ("mobile.shop.example", "anketa.shop.example")]
    assert len(rest) == 1 and by[rest[0]] == (5, 0)
    # лог без разбивки - как раньше, одной полосой
    plain = {k: v for k, v in SHARED.items() if k != "hosts"}
    tops, _ = collector.web_breakdown({"web-rate": {**block, "logs": [plain]}}, now)
    assert len(tops) == 1 and tops[0]["r"] == 100


def test_where_text_names_domains():
    txt = collector.web_where_text({"logs": [["nginx (anketa.shop.example +2)", 12]], "codes": {"500": 12},
                                    "paths": {}, "hosts": {"mobile.shop.example": 11, "anketa.shop.example": 1}})
    assert "По доменам: mobile.shop.example - 11, anketa.shop.example - 1" in txt


async def test_errors_page_shows_domains(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "mob"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    block = {"ts": int(time.time()), "rpm": 100, "e5": 12, "logs": [SHARED]}
    report = {"hostname": "mob", "os": "U", "agent_version": "2.21", "cpu_percent": 5,
              "mem_used": 1, "mem_total": 2, "extras": {"web-rate": block}}
    r = await client.post("/api/agent/report", json=report, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    rows = (await client.get(f"/api/servers/{sid}/web-errors?hours=1", headers=auth_headers)).json()
    assert len(rows) == 1
    # домены без ошибок и неразобранное в сводку ошибок не идут
    assert rows[0]["hosts"] == [{"h": "mobile.shop.example", "n": 11}, {"h": "anketa.shop.example", "n": 1}]


async def test_unparsed_domain_is_not_a_domain(client, auth_headers):
    """Строки, из которых домен не достался ("-"), даже с ошибками не показываются доменом."""
    r = await client.post("/api/servers", json={"name": "skid"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    log = {"log": "docker:deals-nginx", "rpm": 10, "e5": 4, "c5": {"502": 4},
           "sites": ["api.deals.example", "admin.deals.example"],
           "hosts": [{"h": "-", "rpm": 5, "e5": 3}, {"h": "api.deals.example", "rpm": 5, "e5": 1}]}
    report = {"hostname": "skid", "os": "U", "agent_version": "2.22", "cpu_percent": 5, "mem_used": 1,
              "mem_total": 2, "extras": {"web-rate": {"ts": int(time.time()), "rpm": 10, "e5": 4, "logs": [log]}}}
    r = await client.post("/api/agent/report", json=report, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    rows = (await client.get(f"/api/servers/{sid}/web-errors?hours=1", headers=auth_headers)).json()
    assert rows[0]["hosts"] == [{"h": "api.deals.example", "n": 1}]
