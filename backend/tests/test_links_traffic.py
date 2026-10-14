"""Связь между серверами (агент 2.31 пробует открыть TCP-соединение с заданными адресами) и
трафик за месяц по настоящим сетевым картам с лимитом тарифа."""
from datetime import date, datetime, timedelta, timezone

from app import collector, netlinks, traffic
from app.models import Server

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
REP = {"agent_version": "2.31", "mem_used": 1, "mem_total": 2, "disks": [{"mount": "/", "used": 1, "total": 2}]}


def _srv(**kw):
    return Server(id=1, name="vpn-in", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report={"agent_version": "2.31"},
                  alert_state={}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[], **kw)


async def test_link_flow(client, auth_headers):
    a = (await client.post("/api/servers", json={"name": "vpn-in"}, headers=auth_headers)).json()
    b = (await client.post("/api/servers", json={"name": "vpn-out"}, headers=auth_headers)).json()
    sid, ah = a["server"]["id"], {"Authorization": f"Bearer {a['token']}"}
    url = f"/api/servers/{sid}/links"
    r = await client.post(url, json={"target_server_id": b["server"]["id"], "host": "203.0.113.5", "port": 443,
                                     "note": "выход"}, headers=auth_headers)
    assert r.status_code == 201
    lid = r.json()["id"]
    for body, code in (({"host": "http://203.0.113.5", "port": 443}, 400),
                       ({"host": "203.0.113.5", "port": 443}, 409),
                       ({"host": "db.example.com", "port": 70000}, 422),
                       ({"target_server_id": sid, "host": "203.0.113.9", "port": 22}, 400)):
        assert (await client.post(url, json=body, headers=auth_headers)).status_code == code, body
    # агент получает адрес в ответе на отчет и возвращает попытки следующим отчетом
    out = (await client.post("/api/agent/report", json=REP, headers=ah)).json()
    assert out["links"] == [{"id": lid, "host": "203.0.113.5", "port": 443}]
    rep = dict(REP, links=[{"id": lid, "sent": 3, "ok": 0, "err": "timeout"}, {"id": 999, "sent": 3, "ok": 3}])
    assert (await client.post("/api/agent/report", json=rep, headers=ah)).status_code == 200
    got = (await client.get(url, headers=auth_headers)).json()
    assert got["agent_ok"] and got["max"] == 16 and [t["name"] for t in got["targets"]] == ["vpn-out"]
    ln = got["links"][0]
    assert (ln["name"], ln["loss"], ln["err"], ln["fresh"], ln["held"]) == ("vpn-out:443 (выход)", 100.0, "timeout",
                                                                         True, False)
    srv = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    assert "links" not in (srv.get("last_report") or {})
    pts = (await client.get(f"{url}/{lid}/history?hours=1", headers=auth_headers)).json()["points"]
    assert len(pts) == 1 and pts[0]["loss"] == 100.0 and pts[0]["rtt"] is None
    sp = ln["spark"]
    assert len(sp) == 30 and sp[-1] == [None, 100.0] and sp[0] is None
    # окно 5 минут: следующая порция удачная - доля неудач за окно половина
    rep = dict(REP, links=[{"id": lid, "sent": 3, "ok": 3, "rtt_ms": 41.2, "rtt_max": 44.0}])
    await client.post("/api/agent/report", json=rep, headers=ah)
    ln = (await client.get(url, headers=auth_headers)).json()["links"][0]
    assert (ln["loss"], ln["rtt"], ln["err"]) == (50.0, 41.2, "timeout")
    assert (await client.delete(f"{url}/{lid}", headers=auth_headers)).status_code == 204
    assert (await client.post("/api/agent/report", json=REP, headers=ah)).json()["links"] == []
    assert (await client.get(url, headers=auth_headers)).json()["links"] == []


async def test_deleting_server_keeps_links_to_it(client, auth_headers):
    a = (await client.post("/api/servers", json={"name": "app"}, headers=auth_headers)).json()
    b = (await client.post("/api/servers", json={"name": "db"}, headers=auth_headers)).json()
    url = f"/api/servers/{a['server']['id']}/links"
    await client.post(url, json={"target_server_id": b["server"]["id"], "host": "10.0.0.5", "port": 5432},
                      headers=auth_headers)
    await client.post(f"/api/servers/{b['server']['id']}/links", json={"host": "10.0.0.1", "port": 22},
                      headers=auth_headers)
    assert (await client.delete(f"/api/servers/{b['server']['id']}", headers=auth_headers)).status_code == 204
    ln = (await client.get(url, headers=auth_headers)).json()["links"]
    assert [(x["target_server_id"], x["name"]) for x in ln] == [(None, "10.0.0.5:5432")]


