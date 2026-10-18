"""Что открыто в интернет: какие порты проверять, как понимать ответ прокси, что считать новым
портом и опасным сервисом, алерты и строки на главной."""
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, open_ports
from app.models import Server

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def _srv(rep=None, exposure=None, **kw):
    base = dict(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or {},
                alert_state={}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                web_5xx_alert_percent=0, kube_expiry_warn_days=[], external_ip="", local_ip="",
                exposure=exposure)
    base.update(kw)
    return Server(**base)


REP = {
    "ips": ["203.0.113.5", "10.0.0.5", "100.100.100.100"],
    "listen": [
        {"p": "tcp", "ip": "0.0.0.0", "port": 22, "u": "root"},
        {"p": "tcp", "ip": "0.0.0.0", "port": 5432, "u": "postgres"},
        {"p": "tcp6", "ip": "::", "port": 9100, "u": "node_exporter"},
        {"p": "tcp", "ip": "10.0.0.5", "port": 6379, "u": "redis"},  # внутренний адрес
        {"p": "tcp", "ip": "203.0.113.5", "port": 8443, "u": "www-data"},
        {"p": "udp", "ip": "0.0.0.0", "port": 51820, "u": "0"},  # UDP снаружи не проверить
    ],
    "docker": {"containers": [
        {"name": "cache", "image": "redis:7", "ports": [{"ip": "0.0.0.0", "pub": 6380, "priv": 6379, "proto": "tcp"},
                                                         {"ip": "127.0.0.1", "pub": 6381, "priv": 6379}]},
    ]},
    "kube": {"nodeports": [{"ns": "web", "name": "shop", "type": "NodePort", "port": 80, "node_port": 30080,
                            "proto": "TCP"}]},
}


def test_targets_take_what_listens_outside():
    ips, ports = open_ports.targets(_srv(REP, external_ip="198.51.100.7"))
    # внутренние, CGNAT и меш не проверяем; адрес, с которого агент ходит в панель, - да
    assert ips == ["203.0.113.5", "198.51.100.7"]
    assert sorted(ports) == [22, 5432, 6380, 8443, 9100, 30080]
    assert ports[6380] == "контейнер cache (redis:7)"
    assert ports[30080] == "сервис Kubernetes web/shop (NodePort)"
    assert ports[5432] == "процесс пользователя postgres"


def test_targets_name_the_process_from_the_helper():
    """changes-setup 0.2 видит, какой процесс держит порт; пользователь остается для classify"""
    rep = dict(REP, extras={"changes": {"v": 1, "ts": 1, "listen": {"22": "sshd", "5432": "postgres",
                                                                    "9100": "node_exporter"}}})
    _ips, ports = open_ports.targets(_srv(rep))
    assert ports[22] == "процесс sshd, пользователь root"
    assert ports[5432] == "процесс postgres"
    assert ports[9100] == "процесс node_exporter"
    assert ports[8443] == "процесс пользователя www-data"  # helper про порт не знает
    assert open_ports.classify(3306, "процесс mysqld, пользователь mysql") == ("MySQL", True)
    assert open_ports.classify(10249, "процесс kube-proxy, пользователь root") == ("kube-proxy, метрики", False)
    assert open_ports.classify(2221, "процесс sshd, пользователь root") == ("SSH", False)
    assert open_ports.classify(2376, "процесс dockerd, пользователь root") == ("Docker API", True)
    assert open_ports.classify(12375, "процесс dockerd, пользователь root") == ("Docker API", True)
    assert open_ports.classify(2221, "процесс пользователя root") == ("", False)


def test_targets_behind_nat_check_the_internal_address():
    rep = dict(REP, ips=["10.0.0.5"])
    ips, ports = open_ports.targets(_srv(rep, external_ip="198.51.100.7", local_ip="10.0.0.5"))
    # публичного адреса на интерфейсах нет: снаружи виден внутренний за 1:1 NAT облака
    assert ips == ["198.51.100.7"] and 6379 in ports


def test_classify_by_port_container_and_owner():
    assert open_ports.classify(5432, "процесс пользователя postgres") == ("PostgreSQL", True)
    assert open_ports.classify(15432, "контейнер db (bitnami/postgresql:16)") == ("PostgreSQL", True)
    assert open_ports.classify(3000, "контейнер api (postgrest/postgrest:v12)") == ("", False)
    assert open_ports.classify(9187, "контейнер e (prometheuscommunity/postgres-exporter)") == ("", False)
    assert open_ports.classify(443, "контейнер db (postgres:16)") == ("HTTPS", False)
    assert open_ports.classify(2375, "процесс пользователя root") == ("Docker API", True)
    assert open_ports.classify(22, "") == ("SSH", False)
    assert open_ports.classify(12345, "") == ("", False)


