"""503, на которые ingress-nginx отвечает сам, пока у сервиса нет живых подов (helper webserver-setup
0.28, вид noep). Короткий период при выкатке не алертит, долгий алертит - с причиной "нет живых
подов у сервиса X" вместо общего "N% запросов с ошибкой 5xx"."""
import time
from datetime import datetime, timedelta, timezone

from app import alert_what, collector, web_upstream
from app.alerts import full_text
from app.models import Server, ServerMetric, WebErrorSample

from tests.test_web_upstream import _funcs, _lines, _panel, _rate_body, _run, _sent

CTRL = "default/ingress-nginx-controller (a.example, b.example +11)"
NOEP = [{"u": "shop-shop-back-80", "k": "noep", "n": 30, "s": "shop/shop-back", "i": "shop", "h": "api.example.com"}]

# формат ingress-nginx по умолчанию в логе пода (CRI): 503 без адреса пода и с ним
ING = [
    '2026-10-27T11:57:41.1Z stdout F 203.0.113.7 - - [27/Oct/2026:11:57:41 +0000] "GET /.env HTTP/2.0" 503 190 "-" "Go" 11 0.000 [shop-shop-back-80] [] - - - - 7a1c',
    '2026-10-27T11:57:42.1Z stdout F 203.0.113.8 - - [27/Oct/2026:11:57:42 +0000] "GET / HTTP/1.1" 503 592 "-" "M" 488 0.000 [shop-shop-back-80] [] - - - - 7a1d',
    '2026-10-27T11:57:43.1Z stdout F 203.0.113.9 - - [27/Oct/2026:11:57:43 +0000] "GET /api HTTP/1.1" 503 190 "-" "M" 71 0.002 [shop-api-80] [] 10.244.1.5:8080 190 0.002 503 7a1e',
]
LIMIT = '2026-10-27T11:57:44.1Z stderr F 2026/10/27 11:57:44 [error] 31#31: *9 limiting requests, excess: 10.500 by zone "shop-rps", client: 203.0.113.7, server: api.example.com, request: "GET / HTTP/1.1", host: "api.example.com"'
CONF = """
	## start server api.example.com
	server {
		server_name api.example.com ;
		listen 80  ;
		location / {
			set $namespace      "shop";
			set $ingress_name   "shop";
			set $service_name   "shop-back";
			set $service_port   "80";
			set $proxy_upstream_name "shop-shop-back-80";
		}
	}
	## start server www.example.com
	server {
		server_name www.example.com ;
		location / {
			set $namespace      "shop";
			set $ingress_name   "shop-www";
			set $service_name   "shop-back";
			set $proxy_upstream_name "shop-shop-back-80";
		}
	}
	server {
		server_name _ ;
		location / {
			set $proxy_upstream_name "upstream-default-backend";
		}
	}
"""


def test_helper_tells_no_endpoints_from_app_errors(tmp_path):
    t = tmp_path.as_posix()
    script = (f"set -u\nT='{t}'\n" + _funcs() + _lines("ing", ING) + _lines("lim", ING + [LIMIT])
              + f"cat > \"$T/conf\" <<'C_EOF'\n{CONF}\nC_EOF\n"
              + 'echo "I $(count_codes "" 60 1 < "$T/ing" | sed -n 6p)"\n'
              + 'echo "L $(count_codes "" 60 1 < "$T/lim" | sed -n 6p)"\n'
              + 'echo "C $(count_codes "" 60 1 < "$T/ing" | sed -n 1p)"\n'
              + 'INGMAP=$(ing_conf < "$T/conf")\n'
              + 'echo "M $(printf %s "$INGMAP" | tr "\\t\\n" "|;")"\n'
              + 'echo "N $(ing_names \'[{"u":"shop-shop-back-80","k":"noep","n":2},{"u":"other-x-80","k":"noep","n":1}]\')"\n')
    out = dict(line.split(" ", 1) for line in _run(script).splitlines() if " " in line)
    # три запроса, все 503; без upstream - две строки сервиса без подов, третья ушла в под
    assert out["C"].split()[:2] == ["3", "3"]
    assert out["I"] == '[{"u":"shop-shop-back-80","k":"noep","n":2}]', out["I"]
    # limit_req в том же потоке: это не "нет подов"
    assert out["L"] == '[{"u":"shop-shop-back-80","k":"limit","n":2}]', out["L"]
    # из nginx.conf контроллера: сервис, Ingress (последний location) и хосты; default backend мимо
    assert out["M"] == "shop-shop-back-80|shop/shop-back|shop-www|api.example.com www.example.com", out["M"]
    assert out["N"] == ('[{"u":"shop-shop-back-80","k":"noep","s":"shop/shop-back","i":"shop-www",'
                        '"h":"api.example.com www.example.com","n":2},{"u":"other-x-80","k":"noep","n":1}]'), out["N"]