def test_host_and_window():
    assert netlinks.host_ok("10.0.0.5") and netlinks.host_ok("2001:db8::1") and netlinks.host_ok("db.example.com")
    for bad in ("", "http://x", "a b", "u@h", "x:22", "a/b"):
        assert not netlinks.host_ok(bad), bad
    t = NOW.timestamp()
    assert netlinks.window_loss([[t - 400, 3, 0], [t - 60, 3, 3], [t, 3, 1]], NOW) == (33.3, 6)
    assert netlinks.window_loss([[t - 400, 3, 0]], NOW) == (None, 0)


def test_link_condition_and_home_row():
    st = [{"id": 1, "name": "vpn-out:443", "loss": 100.0, "err": "timeout", "held": False},
          {"id": 2, "name": "db:5432", "loss": 25.0, "err": "", "held": False},
          {"id": 3, "name": "kz:443", "loss": 100.0, "err": "timeout", "held": True}]
    s = _srv(links_state=st)
    lvl, ctx = collector._server_conditions(s, NOW)["link"]
    assert lvl == 0 and ctx["since"] == NOW.isoformat()
    s.alert_state = {"link_since": (NOW - timedelta(minutes=16)).isoformat()}
    lvl, ctx = collector._server_conditions(s, NOW)["link"]
    assert lvl == 2 and ctx["sig"] == ["1", "2"]
    assert ctx["detail"] == "нет связи с vpn-out:443: timeout; связь с db:5432 теряет 25% попыток подключения"
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "link"]
    assert [(p["level"], p["sec"], p["mute"]) for p in rows] == [(2, "links", "link")]
    # объявленная связь починилась, сломалась другая: в sig обе, чтобы та же снова не дала сообщения
    s.alert_state = {"link": 2, "link_sig": ["1"], "link_since": (NOW - timedelta(minutes=30)).isoformat()}
    s.links_state = [dict(st[1], loss=100.0)]
    lvl, ctx = collector._server_conditions(s, NOW)["link"]
    assert lvl == 2 and ctx["sig"] == ["1", "2"]
    # сервер, с которым связь, сам молчит - не алертим
    s.alert_state, s.links_state = {}, [st[2]]
    assert collector._server_conditions(s, NOW)["link"][0] == 0 and netlinks.problems(s.links_state) == []
    assert collector._recovery_detail("link", {}, {}) == "связь с другими серверами снова в порядке"


def test_period():
    assert traffic.period(NOW, 1) == (date(2026, 10, 1), date(2026, 11, 1))
    assert traffic.period(NOW, 15) == (date(2026, 9, 15), date(2026, 10, 15))
    assert traffic.period(datetime(2026, 1, 5, tzinfo=timezone.utc), 20) == (date(2025, 12, 20), date(2026, 1, 20))
    assert traffic.period(datetime(2026, 12, 25, tzinfo=timezone.utc), 20) == (date(2026, 12, 20), date(2027, 1, 20))
    assert traffic.period(NOW, 31) == (date(2026, 9, 28), date(2026, 10, 28))


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Ses:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _q):
        return _Rows(self.rows)


async def test_summary_forecast():
    s = _srv(traffic_ctr={"since": (NOW - timedelta(days=10)).isoformat(), "c": {}}, traffic_limit_gb=20_000,
             traffic_count="out", traffic_cycle_day=1)
    out = await traffic.summary(_Ses([(10**12, 3 * 10**12)]), s, NOW)
    # 3 ТБ за 10 суток, до конца октября еще 17.5 суток
    assert (out["used"], out["limit"], out["days"], out["left"], out["full"]) == (3 * 10**12, 2 * 10**13, 10.0,
                                                                                 17.5, False)
    assert out["forecast"] == 8_250_000_000_000
    s.traffic_count = "both"
    assert (await traffic.summary(_Ses([(10**12, 3 * 10**12)]), s, NOW))["used"] == 4 * 10**12
    # меньше трех суток данных - прогноза нет
    s.traffic_ctr = {"since": (NOW - timedelta(days=2)).isoformat()}
    assert (await traffic.summary(_Ses([]), s, NOW))["forecast"] is None


