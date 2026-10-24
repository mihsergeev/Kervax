"""Команда установки переживает закрытую панель, а окно установки видит, что происходит с нодой.

Было: новая нода стучалась в панель раньше, чем хостовый ops/agent-firewall-sync.sh (крон раз в 2
минуты) открывал ей адрес, и человек вводил команду раз за разом. Через DPI, который обрывает
передачу посреди файла, `curl | sh` выполнял половину установщика и висел, ожидая остаток.
Стало: команда сама повторяет попытки и запускает установщик, только скачав его целиком; синк
пишет панели, что открыто (agent_allow_ips.applied); окно показывает статус адреса и что нода
уже скачала."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app import config, install_watch

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agent"
TOKEN = "fake-token-example"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("KERVAX_DATA_DIR", (tmp_path / "data").as_posix())
    monkeypatch.setenv("KERVAX_PANEL_URL", "https://panel.example.com")
    config.get_settings.cache_clear()
    (tmp_path / "data").mkdir()
    yield tmp_path / "data"
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clean_watch():
    install_watch._SEEN.clear()
    install_watch._VIA.clear()
    yield
    install_watch._SEEN.clear()
    install_watch._VIA.clear()


def test_command_is_one_line_with_the_installer_args_last(settings):
    from app.api.servers import INSTALL_GIVEUP_MSG, INSTALL_WAIT_MSG, _install_cmd
    cmd = _install_cmd(TOKEN)
    assert "\n" not in cmd
    # то, что окно предлагает дописать (--no-auto, имя инстанса), попадает установщику
    assert cmd.endswith("' - https://panel.example.com")
    # токен в окружении перед sh -c, а не в аргументах (их видит ps любого пользователя ноды)
    assert cmd.startswith(f"KERVAX_TOKEN={TOKEN} sh -c '") and cmd.count(TOKEN) == 1
    script = cmd[len(f"KERVAX_TOKEN={TOKEN} sh -c '"):cmd.rindex("' - ")]
    assert "'" not in script
    # сначала файл целиком, потом запуск; зависшая передача - тоже ошибка
    assert '-o "$f" https://panel.example.com/api/agent/install.sh && exec sh "$f" "$@"' in script
    assert " -m 120" in script and "--compressed" in script and "| sh" not in script
    assert "$(seq 24)" in script and "sleep 10" in script
    assert INSTALL_WAIT_MSG in script and INSTALL_GIVEUP_MSG in script
    assert '${KERVAX_PROXY:+--proxy "$KERVAX_PROXY"}' in script


def test_messages_match_the_frontend():
    # английский интерфейс подменяет их по точному тексту: installProxy.ts и i18n.tsx
    from app.api.servers import INSTALL_GIVEUP_MSG, INSTALL_WAIT_MSG
    front = (ROOT / "frontend/src/installProxy.ts").read_text(encoding="utf-8")
    i18n = (ROOT / "frontend/src/i18n.tsx").read_text(encoding="utf-8")
    for m in (INSTALL_WAIT_MSG, INSTALL_GIVEUP_MSG):
        assert f"'{m}'" in front and f"'{m}':" in i18n
        assert "{" not in m and "'" not in m


FAKE_CURL = """#!/bin/sh
# имитация панели: первые FAIL_N вызовов - таймаут подключения, потом отдает установщик
n=$(cat "$STATE" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$STATE"
echo "$*" >> "$STATE.args"
[ "$n" -le "$FAIL_N" ] && exit 28
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
printf '%s\\n' 'echo "installer: $* token=$KERVAX_TOKEN"' > "$out"
"""


def _run(cmd: str, tmp_path: Path, fail_n: int, extra_env: dict | None = None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_bytes(FAKE_CURL.encode())
    (bin_dir / "curl").chmod(0o755)
    (bin_dir / "sleep").write_bytes(b"#!/bin/sh\nexit 0\n")  # ожидание не нужно тесту
    (bin_dir / "sleep").chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
               STATE=(tmp_path / "n").as_posix(), FAIL_N=str(fail_n), **(extra_env or {}))
    return subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)


needs_sh = pytest.mark.skipif(shutil.which("sh") is None or shutil.which("mktemp") is None
                              or shutil.which("seq") is None, reason="нет sh/mktemp/seq")


@needs_sh
def test_command_waits_for_the_panel_and_runs_the_installer(settings, tmp_path):
    from app.api.servers import _install_cmd
    r = _run(_install_cmd(TOKEN) + " main", tmp_path, fail_n=3)
    assert r.returncode == 0, r.stderr
    # три отказа - три понятные строки с попыткой, потом установщик с аргументами и хвостом
    assert r.stderr.count("Панель пока закрыта для этого сервера") == 3
    assert "(curl 28, попытка 3/24)" in r.stderr
    # токен доходит до установщика окружением, а в его аргументах только адрес и хвост
    assert f"installer: https://panel.example.com main token={TOKEN}" in r.stdout
    assert TOKEN not in r.stderr  # повторы токен не печатают
    assert "--proxy" not in (tmp_path / "n.args").read_text()


@needs_sh
def test_command_gives_up_with_a_hint(settings, tmp_path):
    from app.api.servers import _install_cmd
    r = _run(_install_cmd(TOKEN), tmp_path, fail_n=100)
    assert r.returncode == 1
    assert "(curl 28, попытка 24/24)" in r.stderr and "так и не ответила" in r.stderr
    assert "installer:" not in r.stdout and TOKEN not in r.stderr + r.stdout


@needs_sh
def test_proxy_reaches_curl_from_the_environment(settings, tmp_path):
    from app.api.servers import _install_cmd
    r = _run(_install_cmd(TOKEN), tmp_path, fail_n=0, extra_env={"KERVAX_PROXY": "http://u:fakepass@proxy.example.com:8790"})
    assert r.returncode == 0, r.stderr
    assert "--proxy http://u:fakepass@proxy.example.com:8790" in (tmp_path / "n.args").read_text()
    assert "fakepass" not in r.stdout + r.stderr


def test_installers_survive_a_cut_transfer():
    for name in ("install.sh", "install-ru.sh"):
        src = (AGENT / name).read_text(encoding="utf-8")
        # весь скрипт - один блок: обрезанный не выполнится вовсе
        assert "\n{\nset -eu\n" in src and src.endswith("\nfi\n}\n"), name
        assert 'PROXY="${KERVAX_PROXY:-}"' in src, name
        # зависшая передача бинаря - ошибка и повтор с места обрыва; сначала сжатый
        assert "--speed-limit 512 --speed-time 60 --retry 10" in src and "-C - \"$1\"" in src, name
        assert src.index("download-gz/$ARCH") < src.index("/api/agent/download/$ARCH"), name
        assert 'gzip -dc "$BIN.new.gz" > "$BIN.new"' in src, name
        # helper'ы: сначала файл, потом bash
        assert '/api/agent/setup/$1.sh" | bash' not in src and 'bash "$f" </dev/null' in src, name


@pytest.mark.skipif(shutil.which("sh") is None, reason="нет sh")
def test_truncated_installer_runs_nothing(tmp_path):
    src = (AGENT / "install.sh").read_bytes()
    cut = src[: len(src) * 2 // 3]
    r = subprocess.run(["sh", "-s", "--", "https://panel.example.com", TOKEN], input=cut,
                       capture_output=True, timeout=30)
    assert r.returncode != 0
    assert b"Root required" not in r.stderr + r.stdout  # ни одна строка не выполнилась


# --- что открыто в фаерволе панели ---


def _applied(data: Path, fw: str, ips: list[str], age: int = 0):
    p = data / "agent_allow_ips.applied"
    p.write_text(f"# kervax-agent-fw fw={fw} ts={int(time.time()) - age}\n" + "".join(i + "\n" for i in ips))


def test_firewall_state(tmp_path):
    d = tmp_path
    assert install_watch.firewall_state("203.0.113.5", str(d)) == ("", None)  # скрипта нет
    _applied(d, "none", [])
    assert install_watch.firewall_state("203.0.113.5", str(d))[0] == ""  # фаервола нет
    _applied(d, "ufw", ["203.0.113.9", "198.51.100.0/24"])
    assert install_watch.firewall_state("203.0.113.5", str(d))[0] == "opening"
    assert install_watch.firewall_state("203.0.113.9", str(d))[0] == "open"
    assert install_watch.firewall_state("198.51.100.7", str(d))[0] == "open"
    assert install_watch.firewall_state("", str(d))[0] == "no_ip"
    _applied(d, "ufw", [], age=3600)
    st, age = install_watch.firewall_state("203.0.113.5", str(d))
    assert st == "stale" and age >= 3600


def test_relay_state():
    assert install_watch.relay_state(7, None) == ""
    install_watch.via_relay(7, now=1000.0)
    assert install_watch.relay_state(7, 999.0) == "opening"
    assert install_watch.relay_state(7, 1001.0) == "open"


def test_seen_is_bounded():
    for i in range(install_watch._SEEN_MAX + 50):
        install_watch.note(f"10.0.{i // 256}.{i % 256}", "script", now=1000.0 + i)
    assert len(install_watch._SEEN) == install_watch._SEEN_MAX
    assert install_watch.seen("10.0.0.0") == {}  # самые старые ушли
    assert install_watch.seen(f"10.0.0.{60}", since=2000.0) == {}


async def test_install_status_endpoint(client, auth_headers, tmp_path):
    r = await client.post("/api/servers", json={"name": "fresh-node", "agent_ip": "203.0.113.20"}, headers=auth_headers)
    sid = r.json()["server"]["id"]
    st = (await client.get(f"/api/servers/{sid}/install-status", headers=auth_headers)).json()
    assert st["firewall"] == "" and st["script_at"] is None and not st["connected"]

    data = Path(config.get_settings().data_dir)
    _applied(data, "ufw", [])
    st = (await client.get(f"/api/servers/{sid}/install-status", headers=auth_headers)).json()
    assert st["firewall"] == "opening" and st["ip"] == "203.0.113.20"
    _applied(data, "ufw", ["203.0.113.20"])
    st = (await client.get(f"/api/servers/{sid}/install-status", headers=auth_headers)).json()
    assert st["firewall"] == "open"

    # нода скачала установщик и агента: панель видит по адресу запроса
    await client.get("/api/agent/install.sh", headers={"X-Forwarded-For": "203.0.113.20"})
    await client.get("/api/agent/download/amd64", headers={"X-Forwarded-For": "203.0.113.20"})
    await client.get("/api/agent/install.sh", headers={"X-Forwarded-For": "198.51.100.1"})  # чужая
    st = (await client.get(f"/api/servers/{sid}/install-status", headers=auth_headers)).json()
    assert st["script_at"] and st["agent_at"]

    # без IP: фаервол есть, открыть нечего
    r = await client.post("/api/servers", json={"name": "no-ip-node"}, headers=auth_headers)
    st = (await client.get(f"/api/servers/{r.json()['server']['id']}/install-status", headers=auth_headers)).json()
    assert st["firewall"] == "no_ip"

    assert (await client.get(f"/api/servers/{sid}/install-status")).status_code == 401


SYNC = ROOT / "ops" / "agent-firewall-sync.sh"


@pytest.mark.skipif(shutil.which("sh") is None, reason="нет sh")
def test_sync_reports_what_is_open(tmp_path):
    # ufw-заглушка: "активен", правила копит в файле, status печатает их как настоящий ufw
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rules = tmp_path / "rules"
    rules.write_text("")
    (bin_dir / "ufw").write_bytes(f"""#!/bin/sh
R={rules.as_posix()}
case "$1" in
  status) echo "Status: active"; cat "$R" ;;
  allow) echo "80,443/tcp                 ALLOW       $5            # kervax-agent" >> "$R" ;;
  *) : ;;
esac
""".encode())
    (bin_dir / "ufw").chmod(0o755)
    (bin_dir / "logger").write_bytes(b"#!/bin/sh\n")
    (bin_dir / "logger").chmod(0o755)
    ips = tmp_path / "agent_allow_ips"
    ips.write_text("203.0.113.30\n")
    env = dict(os.environ, PATH=f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
               KERVAX_AGENT_IPS=ips.as_posix(), KERVAX_FW_STATE=(tmp_path / "state").as_posix())
    r = subprocess.run(["sh", SYNC.as_posix()], capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    applied = (tmp_path / "agent_allow_ips.applied").read_text().splitlines()
    assert applied[0].startswith("# kervax-agent-fw fw=ufw ts=") and applied[1:] == ["203.0.113.30"]
    assert install_watch.firewall_state("203.0.113.30", str(tmp_path))[0] == "open"
