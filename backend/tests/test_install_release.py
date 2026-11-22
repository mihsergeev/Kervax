"""Установщик агента и quickstart не берут непроверенное.

Было: install.sh принимал http:// панели, и бинарь, helper'ы (bash от root) и токен шли открытым
текстом; первый бинарь не сверялся с подписанным манифестом панели и качался прямо в каталог kervax,
где агент мог подложить под временное имя симлинк. quickstart брал скрипты из main, а образы latest:
любой коммит в main сразу исполнялся от root на новых установках в обход выпуска, и версии скриптов
и образов расходились. .env с секретами создавался 0644 и до chmod был читаем всем.
Стало: http только с --insecure-http (команда панели на http сама его несет), по https редиректы
только на https, sha256 бинаря по манифесту в собственном каталоге root, quickstart ставит старший
стабильный тег, не откатывается сам и пишет KERVAX_VERSION, .env сразу 0600."""
import gzip
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import config

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "agent"
INSTALLERS = ("install.sh", "install-ru.sh")
QUICKSTARTS = ("quickstart.sh", "quickstart-ru.sh")
TOKEN = "fake-token-example"
PROTO = "--proto =https --proto-redir =https"

posix = pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None, reason="нужен POSIX sh")


def _stub(d: Path, name: str, body: str) -> None:
    (d / name).write_bytes(("#!/bin/sh\n" + body).encode())
    (d / name).chmod(0o755)


def _src(name: str) -> str:
    return (AGENT / name).read_text(encoding="utf-8")


def _pcurl(s: str) -> str:
    """Определение pcurl вместе с ограничением протоколов."""
    return s[s.index('PROTO="--proto'):s.index("\n}\n", s.index("pcurl() {")) + 3]


# --- http:// панели ---


def _gate(name: str) -> str:
    s = _src(name)
    start = s.index('URL=""; TOKEN=""')
    loop = s[start:s.index("\ndone\n", start) + 6]
    gate = s[s.index('case "$URL" in\n  [Hh][Tt][Tt][Pp][Ss]://*)'):s.index('case "$PROXY" in')]
    return "set -eu\n" + loop + gate + 'echo "passed $URL"\n'


@posix
@pytest.mark.parametrize("name", INSTALLERS)
@pytest.mark.parametrize("args,ok", [
    (["http://panel.example.test", "tok"], False),
    (["http://panel.example.test:8080", "tok", "main"], False),
    (["HTTP://panel.example.test", "tok"], False),   # регистр схемы не обходит проверку
    (["--insecure-http", "http://panel.example.test", "tok"], True),
    (["http://panel.example.test", "tok", "main", "--insecure-http"], True),
    (["--insecure-http", "Http://panel.example.test", "tok"], True),
    (["https://panel.example.test", "tok"], True),
    (["HTTPS://panel.example.test", "tok"], True),
    (["panel.example.test", "tok"], False),          # curl сам подставил бы http://
    (["ftp://panel.example.test", "tok"], False),
])
def test_installer_refuses_plain_http_without_the_flag(name, args, ok):
    r = subprocess.run(["sh", "-c", _gate(name), "sh", *args], capture_output=True, text=True,
                       encoding="utf-8", timeout=30)
    assert ("passed" in r.stdout) is ok, r.stderr
    assert (r.returncode == 0) is ok
    if not ok:
        assert "https://" in r.stderr and "--insecure-http" in r.stderr
    else:
        # схема дальше всегда в нижнем регистре: от нее зависят порт GRE и --proto
        assert "passed https://panel.example.test" in r.stdout or "passed http://panel.example.test" in r.stdout
        if "--insecure-http" in args:
            assert "--insecure-http" in r.stderr  # предупреждение остается и с флагом


def test_http_gate_is_before_the_first_request_to_the_panel():
    for name in INSTALLERS:
        s = _src(name)
        gate = s.index('case "$URL" in\n  [Hh][Tt][Tt][Pp][Ss]://*)')
        # до проверки CA, скачивания агента и GRE-подмены адреса; после --uninstall (ему URL не нужен)
        assert s.index('if [ "$UNINSTALL" = 1 ]; then') < gate < s.index('PROTO="--proto') < s.index("pcurl() {"), name
        assert gate < s.index('curl() { command curl --resolve'), name
        assert "--insecure-http) INSECURE_HTTP=1 ;;" in s and "[--insecure-http]" in s, name


