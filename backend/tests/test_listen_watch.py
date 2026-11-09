"""Пропал порт, который слушался неделями, и диск, который устойчиво тормозит."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, listen_watch
from app.models import AlertEvent, AuditLog, Server

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


async def test_retire_exact_missing_port_from_dashboard(client, auth_headers):
    factory = client._transport.app.state.session_factory
    now = datetime.now(timezone.utc)
    miss = now - timedelta(minutes=25)
    live = listen_watch.update(None, _rep(), now - timedelta(days=10))
    live = listen_watch.update(live, _rep(), miss - timedelta(minutes=1))
    absent = _rep(ports=())
    state = {
        "listen_hist": listen_watch.update(live, absent, miss),
        "listen_gone": 1,
        "listen_gone_sig": list(live),
        "listen_gone_told": list(live),
        "listen_gone_from": miss.isoformat(),
    }
    async with factory() as session:
        s = _srv(state, mutes=["dns"], rep=absent)
        s.last_seen = now
        session.add(s)
        await session.commit()
        path = f"/api/servers/{s.id}"

    def ports(response):
        assert response.status_code == 200, response.text
        return {p["listen_port"]: p["since"] for p in response.json()["problems"] if p["kind"] == "listen_gone"}

    async def retire(port, since):
        return await client.post(f"{path}/listen/{port}/retire", headers=auth_headers, json={"missed_since": since})

    first = ports(await client.get(path, headers=auth_headers))
    assert first == {22: miss.isoformat(), 8080: miss.isoformat()}
    assert (await retire(22, now.isoformat())).status_code == 409
    assert ports(await retire(22, first[22])) == {8080: first[8080]}
    assert ports(await retire(22, first[22])) == {8080: first[8080]}
    assert ports(await client.get(path, headers=auth_headers)) == {8080: first[8080]}
    assert ports(await retire(8080, first[8080])) == {}

    async with factory() as session:
        s = await session.get(Server, 1)
        assert s.alert_state == {"listen_hist": {}}
        assert s.alert_mutes == ["dns"]
        records = (await session.scalars(select(AuditLog).where(AuditLog.action == "listen_retired"))).all()
        assert sorted(record.detail for record in records) == ["tcp:22", "tcp:8080"]
        event = (await session.scalars(select(AlertEvent).where(AlertEvent.kind == "listen_gone"))).one()
        assert event.recovery and "8080" in event.text


def test_retirement_survives_stale_tick_and_port_can_alert_again():
    absent = _rep(ports=(22,))
    history = listen_watch.update(_week_old(), absent, NOW)
    state = {"listen_hist": history, "listen_gone": 1, "listen_gone_sig": ["tcp:8080"]}
    retired = listen_watch.retire(state, "tcp:8080")
    proposed = {**state, "cpu": 1, "listen_hist": listen_watch.update(history, absent, NOW + timedelta(minutes=20))}
    merged = listen_watch.merge_tick(state, retired, proposed)
    assert merged == {**retired, "cpu": 1}
    assert listen_watch.problems(_srv(merged), NOW + timedelta(minutes=30)) == []

    returned = NOW + timedelta(hours=1)
    history = listen_watch.update(merged["listen_hist"], _rep(), returned)
    assert history["tcp:8080"]["first"] == returned.isoformat()
    history = listen_watch.update(history, _rep(), returned + timedelta(days=8))
    lost = returned + timedelta(days=8, minutes=1)
    history = listen_watch.update(history, absent, lost)
    problems = listen_watch.problems(_srv({"listen_hist": history}), lost + timedelta(minutes=20))
    assert [(item["key"], item["miss"]) for item in problems] == [("tcp:8080", lost.isoformat())]


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