def test_helper_wiring_noep():
    body = _rate_body()
    assert "for (x in ne) un[x SUBSEP (lim ? \"limit\" : \"noep\")] += ne[x]" in body
    assert "ingmap_load; up=$(ing_names \"$up\")" in body


def test_clean_keeps_service_fields():
    got = web_upstream.clean([{"u": "shop-shop-back-80", "k": "noep", "n": 5, "s": "shop/shop-back", "i": "shop",
                               "h": "api.example.com bad\"host www.example.com a.example b.example"},
                              {"u": "x-y-80", "k": "noep", "n": 1, "s": "no slash", "i": "bad name!"}])
    assert got == [{"u": "shop-shop-back-80", "k": "noep", "n": 5, "s": "shop/shop-back", "i": "shop",
                    "h": "api.example.com www.example.com a.example"},
                   {"u": "x-y-80", "k": "noep", "n": 1}]
    assert web_upstream.item_text(got[0]) == (
        "нет живых подов у сервиса shop/shop-back (ingress shop, api.example.com, www.example.com, a.example)")
    assert web_upstream.item_text(got[1]) == "нет живых подов у сервиса x-y-80"


def test_from_lines_for_old_helper():
    lines = [x.split(" F ", 1)[1] for x in ING[:2]]
    got = web_upstream.from_lines(lines, {"503": 40}, 40, {"shop", "kube-system"})
    assert got == [{"u": "shop-shop-back-80", "k": "noep", "n": 40, "s": "shop/shop-back"}]
    # строка, которую ответил под, или другой код - минуту не угадываем
    assert web_upstream.from_lines(lines + [ING[2].split(" F ", 1)[1]], {"503": 40}, 40) is None
    assert web_upstream.from_lines(lines, {"503": 30, "502": 10}, 40) is None
    assert web_upstream.from_lines(None, {"503": 1}, 1) is None
    # без namespace'ов имя upstream не делим
    assert web_upstream.svc_of("shop-shop-back-80", ()) == ""
    assert web_upstream.svc_of("my-shop-back-80", {"my", "my-shop"}) == "my-shop/back"


def test_noep_minute_needs_most_errors():
    assert web_upstream.noep_services(NOEP, 30) == ["shop-shop-back-80"]
    assert web_upstream.noep_services(NOEP, 100) == []  # остальное - ответы приложения
    assert web_upstream.noep_services([{"u": "a:80", "k": "refused", "n": 9}], 9) == []
    assert web_upstream.noep_services(None, 9) == []


def test_report_line_names_the_service():
    body = ("2.42% запросов за 15 минут закончились ошибкой 5xx (70 из 2887). Больше всего: "
            "default/ingress-nginx-controller - 105. Коды: 503 - 109. Почему: нет живых подов у сервиса "
            "shop/shop-back (ingress shop, api.example.com) - 105. Чаще всего падает: / - 21")
    assert alert_what._web5xx(body, []) == [("нет живых подов у сервиса shop/shop-back",
                                             "2.42% запросов, nginx отвечает 503")]


def test_where_label_drops_controller_hosts():
    assert collector._where_label(CTRL) == "default/ingress-nginx-controller"
    assert collector._where_label("shop/web (shop.example)") == "shop/web (shop.example)"
    assert collector._where_label("app-nginx (a.example, b.example)") == "app-nginx (a.example, b.example)"


