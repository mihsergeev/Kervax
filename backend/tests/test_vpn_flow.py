"""Клиенты VPN (vpn_flow, helper vpnflow-setup): похоже на торрент, качает очень много, заглушки,
"Что сломано", алерт, короткий текст и сам helper (разбор netlink, подсчет по клиентам, история)."""
import ipaddress
import re
import socket
import struct
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import collector, setup_scripts, srv_short, vpn_flow as vf
from app.models import Server

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())
SRC = "amnezia-awg3"
K9 = f"{SRC}|10.8.3.9"
K5 = f"{SRC}|10.8.3.5"
GB = 1024 ** 3


def _cl(ip, hp=0, h=None, flows=100, udp=10, peers=40, bt=0):
    return {"ip": ip, "flows": flows, "udp": udp, "peers": peers, "hp": hp, "bt": bt,
            "h": h if h is not None else [hp]}


def _vpn(clients=None, ts=TS):
    """Блок списка клиентов (helper vpn-setup, app/vpn.py)"""
    cl = clients if clients is not None else [
        {"name": "alice", "ip": "10.8.3.9", "hs": TS - 30, "rx": 1 * GB, "tx": 9 * GB, "ep": "203.0.113.5"},
        {"name": "bob", "ip": "10.8.3.5", "hs": TS - 30, "rx": GB, "tx": GB, "ep": "203.0.113.6"},
    ]
    return {"v": 1, "ts": ts, "svc": [{"k": "awg", "src": SRC, "role": "server", "port": 47300, "proto": "udp",
                                      "n": len(cl), "clients": cl}]}


def _rep(nets=None, ts=TS, vpn=None):
    nets = nets if nets is not None else [{"where": f"container {SRC}", "ifaces": ["awg0 10.8.3.0/24"], "flows": 900,
                                           "clients": [_cl("10.8.3.9"), _cl("10.8.3.5")]}]
    return {"mem_total": 8 * GB, "mem_used": GB, "clock_unix": TS,
            "docker": {"present": True, "access": True, "containers": [
                {"name": SRC, "image": "acontrol-awg3", "state": "running",
                 "ports": [{"ip": "0.0.0.0", "pub": 47300, "priv": 47300, "proto": "udp"}]}]},
            "extras": {"vpnflow": {"v": 1, "ts": ts, "nets": nets, "skipped": []},
                       "vpn": vpn if vpn is not None else _vpn()}}


def _torrent_rep(h, hp=None, ip="10.8.3.9"):
    hp = h[-1] if hp is None else hp
    return _rep([{"where": f"container {SRC}", "ifaces": ["awg0 10.8.3.0/24"], "flows": 3000,
                  "clients": [_cl(ip, hp=hp, h=h, flows=1800, udp=1100, peers=420, bt=4), _cl("10.8.3.5")]}])


