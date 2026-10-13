"""Пропал порт, который слушался неделями, и диск, который устойчиво тормозит."""
from datetime import datetime, timedelta, timezone

from app import collector, listen_watch
from app.models import Server

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _rep(ports=(22, 8080), procs=None, **kw):
    r = {"mem_total": 8 * 2**30, "mem_used": 2**30,
         "listen": [{"p": "tcp", "ip": "0.0.0.0", "port": p} for p in ports]
         + [{"p": "udp", "ip": "0.0.0.0", "port": 53}],
         "extras": {"changes": {"listen": procs if procs is not None else {"22": "sshd", "8080": "python3"}}}}
    r.update(kw)
    return r


def _srv(state=None, mutes=None, rep=None):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def _week_old():
    """История: оба порта слушаются 10 дней"""
    st = listen_watch.update(None, _rep(), NOW - timedelta(days=10))
    return listen_watch.update(st, _rep(), NOW - timedelta(minutes=1))


def test_current_skips_udp_ephemeral_and_container_proxies():
    rep = _rep(ports=(22, 80, 45000, 9000), procs={"22": "sshd", "80": "docker-proxy", "9000": ""})
    assert listen_watch.current(rep) == {"tcp:22": {"port": 22, "proc": "sshd"},
                                         "tcp:9000": {"port": 9000, "proc": ""}}
    assert listen_watch.current({"mem_total": 1}) is None  # агент без списка портов
    assert listen_watch.update({"tcp:22": {}}, {"mem_total": 1}, NOW) is None


def test_gone_port_alerts_after_grace_and_names_the_process():
    st = _week_old()
    st = listen_watch.update(st, _rep(ports=(22,)), NOW)
    assert st["tcp:8080"]["miss"] == NOW.isoformat()
    assert listen_watch.problems(_srv({"listen_hist": st}), NOW + timedelta(minutes=10)) == []  # еще рано
    p = listen_watch.problems(_srv({"listen_hist": st}), NOW + timedelta(minutes=20))
    assert [x["key"] for x in p] == ["tcp:8080"]
    assert p[0]["text"].startswith("порт 8080 (python3) слушался 9 дн и пропал 20 мин назад")
    # заглушен точечно - молчим
    assert listen_watch.problems(_srv({"listen_hist": st}, mutes=["listen:8080"]), NOW + timedelta(minutes=20)) == []
    # вернулся - проблемы нет, стаж не потерян
    back = listen_watch.update(st, _rep(), NOW + timedelta(minutes=30))
    assert "miss" not in back["tcp:8080"] and back["tcp:8080"]["first"] == st["tcp:8080"]["first"]
    assert listen_watch.problems(_srv({"listen_hist": back}), NOW + timedelta(hours=1)) == []


def test_young_port_and_forgotten_port_stay_quiet():
    st = listen_watch.update(None, _rep(), NOW - timedelta(days=2))
    st = listen_watch.update(st, _rep(ports=(22,)), NOW)
    assert listen_watch.problems(_srv({"listen_hist": st}), NOW + timedelta(hours=1)) == []  # слушался 2 дня
    old = listen_watch.update(_week_old(), _rep(ports=(22,)), NOW)
    old = listen_watch.update(old, _rep(ports=(22,)), NOW + timedelta(days=8))
    assert "tcp:8080" not in old  # через неделю после пропажи забыли


def test_gap_in_reports_counts_from_first_report_without_port():
    """После перезагрузки (агент молчал час) сервис еще стартует: алерт не с первого отчета"""
    st = _week_old()
    st = listen_watch.update(st, _rep(ports=(22,)), NOW + timedelta(hours=1))
    assert listen_watch.problems(_srv({"listen_hist": st}), NOW + timedelta(hours=1, minutes=1)) == []
    st = listen_watch.update(st, _rep(), NOW + timedelta(hours=1, minutes=2))
    assert listen_watch.problems(_srv({"listen_hist": st}), NOW + timedelta(hours=2)) == []


def test_port_back_after_long_absence_starts_over():
    st = _week_old()
    st = listen_watch.update(st, _rep(ports=(22,)), NOW)
    st = listen_watch.update(st, _rep(), NOW + timedelta(days=2))
    assert st["tcp:8080"]["first"] == (NOW + timedelta(days=2)).isoformat()


def test_truncated_list_says_nothing_about_absent_ports():
    st = _week_old()
    many = [{"p": "tcp", "ip": "0.0.0.0", "port": p} for p in range(1000, 1150)]
    st = listen_watch.update(st, _rep(ports=(22,), listen=many + [{"p": "tcp", "ip": "0.0.0.0", "port": 22}]), NOW)
    assert "miss" not in st["tcp:8080"]
    assert listen_watch.problems(_srv({"listen_hist": st}), NOW + timedelta(hours=1)) == []


def test_live_ports_are_not_rewritten_every_tick():
    st = _week_old()
    assert listen_watch.update(st, _rep(), NOW) == st
    later = listen_watch.update(st, _rep(), NOW + timedelta(minutes=10))
    assert later["tcp:22"]["last"] == (NOW + timedelta(minutes=10)).isoformat()


def test_condition_and_home_row():
    st = listen_watch.update(_week_old(), _rep(ports=(22,)), NOW - timedelta(minutes=20))
    s = _srv({"listen_hist": st}, rep=_rep(ports=(22,)))
    lvl, ctx = collector._server_conditions(s, NOW)["listen_gone"]
    assert lvl == 1 and "порт 8080" in ctx["detail"] and ctx["sig"] == ["tcp:8080"]
    rows = [r for r in collector.server_problems(s, NOW) if r["kind"] == "listen_gone"]
    assert len(rows) == 1 and rows[0]["mute"] == "listen:8080" and rows[0]["sec"] == "exposure"
    assert "listen_gone" not in collector._server_conditions(_srv(), NOW)  # истории еще нет


def _disks(*devs):
    return _rep(disk_devs=[{"dev": d, "await": a, "util": u} for d, a, u in devs])


def test_disk_slow_levels_and_junk():
    assert collector.disk_slow(_disks(("sda", 12, 30))) == (0, "")
    assert collector.disk_slow(_disks(("sda", 140.4, 97), ("sdb", 20, 5))) == (
        1, "диск sda тормозит: задержка 140 мс, загружен на 97%")
    assert collector.disk_slow(_disks(("nvme0n1", 650, 100)))[0] == 2
    # сбой счетчика и виртуальные устройства не в счет
    assert collector.disk_slow(_disks(("nvme0n1", 717480, 3), ("loop3", 900, 1), ("zram0", 300, 2))) == (0, "")


def test_disk_slow_needs_sustain():
    rep = _disks(("sda", 250, 99))
    lvl, ctx = collector._server_conditions(_srv(rep=rep), NOW)["disk_slow"]
    assert lvl == 0 and ctx["since"] == NOW.isoformat()  # только началось
    held = {"disk_slow_since": (NOW - timedelta(minutes=20)).isoformat()}
    lvl, ctx = collector._server_conditions(_srv(held, rep=rep), NOW)["disk_slow"]
    assert lvl == 1 and ctx["detail"].startswith("диск sda тормозит")
    assert [r["kind"] for r in collector.server_problems(_srv(held, rep=rep), NOW)].count("disk_slow") == 1
    assert not [r for r in collector.server_problems(_srv(rep=rep), NOW) if r["kind"] == "disk_slow"]