def test_traffic_problem_and_condition():
    base = {"at": NOW.isoformat(), "end": "2026-11-01", "count": "out", "limit": 2 * 10**13, "forecast": None}
    assert traffic.problem(dict(base, used=21 * 10**12), NOW) == (
        2, "лимит исходящего трафика исчерпан: 21 ТБ из 20 ТБ, период до 01.11")
    assert traffic.problem(dict(base, used=186 * 10**11), NOW) == (
        1, "израсходовано 93% исходящего трафика: 18.6 ТБ из 20 ТБ, период до 01.11")
    assert traffic.problem(dict(base, used=10**13, forecast=25 * 10**12, count="both"), NOW) == (
        1, "к концу периода выйдет около 25 ТБ трафика при лимите 20 ТБ (сейчас 10 ТБ, период до 01.11)")
    assert traffic.problem(dict(base, used=10**13, forecast=15 * 10**12), NOW) == (0, "")
    assert traffic.problem(dict(base, used=21 * 10**12, limit=0), NOW) == (0, "")
    stale = (NOW - timedelta(hours=3)).isoformat()
    assert traffic.problem(dict(base, used=21 * 10**12, at=stale), NOW) == (0, "")
    s = _srv(traffic_sum=dict(base, used=21 * 10**12))
    s.alert_state = {"traffic_since": (NOW - timedelta(minutes=16)).isoformat()}
    lvl, ctx = collector._server_conditions(s, NOW)["traffic"]
    assert lvl == 2 and ctx["detail"].startswith("лимит исходящего трафика исчерпан")
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "traffic"]
    assert [(p["level"], p["sec"]) for p in rows] == [(2, "traffic")]
    assert traffic.fmt_bytes(512 * 10**6) == "512 МБ" and traffic.fmt_bytes(1.25 * 10**9) == "1.2 ГБ"


async def test_traffic_ingest(client, auth_headers):
    r = (await client.post("/api/servers", json={"name": "web"}, headers=auth_headers)).json()
    sid, ah = r["server"]["id"], {"Authorization": f"Bearer {r['token']}"}

    def rep(rx, tx):
        return dict(REP, net_ifaces=[
            {"if": "eth0", "rx": 1.0, "tx": 2.0, "errs": 0, "drops": 0, "rx_total": rx, "tx_total": tx, "phys": True},
            {"if": "docker0", "rx": 1.0, "tx": 2.0, "errs": 0, "drops": 0, "rx_total": 10**12, "tx_total": 10**12}])

    # первый отчет только запоминает счетчики, дальше приросты; счетчик меньше прошлого - перезагрузка
    for rx, tx in ((1000, 5000), (3000, 9000), (100, 50)):
        assert (await client.post("/api/agent/report", json=rep(rx, tx), headers=ah)).status_code == 200
    got = (await client.get(f"/api/servers/{sid}/traffic", headers=auth_headers)).json()
    assert [(d["rx"], d["tx"]) for d in got["days"]] == [(2100, 4050)]
    assert got["ifaces"] == ["eth0"] and got["agent_ok"] and got["sum"]["used"] == 4050
    assert got["periods"][-1]["tx"] == 4050 and got["limit_gb"] == 0 and got["since"]
    r = await client.patch(f"/api/servers/{sid}", json={"traffic_limit_gb": 20000, "traffic_count": "both",
                                                         "traffic_cycle_day": 15}, headers=auth_headers)
    assert r.status_code == 200 and (r.json()["traffic_limit_gb"], r.json()["traffic_count"],
                                     r.json()["traffic_cycle_day"]) == (20000, "both", 15)
    bad = await client.patch(f"/api/servers/{sid}", json={"traffic_count": "in"}, headers=auth_headers)
    assert bad.status_code == 422
    got = (await client.get(f"/api/servers/{sid}/traffic", headers=auth_headers)).json()
    assert got["sum"]["used"] == 6150 and got["sum"]["limit"] == 2 * 10**13
    # в историю метрик счетчики с загрузки не попадают
    pts = (await client.get(f"/api/servers/{sid}/metrics?hours=1", headers=auth_headers)).json()
    ifs = [i for p in pts for i in (p.get("net_ifaces") or [])]
    assert ifs and all("rx_total" not in i for i in ifs)
