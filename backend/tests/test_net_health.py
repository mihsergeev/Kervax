"""Сеть ноды: TCP теряет пакеты, сервис не успевает принимать соединения, conntrack выбрасывает
новые, ошибки и дропы интерфейсов. Алерт с выдержкой и строка в "Что сломано"."""
from datetime import datetime, timedelta, timezone

from app import collector
from app.models import Server

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
CALM = {"tcp": {"out": 2400.0, "retrans": 12.0, "in_errs": 0.0, "listen_ovf": 0.0, "listen_drop": 0.0,
                "ct_drop": 0.0},
        "net_ifaces": [{"if": "eth0", "rx": 1e6, "tx": 2e6, "errs": 0.0, "drops": 0.99},
                       {"if": "cali1234", "rx": 1e3, "tx": 1e3, "errs": 0.0, "drops": 500.0}]}


def _srv(rep, state=None):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state=state or {}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_calm_network_and_virtual_drops():
    # пара дропов в секунду на eth0 и дропы на мосту Calico - не беда
    assert collector.net_problems(CALM) == []
    assert collector.net_problems({}) == []


def test_each_cause():
    rep = {"tcp": {"out": 1000.0, "retrans": 180.0, "listen_ovf": 4.2, "ct_drop": 12.0},
           "net_ifaces": [{"if": "eth0", "errs": 3.0, "drops": 250.0}, {"if": "docker0", "errs": 0, "drops": 900}]}
    assert collector.net_problems(rep) == [
        (2, "conntrack", "conntrack выбрасывает новые соединения: 12 в секунду"),
        (1, "retrans", "TCP теряет пакеты: 18% сегментов уходят повторно"),
        (1, "listen", "сервис не успевает принимать соединения: очередь accept переполняется 4 раза в секунду"),
        (1, "iface", "eth0: 3 ошибки в секунду"),
        (1, "iface", "eth0: 250 дропов в секунду"),
    ]
    # повторы SYN (подключения без ответа: торрент за VPN) - не потери: 670 из 680 повторов
    vpn = {"tcp": {"out": 1870.0, "retrans": 678.0, "syn_retrans": 674.0}}
    assert collector.net_problems(vpn) == []
    # а настоящие потери на том же фоне видно
    assert collector.net_problems({"tcp": {"out": 1870.0, "retrans": 900.0, "syn_retrans": 674.0}})[0][1] == "retrans"
    # на тихой ноде доля повторов прыгает от пары потерь - не судим
    assert collector.net_problems({"tcp": {"out": 20.0, "retrans": 10.0}}) == []
    assert collector.net_problems({"tcp": {"out": "x"}, "net_ifaces": ["y", {"if": "eth0", "errs": None}]}) == []


def test_sustain_and_home_row():
    bad = dict(CALM, tcp=dict(CALM["tcp"], ct_drop=30.0))
    s = _srv(bad)
    lvl, ctx = collector._server_conditions(s, NOW)["net_health"]
    assert lvl == 0 and ctx["since"] == NOW.isoformat()
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] == "net_health"]
    s.alert_state = {"net_health_since": (NOW - timedelta(minutes=16)).isoformat()}
    lvl, ctx = collector._server_conditions(s, NOW)["net_health"]
    assert lvl == 2 and ctx["detail"] == "conntrack выбрасывает новые соединения: 30 в секунду"
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "net_health"]
    assert [(p["level"], p["sec"], p["mute"]) for p in rows] == [(2, "nethealth", "net_health")]
    # ошибки интерфейса ведут к графику ошибок, а не к TCP
    s.last_report = dict(CALM, net_ifaces=[{"if": "eth0", "errs": 5.0, "drops": 0.0}])
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "net_health"]
    assert [(p["level"], p["sec"], p["text"]) for p in rows] == [(1, "neterr", "eth0: 5 ошибок в секунду")]
    s.last_report = CALM
    lvl, ctx = collector._server_conditions(s, NOW)["net_health"]
    assert lvl == 0 and ctx["since"] is None
