"""Панель в локальной сети: quickstart --local-ca, корень CA в команде установки, install.sh --ca-file.

Let's Encrypt не достучится до приватного адреса, а <приватный-IP>.sslip.io за домашним роутером
с защитой от DNS rebinding не резолвится вовсе. Панель вставала, https не поднимался, и агенты не
ставились: первая же curl команды установки падала на проверке сертификата. Теперь сертификат
выдает внутренний CA caddy (метки на контейнере панели), quickstart кладет корень в
data/panel-ca.crt, панель везет его в команде установки, а install.sh добавляет его в системное
хранилище ноды. Проверка сертификата нигде не отключается.
"""
import base64
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import config

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agent"
QUICKSTARTS = ("quickstart.sh", "quickstart-ru.sh")
INSTALLERS = ("install.sh", "install-ru.sh")

PEM = """-----BEGIN CERTIFICATE-----
MIIBpDCCAUmgAwIBAgIQTestRootOnlyForTheTestSuite0wCgYIKoZIzj0EAwIw
MDEuMCwGA1UEAxMlQ2FkZHkgTG9jYWwgQXV0aG9yaXR5IC0gMjAyNiBFQ0MgUm9v
dDAeFw0yNjEwMTkwMDAwMDBaFw0zNjA4MjgwMDAwMDBaMDAxLjAsBgNVBAMTJUNh
-----END CERTIFICATE-----
"""


def src(path):
    return path.read_text(encoding="utf-8")


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("KERVAX_DATA_DIR", (tmp_path / "data").as_posix())
    monkeypatch.setenv("KERVAX_PANEL_URL", "https://192.168.1.10")
    config.get_settings.cache_clear()
    (tmp_path / "data").mkdir()
    yield tmp_path / "data"
    config.get_settings.cache_clear()


def test_install_cmd_without_ca_has_no_root(settings):
    from app.api.servers import _install_cmd
    cmd = _install_cmd("tok")
    # токен в окружении sh, а не в его аргументах: аргументы видны в ps любому пользователю ноды
    assert cmd.startswith("KERVAX_TOKEN=tok sh -c '") and "--cacert" not in cmd and "--ca-file" not in cmd
    assert cmd.endswith("' - https://192.168.1.10")


def test_install_cmd_carries_the_root(settings):
    from app.api.servers import _install_cmd
    (settings / "panel-ca.crt").write_text("junk before\n" + PEM, encoding="ascii")
    cmd = _install_cmd("tok")
    # корень - во временный файл mktemp, а не в фиксированный /tmp/kervax-panel-ca.crt
    b64 = re.match(r'KERVAX_CA=\$\(mktemp\) && echo ([A-Za-z0-9+/=]+) \| base64 -d > "\$KERVAX_CA" && ', cmd)
    assert b64 and "/tmp/" not in cmd
    assert base64.b64decode(b64.group(1)).decode() == PEM  # только сам сертификат, без мусора
    # curl проверяет панель этим корнем (вторым аргументом после --ca-file), без -k / --insecure
    assert '--cacert "$2"' in cmd and "https://192.168.1.10/api/agent/install.sh" in cmd
    assert " -k " not in cmd and "--insecure" not in cmd
    # --ca-file до адреса: UI дописывает имя инстанса (" main") в самый конец команды
    assert cmd.endswith('\' - --ca-file "$KERVAX_CA" https://192.168.1.10')
    assert "KERVAX_TOKEN=tok sh -c '" in cmd


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("base64") is None, reason="нужен sh")
def test_install_cmd_runs_in_sh(settings, tmp_path):
    """Команда целиком в sh: установщик получает токен из окружения, в его аргументах токена нет,
    имя инстанса, дописанное в конец, доходит до него, а curl проверяет панель тем же корнем."""
    from app.api.servers import _install_cmd
    (settings / "panel-ca.crt").write_text(PEM, encoding="ascii")
    fake = tmp_path / "bin"
    fake.mkdir()
    # curl кладет в -o "установщик", который печатает свои аргументы, токен и первую строку корня;
    # сам curl отмечает, какой корень ему дали
    (fake / "curl").write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do case \"$1\" in -o) out=$2; shift;; --cacert) echo \"cacert=$2\"; shift;; esac; shift; done\n"
        "echo 'echo \"args=$*\"; echo \"tok=$KERVAX_TOKEN\"; head -1 \"$2\"' > \"$out\"\n",
        encoding="ascii", newline="\n")
    (fake / "curl").chmod(0o755)
    path = fake.as_posix() + ":" + ":".join(p for p in ("/usr" + "/bin", "/bin"))
    out = subprocess.run(["sh", "-c", _install_cmd("fake-token-example") + " main"], capture_output=True,
                         text=True, env={"PATH": path}, timeout=30)
    assert "tok=fake-token-example" in out.stdout, (out.stdout, out.stderr)
    args = next(x for x in out.stdout.splitlines() if x.startswith("args="))
    assert "fake-token-example" not in args and args.endswith("https://192.168.1.10 main")
    cacert = next(x for x in out.stdout.splitlines() if x.startswith("cacert="))
    assert args.startswith("args=--ca-file " + cacert[len("cacert="):] + " ")
    assert "-----BEGIN CERTIFICATE-----" in out.stdout