def _row(port, state="open", ip="203.0.113.5", risk=False, name=""):
    return {"ip": ip, "port": port, "state": state, "what": "", "name": name, "risk": risk}


def test_new_port_is_new_against_a_month_of_checks():
    first = open_ports.merge({}, [_row(22), _row(443)], ["203.0.113.5"], "Казахстан, Алматы", NOW)
    assert "new" not in first  # первая проверка - точка отсчета
    t1 = NOW + timedelta(hours=6)
    second = open_ports.merge(first, [_row(22), _row(443), _row(8080)], ["203.0.113.5"], "Казахстан, Алматы", t1)
    assert [x["port"] for x in second["new"]] == [8080] and second["new_ts"] == t1.isoformat()
    # прокси упал на одну проверку: все за файрволом; потом снова открыто - это не новые порты
    t2 = t1 + timedelta(hours=6)
    third = open_ports.merge(second, [_row(22, "filtered"), _row(443, "filtered"), _row(8080, "filtered")],
                             ["203.0.113.5"], "Казахстан, Алматы", t2)
    t3 = t2 + timedelta(hours=6)
    fourth = open_ports.merge(third, [_row(22), _row(443), _row(8080)], ["203.0.113.5"], "Казахстан, Алматы", t3)
    assert fourth["new_ts"] == t1.isoformat()  # новость все та же, с прошлого прироста
    # не был открыт больше месяца - снова новый
    t4 = t3 + timedelta(days=31)
    fifth = open_ports.merge(fourth, [_row(22), _row(443)], ["203.0.113.5"], "Казахстан, Алматы", t4)
    sixth = open_ports.merge(fifth, [_row(22), _row(443), _row(8080)], ["203.0.113.5"], "панель",
                             t4 + timedelta(hours=6))
    assert [x["port"] for x in sixth["new"]] == [8080]


def test_exposed_and_new_rows_on_home():
    ex = {"ts": NOW.isoformat(), "via": "Казахстан, Алматы", "own": False, "ips": ["203.0.113.5"],
          "ports": [_row(22, name="SSH"), _row(6379, risk=True, name="Redis"), _row(5432, "filtered", risk=True)],
          "new": [_row(6379, risk=True, name="Redis")], "new_ts": (NOW - timedelta(hours=2)).isoformat(),
          "seen": {}}
    s = _srv({"listen": [{}]}, ex)
    rows = {p["kind"]: p for p in collector.server_problems(s, NOW)}
    assert rows["exposed"]["level"] == 2 and rows["exposed"]["sec"] == "exposure"
    assert rows["exposed"]["text"] == "открыто в интернет: Redis 6379 на 203.0.113.5"
    assert rows["port_new"]["level"] == 2  # открыт всем - красное
    assert rows["port_new"]["text"] == "в интернет открылся новый порт: Redis 6379 на 203.0.113.5"
    lvl, ctx = collector._server_conditions(s, NOW)["exposed"]
    assert lvl == 2 and ctx["detail"] == "открыто в интернет: Redis 6379 на 203.0.113.5"
    # порт отмечен намеренным - ни алерта, ни строк
    s.alert_mutes = ["exposed:6379"]
    kinds = {p["kind"] for p in collector.server_problems(s, NOW)}
    assert not kinds & {"exposed", "port_new"}
    assert collector._server_conditions(s, NOW)["exposed"][0] == 0
    # проверка давно не шла - не судим; новость старше суток с главной уходит
    s.alert_mutes = []
    later = NOW + timedelta(days=3)
    s.last_seen = later
    kinds = {p["kind"] for p in collector.server_problems(s, later)}
    assert not kinds & {"exposed", "port_new"}
    # карточке служебная история не нужна
    assert "seen" not in open_ports.view(s) and open_ports.view(s)["muted"] == []
    assert open_ports.view(s)["own"] is False