async def _noep_case(tmp_path, name, minutes):
    """Ingress-контроллер на одной ноде: у нового сервиса нет живых подов minutes минут подряд,
    по 30 ответов 503 в минуту при 1000 запросов."""
    engine, factory = await _panel(tmp_path, name)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name="node-a", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, last_report={"cpu_percent": 20}, cpu_alert_percent=0,
                     mem_alert_percent=0, disk_alert_percent=0, web_5xx_alert_percent=0.05))
        await s.commit()
        for m in range(15):
            ts = now - timedelta(minutes=m)
            e5 = 30 if m < minutes else 0
            s.add(ServerMetric(server_id=1, ts=ts, cpu_percent=20, web_rpm=1000, web_5xx=e5))
            if e5:
                # два пода контроллера пишут одну и ту же минуту
                for pod in ("x7k2p", "q9z4w"):
                    s.add(WebErrorSample(server_id=1, ts=ts, src_ts=10_000 - m * 60, e5=e5 // 2, rpm=500,
                                         log=f"/var/log/pods/{pod}/0.log", label=CTRL, codes={"503": e5 // 2},
                                         paths=[{"p": "/", "n": e5 // 2}],
                                         ups=[dict(NOEP[0], n=e5 // 2)]))
        await s.commit()
    return engine, factory, now


async def test_short_no_endpoints_is_not_an_alert(tmp_path, monkeypatch):
    """Выкатка: четыре минуты без подов - не авария."""
    engine, factory, now = await _noep_case(tmp_path, "short.db", 4)
    assert [x for x in await _sent(factory, now, monkeypatch) if "5xx" in x or "подов" in x] == []
    async with factory() as s:
        w = (await collector.web_error_window(s, now))[1]
        assert w[3] == {} and w[4] == 120  # все ошибки не в счет, обе половины каждой минуты
    await engine.dispose()


async def test_long_no_endpoints_alerts_with_the_reason(tmp_path, monkeypatch):
    """Сервис без подов дольше выдержки - это сбой: алерт называет сервис и хост."""
    engine, factory, now = await _noep_case(tmp_path, "long.db", 13)
    sent = [x for x in await _sent(factory, now, monkeypatch) if "подов" in x]
    assert len(sent) == 1, sent
    lines = sent[0].split("\n")
    assert lines[1] == "<b>Нет живых подов, nginx отвечает 503</b>", sent[0]
    assert lines[2:4] == ["Сервис: shop/shop-back", "Хост: api.example.com"], sent[0]
    full = full_text(sent[0])
    assert "Почему: нет живых подов у сервиса shop/shop-back (ingress shop, api.example.com) - 390" in full, full
    # в "больше всего" - контроллер без чужих хостов
    assert "Больше всего: default/ingress-nginx-controller - 390" in full, full
    assert "b.example" not in full
    async with factory() as s:
        w = (await collector.web_error_window(s, now))[1]
        assert w[3] == {"default/ingress-nginx-controller": {10_000 - m * 60: 30 for m in range(13)}}
    await engine.dispose()


async def test_old_helper_noep_from_lines(client, auth_headers):
    """Нода со старым helper: вид noep панель находит по строкам с 5xx, сервис - по namespace'ам."""
    r = await client.post("/api/servers", json={"name": "node-b"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    log = {"log": "/var/log/pods/a/0.log", "name": "default/ingress-nginx-controller-5c67cd68dd-djg5j",
           "rpm": 100, "e5": 12, "c5": {"503": 12}, "l5": [x.split(" F ", 1)[1] for x in ING[:2]]}
    report = {"hostname": "node-b", "os": "U", "agent_version": "2.38", "cpu_percent": 5, "mem_used": 1,
              "mem_total": 2, "kube": {"present": True, "pods": [{"ns": "shop", "name": "shop-back-x"}]},
              "extras": {"web-rate": {"ts": int(time.time()), "rpm": 100, "e5": 12, "logs": [log]}}}
    r = await client.post("/api/agent/report", json=report, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    rows = (await client.get(f"/api/servers/{sid}/web-errors?hours=1", headers=auth_headers)).json()
    assert rows[0]["ups"] == [{"u": "shop-shop-back-80", "k": "noep", "n": 12, "s": "shop/shop-back"}]


def test_hold_fits_the_window():
    # выдержка "нет подов" короче окна алерта: иначе долгий сбой не набрал бы ее никогда
    assert collector._NOEP_HOLD < collector._WEB_ERR_WINDOW
