"""Путь "Через GRE" в окне команды установки (app/gre.install_view, install_cmd).

Было: GRE до панели включался галочкой, а helper gre-setup, который поднимает туннель, ставился
только после агента. Сам агент (~10 МБ) на ноде за DPI, который замораживает соединение после
~16 КБ, качался напрямую часами, и туннель поднимали руками с /etc/hosts.
Стало: окно включает GRE само и выдает команду, только когда хост панели держит свою сторону;
команда до первого запроса в панель поднимает kervax-gre на ноде, а установщик и curl в ней идут
на адрес панели в туннеле с ее именем в TLS (--resolve) и пишут агенту connect=."""
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from app import config, gre

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agent"
TOKEN = "fake-token-example"
REPORT = {"hostname": "h", "os": "Ubuntu", "agent_version": "2.56", "cpu_percent": 1.0,
          "mem_used": 1, "mem_total": 2, "disks": []}
needs_sh = pytest.mark.skipif(shutil.which("sh") is None or shutil.which("mktemp") is None
                              or shutil.which("seq") is None, reason="нет sh/mktemp/seq")


def _data() -> Path:
    return Path(os.environ["KERVAX_DATA_DIR"])


def _applied(*lines: str, age: int = 0) -> None:
    (_data() / "gre_tunnels.applied").write_text(
        f"# kervax-gre fw=ufw ts={int(time.time()) - age}\n" + "".join(x + "\n" for x in lines), encoding="utf-8")


def _tunnels() -> list[str]:
    return (_data() / "gre_tunnels").read_text(encoding="utf-8").splitlines()