def test_check_from_the_panel_itself_does_not_alert():
    """С адреса панели видно то, что открыто для панели: на одной ноде etcd, kubelet и
    node_exporter были открыты только для нее (белый список файрвола)"""
    ex = {"ts": NOW.isoformat(), "via": "панель", "own": True, "ips": ["203.0.113.5"],
          "ports": [_row(2380, risk=True, name="etcd"), _row(10250, risk=True, name="kubelet")],
          "new": [_row(2380, risk=True, name="etcd")], "new_ts": (NOW - timedelta(hours=2)).isoformat(),
          "seen": {}}
    s = _srv({"listen": [{}]}, ex)
    assert not {p["kind"] for p in collector.server_problems(s, NOW)} & {"exposed", "port_new"}
    lvl, ctx = collector._server_conditions(s, NOW)["exposed"]
    assert lvl == 0 and ctx["own"] is True
    assert open_ports.view(s)["own"] is True
    # горевший алерт уходит с объяснением, а не "опасных сервисов больше нет"
    assert collector._recovery_detail("exposed", {}, ctx).startswith("проверка шла с адреса панели")
    assert collector._recovery_detail("exposed", {}, {"own": False}) == (
        "опасных и служебных портов, открытых в интернет, больше нет")
    # проверки до 1.4.135 без флага: с панели - значит via "панель"
    del ex["own"]
    assert open_ports.from_panel(s) is True
    s.exposure = dict(ex, via="Казахстан, Алматы")
    assert open_ports.from_panel(s) is False


def test_labels_follow_the_fresh_report():
    """helper прислал имена процессов после проверки: подписи берутся из свежего отчета сразу"""
    old = {"ip": "203.0.113.5", "port": 2221, "state": "open", "what": "процесс пользователя root", "name": "",
           "risk": False}
    gone = {"ip": "203.0.113.5", "port": 7000, "state": "open", "what": "процесс пользователя app", "name": "",
            "risk": False}
    ex = {"ts": NOW.isoformat(), "via": "Казахстан, Алматы", "own": False, "ips": ["203.0.113.5"],
          "ports": [old, gone], "seen": {}}
    rep = {"ips": ["203.0.113.5"], "listen": [{"p": "tcp", "ip": "0.0.0.0", "port": 2221, "u": "root"},
                                               {"p": "tcp", "ip": "0.0.0.0", "port": 8132, "u": "konnectivity-server"}],
           "extras": {"changes": {"v": 1, "ts": 1, "listen": {"2221": "dockerd", "8132": "konnectivity-se"}}}}
    s = _srv(rep, ex)
    rows = {x["port"]: x for x in open_ports.view(s)["ports"]}
    assert rows[2221]["what"] == "процесс dockerd, пользователь root" and rows[2221]["name"] == "Docker API"
    assert rows[7000]["what"] == "процесс пользователя app"  # порт ушел из отчета - подпись прежняя
    # опасность тоже по свежей подписи: Docker API на 2221 - алерт без новой проверки
    assert [x["port"] for x in open_ports.exposed(s, NOW)] == [2221]
    assert s.exposure["ports"][0]["what"] == "процесс пользователя root"  # сохраненное не трогаем
    _ips, ports = open_ports.targets(s)
    assert ports[8132] == "процесс konnectivity-server"


def test_service_ports_should_be_closed_too():
    """Служебные порты Kubernetes, агентов и экспортеров снаружи не нужны: желтое и предупреждение.
    Проверка здоровья балансировщика (kube-proxy на порту NodePort) - часть сервиса, не служебное"""
    assert open_ports.label(10249, "процесс kube-proxy, пользователь root") == {
        "name": "kube-proxy, метрики", "risk": False, "svc": True}
    assert open_ports.label(8080, "процесс kube-router, пользователь root")["svc"] is True
    assert open_ports.label(6443, "процесс kube-apiserver")["svc"] is True
    assert open_ports.label(9187, "контейнер e (prometheuscommunity/postgres-exporter)") == {
        "name": "экспортер метрик", "risk": False, "svc": True}
    assert open_ports.label(9100, "процесс node_exporter, пользователь nobody") == {
        "name": "node_exporter", "risk": True, "svc": False}
    assert open_ports.label(32530, "процесс kube-proxy, пользователь root") == {
        "name": "проверка здоровья балансировщика (kube-proxy)", "risk": False, "svc": False}
    assert open_ports.label(2221, "процесс sshd, пользователь root") == {"name": "SSH", "risk": False, "svc": False}
    assert open_ports.label(30300, "сервис Kubernetes monitoring/grafana (NodePort)")["svc"] is False

    rep = {"ips": ["203.0.113.5"], "listen": [{"p": "tcp6", "ip": "::", "port": 10249, "u": "root"},
                                               {"p": "tcp6", "ip": "::", "port": 2380, "u": "etcd"}],
           "extras": {"changes": {"v": 1, "ts": 1, "listen": {"10249": "kube-proxy", "2380": "etcd"}}}}
    svc_row = {"ip": "203.0.113.5", "port": 10249, "state": "open", "what": "", "name": "", "risk": False}
    etcd_row = {"ip": "203.0.113.5", "port": 2380, "state": "open", "what": "", "name": "", "risk": False}
    ex = {"ts": NOW.isoformat(), "via": "Казахстан, Алматы", "own": False, "ips": ["203.0.113.5"],
          "ports": [svc_row], "seen": {}}
    s = _srv(rep, ex)
    lvl, ctx = collector._server_conditions(s, NOW)["exposed"]
    assert lvl == 1 and ctx["detail"] == (
        "служебное открыто в интернет: kube-proxy, метрики 10249 (процесс kube-proxy, пользователь root) на 203.0.113.5")
    rows = [p for p in collector.server_problems(s, NOW) if p["kind"] == "exposed"]
    assert [p["level"] for p in rows] == [1]
    # открылся и etcd - уже проблема, в тексте сначала опасное
    s.exposure = dict(ex, ports=[svc_row, etcd_row])
    lvl, ctx = collector._server_conditions(s, NOW)["exposed"]
    assert lvl == 2 and ctx["detail"].startswith("открыто в интернет: etcd 2380 (процесс etcd) на 203.0.113.5; служебное")
    # служебный порт открыт намеренно - остается только etcd
    s.alert_mutes = ["exposed:10249"]
    assert [x["port"] for x in open_ports.exposed(s, NOW)] == [2380]