def test_not_a_certificate_is_ignored(settings):
    from app.api.servers import _install_cmd
    (settings / "panel-ca.crt").write_text("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")
    assert "--ca-file" not in _install_cmd("tok")


async def test_ca_endpoint(client, tmp_path):
    assert (await client.get("/api/agent/ca.crt")).status_code == 404
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "panel-ca.crt").write_text(PEM, encoding="ascii")
    r = await client.get("/api/agent/ca.crt")
    assert r.status_code == 200 and r.text == PEM
    assert "kervax-panel-ca.crt" in r.headers["content-disposition"]


def test_overlay_sets_internal_ca_only_for_the_panel():
    body = src(ROOT / "compose.localca.yml")
    labels = dict(re.findall(r"^      (caddy[\w.]*): (.+)$", body, re.MULTILINE))
    # сайт панели, а не глобальный local_certs: остальные сайты общего caddy остаются на Let's Encrypt
    assert labels == {"caddy.tls": "internal", "caddy_99.default_sni": "${KERVAX_DOMAIN}"}
    assert "local_certs" not in "".join(line for line in body.splitlines() if not line.startswith("#"))


@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_local_ca(name):
    s = src(ROOT / "ops" / name)
    assert "\r" not in s
    assert "--local-ca) LOCAL_CA=1; shift ;;" in s
    assert '[ "$LOCAL_CA" = "1" ] && COMPOSE_LIST="$COMPOSE_LIST:compose.localca.yml"' in s
    # повторный запуск (обновление) не переключает панель обратно на Let's Encrypt
    assert "grep -q '^COMPOSE_FILE=.*compose\\.localca\\.yml' \"$DIR/.env\" && LOCAL_CA=1" in s
    # адрес - IP в локалке (src из маршрута), а не публичный через ipify
    local = s[s.index('if [ -z "$DOMAIN" ] && [ "$LOCAL_CA" = "1" ]; then'):s.index('elif [ -z "$DOMAIN" ]; then')]
    assert "ipify" not in local and "sslip" not in local and " src " in local
    # корень - через docker из самого caddy (caddy может быть чужим), stdin закрыт: curl | sh
    assert ('docker exec "$CADDY_CT" cat /data/caddy/pki/authorities/local/root.crt </dev/null' in s)
    assert 'CA_FILE="$DIR/data/panel-ca.crt"' in s
    # https проверяется этим корнем, а не пропускается и не -k
    assert "$CURL_CA \"https://$DOMAIN/api/health\"" in s
    assert "curl -k" not in s and "--insecure" not in s
    # CADDY_CT нужен до чтения корня
    assert s.index("CADDY_CT=$(docker ps") < s.index('docker exec "$CADDY_CT"')
    assert s.count("CADDY_CT=$(docker ps") == 1
    # лимит Let's Encrypt к внутреннему CA отношения не имеет
    assert 'if [ "$LOCAL_CA" != "1" ] && [ -n "$CADDY_CT" ] && docker logs' in s


@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_help_prints_the_whole_header(name):
    s = src(ROOT / "ops" / name)
    lines = s.splitlines()
    end = int(re.search(r"sed -n '2,(\d+)p' \"\$0\"", s).group(1))
    assert lines[end] == "set -eu"  # строка end+1 (с единицы) - уже код
    assert "--local-ca" in "\n".join(lines[1:end])


@pytest.mark.parametrize("name", INSTALLERS)
def test_installer_trusts_the_panel_ca(name):
    s = src(AGENT / name)
    assert '--ca-file) WANT_CA=1 ;;' in s and '--ca-file=*) CA_FILE="${a#--ca-file=}" ;;' in s
    trust = s.index('if [ -n "$CA_FILE" ]; then')
    # до первого скачивания с панели: оно уже идет через системное хранилище
    assert trust < s.index('"${URL%/}/api/agent/download/$ARCH"')
    block = "\n".join(line for line in s[trust:s.index("\nfi\n", trust)].splitlines()
                      if not line.lstrip().startswith("#"))
    for d in ("/usr/local/share/ca-certificates/", "/etc/pki/ca-trust/source/anchors/", "/etc/pki/trust/anchors/"):
        assert f"CA_DST={d}$CA_NAME" in block, d
    # проверка без --cacert и под /api/agent (белый список панели там не действует)
    check = re.search(r"curl -fsS --connect-timeout 15 [^\n]*-o /dev/null \"\$\{URL%/\}/api/agent/install\.sh\"", block)
    assert check and "--cacert" not in block and " -k " not in block
    assert 'CA_NAME="kervax-panel${INSTANCE:+-$INSTANCE}.crt"' in s


