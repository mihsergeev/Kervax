"""Helper gre-setup (туннель с ноды) и ops/gre-sync.sh (сторона хоста панели).

Синхронизация helper'а - python внутри agent/gre-setup.sh: тест вынимает его из скрипта и гоняет с
подмененными системными вызовами (ip, systemctl, ufw) и ответом панели. Хостовый скрипт гоняется
целиком через sh с заглушками ip и ufw."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "agent" / "gre-setup.sh"
HOST = ROOT / "ops" / "gre-sync.sh"


def _part(tag: str) -> str:
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"<<'%s'\n(.*?)\n%s\n" % (tag, tag), src, re.S)
    assert m, tag
    return m.group(1)


ANSWER = {"enabled": True, "remote": "203.0.113.5", "local_addr": "10.254.0.2", "panel_addr": "10.254.0.1",
          "prefix": 30, "mtu": 1476}


@pytest.fixture
def sync(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_loader("gre_sync", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(_part("SYNC_EOF"), "kervax-gre-sync.py", "exec"), mod.__dict__)
    agent = tmp_path / "kervax-agent.conf"
    agent.write_text("url=https://panel.example\ntoken=fake-token-example\n", encoding="utf-8")
    for name, val in (("AGENT_CONF", agent), ("CONF", tmp_path / "gre.json"), ("STATE", tmp_path / "gre" / "state.json"),
                      ("VERSION_FILE", tmp_path / "gre-setup.ver"), ("HELPER_DIR", tmp_path)):
        monkeypatch.setattr(mod, name, str(val))
    (tmp_path / "gre").mkdir()
    (tmp_path / "gre-setup.ver").write_text("0.1\n")
    calls = []
    env = {"src": "198.51.100.10", "reach": True, "fw": "ufw", "reply": dict(ANSWER), "sent": []}

    class R:
        def __init__(self, out=""):
            self.returncode, self.stdout = 0, out

    def run(*args):
        calls.append(" ".join(args))
        if args[:4] == ("ip", "-o", "route", "get"):
            return R(f"{args[4]} via 192.0.2.1 dev eth0 src {env['src']} uid 0")
        return R()

    def request(url, token, body, via=None):
        env["sent"].append((url, token, json.loads(body), via))
        r = env["reply"]
        if isinstance(r, Exception):
            raise r
        if isinstance(r, int):
            return r, b"{}"
        return 200, json.dumps(r).encode()

    mod.run = run
    mod.request = request
    mod.firewall = lambda: env["fw"]
    mod.active = lambda unit: True
    mod.iface_up = lambda: True
    mod.reach = lambda addr, port, timeout=4: env["reach"]
    monkeypatch.setattr(mod.socket, "gethostbyname", lambda h: "203.0.113.80")
    monkeypatch.setattr(mod.os, "chown", lambda *a: None, raising=False)  # на Windows его нет
    return mod, calls, env, tmp_path


def _conf(tmp) -> str:
    return (tmp / "kervax-agent.conf").read_text(encoding="utf-8")


def test_sync_brings_the_tunnel_up_and_points_the_agent_at_it(sync):
    mod, calls, env, tmp = sync
    assert mod.main(["x"]) == 0
    url, token, status, via = env["sent"][0]
    assert url == "https://panel.example" and token == "fake-token-example" and status["version"] == "0.1"
    assert via is None  # туннеля еще нет - напрямую
    conf = json.loads((tmp / "gre.json").read_text())
    assert conf == {"local": "198.51.100.10", "remote": "203.0.113.5", "addr": "10.254.0.2", "peer": "10.254.0.1",
                    "prefix": 30, "mtu": 1476}
    assert "systemctl restart kervax-gre.service" in calls
    assert "ufw allow proto gre from 203.0.113.5 to any comment kervax-gre" in calls
    # панель через туннель отвечает - агенту адрес подключения, токен и url на месте
    assert _conf(tmp).splitlines() == ["url=https://panel.example", "token=fake-token-example", "connect=10.254.0.1"]
    state = json.loads((tmp / "gre" / "state.json").read_text())
    assert state["up"] is True and state["local_ip"] == "198.51.100.10" and state["error"] == ""

    # следующий раз - сначала через туннель, и ничего не перезапускает зря
    calls.clear()
    assert mod.main(["x"]) == 0
    assert env["sent"][-1][3] == "10.254.0.1" and env["sent"][-1][2]["up"] is True
    assert not any("restart" in c or "ufw allow" in c for c in calls)


def test_sync_takes_the_agent_off_the_tunnel_when_the_panel_does_not_answer_there(sync):
    mod, calls, env, tmp = sync
    mod.main(["x"])
    env["reach"] = False
    assert mod.main(["x"]) == 0
    assert "connect=" not in _conf(tmp)
    state = json.loads((tmp / "gre" / "state.json").read_text())
    assert state["up"] is False and "does not answer through the tunnel" in state["error"]


def test_sync_behind_nat_brings_nothing_up(sync):
    mod, calls, env, tmp = sync
    env["src"] = "10.0.0.5"
    assert mod.main(["x"]) == 0
    state = json.loads((tmp / "gre" / "state.json").read_text())
    assert state["nat"] is True and "behind NAT" in state["error"]
    assert not (tmp / "gre.json").exists() and "connect=" not in _conf(tmp)
    assert not any("ufw allow" in c or "restart" in c for c in calls)


def test_sync_waits_for_the_panel_host(sync):
    mod, calls, env, tmp = sync
    env["reply"] = dict(ANSWER, remote="")
    assert mod.main(["x"]) == 0
    state = json.loads((tmp / "gre" / "state.json").read_text())
    assert "waiting for the panel host" in state["error"] and state["local_ip"] == "198.51.100.10"
    assert "ip -o route get 203.0.113.80" in calls  # адрес ноды все равно назван панели
    assert not (tmp / "gre.json").exists()


def test_sync_refuses_a_strange_answer(sync):
    mod, calls, env, tmp = sync
    for bad in (dict(ANSWER, remote="10.1.1.1"), dict(ANSWER, panel_addr="10.254.9.1"), dict(ANSWER, mtu=9000),
                dict(ANSWER, prefix="x")):
        env["reply"] = bad
        assert mod.main(["x"]) == 1, bad
    assert not (tmp / "gre.json").exists()


def test_sync_removes_everything_when_switched_off_or_deleted(sync):
    mod, calls, env, tmp = sync
    mod.main(["x"])
    calls.clear()
    env["reply"] = {"enabled": False}
    assert mod.main(["x"]) == 0
    assert "connect=" not in _conf(tmp) and "token=fake-token-example" in _conf(tmp)
    assert "ufw delete allow proto gre from 203.0.113.5 to any" in calls
    assert "systemctl disable --now kervax-gre.service" in calls and "ip link del kervax-gre" in calls
    assert not (tmp / "gre.json").exists() and not (tmp / "gre-setup.ver").exists()


def test_sync_keeps_the_tunnel_when_the_panel_is_unreachable_and_removes_it_for_a_deleted_server(sync):
    mod, calls, env, tmp = sync
    mod.main(["x"])
    env["reply"] = OSError("timed out")
    assert mod.main(["x"]) == 1 and (tmp / "gre.json").exists() and "connect=10.254.0.1" in _conf(tmp)
    env["reply"] = 502
    assert mod.main(["x"]) == 1 and (tmp / "gre.json").exists()
    env["reply"] = 401
    assert mod.main(["x"]) == 0 and not (tmp / "gre.json").exists()


def test_sync_does_not_point_an_agent_behind_a_proxy_at_the_tunnel(sync):
    mod, calls, env, tmp = sync
    (tmp / "kervax-agent.conf").write_text("url=https://panel.example\ntoken=fake-token-example\n"
                                          "proxy=http://user:fakepass@proxy.example:3128\n", encoding="utf-8")
    mod.main(["x"])
    assert "connect=" not in _conf(tmp)


def test_helper_script_rules():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "\r" not in src and "/usr/local" not in src and "/lib65/kervax" in src
    when = re.search(r'^KERVAX_SETUP_WHEN="([^"]+)"', src, re.M).group(1)
    assert not set(when) & set("{}'\\"), when
    assert "/api/agent/gre-wanted" in when
    # условие идет до set -e: подстановка в нем выполняется и при запуске самого скрипта
    assert src.index("KERVAX_SETUP_WHEN=") < src.index("set -euo pipefail")
    assert re.search(r"^KERVAX_SETUP_VERSION=[0-9.]+  # ", src, re.M)
    assert 'mv -f "$SYNC.new" "$SYNC"' in src
    assert 'chmod 0644 "$STATE_DIR/versions/gre-setup.ver"' in src
    assert "mtu" in src and "1476" not in _part("SYNC_EOF")  # mtu приходит от панели


# --- сторона хоста панели ---

# ip-заглушка: интерфейсы - файлы в каталоге, по одному на интерфейс (remote, local, адрес)
IP_STUB = r"""#!/bin/sh
D={d}
case "$*" in
  "-o route get "*) echo "$4 via 192.0.2.1 dev eth0 src 203.0.113.5 uid 0" ;;
  "-o tunnel show "*) [ -f "$D/$4" ] && echo "$4: gre/ip remote $(sed -n 1p "$D/$4") local $(sed -n 2p "$D/$4") ttl 64" ;;
  "tunnel add "*) printf '%s\n%s\n\n' "$9" "$7" > "$D/$3" ;;
  "link del "*) [ -f "$D/$3" ] || exit 1; rm -f "$D/$3" ;;
  "-o addr show dev "*) [ -f "$D/$5" ] && a=$(sed -n 3p "$D/$5") && [ -n "$a" ] && echo "9: $5    inet $a scope global $5" ;;
  "addr flush dev "*) [ -f "$D/$4" ] && sed -i 3d "$D/$4" && echo >> "$D/$4" ;;
  "addr add "*) [ -f "$D/$5" ] && { head -n 2 "$D/$5"; echo "$3"; } > "$D/x" && mv "$D/x" "$D/$5" ;;
  "link set "*) [ -f "$D/$3" ] ;;
  "-o link show") n=10; for f in "$D"/kvxg*; do [ -f "$f" ] && n=$((n+1)) && echo "$n: $(basename "$f")@NONE: <POINTOPOINT,NOARP,UP> mtu 1476"; done; echo "2: eth0: <UP> mtu 1500" ;;