async def test_scan_without_proxy_marks_own_check(monkeypatch):
    async def fake_probe(ip, port, proxy):
        return "open"

    monkeypatch.setattr(open_ports, "probe", fake_probe)
    srv = _srv(REP)
    out = await open_ports.scan_server(srv, None, "панель", NOW)
    assert out["own"] is True
    out = await open_ports.scan_server(srv, "socks5://127.0.0.1:1080", "Казахстан, Алматы", NOW)
    assert out["own"] is False


def test_text_names_unknown_ports():
    assert open_ports.new_text([_row(8080), _row(6379, risk=True, name="Redis")]) == (
        "Redis 6379 на 203.0.113.5; порт 8080 на 203.0.113.5")


# ---- проверка порта ----

async def _serve(handler):
    srv = await asyncio.start_server(handler, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def test_direct_probe_open_and_closed():
    async def h(r, w):
        w.close()

    srv, port = await _serve(h)
    async with srv:
        assert await open_ports._direct("127.0.0.1", port) == "open"
    assert await open_ports._direct("127.0.0.1", port) == "closed"


async def test_http_connect_answers():
    for code, want in ((200, "open"), (502, "closed"), (504, "filtered"), (407, "unknown")):
        seen: list[bytes] = []

        async def h(r, w, code=code):
            seen.append(await r.readuntil(b"\r\n\r\n"))
            w.write(f"HTTP/1.1 {code} X\r\n\r\n".encode())
            await w.drain()
            w.close()

        srv, port = await _serve(h)
        async with srv:
            got = await open_ports._via_proxy(f"http://u:p%40ss@127.0.0.1:{port}", "203.0.113.5", 6379)
        assert got == want, code
        assert seen[0].startswith(b"CONNECT 203.0.113.5:6379 HTTP/1.1\r\n")
        assert b"Proxy-Authorization: Basic dTpwQHNz\r\n" in seen[0]  # u:p@ss


async def test_socks5_answers():
    for rep_code, want in ((0, "open"), (5, "closed"), (4, "filtered"), (1, "unknown")):
        got_req: list[bytes] = []

        async def h(r, w, rep_code=rep_code):
            await r.readexactly(3)
            w.write(b"\x05\x00")
            await w.drain()
            got_req.append(await r.readexactly(10))
            w.write(bytes([5, rep_code, 0, 1, 0, 0, 0, 0, 0, 0]))
            await w.drain()
            w.close()

        srv, port = await _serve(h)
        async with srv:
            got = await open_ports._via_proxy(f"socks5://127.0.0.1:{port}", "203.0.113.5", 6379)
        assert got == want, rep_code
        assert got_req[0] == b"\x05\x01\x00\x01" + bytes([203, 0, 113, 5]) + (6379).to_bytes(2, "big")


async def test_proxy_only_no_direct_fallback():
    async def h(r, w):
        w.close()

    srv, port = await _serve(h)
    async with srv:
        # прокси не отвечает: не знаем, а не "открыт" - напрямую панель видит то, что открыто
        # только для нее
        dead = await _serve(h)
        dead[0].close()
        await dead[0].wait_closed()
        assert await open_ports.probe("127.0.0.1", port, f"socks5://127.0.0.1:{dead[1]}") == "unknown"
        assert await open_ports.probe("127.0.0.1", port, None) == "open"


async def test_pick_proxy_prefers_socks(tmp_path):
    from app.db import Base, create_engine_and_factory
    from app.models import Location

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'x.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        assert await open_ports.pick_proxy(s) == (None, "панель")
        s.add_all([Location(name="web", url="http://203.0.113.9:3128", enabled=True),
                   Location(name="off", url="socks5://203.0.113.8:1080", enabled=False),
                   Location(name="sock", url="socks5://203.0.113.7:1080", enabled=True)])
        await s.commit()
        assert await open_ports.pick_proxy(s) == ("socks5://203.0.113.7:1080", "sock")
        # локация только для портов (мониторы через нее не ходят) - первой, даже HTTP
        s.add(Location(name="ports", url="http://203.0.113.6:3128", enabled=True, monitors=False))
        await s.commit()
        assert await open_ports.pick_proxy(s) == ("http://203.0.113.6:3128", "ports")
    await engine.dispose()


async def test_ports_only_location_is_not_used_by_monitors(tmp_path, monkeypatch):
    """Сайты, открытые лишь сети панели, через локацию "только порты" не проверяются"""
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Check, Location

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'm.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        s.add_all([Location(name="ports", url="http://203.0.113.6:3128", enabled=True, monitors=False),
                   Check(name="dev", type="http", target="https://dev.example.com", enabled=True,
                         check_locations=True)])
        await s.commit()
    probed: list[str] = []

    async def fake_probe(check, proxy):
        probed.append(proxy)

    monkeypatch.setattr("app.checks.probe_via_proxy", fake_probe)
    assert await collector.run_location_probes(factory, Settings()) == 0
    assert probed == []
    await engine.dispose()


