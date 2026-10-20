"""Helper relay-setup: прокси ретранслятора и синхронизация с панелью.

Прокси и синхронизация - python внутри agent/relay-setup.sh. Тест вынимает их из скрипта и гоняет
по-настоящему: прокси - на локальном адресе с локальной "панелью", синхронизацию - с подмененными
системными вызовами (systemctl, ufw) и ответом панели."""
import base64
import importlib.util
import io
import json
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "agent" / "relay-setup.sh"


def _part(tag: str) -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"<<'%s'\n(.*?)\n%s\n" % (tag, tag), src, re.S)
    assert m, tag
    return m.group(1)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def panel():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def relay(tmp_path, panel):
    proxy = tmp_path / "kervax-relay.py"
    proxy.write_text(_part("PROXY_EOF"), encoding="utf-8")
    port = _free_port()
    conf = tmp_path / "relay.json"
    stats = tmp_path / "stats.json"

    def write(clients):
        conf.write_text(json.dumps({"port": port, "dest_host": "127.0.0.1", "dest_port": panel, "user": "kervax",
                                    "password": "secret-pw", "clients": clients}), encoding="utf-8")
    write(["127.0.0.1"])
    p = subprocess.Popen([sys.executable, str(proxy), str(conf), str(stats)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    yield port, panel, write
    p.terminate()
    out = p.communicate(timeout=10)[0]
    assert "secret-pw" not in out and "127.0.0.1" not in out  # ни пароля, ни адресов клиентов в логе


def _connect(port, target, auth="kervax:secret-pw") -> tuple[socket.socket, bytes]:
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    hdr = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if auth:
        hdr += "Proxy-Authorization: Basic " + base64.b64encode(auth.encode()).decode() + "\r\n"
    s.sendall((hdr + "\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        try:
            chunk = s.recv(4096)
        except ConnectionError:  # закрыто без ответа (Windows отдает это сбросом)
            break
        if not chunk:
            break
        buf += chunk
    return s, buf


def test_proxy_lets_through_only_to_the_panel_with_the_login(relay):
    port, dest, write = relay
    s, head = _connect(port, f"127.0.0.1:{dest}")
    assert head.startswith(b"HTTP/1.1 200")
    s.sendall(b"GET / HTTP/1.0\r\n\r\n")
    data = b""
    while chunk := s.recv(4096):
        data += chunk
    assert data.endswith(b"ok")
    s.close()

    assert _connect(port, f"127.0.0.1:{dest}", auth="kervax:wrong")[1].startswith(b"HTTP/1.1 407")
    assert _connect(port, f"127.0.0.1:{dest}", auth="")[1].startswith(b"HTTP/1.1 407")
    # куда угодно еще - нельзя, даже с логином: ретранслятор не открытый прокси
    assert _connect(port, "example.com:443")[1].startswith(b"HTTP/1.1 403")
    assert _connect(port, f"127.0.0.1:{dest + 1}")[1].startswith(b"HTTP/1.1 403")
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    s.sendall(f"GET http://127.0.0.1:{dest}/ HTTP/1.1\r\n\r\n".encode())
    assert s.recv(100).startswith(b"HTTP/1.1 405")


def test_proxy_drops_addresses_that_did_not_choose_it(relay):
    port, dest, write = relay
    write(["203.0.113.40"])
    time.sleep(0.05)
    s, head = _connect(port, f"127.0.0.1:{dest}")
    assert head == b""  # соединение закрыто без ответа


# --- синхронизация ---

@pytest.fixture
def sync(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_loader("relay_sync", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(_part("SYNC_EOF"), "kervax-relay-sync.py", "exec"), mod.__dict__)
    agent = tmp_path / "kervax-agent.conf"
    agent.write_text("url=https://panel.example\ntoken=tok-1\n", encoding="utf-8")
    for name, val in (("AGENT_CONF", agent), ("CONF", tmp_path / "relay.json"), ("STATE", tmp_path / "state.json"),
                      ("STATS", tmp_path / "stats.json"), ("VERSION_FILE", tmp_path / "relay-setup.ver"),
                      ("HELPER_DIR", tmp_path)):
        monkeypatch.setattr(mod, name, str(val))
    (tmp_path / "relay-setup.ver").write_text("0.1\n")
    calls = []

    class R:
        returncode, stdout = 0, ""

    def run(*args, check=False):
        calls.append(" ".join(args))
        return R()

    mod.run = run
    mod.shutil.chown = lambda *a, **k: None
    mod.firewall = lambda: "ufw"
    mod.active = lambda unit: True
    answer = {}

    class Opener:
        def open(self, req, timeout=0):
            answer["sent"] = (req.full_url, req.headers.get("Authorization"), json.loads(req.data))
            if isinstance(answer["reply"], int):
                raise urllib.error.HTTPError(req.full_url, answer["reply"], "x", {}, None)
            return io.BytesIO(json.dumps(answer["reply"]).encode())

    mod.urllib.request.build_opener = lambda *a: Opener()
    return mod, calls, answer, tmp_path


def test_sync_holds_what_the_panel_says_but_only_to_its_own_panel(sync):
    mod, calls, answer, tmp = sync
    answer["reply"] = {"enabled": True, "port": 8790, "user": "kervax", "password": "pw-1",
                       "clients": ["203.0.113.40", "bad", "203.0.113.41"], "dest_host": "evil.example"}
    assert mod.main() == 0
    url, auth, status = answer["sent"]
    assert url == "https://panel.example/api/agent/relay" and auth == "Bearer tok-1" and status["version"] == "0.1"
    conf = json.loads((tmp / "relay.json").read_text())
    # куда пускать - из конфига агента, а не из ответа
    assert (conf["dest_host"], conf["dest_port"]) == ("panel.example", 443)
    assert conf["clients"] == ["203.0.113.40", "203.0.113.41"]
    assert "ufw allow proto tcp from 203.0.113.40 to any port 8790 comment kervax-relay" in calls

    # клиент ушел - правило снимается, лишнего не трогаем
    calls.clear()
    answer["reply"]["clients"] = ["203.0.113.41"]
    assert mod.main() == 0
    assert calls.count("ufw delete allow proto tcp from 203.0.113.40 to any port 8790") == 1
    assert not any("ufw allow" in c for c in calls)

    # конфиг, который helper не принимает
    answer["reply"] = {"enabled": True, "port": 80, "user": "kervax", "password": "pw-1", "clients": []}
    assert mod.main() == 1


def test_sync_removes_the_relay_when_switched_off(sync):
    mod, calls, answer, tmp = sync
    answer["reply"] = {"enabled": True, "port": 8790, "user": "kervax", "password": "pw-1", "clients": ["203.0.113.40"]}
    mod.main()
    calls.clear()
    answer["reply"] = {"enabled": False}
    assert mod.main() == 0
    assert "ufw delete allow proto tcp from 203.0.113.40 to any port 8790" in calls
    assert "systemctl disable --now kervax-relay.service" in calls and "userdel kervax-relay" in calls
    assert not (tmp / "relay.json").exists() and not (tmp / "relay-setup.ver").exists()


def test_sync_keeps_the_relay_when_the_panel_is_unreachable_and_removes_it_for_a_deleted_server(sync):
    mod, calls, answer, tmp = sync
    answer["reply"] = {"enabled": True, "port": 8790, "user": "kervax", "password": "pw-1", "clients": []}
    mod.main()
    answer["reply"] = 502
    assert mod.main() == 1 and (tmp / "relay.json").exists()
    answer["reply"] = 401
    assert mod.main() == 0 and not (tmp / "relay.json").exists()


def test_helper_script_rules():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "\r" not in src and "/usr/local" not in src
    when = re.search(r'^KERVAX_SETUP_WHEN="([^"]+)"', src, re.M).group(1)
    assert not set(when) & set("{}'\\"), when
    # условие идет до set -e: подстановка в нем выполняется и при запуске самого скрипта
    assert src.index("KERVAX_SETUP_WHEN=") < src.index("set -euo pipefail")
    assert 'mv -f "$PROXY.new" "$PROXY"' in src and 'mv -f "$SYNC.new" "$SYNC"' in src
    assert 'chmod 0644 "$STATE_DIR/versions/relay-setup.ver"' in src