def _srv(rep=None, mutes=None, state=None):
    return Server(id=1, name="vpn-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_flows_join_names_from_the_vpn_list():
    fl = vf.flows(_rep())
    a = next(x for x in fl if x["ip"] == "10.8.3.9")
    assert a["key"] == K9 and a["name"] == "alice" and a["ep"] == "203.0.113.5" and a["src"] == SRC
    # клиент, которого нет в списке (helper vpn-setup еще не стоит), - без имени, но виден
    rep = _rep(vpn={"v": 1, "ts": TS, "svc": []})
    assert [x["name"] for x in vf.flows(rep)] == ["", ""]
    assert vf.who_text({"ip": "10.8.3.9", "name": "", "ep": "203.0.113.5"}) == "10.8.3.9 (с 203.0.113.5)"
    assert vf.who_text({"ip": "10.8.3.9", "name": "alice", "ep": "203.0.113.5:51820"}) == "alice (10.8.3.9, с 203.0.113.5)"


def test_torrent_needs_ten_minutes_of_fifteen():
    # обычные клиенты: hp 0-3 - тихо
    assert vf.torrent_problems(_srv(), NOW) == []
    # всплеск на пару минут - не торрент
    s = _srv(_torrent_rep([0] * 10 + [400, 500, 0, 0, 0]))
    assert vf.torrent_problems(s, NOW) == []
    # девять минут из пятнадцати - еще нет, десять - да
    assert vf.torrent_problems(_srv(_torrent_rep([0] * 6 + [300] * 9)), NOW) == []
    p = vf.torrent_problems(_srv(_torrent_rep([0] * 5 + [300] * 10)), NOW)
    assert [x["key"] for x in p] == [K9] and p[0]["level"] == 1
    assert p[0]["text"] == ("Похоже, качает торрент: alice (10.8.3.9, с 203.0.113.5) в amnezia-awg3 - 300 адресов"
                            " на высоких портах, 61% UDP, 1800 потоков, 4 на портах BitTorrent")
    # порог - TORRENT_HP разных адресов
    assert vf.torrent_problems(_srv(_torrent_rep([vf.TORRENT_HP - 1] * 15)), NOW) == []
    assert len(vf.torrent_problems(_srv(_torrent_rep([vf.TORRENT_HP] * 15)), NOW)) == 1


def test_torrent_held_until_it_stops():
    # объявленный держится, пока признак есть хотя бы 3 минуты из 15, без мигания алерта
    rep = _torrent_rep([300] * 3 + [0] * 12)
    assert vf.torrent_problems(_srv(rep), NOW) == []
    assert len(vf.torrent_problems(_srv(rep, state={"vpn_torrent_sig": [K9]}), NOW)) == 1
    assert vf.torrent_problems(_srv(_torrent_rep([300] * 2 + [0] * 13), state={"vpn_torrent_sig": [K9]}), NOW) == []


def test_torrent_mute_snooze_and_home():
    s = _srv(_torrent_rep([300] * 15))
    rows = [x for x in collector.server_problems(s, NOW) if x["kind"] == "vpn_torrent"]
    assert len(rows) == 1 and rows[0]["mute"] == "torrent:" + K9 and rows[0]["sec"] == "vpnflow"
    s.alert_mutes = ["torrent:" + K9]
    assert vf.torrent_problems(s, NOW) == []
    v = vf.view(s, NOW)
    r = next(x for x in v["clients"] if x["key"] == K9)
    assert r["mute_t"] == {"perm": True} and "torrent" not in r and r["hot"] == 15
    s.alert_mutes = []
    until = (NOW + timedelta(days=1)).isoformat()
    s.alert_snoozes = {"torrent:" + K9: until}
    assert vf.torrent_problems(s, NOW) == []
    assert next(x for x in vf.view(s, NOW)["clients"] if x["key"] == K9)["mute_t"] == {"until": until}
    s.alert_snoozes = {"torrent:" + K9: (NOW - timedelta(minutes=1)).isoformat()}
    assert len(vf.torrent_problems(s, NOW)) == 1
    from app.schemas import AlertSnoozeIn
    assert AlertSnoozeIn(kind="torrent:" + K9, hours=24).kind == "torrent:" + K9


def test_stale_block_is_not_judged():
    s = _srv(_rep([{"where": f"container {SRC}", "ifaces": [], "flows": 1,
                    "clients": [_cl("10.8.3.9", hp=500, h=[500] * 15)]}], ts=TS - 3600))
    assert vf.torrent_problems(s, NOW) == []


def _feed(rep, minutes, step_bytes, start=None, state=None):
    """Отчеты раз в минуту; у alice счетчик растет на step_bytes за минуту"""
    st = state
    t0 = start or NOW - timedelta(minutes=minutes)
    base = 10 * GB
    for m in range(minutes + 1):
        cl = [{"name": "alice", "ip": "10.8.3.9", "hs": TS, "rx": GB, "tx": base + m * step_bytes, "ep": "203.0.113.5"},
              {"name": "bob", "ip": "10.8.3.5", "hs": TS, "rx": GB, "tx": GB, "ep": "203.0.113.6"}]
        now = t0 + timedelta(minutes=m)
        rep["extras"]["vpn"] = _vpn(cl, ts=int(now.timestamp()))
        rep["clock_unix"] = int(now.timestamp())
        st = vf.update(st, rep, now)
    rep["clock_unix"] = TS
    rep["extras"]["vpn"]["ts"] = TS
    return st


def test_heavy_traffic_over_three_hours():
    rep = _rep()
    # 200мб в минуту три часа подряд = 35гб: много
    st = _feed(rep, 180, 200 * 1024 ** 2)
    assert len(st[K9]) >= 36 and len(st[K5]) >= 36
    vol, span = vf.volume(st, K9, NOW)
    assert 34 * GB < vol < 36 * GB and span >= 3 * 3600 - 600
    p = vf.heavy_problems(_srv(rep, state={"vpnctr": st}), NOW)
    assert [x["key"] for x in p] == [K9]
    assert p[0]["text"].startswith("Клиент VPN качает очень много: alice (10.8.3.9, с 203.0.113.5) в amnezia-awg3 - 3")
    assert p[0]["text"].endswith("35гб за 3 ч")
    # 100мб в минуту - 17гб за 3 часа: не повод
    assert vf.heavy_problems(_srv(rep, state={"vpnctr": _feed(_rep(), 180, 100 * 1024 ** 2)}), NOW) == []
    # объявленный держится до половины порога
    st2 = _feed(_rep(), 180, 100 * 1024 ** 2)
    assert len(vf.heavy_problems(_srv(rep, state={"vpnctr": st2, "vpn_heavy_sig": [K9]}), NOW)) == 1
    # заглушка по клиенту
    assert vf.heavy_problems(_srv(rep, mutes=["vpnheavy:" + K9], state={"vpnctr": st}), NOW) == []


def test_heavy_needs_the_whole_window():
    # 10гб за 20 минут - всплеск (скачали игру), окно в 3 часа еще не видно: молчим
    rep = _rep()
    st = _feed(rep, 20, 2 * GB)
    assert vf.heavy_problems(_srv(rep, state={"vpnctr": st}), NOW) == []


def test_counter_reset_starts_over():
    rep = _rep()
    st = _feed(rep, 30, 500 * 1024 ** 2)
    # контейнер перезапустили: счетчики с нуля - не минус и не гигантский прирост
    rep["extras"]["vpn"] = _vpn([{"name": "alice", "ip": "10.8.3.9", "hs": TS, "rx": 0, "tx": 1000, "ep": ""}])
    st = vf.update(st, rep, NOW + timedelta(minutes=6))
    assert st[K9] == [[TS + 360, 1000]] and vf.volume(st, K9, NOW + timedelta(minutes=6)) == (0, 0)
    # пропавший клиент уходит из состояния после KEEP_S
    st = vf.update(st, rep, NOW + timedelta(seconds=vf.KEEP_S + 3600))
    assert K5 not in st


def test_alert_condition_and_short_text():
    s = _srv(_torrent_rep([300] * 15))
    lv, ctx = collector._server_conditions(s, NOW)["vpn_torrent"]
    assert lv == 1 and ctx["sig"] == [K9] and ctx["detail"].startswith("Похоже, качает торрент: alice")
    assert collector._server_conditions(_srv(), NOW)["vpn_torrent"][0] == 0
    what, facts = srv_short.short("vpn_torrent", ctx, s, ctx["detail"], lv, NOW)
    assert what == "Похоже, качает торрент"
    assert facts == ["Клиент: alice (10.8.3.9, с 203.0.113.5)",
                     "300 адресов на высоких портах, 61% UDP, 1800 потоков, 4 на портах BitTorrent"]
    assert srv_short.icon("vpn_torrent") == "⚠️🧲" and srv_short.icon("vpn_heavy") == "⚠️📶"
    assert srv_short.RECOVERY["vpn_torrent"] and collector.own_helper_unit("kervax-vpnflow.service")
    # отбой и текст в "Что сломано" - только символы клавиатуры
    for t in (ctx["detail"], *facts, srv_short.RECOVERY["vpn_torrent"], srv_short.RECOVERY["vpn_heavy"]):
        assert not set(t) & set("—–«»“”→…ё")
    # двое качают: второй в конкретике
    rep = _rep([{"where": f"container {SRC}", "ifaces": [], "flows": 3000,
                 "clients": [_cl("10.8.3.9", hp=300, h=[300] * 15), _cl("10.8.3.5", hp=200, h=[200] * 15)]}])
    ctx = collector._server_conditions(_srv(rep), NOW)["vpn_torrent"][1]
    assert ctx["sig"] == [K9, K5]
    assert srv_short.short("vpn_torrent", ctx, _srv(rep), ctx["detail"], 1, NOW)[1][1] == "Еще: bob"


def test_heavy_short_text():
    rep = _rep()
    st = _feed(rep, 180, 200 * 1024 ** 2)
    s = _srv(rep, state={"vpnctr": st})
    lv, ctx = collector._server_conditions(s, NOW)["vpn_heavy"]
    assert lv == 1 and ctx["sig"] == [K9]
    what, facts = srv_short.short("vpn_heavy", ctx, s, ctx["detail"], lv, NOW)
    assert what == "Клиент VPN качает очень много" and facts[0] == "Клиент: alice (10.8.3.9, с 203.0.113.5)"
    assert facts[1].endswith("35гб за 3 ч")


def test_view_for_the_card():
    rep = _torrent_rep([300] * 15)
    st = _feed(rep, 180, 200 * 1024 ** 2)
    s = _srv(rep, state={"vpnctr": st})
    v = vf.view(s, NOW)
    assert v["helper"] and v["torrent_hp"] == vf.TORRENT_HP
    a, b = v["clients"][:2]
    assert a["key"] == K9 and a["torrent"] and a["heavy"] and a["vol"] > 30 * GB and a["hp"] == 300
    assert b["key"] == K5 and "torrent" not in b
    # без блока vpnflow (helper не стоит), но со списком клиентов - только трафик
    rep2 = _rep()
    del rep2["extras"]["vpnflow"]
    v2 = vf.view(_srv(rep2, state={"vpnctr": st}), NOW)
    assert not v2["helper"] and {x["key"] for x in v2["clients"]} == {K9, K5} and "flows" not in v2["clients"][0]
    # ни того, ни другого - карточки нет
    assert vf.view(_srv({"clock_unix": TS}), NOW) is None


def test_helper_is_needed_where_vpn_is_served():
    assert setup_scripts.setup_needed("vpnflow-setup", _rep())
    assert not setup_scripts.setup_needed("vpnflow-setup", {"docker": {"containers": []}})
    assert "vpnflow-setup" in setup_scripts._HELPER_LABEL


# ---- сам helper ----

ROOT = Path(__file__).resolve().parents[2]


def _helper():
    src = (ROOT / "agent" / "vpnflow-setup.sh").read_text(encoding="utf-8")
    py = src.split("<<'HELPER_EOF'\n", 1)[1].split("\nHELPER_EOF\n", 1)[0]
    mod = types.ModuleType("kervax_vpnflow")
    exec(compile(py, "kervax-vpnflow", "exec"), mod.__dict__)
    return src, py, mod


def _nla(tp, payload, nested=False):
    ln = 4 + len(payload)
    return struct.pack("=HH", ln, tp | (0x8000 if nested else 0)) + payload + b"\0" * ((4 - ln % 4) % 4)


def _ct(src, dst, proto, sport, dport, v6=False):
    fam = socket.AF_INET6 if v6 else socket.AF_INET
    ip = _nla(3 if v6 else 1, socket.inet_pton(fam, src)) + _nla(4 if v6 else 2, socket.inet_pton(fam, dst))
    pr = _nla(1, bytes([proto])) + _nla(2, struct.pack(">H", sport)) + _nla(3, struct.pack(">H", dport))
    orig = _nla(1, _nla(1, ip, True) + _nla(2, pr, True), True)
    # счетчики и статус после кортежа: разбор их пропускает
    body = struct.pack("=BBH", fam, 0, 0) + orig + _nla(3, struct.pack(">I", 0x8))
    return struct.pack("=IHHII", 16 + len(body), (1 << 8) | 0, 2, 1, 0) + body


def test_helper_parses_netlink_and_counts_clients():
    _src, _py, h = _helper()
    data = (_ct("10.8.3.9", "198.51.100.1", 17, 40000, 6881) + _ct("10.8.3.9", "198.51.100.2", 17, 40000, 51413)
            + _ct("10.8.3.9", "198.51.100.3", 6, 40001, 443) + _ct("10.8.3.9", "198.51.100.4", 17, 40002, 3478)
            + _ct("172.17.0.3", "198.51.100.5", 6, 1, 51413) + _ct("10.8.3.0", "198.51.100.6", 6, 1, 51413)
            + _ct("fd00::9", "2001:db8::1", 17, 1, 50000, v6=True)
            + struct.pack("=IHHII", 16, 3, 2, 1, 0))  # NLMSG_DONE
    rows, done = h.parse(data)
    assert done and len(rows) == 7 and rows[0] == ("10.8.3.9", "198.51.100.1", 17, 6881)
    assert rows[-1] == ("fd00::9", "2001:db8::1", 17, 50000)
    # кусок дампа без конца - читать дальше
    assert h.parse(_ct("10.8.3.9", "198.51.100.1", 17, 1, 2))[1] is False
    nets, own, ifs = h.vpn_nets(
        "1: lo    inet 127.0.0.1/8 scope host lo\n"
        "3: eth0@if271    inet 172.17.0.3/16 brd 172.17.255.255 scope global eth0\n"
        "4: awg0    inet 10.8.3.0/24 scope global awg0\n"
        "4: awg0    inet6 fe80::1/64 scope link\n"
        "5: tun0    inet6 fd00::1/64 scope global\n")
    assert ifs == ["awg0 10.8.3.0/24", "tun0 fd00::/64"] and own == {"10.8.3.0", "fd00::1"}
    total, cl = h.tally(rows, nets, own)
    # 10.8.3.9: 4 потока, 3 UDP, 4 адреса, на высоких портах мимо служб - 2 (443 и STUN 3478 не в счет), 1 на 6881
    assert total == 7 and cl["10.8.3.9"] == [4, 3, 4, 2, 1]
    # адрес контейнера и самого интерфейса - не клиенты; IPv6-клиент в своей подсети - да
    assert set(cl) == {"10.8.3.9", "fd00::9"} and cl["fd00::9"] == [1, 1, 1, 1, 0]
    assert isinstance(nets[0], ipaddress.IPv4Network)


def test_helper_keeps_fifteen_minutes_of_history():
    _src, _py, h = _helper()
    nowm = TS // 60
    hist = {"container a|10.8.3.9": [[nowm - m, 300] for m in range(20, 0, -1)],
            "container a|10.8.3.7": [[nowm - 3, 500]], "container a|10.8.3.6": [[nowm - 30, 500]]}
    nh = {}
    rows = h.rows_of("container a", {"10.8.3.9": [10, 5, 9, 250, 0], "10.8.3.5": [3, 0, 3, 0, 0]}, hist, nowm, nh)
    assert [r["ip"] for r in rows] == ["10.8.3.9", "10.8.3.5"]
    assert rows[0]["h"] == [300] * 14 + [250] and rows[1]["h"] == [0]
    h.keep(hist, nowm, nh)
    # пауза на минуту - история остается; старше 15 минут - уходит
    assert nh["container a|10.8.3.7"] == [[nowm - 3, 500]] and "container a|10.8.3.6" not in nh
    # второй прогон в ту же минуту заменяет значение, а не добавляет
    nh2 = {}
    assert h.rows_of("container a", {"10.8.3.9": [1, 0, 1, 7, 0]}, nh, nowm, nh2)[0]["h"] == [300] * 14 + [7]


def test_helper_script():
    src, py, _h = _helper()
    assert "KERVAX_SETUP_VERSION=0.1" in src and "\r" not in src and "KERVAX_SETUP_ALWAYS" not in src
    # условие установки идет через Jinja и YAML плейбука: без $, апострофов и фигурных скобок
    when = src.split('KERVAX_SETUP_WHEN="', 1)[1].split('"\n', 1)[0]
    assert "nsenter" in when and "amnezia" in when and not set(when) & set("$'{}")
    # только сеть контейнера: ни -m, ни -p, ни файлов контейнера
    assert '"nsenter", "-t", str(pid), "-n", sys.executable, me, "--ns"' in py
    assert "/root" not in py and "docker exec" not in src
    # таймер раз в минуту, юнит под маской удаления
    assert "/etc/systemd/system/kervax-vpnflow.timer" in src and "OnUnitActiveSec=1min" in src
    # Python 3.8 (Ubuntu 20.04): без match, removeprefix и прочего новее
    assert "removeprefix" not in py and not re.search(r"^\s*match .*:$", py, re.M) and ":=" not in py
    compile(py, "kervax-vpnflow", "exec")
    assert sys.version_info >= (3, 8)