# ---- алерт "новый порт" ----

async def test_port_new_alert_once_per_check(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'p.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    base = open_ports.merge({}, [_row(22, name="SSH")], ["203.0.113.5"], "Казахстан, Алматы", now - timedelta(hours=6))
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report={"uptime_seconds": 100000},
                     exposure=base))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")
    await collector.evaluate_servers(factory, cfg, now)
    assert sent == []
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        srv.exposure = open_ports.merge(srv.exposure, [_row(22, name="SSH"), _row(6379, risk=True, name="Redis")],
                                        ["203.0.113.5"], "Казахстан, Алматы", now)
        await s.commit()
    await collector.evaluate_servers(factory, cfg, now + timedelta(seconds=30))
    news = [m for m in sent if m.startswith("🔴🚪")]
    assert len(news) == 1 and "в интернет открылся новый порт: Redis 6379 на 203.0.113.5" in news[0], sent
    # опасный сервис - еще и состояние (без выдержки: проверка раз в 6 часов)
    assert any(m.startswith("🔓") and "открыто в интернет: Redis 6379" in m for m in sent), sent
    n = len(sent)
    await collector.evaluate_servers(factory, cfg, now + timedelta(seconds=60))
    assert len(sent) == n  # та же проверка - второго сообщения нет
    await engine.dispose()


async def test_ssh_banner_direct_and_through_socks():
    """Баннер SSH читается и напрямую, и через socks5: ответ прокси (с адресом привязки) должен быть
    прочитан целиком, иначе его хвост принимался бы за начало баннера"""
    async def sshd(r, w):
        w.write(b"SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.19\r\n")
        await w.drain()
        w.close()

    async def socks(r, w):
        await r.readexactly(3)  # ver, 1 method, no auth
        w.write(b"\x05\x00")
        await w.drain()
        await r.readexactly(4 + 4 + 2)  # CONNECT IPv4
        w.write(b"\x05\x00\x00\x01" + bytes([10, 0, 0, 1]) + (4242).to_bytes(2, "big"))
        w.write(b"SSH-2.0-OpenSSH_7.4\r\n")  # дальше в туннеле - сам сервис
        await w.drain()
        w.close()

    srv, port = await _serve(sshd)
    async with srv:
        assert await open_ports.ssh_banner("127.0.0.1", port, None) == "SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.19"
    proxy, pport = await _serve(socks)
    async with proxy:
        assert await open_ports.ssh_banner("203.0.113.5", 2221, f"socks5://127.0.0.1:{pport}") == "SSH-2.0-OpenSSH_7.4"
    # не SSH - не баннер
    async def http(r, w):
        w.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await w.drain()
        w.close()
    srv2, port2 = await _serve(http)
    async with srv2:
        assert await open_ports.ssh_banner("127.0.0.1", port2, None) == ""