@pytest.mark.parametrize("name", INSTALLERS)
def test_installer_refuses_foreign_ca_file(name):
    """Корень берется только из обычного файла root или того, кто вызвал sudo, и в который больше
    никто не пишет: подложенный в /tmp чужой файл или ссылка не попадают в доверенные. Файл
    читается один раз в свой временный, копируется уже он; файл ошибок проверки - свой временный."""
    s = src(AGENT / name)
    guard = s.index('if [ -L "$CA_FILE" ] || [ ! -f "$CA_FILE" ] || [ $((0${CA_OWN#* } & 022)) != 0 ]')
    assert '"${CA_OWN% *}" != "${SUDO_UID:-0}"' in s
    assert guard < s.index('cat "$CA_FILE" > "$CA_TMP"') < s.index('cp "$CA_TMP" "$CA_DST"')
    assert 'cp "$CA_FILE"' not in s
    assert "/tmp/kervax-ca-check.err" not in s and 'CA_ERR=$(mktemp)' in s


@pytest.mark.parametrize("name", INSTALLERS)
def test_uninstall_removes_the_panel_ca(name):
    s = src(AGENT / name)
    block = s[s.index('if [ "$UNINSTALL" = 1 ]; then'):s.index("\n  exit 0\nfi\n")]
    assert 'CA_GLOB="kervax-panel-$INSTANCE.crt"; else CA_GLOB="kervax-panel*.crt"' in block
    assert "for d in /usr/local/share/ca-certificates /etc/pki/ca-trust/source/anchors /etc/pki/trust/anchors; do" in block
    assert "update-ca-trust extract" in block and "update-ca-certificates" in block


def parse(name, *args):
    """Гоняет разбор аргументов установщика в sh и возвращает, что он понял."""
    s = src(AGENT / name)
    start = s.index('URL=""; TOKEN=""')
    loop = s[start:s.index("\ndone\n", start) + 6]
    script = loop + 'printf "%s|%s|%s|%s|%s" "$CA_FILE" "$URL" "$TOKEN" "$INSTANCE" "$NO_AUTO"\n'
    out = subprocess.run(["sh", "-c", script, "sh", *args], capture_output=True, text=True, check=True)
    return out.stdout


@pytest.mark.skipif(shutil.which("sh") is None, reason="нужен sh")
@pytest.mark.parametrize("name", INSTALLERS)
def test_installer_args(name):
    ca = "/tmp/kervax-panel-ca.crt"
    # ровно как в команде из панели, плюс имя инстанса, которое UI дописывает в конец
    assert parse(name, "--ca-file", ca, "https://p", "tok", "main") == f"{ca}|https://p|tok|main|0"
    assert parse(name, "--ca-file=/x.pem", "https://p", "tok", "--no-auto") == "/x.pem|https://p|tok||1"
    assert parse(name, "https://p", "tok") == "|https://p|tok||0"


def parse_env(name, token, *args):
    """Разбор аргументов вместе с токеном из KERVAX_TOKEN (так его передает команда из панели)."""
    s = src(AGENT / name)
    start = s.index('URL=""; TOKEN=""')
    end = s.index("unset KERVAX_TOKEN\n", start) + len("unset KERVAX_TOKEN\n")
    script = s[start:end] + 'printf "%s|%s|%s|%s|%s" "$URL" "$TOKEN" "$INSTANCE" "$NO_AUTO" "${KERVAX_TOKEN-unset}"\n'
    out = subprocess.run(["sh", "-c", script, "sh", *args], capture_output=True, text=True, check=True,
                         env={"KERVAX_TOKEN": token})
    return out.stdout


@pytest.mark.skipif(shutil.which("sh") is None, reason="нужен sh")
@pytest.mark.parametrize("name", INSTALLERS)
def test_installer_token_from_env(name):
    # новая команда: токен в окружении, второй позиционный - имя инстанса
    assert parse_env(name, "tok", "https://p") == "https://p|tok||0|unset"
    assert parse_env(name, "tok", "--ca-file", "/x", "https://p", "main", "--no-auto") == "https://p|tok|main|1|unset"
    # старая команда (токен аргументом) работает как раньше
    assert parse_env(name, "", "https://p", "tok", "main") == "https://p|tok|main|0|unset"