esac
exit 0
"""


@pytest.mark.skipif(shutil.which("sh") is None, reason="нет sh")
def test_host_brings_up_only_its_own_tunnels_and_rules(tmp_path):
    bin_dir, ifs = tmp_path / "bin", tmp_path / "ifs"
    bin_dir.mkdir()
    ifs.mkdir()
    ufw_log = tmp_path / "ufw.log"
    (bin_dir / "ip").write_bytes(IP_STUB.replace("{d}", ifs.as_posix()).encode())
    (bin_dir / "ufw").write_bytes(f"""#!/bin/sh
case "$1" in status) echo "Status: active" ;; *) echo "$*" >> {ufw_log.as_posix()} ;; esac
""".encode())
    for name in ("logger", "flock"):
        (bin_dir / name).write_bytes(b"#!/bin/sh\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    (ifs / "eth0").write_text("")  # чужой интерфейс не трогается (он и не kvxg)
    lst = tmp_path / "gre_tunnels"
    env = dict(os.environ, PATH=f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
               KERVAX_GRE_LIST=lst.as_posix(), KERVAX_GRE_STATE=(tmp_path / "state").as_posix(),
               KERVAX_GRE_LOCK=(tmp_path / "gre.lock").as_posix())

    def run():
        r = subprocess.run(["sh", HOST.as_posix()], capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
        assert r.returncode == 0, r.stderr
        return (tmp_path / "gre_tunnels.applied").read_text().splitlines()

    # без заголовка - чужой или недописанный файл: ничего не трогаем
    lst.write_text("kvxg3 198.51.100.10 10.254.0.1/30 10.254.0.2\n")
    run_r = subprocess.run(["sh", HOST.as_posix()], capture_output=True, env=env, timeout=60)
    assert run_r.returncode == 0 and not (tmp_path / "gre_tunnels.applied").exists()

    lst.write_text("# kervax-gre v1\nkvxg3 198.51.100.10 10.254.0.1/30 10.254.0.2\n"
                   "kvxg4 198.51.100.11 10.254.0.5/30 10.254.0.6\nkvxg5 $(reboot) 10.254.0.9/30 x\n")
    applied = run()
    assert applied[0].startswith("# kervax-gre fw=ufw ts=")
    assert applied[1:] == ["kvxg3 198.51.100.10 203.0.113.5 ok", "kvxg4 198.51.100.11 203.0.113.5 ok"]
    assert (ifs / "kvxg3").read_text().split("\n")[:3] == ["198.51.100.10", "203.0.113.5", "10.254.0.1/30"]
    log = ufw_log.read_text().splitlines()
    assert "allow proto gre from 198.51.100.10 to any comment kervax-gre" in log
    assert "allow in on kvxg3 proto tcp to any port 80,443 comment kervax-gre" in log
    assert "route allow in on kvxg3 proto tcp to any port 80,443 comment kervax-gre" in log

    # повторный прогон ничего не добавляет
    ufw_log.write_text("")
    run()
    assert ufw_log.read_text() == ""

    # сервер выключили - его интерфейс и правила уходят, остальное на месте
    lst.write_text("# kervax-gre v1\nkvxg4 198.51.100.11 10.254.0.5/30 10.254.0.6\n")
    applied = run()
    assert applied[1:] == ["kvxg4 198.51.100.11 203.0.113.5 ok"]
    assert not (ifs / "kvxg3").exists() and (ifs / "kvxg4").exists() and (ifs / "eth0").exists()
    log = ufw_log.read_text().splitlines()
    assert "delete allow proto gre from 198.51.100.10 to any" in log
    assert "delete allow in on kvxg3 proto tcp to any port 80,443" in log
    assert "route delete allow in on kvxg3 proto tcp to any port 80,443" in log
    assert not any("198.51.100.11" in x or "kvxg4" in x for x in log)

    # пустой список с заголовком - выключили всех
    lst.write_text("# kervax-gre v1\n")
    assert run()[1:] == []
    assert not (ifs / "kvxg4").exists()