# ---- порты LoadBalancer и сайты через Cloudflare ----

LB_REP = {
    "ips": ["203.0.113.5"],
    "listen": [{"p": "tcp", "ip": "0.0.0.0", "port": 2221, "u": "root"}],
    "kube": {"nodeports": [
        {"ns": "default", "name": "envoy-main", "type": "LoadBalancer", "port": 80, "node_port": 31196, "proto": "TCP"},
        {"ns": "default", "name": "envoy-main", "type": "LoadBalancer", "port": 443, "node_port": 31996, "proto": "TCP"},
        {"ns": "default", "name": "metrics", "type": "NodePort", "port": 9100, "node_port": 30100, "proto": "TCP"},
    ]},
    "web_services": [{"kind": "Envoy", "source": "kubernetes", "sites": ["a.example.com", "b.example.com"]},
                     {"kind": "nginx", "sites": ["*.example.com", "203.0.113.5", "c.example.org"]}],
}


def test_targets_take_load_balancer_ports():
    """Шлюз в кластере слушает 80 и 443 через DNAT kube-proxy: сокета нет, а снаружи порт есть"""
    _ips, ports = open_ports.targets(_srv(LB_REP))
    assert sorted(ports) == [80, 443, 2221, 30100, 31196, 31996]
    assert ports[443] == "сервис Kubernetes default/envoy-main (LoadBalancer)"
    assert ports[30100] == "сервис Kubernetes default/metrics (NodePort)"
    assert open_ports.label(443, ports[443]) == {"name": "HTTPS", "risk": False, "svc": False}
    # ingress-nginx в сети хоста слушает 443 сам: подпись - процесс
    rep = dict(LB_REP, listen=[*LB_REP["listen"], {"p": "tcp", "ip": "0.0.0.0", "port": 443, "u": "www-data"}])
    assert open_ports.targets(_srv(rep))[1][443] == "процесс пользователя www-data"


def test_new_rules_are_a_baseline_not_new_ports():
    """Старая проверка (до порта LoadBalancer): 443 был открыт и раньше, просто не проверялся"""
    old = {"ts": NOW.isoformat(), "via": "Казахстан, Алматы", "ips": ["203.0.113.5"], "ports": [_row(2221)],
           "seen": {"203.0.113.5|2221": NOW.isoformat()}}
    t1 = NOW + timedelta(hours=1)
    first = open_ports.merge(old, [_row(2221), _row(443)], ["203.0.113.5"], "Казахстан, Алматы", t1)
    assert "new" not in first and first["rules"] == open_ports.RULES and "203.0.113.5|443" in first["seen"]
    second = open_ports.merge(first, [_row(2221), _row(443), _row(8080)], ["203.0.113.5"], "Казахстан, Алматы",
                              t1 + timedelta(hours=6))
    assert [x["port"] for x in second["new"]] == [8080]


def test_site_names_and_cloudflare_addresses():
    assert open_ports.site_names(_srv(LB_REP)) == ["a.example.com", "b.example.com", "c.example.org"]
    assert open_ports.is_cloudflare("188.114.96.1") and open_ports.is_cloudflare("104.18.30.120")
    assert open_ports.is_cloudflare("2606:4700::6810:1e78") and open_ports.is_cloudflare("8.6.112.0")
    assert not open_ports.is_cloudflare("203.0.113.5") and not open_ports.is_cloudflare("nope")