async def _new(client, auth_headers, name="dpi-new") -> int:
    r = await client.post("/api/servers", json={"name": name}, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()["server"]["id"]


async def _via(client, auth_headers, sid, **body) -> httpx.Response:
    return await client.post(f"/api/servers/{sid}/install-via", json=body, headers=auth_headers)


async def test_window_switches_gre_on_and_gives_the_command_only_when_the_host_is_ready(
        client: httpx.AsyncClient, auth_headers):
    sid = await _new(client, auth_headers)
    # адреса нет: вести туннель некуда, и окно просит его, а не молчит
    r = await _via(client, auth_headers, sid, mode="gre")
    assert r.status_code == 400 and "внешний IP" in r.json()["detail"]
    # частный адрес - за NAT, GRE не пройдет
    r = await _via(client, auth_headers, sid, mode="gre", ip="192.168.1.10")
    assert r.status_code == 400 and "публичный IPv4" in r.json()["detail"]

    r = await _via(client, auth_headers, sid, mode="gre", ip="198.51.100.10")
    assert r.status_code == 200, r.text
    g = r.json()["gre"]
    # хостовый скрипт не стоит вовсе: команды нет, окно говорит почему
    assert (g["state"], g["cmd"], g["connect"]) == ("host_missing", "", "")
    s = next(x for x in (await client.get("/api/servers", headers=auth_headers)).json() if x["id"] == sid)
    assert s["gre_enabled"] is True and s["gre"]["net"] == "10.254.0.0/30"
    assert f"kvxg{sid} 198.51.100.10 10.254.0.1/30 10.254.0.2" in _tunnels()

    get = lambda: client.get(f"/api/servers/{sid}/gre-install", headers=auth_headers)  # noqa: E731
    # скрипт отвечает, но давно
    _applied(age=3600)
    g = (await get()).json()
    assert g["state"] == "host_stale" and g["age"] >= 3600 and not g["cmd"]
    # отвечает, а туннеля к этой ноде еще нет (path-юнит или крон поднимет)
    _applied()
    assert (await get()).json()["state"] == "host_wait"
    # не смог
    _applied(f"kvxg{sid} 198.51.100.10 - err ip tunnel add: no such device")
    g = (await get()).json()
    assert g["state"] == "host_error" and "no such device" in g["error"]
    # держит: начало команды и адрес панели в туннеле
    _applied(f"kvxg{sid} 198.51.100.10 203.0.113.5 ok")
    g = (await get()).json()
    assert g["state"] == "ready" and g["connect"] == "10.254.0.1"
    assert g["cmd"].startswith("sh -c '") and g["cmd"].endswith("' - 203.0.113.5 10.254.0.2/30 198.51.100.10")

    # IP поправили в окне: хост ведет туннель к другому адресу, пока не переделал - ждем
    r = await _via(client, auth_headers, sid, mode="gre", ip="198.51.100.30")
    assert r.json()["gre"]["state"] == "host_wait"
    assert f"kvxg{sid} 198.51.100.30 10.254.0.1/30 10.254.0.2" in _tunnels()
    _applied(f"kvxg{sid} 198.51.100.30 203.0.113.5 ok")
    assert (await get()).json()["cmd"].endswith(" 198.51.100.30")

    # ушли на другой путь - GRE выключается (иначе helper все равно увел бы агента в туннель)
    r = await _via(client, auth_headers, sid, mode="direct")
    assert r.status_code == 200 and r.json()["gre"] is None
    assert not any(x.startswith(f"kvxg{sid} ") for x in _tunnels())
    assert (await get()).json()["state"] == "off"
    acts = [e["action"] for e in (await client.get("/api/audit", headers=auth_headers)).json()]
    assert "gre_on" in acts and "gre_off" in acts


async def test_window_takes_the_address_the_agent_came_from(client: httpx.AsyncClient, auth_headers):
    # переустановка: агент уже ходил напрямую, его адрес известен, вводить не нужно
    r = await client.post("/api/servers", json={"name": "dpi-old"}, headers=auth_headers)
    sid, tok = r.json()["server"]["id"], r.json()["token"]
    await client.post("/api/agent/report", json=REPORT,
                      headers={"Authorization": f"Bearer {tok}", "X-Forwarded-For": "198.51.100.40, 172.18.0.1"})
    _applied(f"kvxg{sid} 198.51.100.40 203.0.113.5 ok")
    g = (await _via(client, auth_headers, sid, mode="gre")).json()["gre"]
    assert g["state"] == "ready" and g["cmd"].endswith(" 198.51.100.40")


async def test_install_status_sees_downloads_through_the_tunnel(client: httpx.AsyncClient, auth_headers):
    from app import install_watch
    sid = await _new(client, auth_headers, "dpi-st")
    await _via(client, auth_headers, sid, mode="gre", ip="198.51.100.10")
    install_watch.note("10.254.0.2", "agent")
    try:
        st = (await client.get(f"/api/servers/{sid}/install-status", headers=auth_headers)).json()
        assert st["agent_at"] is not None
    finally:
        install_watch._SEEN.clear()


# --- команда: поднять туннель, потом установщик через него ---

IP_STUB = """#!/bin/sh
echo "ip $*" >> "$LOG"
case "$*" in
  "-o route get "*) echo "$4 via 192.0.2.1 dev eth0 src $SRC uid 0 \\\\    cache" ;;
esac
exit 0
"""
UFW_STUB = """#!/bin/sh
case "$1" in status) echo "Status: active" ;; *) echo "ufw $*" >> "$LOG" ;; esac
"""
CURL_STUB = """#!/bin/sh
echo "curl $*" >> "$LOG"
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
[ -z "$out" ] || printf '%s\\n' 'echo "installer: $* connect=$KERVAX_CONNECT"' > "$out"
exit 0
"""


def _bin(tmp_path: Path, src: str = "198.51.100.10") -> dict:
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    for name, body in (("ip", IP_STUB), ("ufw", UFW_STUB), ("curl", CURL_STUB)):
        (b / name).write_bytes(body.encode())
        (b / name).chmod(0o755)
    return dict(os.environ, PATH=f"{b.as_posix()}{os.pathsep}{os.environ['PATH']}",
                LOG=(tmp_path / "log").as_posix(), SRC=src)


def _with_gre(cmd: str, view: dict) -> str:
    # то же, что делает окно (frontend/src/installProxy.ts, withGre)
    return f"{view['cmd']} && " + cmd.replace("sh -c '", f"KERVAX_CONNECT={view['connect']} sh -c '", 1)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("KERVAX_DATA_DIR", (tmp_path / "data").as_posix())
    monkeypatch.setenv("KERVAX_PANEL_URL", "https://panel.example.com")
    config.get_settings.cache_clear()
    (tmp_path / "data").mkdir()
    yield
    config.get_settings.cache_clear()


def test_install_command_dials_the_inner_address_only_when_asked(settings):
    from app.api.servers import _install_cmd
    cmd = _install_cmd(TOKEN)
    assert '${KERVAX_CONNECT:+--resolve "panel.example.com:443:$KERVAX_CONNECT"}' in cmd
    script = cmd[cmd.index("sh -c '") + 7:cmd.rindex("' - ")]
    assert "'" not in script


def test_tunnel_part_matches_the_helper():
    cmd = gre.install_cmd("203.0.113.5", "10.254.0.2/30", "198.51.100.10")
    script = cmd[len("sh -c '"):cmd.rindex("' - ")]
    assert "'" not in script and "\n" not in script
    # те же интерфейс, режим, ttl и mtu, что держит helper: он пересоздаст интерфейс таким же
    helper = (AGENT / "gre-setup.sh").read_text(encoding="utf-8")
    assert 'IFACE = "kervax-gre"' in helper and '"mode", "gre"' in helper and '"ttl", "64"' in helper
    assert f"mode gre local \"$l\" remote \"$1\" ttl 64" in script and f"mtu {gre.MTU} up" in script
    assert 'COMMENT = "kervax-gre"' in helper and "comment kervax-gre" in script
    # сообщения для ноды фронт переводит по точному тексту
    front = (ROOT / "frontend/src/installProxy.ts").read_text(encoding="utf-8")
    i18n = (ROOT / "frontend/src/i18n.tsx").read_text(encoding="utf-8")
    for m in (gre.INSTALL_NO_IP_MSG, gre.INSTALL_SRC_MSG, gre.INSTALL_FAIL_MSG):
        assert m in script and f"'{m}'" in front and f"'{m}':" in i18n
        assert "{" not in m and "'" not in m


@needs_sh
def test_command_brings_the_tunnel_up_before_the_installer(settings, tmp_path):
    from app.api.servers import _install_cmd
    view = {"cmd": gre.install_cmd("203.0.113.5", "10.254.0.2/30", "198.51.100.10"), "connect": "10.254.0.1"}
    env = _bin(tmp_path)
    r = subprocess.run(["sh", "-c", _with_gre(_install_cmd(TOKEN), view) + " main"], capture_output=True,
                       text=True, encoding="utf-8", timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "log").read_text().splitlines()
    want = ["ip -o route get 203.0.113.5", "ip link del kervax-gre",
            "ip tunnel add kervax-gre mode gre local 198.51.100.10 remote 203.0.113.5 ttl 64",
            "ip addr add 10.254.0.2/30 dev kervax-gre", "ip link set kervax-gre mtu 1476 up",
            "ufw allow proto gre from 203.0.113.5 to any comment kervax-gre"]
    assert log[:len(want)] == want
    # первый запрос в панель - уже через туннель, с именем панели в TLS
    curl = next(x for x in log if x.startswith("curl "))
    assert "--resolve panel.example.com:443:10.254.0.1" in curl and "https://panel.example.com/api/agent/install.sh" in curl
    # установщику - адрес в окружении, в аргументах только адрес панели и хвост
    assert "installer: https://panel.example.com main connect=10.254.0.1" in r.stdout


@needs_sh
def test_command_stops_when_the_node_sends_from_another_address(settings, tmp_path):
    from app.api.servers import _install_cmd
    view = {"cmd": gre.install_cmd("203.0.113.5", "10.254.0.2/30", "198.51.100.10"), "connect": "10.254.0.1"}
    env = _bin(tmp_path, src="10.0.0.5")   # за NAT
    r = subprocess.run(["sh", "-c", _with_gre(_install_cmd(TOKEN), view)], capture_output=True,
                       text=True, encoding="utf-8", timeout=60, env=env)
    assert r.returncode == 1
    assert "с адреса 10.0.0.5, а в панели его IP 198.51.100.10" in r.stderr
    log = (tmp_path / "log").read_text()
    assert "tunnel add" not in log and "curl " not in log


# --- установщик ---


def _installer_part(name: str) -> str:
    s = (AGENT / name).read_text(encoding="utf-8")
    start = s.index('URL=""; TOKEN=""')
    loop = s[start:s.index("\ndone\n", start) + 6]
    check = s[s.index('case "$PROXY" in'):s.index('if [ "$(id -u)" != "0" ]; then', s.index('case "$PROXY" in'))]
    return loop + check


@needs_sh
@pytest.mark.parametrize("name", ["install.sh", "install-ru.sh"])
def test_installer_sends_every_request_to_the_inner_address(name, tmp_path):
    env = _bin(tmp_path)
    script = _installer_part(name) + (
        'pcurl -fsS -o /dev/null "${URL%/}/api/agent/install.sh"\n'
        # условия helper'ов (gre-wanted) вызывают голый curl через eval - и они идут в туннель
        'cond="curl -fsS -m 15 ${URL%/}/api/agent/gre-wanted"; ( eval "$cond" )\n')
    r = subprocess.run(["sh", "-c", script, "sh", "https://panel.example.com", "tok"], capture_output=True,
                       text=True, encoding="utf-8", timeout=30, env=dict(env, KERVAX_CONNECT="10.254.0.1"))
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "log").read_text().splitlines()
    assert len(log) == 2 and all(x.startswith("curl --resolve panel.example.com:443:10.254.0.1 ") for x in log), log
    # порт из адреса панели и флаг вместо переменной
    (tmp_path / "log").unlink()
    r = subprocess.run(["sh", "-c", script, "sh", "--connect", "10.254.0.5", "https://panel.example.com:8443/x", "tok"],
                       capture_output=True, text=True, encoding="utf-8", timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "log").read_text().startswith("curl --resolve panel.example.com:8443:10.254.0.5 ")
    # без адреса - как раньше
    (tmp_path / "log").unlink()
    subprocess.run(["sh", "-c", script, "sh", "https://panel.example.com", "tok"], capture_output=True, timeout=30, env=env)
    assert "--resolve" not in (tmp_path / "log").read_text()