@pytest.fixture
def http_panel(tmp_path, monkeypatch):
    monkeypatch.setenv("KERVAX_DATA_DIR", (tmp_path / "data").as_posix())
    (tmp_path / "data").mkdir()

    def use(url: str):
        monkeypatch.setenv("KERVAX_PANEL_URL", url)
        config.get_settings.cache_clear()
    yield use
    config.get_settings.cache_clear()


def test_panel_command_carries_the_flag_only_for_an_http_panel(http_panel, tmp_path):
    from app.api.servers import _install_cmd
    http_panel("https://panel.example.test")
    cmd = _install_cmd(TOKEN)
    assert "--insecure-http" not in cmd and f" -m 120 {PROTO} " in cmd
    http_panel("http://panel.example.test:8080")
    cmd = _install_cmd(TOKEN)
    assert cmd.endswith("' - --insecure-http http://panel.example.test:8080") and "--proto" not in cmd
    http_panel("HTTP://panel.example.test")
    assert " --insecure-http HTTP://panel.example.test" in _install_cmd(TOKEN)
    # с корнем CA флаг стоит после "--ca-file <файл>": curl команды берет файл вторым аргументом
    http_panel("http://panel.example.test:8080")
    (tmp_path / "data" / "panel-ca.crt").write_text(
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n", encoding="ascii")
    cmd = _install_cmd(TOKEN)
    assert cmd.endswith("' - --ca-file \"$KERVAX_CA\" --insecure-http http://panel.example.test:8080")
    assert ' --cacert "$2"' in cmd


@posix
@pytest.mark.parametrize("name", INSTALLERS)
@pytest.mark.parametrize("panel", ["http://panel.example.test", "HTTP://panel.example.test", "https://panel.example.test"])
def test_panel_command_passes_the_installer_gate(name, panel, http_panel, tmp_path):
    # команда из окна целиком: скачивает установщик (здесь - его проверку адреса) и запускает его
    from app.api.servers import _install_cmd
    http_panel(panel)
    b = tmp_path / "bin"
    b.mkdir()
    gate = tmp_path / "gate.sh"
    gate.write_text(_gate(name), encoding="utf-8")
    _stub(b, "curl", f'echo "$*" >> "{(tmp_path / "log").as_posix()}"\n'
                     'out=""; prev=""\nfor a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
                     f'cp "{gate.as_posix()}" "$out"\n')
    env = dict(os.environ, PATH=f"{b.as_posix()}{os.pathsep}{os.environ['PATH']}")
    r = subprocess.run(["sh", "-c", _install_cmd(TOKEN) + " main"], capture_output=True, text=True,
                       encoding="utf-8", timeout=60, env=env)
    assert r.returncode == 0 and "passed" in r.stdout, r.stderr
    assert (PROTO in (tmp_path / "log").read_text()) is panel.startswith("https")


def test_install_window_warns_about_an_http_panel():
    page = (ROOT / "frontend/src/ServersPage.tsx").read_text(encoding="utf-8")
    i18n = (ROOT / "frontend/src/i18n.tsx").read_text(encoding="utf-8")
    assert "enroll.install_cmd.includes(' --insecure-http ')" in page
    msg = page.split("enroll.install_cmd.includes(' --insecure-http ')", 1)[1].split("{t('", 1)[1].split("')}", 1)[0]
    assert "http" in msg and "--insecure-http" in msg and f"'{msg}':" in i18n


# --- редиректы только на https ---


@posix
@pytest.mark.parametrize("name", INSTALLERS)
@pytest.mark.parametrize("insecure,proxy", [("0", ""), ("0", "http://u:fakepass@proxy.example.test:3128"), ("1", "")])
def test_requests_to_the_panel_keep_to_https(name, insecure, proxy, tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    _stub(b, "curl", f'echo "$*" >> "{(tmp_path / "log").as_posix()}"\n')
    script = f'set -eu\nINSECURE_HTTP={insecure}; PROXY="{proxy}"\n' + _pcurl(_src(name)) + 'pcurl -fsS https://panel.example.test/x\n'
    env = dict(os.environ, PATH=f"{b.as_posix()}{os.pathsep}{os.environ['PATH']}")
    r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, encoding="utf-8", timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    log = (tmp_path / "log").read_text()
    assert (PROTO in log) is (insecure == "0")
    assert (f"--proxy {proxy}" in log) is bool(proxy)


@pytest.mark.skipif(shutil.which("curl") is None or os.name == "nt", reason="нужен curl")
def test_real_curl_takes_the_protocol_limits():
    # опции знакомы curl и уживаются с --resolve, --proxy, -C - и повторами: отказ - только по протоколу
    base = ["curl", "-sS", *PROTO.split(), "--connect-timeout", "3", "-m", "5"]
    r = subprocess.run([*base, "http://127.0.0.1:9/"], capture_output=True, timeout=30)
    assert r.returncode == 1, r.stderr                         # CURLE_UNSUPPORTED_PROTOCOL
    for extra in (["--resolve", "panel.example.test:9:127.0.0.1"], ["--proxy", "http://127.0.0.1:9"],
                  ["-C", "-", "-o", os.devnull, "--retry", "1", "--retry-delay", "1"]):
        r = subprocess.run([*base, *extra, "https://panel.example.test:9/"], capture_output=True, timeout=30)
        assert r.returncode in (6, 7, 28), (extra, r.returncode, r.stderr)  # дошли до сети, не до разбора опций


# --- первый бинарь против подписанного манифеста ---


BINARY = b"\x7fELF synthetic agent binary for the test suite\n" * 64


def _manifest(arches: dict[str, bytes], pretty: bool = False) -> str:
    man = {"version": "2.73", "released_at": "2026-11-01T00:00:00+00:00", "min_agent": "1.0",
           "artifacts": {a: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for a, b in arches.items()}}
    # release.py пишет компактно и с сортировкой ключей; разметка с пробелами тоже должна читаться
    return json.dumps(man, sort_keys=True, indent=2) if pretty else json.dumps(man, sort_keys=True, separators=(",", ":"))


def _verify(name: str, tmp_path: Path, code: str, body: str, binary: bytes = BINARY, arch: str = "amd64"):
    s = _src(name)
    fn = s[s.index("verify_agent() {"):s.index("\n}\n", s.index("verify_agent() {")) + 3]
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    (tmp_path / "body").write_text(body, encoding="utf-8")
    _stub(b, "curl", 'echo "$*" >> "$LOG"\nout=""; prev=""\n'
                     'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
                     '[ -n "$out" ] && cat "$BODY" > "$out"\n'
                     '[ "$CODE" = 000 ] && { printf 000; exit 7; }\nprintf "%s" "$CODE"\n')
    agent = tmp_path / "agent.new"
    agent.write_bytes(binary)
    script = (f'set -eu\nURL=https://panel.example.test; PROXY=""; INSECURE_HTTP=0; ARCH={arch}\n' + _pcurl(s) + fn
              + f'if verify_agent "{agent.as_posix()}"; then echo VERIFIED; else echo REFUSED; exit 1; fi\n')
    env = dict(os.environ, PATH=f"{b.as_posix()}{os.pathsep}{os.environ['PATH']}", LOG=(tmp_path / "log").as_posix(),
               BODY=(tmp_path / "body").as_posix(), CODE=code)
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, encoding="utf-8", timeout=30, env=env)


@posix
@pytest.mark.parametrize("name", INSTALLERS)
@pytest.mark.parametrize("case,code,body,arch,ok", [
    ("match", "200", _manifest({"amd64": BINARY, "arm64": b"other"}), "amd64", True),
    ("match-arm64", "200", _manifest({"amd64": b"other", "arm64": BINARY}), "arm64", True),
    ("pretty", "200", _manifest({"amd64": BINARY, "arm64": b"other"}, pretty=True), "amd64", True),
    ("mismatch", "200", _manifest({"amd64": BINARY + b"swapped", "arm64": BINARY}), "amd64", False),
    ("no-arch", "200", _manifest({"arm64": BINARY}), "amd64", False),
    ("not-json", "200", "<html>proxy error</html>", "amd64", False),
    ("no-release", "404", '{"detail":"no manifest"}', "amd64", True),
    ("broken-release", "503", '{"detail":"release is broken"}', "amd64", False),
    ("unreachable", "000", "", "amd64", False),
])
def test_first_binary_is_checked_against_the_signed_manifest(name, tmp_path, case, code, body, arch, ok):
    r = _verify(name, tmp_path, code, body, arch=arch)
    assert ("VERIFIED" in r.stdout) is ok, (case, r.stdout, r.stderr)
    assert (r.returncode == 0) is ok
    log = (tmp_path / "log").read_text()
    assert "https://panel.example.test/api/agent/manifest" in log and PROTO in log
    if case == "no-release":
        assert r.stderr.strip()                       # без манифеста - предупреждение, не тишина
    if case == "mismatch":
        assert hashlib.sha256(BINARY).hexdigest() in r.stderr
    if case == "broken-release":
        assert "release is broken" in r.stderr         # почему панель не отдала манифест


def test_binary_is_verified_before_it_replaces_the_agent():
    for name in INSTALLERS:
        s = _src(name)
        call = s.index('verify_agent "$NEW" || exit 1')
        assert s.index('fetch "${URL%/}/api/agent/download/$ARCH" "$NEW"') < call, name
        assert call < s.index('mv -fT "$NEW" "$BIN"') < s.index('systemctl restart "$UNIT"'), name


# --- агент не подсовывает root'у симлинк в своем каталоге ---


def _download(name: str, tmp_path: Path, plant: str):
    """Шаг скачивания install.sh целиком, /opt/kervax-agent - во временном каталоге. Заглушка curl
    играет и панель, и агента, который в это время подкладывает ссылки в свой BIN_DIR."""
    s = _src(name)
    opt = tmp_path / "opt" / "kervax-agent"
    step = s[s.index("BIN_DIR=/opt/kervax-agent/bin"):s.index('chown -R kervax "$BIN_DIR"\n')]
    step = step.replace("/opt/kervax-agent", opt.as_posix())
    outside = tmp_path / "outside"
    outside.mkdir()
    for f in ("shadow", "shadow-gz", "target"):
        (outside / f).write_bytes(b"root-only secret\n")
        (outside / f).chmod(0o600)
    b = tmp_path / "bin"
    b.mkdir()
    (tmp_path / "agent.gz").write_bytes(gzip.compress(BINARY))
    _stub(b, "curl", 'echo "$*" >> "$LOG"\nout=""; prev=""\n'
                     'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
                     'case "$*" in\n'
                     '  *-fsI*)\n'
                     '    case "$PLANT" in *new*) ln -sfn "$OUT/shadow" "$BINDIR/kervax-agent.new"\n'
                     '                            ln -sfn "$OUT/shadow-gz" "$BINDIR/kervax-agent.new.gz" ;; esac\n'
                     '    case "$PLANT" in *bin*) ln -sfn "$OUT/target" "$BINDIR/kervax-agent" ;; esac\n'
                     '    case "$PLANT" in *dir*) mkdir -p "$BINDIR/kervax-agent" ;; esac ;;\n'
                     '  *download-gz*) case "$PLANT" in *kill*) kill -TERM "$PPID"; exit 22 ;; esac\n'
                     '    cat "$GZ" > "$out" ;;\n'
                     '  *manifest*) printf 404 ;;\n'
                     '  *) exit 22 ;;\n'
                     'esac\n')
    script = ('set -eu\nURL=https://panel.example.test; PROXY=""; INSECURE_HTTP=0; ARCH=amd64\n'
              'chown() { :; }\n' + _pcurl(s) + step + 'echo INSTALLED\n')
    env = dict(os.environ, PATH=f"{b.as_posix()}{os.pathsep}{os.environ['PATH']}", LOG=(tmp_path / "log").as_posix(),
               PLANT=plant, OUT=outside.as_posix(), BINDIR=(opt / "bin").as_posix(), GZ=(tmp_path / "agent.gz").as_posix())
    r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
    for f in ("shadow", "shadow-gz", "target"):
        assert (outside / f).read_bytes() == b"root-only secret\n", (f, r.stderr)
        assert oct((outside / f).stat().st_mode & 0o777) == "0o600", f
    return r, opt


@posix
@pytest.mark.skipif(shutil.which("gzip") is None, reason="нужен gzip")
@pytest.mark.parametrize("name", INSTALLERS)
@pytest.mark.parametrize("plant", ["", "new", "new+bin"])
def test_planted_symlinks_do_not_redirect_the_download(name, plant, tmp_path):
    r, opt = _download(name, tmp_path, plant)
    assert r.returncode == 0 and "INSTALLED" in r.stdout, r.stderr
    agent = opt / "bin" / "kervax-agent"
    # на месте бинаря - сам бинарь, а не ссылка (подложенная заменена записью каталога)
    assert not agent.is_symlink() and agent.read_bytes() == BINARY
    assert oct(agent.stat().st_mode & 0o777) == "0o755"
    assert not list(opt.glob(".install.*"))  # временный каталог root убран


@posix
@pytest.mark.skipif(shutil.which("gzip") is None, reason="нужен gzip")
@pytest.mark.parametrize("name", INSTALLERS)
def test_a_directory_in_place_of_the_binary_stops_the_install(name, tmp_path):
    r, opt = _download(name, tmp_path, "dir")
    assert r.returncode != 0 and "INSTALLED" not in r.stdout
    assert not any((opt / "bin" / "kervax-agent").iterdir())
    assert not list(opt.glob(".install.*"))


@posix
@pytest.mark.skipif(shutil.which("gzip") is None, reason="нужен gzip")
@pytest.mark.parametrize("name", INSTALLERS)
def test_temporary_directory_does_not_outlive_the_install(name, tmp_path):
    # dash не выполняет trap EXIT при смерти от сигнала: каталог убирает ловушка TERM
    r, opt = _download(name, tmp_path, "kill")
    assert r.returncode != 0 and "INSTALLED" not in r.stdout
    assert not list(opt.glob(".install.*")), r.stderr
    # остаток давно убитой установки убирается следующей, идущая рядом (свежая) не трогается
    stale, fresh = opt / ".install.stale0", opt / ".install.fresh0"
    stale.mkdir()
    fresh.mkdir()
    os.utime(stale, (0, 0))
    shutil.rmtree(tmp_path / "bin")
    shutil.rmtree(tmp_path / "outside")
    r, opt = _download(name, tmp_path, "")
    assert r.returncode == 0, r.stderr
    assert not stale.exists() and fresh.exists()
    assert [p.name for p in opt.glob(".install.*")] == [fresh.name]


# --- quickstart: тег выпуска, без отката, KERVAX_VERSION, .env 0600 ---


# старший стабильный - v1.10.0; бэкпорт v1.8.156 выпущен позже, но latest его не делает
TAGS = ["v1.8.155", "v1.9.9", "v1.10.0", "v1.10.1-rc1", "v2.0.0-rc1", "v01.2.3", "vx/refs/tags/v9.9.9", "v1.8.156"]


def _quickstart(name: str, tmp_path: Path, args: list[str], tags=TAGS, ancestor: bool = True, missing: str = ""):
    """quickstart целиком на заглушках: docker, git, curl и прочее только пишут, что их вызвали."""
    b = tmp_path / "bin"
    log = tmp_path / "log"
    if not b.exists():
        b.mkdir()
        tpl = tmp_path / "tpl"
        (tpl / ".git").mkdir(parents=True)
        (tpl / "ops").mkdir()
        shutil.copy(ROOT / ".env.example", tpl / ".env.example")
        (tpl / "ops" / "agent-proxy-peers-setup.sh").write_text("exit 0\n")
        real_cp = shutil.which("cp")
        _stub(b, "id", 'echo 0\n')
        _stub(b, "ss", 'exit 0\n')
        _stub(b, "sleep", 'exit 0\n')
        _stub(b, "openssl", 'echo synthetic00secret00\n')
        # режим .env В МОМЕНТ создания копией, до любых chmod
        _stub(b, "cp", f'"{real_cp}" "$@" || exit $?\nfor last; do :; done\n'
                       'case "$last" in *.env) stat -c %a "$last" >> "$MODES" ;; esac\n')
        _stub(b, "docker", 'echo "docker $*" >> "$LOG"\n'
                           'case "$1" in\n'
                           '  --version) echo "Docker version 0.0.0-test" ;;\n'
                           '  network) case "$*" in *EnableIPv6*) echo true ;; *Gateway*) echo "192.0.2.1 " ;; esac ;;\n'
                           '  ps) echo "caddy-ct lucaslorentz/caddy-docker-proxy:test" ;;\n'
                           # образ в реестре есть, если его нет в $MISSING (имя:версия через пробел)
                           '  manifest) case " $MISSING " in *" ${3##*/} "*) exit 1 ;; esac ;;\n'
                           'esac\nexit 0\n')
        _stub(b, "curl", 'echo "curl $*" >> "$LOG"\n'
                         'case "$*" in *api/health*) printf 200 ;; *) exit 7 ;; esac\n')
        _stub(b, "git", 'echo "git $*" >> "$LOG"\n'
                        'case "$1 $3" in\n'
                        '  ls-remote*) cat "$TAGS" ;;\n'
                        f'  clone*) for last; do :; done; mkdir -p "${{last%/*}}" && "{real_cp}" -R "$TPL" "$last" ;;\n'
                        '  "-C merge-base") exit "$ANCESTOR" ;;\n'
                        '  "-C rev-parse") echo abc1234 ;;\n'
                        'esac\nexit 0\n')
    (tmp_path / "tags").write_text("".join(f"{i:040x}\trefs/tags/{t}\n" for i, t in enumerate(tags)))
    if log.exists():
        log.unlink()
    d = tmp_path / "opt" / "kervax"
    env = dict(os.environ, PATH=f"{b.as_posix()}{os.pathsep}{os.environ['PATH']}", LOG=log.as_posix(),
               MODES=(tmp_path / "modes").as_posix(), TAGS=(tmp_path / "tags").as_posix(),
               TPL=(tmp_path / "tpl").as_posix(), ANCESTOR="0" if ancestor else "1", MISSING=missing)
    r = subprocess.run(["sh", "-c", 'umask 022; exec sh "$0" "$@"', (ROOT / "ops" / name).as_posix(),
                        "--domain", "panel.example.test", "--dir", d.as_posix(), *args],
                       capture_output=True, text=True, encoding="utf-8", timeout=60, env=env, stdin=subprocess.DEVNULL)
    return r, d, (log.read_text() if log.exists() else "")


def _env_lines(d: Path, key: str) -> list[str]:
    return [x for x in (d / ".env").read_text().splitlines() if x.startswith(key + "=")]


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_installs_the_highest_stable_tag(name, tmp_path):
    r, d, log = _quickstart(name, tmp_path, [])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "git ls-remote --tags --refs https://github.com/mihsergeev/Kervax.git refs/tags/v*" in log
    # v1.10.0 > v1.9.9 по числам; бэкпорт v1.8.156, пре-релизы, ведущие нули и чужие пути тегов не в счет.
    # "Последний релиз" GitHub не спрашивается: им становится и бэкпорт
    assert "git clone -q --branch v1.10.0 https://github.com/mihsergeev/Kervax.git" in log
    assert "api.github.com" not in log
    # оба образа выпуска уже в реестре, владелец в нижнем регистре, как публикует release.yml
    assert "docker manifest inspect ghcr.io/mihsergeev/kervax-backend:1.10.0" in log
    assert "docker manifest inspect ghcr.io/mihsergeev/kervax-frontend:1.10.0" in log
    assert log.index("manifest inspect") < log.index("git clone")
    assert _env_lines(d, "KERVAX_VERSION") == ["KERVAX_VERSION=1.10.0"]
    assert _env_lines(d, "COMPOSE_FILE") == ["COMPOSE_FILE=compose.yml:compose.ghcr.yml:compose.caddy.yml"]
    assert "up -d --pull always" in log
    # секреты не бывают читаемы всем: файл создан сразу 0600 и таким остался
    assert (tmp_path / "modes").read_text().split() == ["600"]
    assert oct((d / ".env").stat().st_mode & 0o777) == "0o600"


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_rerun_upgrades_to_the_new_tag(name, tmp_path):
    r, d, log = _quickstart(name, tmp_path, [], tags=["v1.9.9", "v1.8.156"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "git clone -q --branch v1.9.9 " in log
    # повторный запуск - обновление: тот же каталог переходит на новый тег, строка не дублируется
    r, d, log = _quickstart(name, tmp_path, [])
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"git -C {d.as_posix()} fetch -q origin refs/tags/v1.10.0:refs/tags/v1.10.0" in log
    assert "merge-base" not in log  # выпуск известен по KERVAX_VERSION: сравнивается номер
    assert f"git -C {d.as_posix()} checkout -q --detach refs/tags/v1.10.0" in log
    assert "pull" not in log.replace("--pull always", "") and "clone" not in log
    assert _env_lines(d, "KERVAX_VERSION") == ["KERVAX_VERSION=1.10.0"]
    assert (tmp_path / "modes").read_text().split() == ["600"]  # .env не пересоздавался
    # тот же выпуск еще раз - не откат
    r, d, log = _quickstart(name, tmp_path, [])
    assert r.returncode == 0 and "checkout -q --detach refs/tags/v1.10.0" in log, r.stderr


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_does_not_go_behind_a_checkout_ahead_of_the_tag(name, tmp_path):
    # установка из main (старый quickstart, --build): HEAD впереди последнего тега, в базе - его миграции
    r, d, _ = _quickstart(name, tmp_path, ["--build"])
    assert r.returncode == 0, r.stdout + r.stderr
    (d / ".env").write_text((d / ".env").read_text().replace("KERVAX_VERSION=1.10.0\n", ""))
    r, d, log = _quickstart(name, tmp_path, ["--build"], ancestor=False)
    assert r.returncode == 1 and "--allow-downgrade" in r.stderr
    # правда о том, чего ждать: выпуска с этим коммитом, иначе - бэкап той версии
    assert "abc1234" in r.stderr and "v1.10.0" in r.stderr and "main" in r.stderr
    assert "checkout" not in log and " up -d" not in log
    r, d, log = _quickstart(name, tmp_path, ["--build", "--allow-downgrade"], ancestor=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--allow-downgrade" in r.stdout and "checkout -q --detach refs/tags/v1.10.0" in log


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_pins_an_old_main_install_on_released_images(name, tmp_path):
    # старый quickstart из main на образах latest: база - от старшего выпуска, файлы впереди тега
    r, d, _ = _quickstart(name, tmp_path, [])
    assert r.returncode == 0, r.stdout + r.stderr
    (d / ".env").write_text((d / ".env").read_text().replace("KERVAX_VERSION=1.10.0\n", ""))
    r, d, log = _quickstart(name, tmp_path, [], ancestor=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "v1.10.0" in r.stdout and "checkout -q --detach refs/tags/v1.10.0" in log
    assert _env_lines(d, "KERVAX_VERSION") == ["KERVAX_VERSION=1.10.0"]
    # явно ниже старшего выпуска - уже возможный откат схемы
    (d / ".env").write_text((d / ".env").read_text().replace("KERVAX_VERSION=1.10.0\n", ""))
    r, d, log = _quickstart(name, tmp_path, ["--version", "1.9.9"], ancestor=False)
    assert r.returncode == 1 and "v1.10.0" in r.stderr and "--allow-downgrade" in r.stderr
    assert "checkout" not in log


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_upgrades_from_a_backport_release(name, tmp_path):
    # v1.8.156 из бэкпорт-ветки не предок v1.10.0, но это обновление, а не откат
    r, d, _ = _quickstart(name, tmp_path, ["--version", "1.8.156"])
    assert r.returncode == 0, r.stdout + r.stderr
    r, d, log = _quickstart(name, tmp_path, [], ancestor=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "checkout -q --detach refs/tags/v1.10.0" in log
    assert _env_lines(d, "KERVAX_VERSION") == ["KERVAX_VERSION=1.10.0"]


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
@pytest.mark.parametrize("missing", ["kervax-backend:1.10.0 kervax-frontend:1.10.0", "kervax-frontend:1.10.0"])
def test_quickstart_skips_a_release_without_images(name, missing, tmp_path):
    # тег уже есть, а Release workflow еще идет или упал: берется предыдущий выпуск
    r, d, log = _quickstart(name, tmp_path, [], missing=missing)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "v1.10.0" in r.stdout and "git clone -q --branch v1.9.9 " in log
    assert _env_lines(d, "KERVAX_VERSION") == ["KERVAX_VERSION=1.9.9"]


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_changes_nothing_when_no_release_has_images(name, tmp_path):
    r, d, _ = _quickstart(name, tmp_path, [], tags=["v1.9.9"])
    assert r.returncode == 0, r.stdout + r.stderr
    env = (d / ".env").read_bytes()
    new = ["v1.10.0", "v1.10.1", "v1.10.2", "v1.10.3", "v1.10.4", "v1.10.5"]
    gone = " ".join(f"kervax-backend:{t[1:]}" for t in new)
    r, d, log = _quickstart(name, tmp_path, [], tags=["v1.9.9", *new], missing=gone)
    assert r.returncode == 1 and "5" in r.stderr and "--build" in r.stderr
    # смотрятся только 5 старших, и до каталога и .env дело не доходит
    assert log.count("manifest inspect") == 5 and "kervax-backend:1.10.1" in log
    assert ":1.10.0" not in log and ":1.9.9" not in log
    assert "fetch" not in log and "checkout" not in log and " up -d" not in log
    assert (d / ".env").read_bytes() == env


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_refuses_an_explicit_release_without_images(name, tmp_path):
    r, d, _ = _quickstart(name, tmp_path, [], tags=["v1.9.9"])
    assert r.returncode == 0, r.stdout + r.stderr
    env = (d / ".env").read_bytes()
    r, d, log = _quickstart(name, tmp_path, ["--version", "1.10.0"], missing="kervax-backend:1.10.0")
    assert r.returncode == 1 and "v1.10.0" in r.stderr and "--build" in r.stderr
    assert "fetch" not in log and "checkout" not in log and (d / ".env").read_bytes() == env
    # сборка из исходников образы реестра не ждет: старший тег, без проверки реестра
    r, d, log = _quickstart(name, tmp_path, ["--build"], missing="kervax-backend:1.10.0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "manifest inspect" not in log and "checkout -q --detach refs/tags/v1.10.0" in log


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_does_not_go_below_the_installed_version(name, tmp_path):
    r, d, _ = _quickstart(name, tmp_path, [])
    assert r.returncode == 0, r.stdout + r.stderr
    # ниже установленной по номеру: отказ, даже если история бы позволила (проверки независимы)
    for version in ("1.9.9", "v1.8.156"):
        r, d, log = _quickstart(name, tmp_path, ["--version", version])
        assert r.returncode == 1 and "1.10.0" in r.stderr and "--allow-downgrade" in r.stderr, version
        assert "checkout" not in log and " up -d" not in log
        assert _env_lines(d, "KERVAX_VERSION") == ["KERVAX_VERSION=1.10.0"]
    r, d, log = _quickstart(name, tmp_path, ["--version", "1.9.9", "--allow-downgrade"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "checkout -q --detach refs/tags/v1.9.9" in log
    assert _env_lines(d, "KERVAX_VERSION") == ["KERVAX_VERSION=1.9.9"]


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_takes_an_explicit_version(name, tmp_path):
    r, d, log = _quickstart(name, tmp_path, ["--version", "1.8.150"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ls-remote" not in log and "git clone -q --branch v1.8.150 " in log
    assert _env_lines(d, "KERVAX_VERSION") == ["KERVAX_VERSION=1.8.150"]


@posix
@pytest.mark.parametrize("name", QUICKSTARTS)
@pytest.mark.parametrize("args,tags", [
    (["--version", "main"], TAGS),
    (["--version", "1.8.150;id"], TAGS),
    (["--version", "1.2.3\nx|y;id"], TAGS),    # вторая строка не проскакивает мимо построчного grep
    (["--version", "1.2.3\n"], TAGS),
    ([], ["v2.0.0-rc1", "latest"]),            # стабильного тега нет
])
def test_quickstart_stops_without_a_release(name, tmp_path, args, tags):
    r, d, log = _quickstart(name, tmp_path, args, tags=tags)
    assert r.returncode == 1 and "vX.Y.Z" in r.stderr
    assert "git clone" not in log and " up -d" not in log and not d.exists()


@pytest.mark.parametrize("name", QUICKSTARTS)
def test_quickstart_documents_the_release_pinning(name):
    s = (ROOT / "ops" / name).read_text(encoding="utf-8")
    head = s[:s.index("\nset -eu\n")]
    assert "--version <vX.Y.Z>" in head and "--allow-downgrade" in head and "KERVAX_VERSION" in head
    assert "git pull" not in s and "(umask 077 && cp .env.example .env)" in s