async def test_cloudflare_only_and_bypass(monkeypatch):
    """Сайты идут через Cloudflare: 443 напрямую закрыт - открыт только для Cloudflare, 80 открыт -
    Cloudflare можно обойти. Без таких сайтов отметок нет."""
    dns = {"a.example.com": ["188.114.96.1", "2606:4700::1"], "b.example.com": ["188.114.97.4"],
           "c.example.org": ["203.0.113.5"]}

    async def fake_resolve(name):
        return dns.get(name, [])

    async def fake_probe(ip, port, proxy):
        return "open" if port in (80, 2221) else "filtered"

    monkeypatch.setattr(open_ports, "_resolve", fake_resolve)
    monkeypatch.setattr(open_ports, "probe", fake_probe)
    srv = _srv(LB_REP)
    srv.exposure = await open_ports.scan_server(srv, "socks5://127.0.0.1:1080", "Казахстан, Алматы", NOW)
    assert srv.exposure["cf"] == {"sites": ["a.example.com", "b.example.com"], "n": 2, "checked": 3, "total": 3}
    marks = {x["port"]: x.get("cf") for x in open_ports.view(srv)["ports"]}
    assert marks == {80: "also", 443: "only", 2221: None, 31196: None, 31996: None, 30100: None}
    assert open_ports.exposed(srv, NOW) == []  # закрытый для мира порт - не алерт

    dns.update({"a.example.com": ["203.0.113.5"], "b.example.com": []})
    srv.exposure = await open_ports.scan_server(srv, "socks5://127.0.0.1:1080", "Казахстан, Алматы", NOW)
    assert srv.exposure["cf"]["n"] == 0
    assert all("cf" not in x for x in open_ports.view(srv)["ports"])


# ---- белый список с адресом проверки (changes-setup 0.7) ----

FW = {"src": "iptables", "p4": "DROP", "p6": "DROP", "idrop": "46", "fdrop": "4",
      "in": ["4 198.51.100.20/32 2221", "4 198.51.100.20/32 64100", "46 * 80", "4 192.0.2.0/24 *"],
      "fwd": ["4 198.51.100.20/32 8000", "4 * 9000"]}
VIA = ["198.51.100.20"]


def _fw_rep(fw=None):
    return {"ips": ["203.0.113.5"],
            "listen": [{"p": "tcp", "ip": "0.0.0.0", "port": 2221, "u": "root"},
                       {"p": "tcp", "ip": "0.0.0.0", "port": 80, "u": "www-data"},
                       {"p": "tcp", "ip": "0.0.0.0", "port": 5432, "u": "postgres"}],
            "docker": {"containers": [{"name": "rest", "image": "restic/rest-server", "ports": [
                {"ip": "0.0.0.0", "pub": 64100, "priv": 8000, "proto": "tcp"},
                {"ip": "0.0.0.0", "pub": 9001, "priv": 9000, "proto": "tcp"}]}]},
            "kube": {"nodeports": [{"ns": "web", "name": "shop", "type": "NodePort", "port": 80, "node_port": 30080,
                                    "proto": "TCP"}]},
            "extras": {"changes": {"v": 1, "ts": 1, "listen": {"2221": "sshd"}, "fw": FW if fw is None else fw}}}


def test_only_allowlist_with_the_check_address():
    s = _srv(_fw_rep())

    def ok(port, ip="203.0.113.5", via=VIA, srv=s):
        return open_ports.only_allowlist(srv, ip, port, via)

    assert ok(2221)  # SSH пускают только по белому списку, а в нем адрес проверки
    assert ok(64100)  # контейнер: правило ufw-docker на порт контейнера 8000
    assert not ok(80)  # открыт всем
    assert not ok(9001)  # контейнер, а порт 9000 открыт всем
    assert not ok(5432)  # правила для адреса проверки нет, а порт открыт: правила не все - считаем открытым
    assert not ok(30080)  # порт Kubernetes: правила kube-proxy не разбираем
    assert not ok(2221, via=["198.51.100.99"])  # проверка шла с другого адреса - ей правда открыто
    assert not ok(2221, via=[])  # адреса проверки не знаем (проверка до 1.4.263)
    assert not ok(2221, ip="2001:db8::5")  # IPv6: правило для v4-адреса проверки не про него
    assert ok(5432, via=["192.0.2.7"])  # адрес проверки в разрешенной сети - тоже белый список
    # политика ACCEPT и никакого "отбросить остальное" - открыто всем, что бы ни разрешали отдельно
    acc = _srv(_fw_rep(dict(FW, p4="ACCEPT", idrop="", fdrop="")))
    assert not ok(2221, srv=acc) and not ok(64100, srv=acc)
    # ACCEPT, но в конце INPUT "-j DROP" - отказ по умолчанию
    assert ok(2221, srv=_srv(_fw_rep(dict(FW, p4="ACCEPT", idrop="4"))))
    # сеть шире /8 - не белый список
    assert not ok(2221, srv=_srv(_fw_rep(dict(FW, **{"in": ["4 0.0.0.0/1 *", "4 198.51.100.20/32 2221"]}))))
    # helper старше 0.7 - не знаем, значит открыто
    old = _fw_rep()
    del old["extras"]["changes"]["fw"]
    assert not ok(2221, srv=_srv(old))
    assert open_ports._covers("1000:2000", 1500) and open_ports._covers("8000:", 9000)
    assert open_ports._covers("22,80,443", 80) and not open_ports._covers("80,443", 8080)