@needs_sh
@pytest.mark.parametrize("name", ["install.sh", "install-ru.sh"])
@pytest.mark.parametrize("args,env", [
    (["--connect", "panel.example.com"], {}),
    (["--connect", "10.254.0.1;id"], {}),
    ([], {"KERVAX_CONNECT": "10.254.0.1", "KERVAX_PROXY": "http://u:fakepass@proxy.example.com:3128"}),
])
def test_installer_refuses_a_bad_inner_address(name, args, env, tmp_path):
    r = subprocess.run(["sh", "-c", _installer_part(name) + "echo passed\n", "sh", *args, "https://panel.example.com",
                        "tok"], capture_output=True, text=True, encoding="utf-8", timeout=30,
                       env=dict(_bin(tmp_path), **env))
    assert r.returncode == 1 and "passed" not in r.stdout and "--connect" in r.stderr


def test_installers_write_connect_into_the_agent_config():
    for name in ("install.sh", "install-ru.sh"):
        src = (AGENT / name).read_text(encoding="utf-8")
        assert "\r" not in src, name
        assert 'CONNECT="${KERVAX_CONNECT:-}"' in src, name
        conf = src.index('cat > "$CONF" <<EOF')
        assert src.index("printf 'connect=%s\\n' \"$CONNECT\" >> \"$CONF\"", conf) < src.index('chmod 0600 "$CONF"', conf), name
        # подмена адреса - до первого запроса в панель (проверка CA, скачивание агента)
        wrap = src.index('curl() { command curl --resolve "$PANEL_HP:$CONNECT" "$@"; }')
        assert wrap < src.index("pcurl -fsS --connect-timeout 15 -m 60 --retry 3") and wrap < src.index("fetch() {"), name