def test_allowlisted_port_is_not_new_nor_exposed():
    """Сервер бэкапов: адрес проверки (он же адрес мониторов) стоял в белом списке ufw, и про SSH
    на 2221 пришло "в интернет открылся новый порт", хотя снаружи до него не достучаться"""
    s = _srv(_fw_rep())
    first = open_ports.merge({}, [_row(80)], ["203.0.113.5"], "Казахстан, Алматы", NOW - timedelta(hours=6),
                             via_ips=VIA)
    rows = open_ports.mark_allow(s, [_row(80), _row(2221), _row(5432, risk=True)], VIA)
    ex = open_ports.merge(first, rows, ["203.0.113.5"], "Казахстан, Алматы", NOW, via_ips=VIA)
    assert [x["port"] for x in ex["new"]] == [5432] and "203.0.113.5|2221" not in ex["seen"]
    s.exposure = ex
    probs = {p["kind"]: p for p in collector.server_problems(s, NOW)}
    assert probs["port_new"]["level"] == 2
    assert probs["port_new"]["text"] == "в интернет открылся новый порт: PostgreSQL 5432 (процесс пользователя postgres) на 203.0.113.5"
    v = open_ports.view(s)
    assert [x["port"] for x in v["ports"] if x.get("allow")] == [2221] and v["via_ips"] == VIA
    # опасный порт только белому списку - не "открыто в интернет"
    s.last_report["extras"]["changes"]["fw"] = dict(FW, **{"in": FW["in"] + ["4 198.51.100.20/32 5432"]})
    assert open_ports.exposed(s, NOW) == [] and open_ports.new_ports(s, NOW) == []
    # потом SSH открыли всем: на следующей проверке он новый порт
    s.last_report["extras"]["changes"]["fw"] = dict(FW, **{"in": FW["in"] + ["4 * 2221"]})
    rows = open_ports.mark_allow(s, [_row(80), _row(2221), _row(5432, risk=True)], VIA)
    ex2 = open_ports.merge(ex, rows, ["203.0.113.5"], "Казахстан, Алматы", NOW + timedelta(hours=6), via_ips=VIA)
    assert [x["port"] for x in ex2["new"]] == [2221]


def test_old_news_about_an_allowlisted_port_goes_away():
    """Новость, записанная до 1.4.263, держится до следующего прироста: после перепроверки с
    адресами проверки она про порт, открытый только белому списку, и не показывается"""
    ex = {"ts": NOW.isoformat(), "via": "Казахстан, Алматы", "own": False, "ips": ["203.0.113.5"],
          "ports": [_row(2221, name="SSH")], "new": [_row(2221, name="SSH")],
          "new_ts": (NOW - timedelta(hours=2)).isoformat(), "seen": {}, "rules": open_ports.RULES}
    s = _srv(_fw_rep(), ex)
    assert [x["port"] for x in open_ports.new_ports(s, NOW)] == [2221]  # адреса проверки еще неизвестны
    s.exposure = dict(ex, via_ips=VIA)
    assert open_ports.new_ports(s, NOW) == [] and "port_new" not in {p["kind"] for p in collector.server_problems(s, NOW)}
    assert open_ports.view(s)["new"] == []


async def test_scan_remembers_the_check_address(monkeypatch):
    """Проверка через прокси запоминает его адреса; хост прокси по имени резолвится"""
    async def fake_probe(ip, port, proxy):
        return "open"

    async def fake_resolve(name):
        return ["198.51.100.20"] if name == "proxy.example.com" else []

    async def no_banner(ip, port, proxy):
        return ""

    monkeypatch.setattr(open_ports, "probe", fake_probe)
    monkeypatch.setattr(open_ports, "_resolve", fake_resolve)
    monkeypatch.setattr(open_ports, "ssh_banner", no_banner)
    s = _srv(_fw_rep())
    ex = await open_ports.scan_server(s, "http://u:p@proxy.example.com:3128", "Казахстан, Алматы", NOW)
    assert ex["via_ips"] == VIA
    assert {x["port"] for x in ex["ports"] if x.get("allow")} == {2221, 64100}
    assert await open_ports.proxy_ips("socks5://198.51.100.30:1080") == ["198.51.100.30"]
    assert await open_ports.proxy_ips(None) == []