# --- helper подхватывает туннель команды ---


def test_helper_asks_the_panel_through_the_tunnel_of_the_install_command(tmp_path, monkeypatch):
    from tests.test_gre_helper import ANSWER, _part
    import importlib.util
    import json
    spec = importlib.util.spec_from_loader("gre_sync", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(_part("SYNC_EOF"), "kervax-gre-sync.py", "exec"), mod.__dict__)
    agent = tmp_path / "kervax-agent.conf"
    # так конфиг оставляет установщик: туннель поднят командой, у helper'а своего еще ничего нет
    agent.write_text("url=https://panel.example\ntoken=fake-token-example\nconnect=10.254.0.1\n", encoding="utf-8")
    for n, v in (("AGENT_CONF", agent), ("CONF", tmp_path / "gre.json"), ("STATE", tmp_path / "gre" / "state.json"),
                 ("VERSION_FILE", tmp_path / "gre-setup.ver"), ("HELPER_DIR", tmp_path)):
        monkeypatch.setattr(mod, n, str(v))
    (tmp_path / "gre").mkdir()
    calls, sent = [], []

    class R:
        def __init__(self, out=""):
            self.returncode, self.stdout = 0, out

    def run(*args):
        calls.append(" ".join(args))
        if args[:4] == ("ip", "-o", "route", "get"):
            return R(f"{args[4]} via 192.0.2.1 dev eth0 src 198.51.100.10 uid 0")
        return R()

    def request(url, token, body, via=None):
        sent.append(via)
        if via is None:   # напрямую DPI замораживает
            raise OSError("timed out")
        return 200, json.dumps(ANSWER).encode()

    monkeypatch.setattr(mod, "run", run)
    monkeypatch.setattr(mod, "request", request)
    monkeypatch.setattr(mod, "reach", lambda a, p, timeout=4: True)
    monkeypatch.setattr(mod, "firewall", lambda: "ufw")
    monkeypatch.setattr(mod, "iface_up", lambda: True)
    monkeypatch.setattr(mod, "active", lambda u: True)
    assert mod.main(["x"]) == 0
    assert sent == ["10.254.0.1"]
    # свой конфиг туннеля с теми же адресами - и один перезапуск kervax-gre (он пересоздает интерфейс)
    assert json.loads((tmp_path / "gre.json").read_text()) == {
        "local": "198.51.100.10", "remote": "203.0.113.5", "addr": "10.254.0.2", "peer": "10.254.0.1",
        "prefix": 30, "mtu": 1476}
    assert calls.count("systemctl restart kervax-gre.service") == 1
    assert "connect=10.254.0.1" in agent.read_text()
    assert re.search(r"^KERVAX_SETUP_VERSION=0\.2 ", (AGENT / "gre-setup.sh").read_text(encoding="utf-8"), re.M)
