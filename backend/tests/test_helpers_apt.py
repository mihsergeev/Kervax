"""apt в helper'ах агента и потолок установщика на один helper (зависание diskhealth-setup 25.10).

На одной ноде установщик из ssh 5 минут стоял на `timeout 300 apt-get install smartmontools`:
пакет встал за секунду, а хук needrestart после dpkg (ждало новое ядро) без DEBIAN_FRONTEND открыл
debconf-окно whiptail "Pending kernel upgrade" на терминале, в фоновой группе timeout его остановил
SIGTTOU, и apt ждал до таймаута. Все helper'ы после него по алфавиту встали на 5 минут позже."""
import re
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent"

# apt-get, который что-то ставит или обновляет (а не подсказка "apt-get clean" в тексте)
APT_RUN = re.compile(r"\bapt-get\b(?:\s+-\S+(?:\s+[^\s-]\S*)?)*\s+(?:install|update|upgrade|dist-upgrade)\b")


def _src(name: str) -> str:
    s = (AGENT / name).read_text(encoding="utf-8")
    assert "\r" not in s
    return s


def _apt_q_body(s: str) -> str:
    a = s.index("apt_q() {\n")
    return s[a:s.index("\n}\n", a)]


def test_apt_only_through_quiet_wrapper():
    seen = []
    for f in sorted(AGENT.glob("*.sh")):
        s = _src(f.name)
        body = _apt_q_body(s) if "apt_q() {\n" in s else ""
        for line in s.splitlines():
            if line.lstrip().startswith("#"):
                continue
            if APT_RUN.search(line) and line.strip() not in body:
                raise AssertionError(f"{f.name}: apt-get мимо apt_q: {line.strip()}")
        if body:
            seen.append(f.name)
            # без debconf-вопросов и без needrestart, со своим таймаутом, conffile старые, stdin пустой
            for part in ("DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_SUSPEND=1", "NEEDRESTART_MODE=l",
                         "timeout -k 10 ", "DPkg::Lock::Timeout=60", "--force-confdef", "--force-confold",
                         "</dev/null"):
                assert part in body, (f.name, part)
            assert "timeout 300" not in s, f.name
    assert {"diskhealth-setup.sh", "backup-setup.sh", "backupserver-setup.sh"} <= set(seen)


def test_versions_bumped_for_the_apt_fix():
    for name, ver in (("diskhealth-setup.sh", "0.5"), ("backup-setup.sh", "0.47"), ("backupserver-setup.sh", "0.38")):
        assert f"\nKERVAX_SETUP_VERSION={ver}  # MAJOR.MINOR" in _src(name), name


def test_dnf_has_timeout_and_no_stdin():
    s = _src("diskhealth-setup.sh")
    assert "timeout -k 10 120 dnf install -y -q smartmontools </dev/null" in s


def test_installer_caps_one_helper():
    for name in ("install.sh", "install-ru.sh"):
        s = _src(name)
        f = s[s.index("run_remote_setup() {"):]
        f = f[:f.index("\n}\n")]
        assert 'timeout -k 10 180 bash "$f" </dev/null || rc=$?' in f, name
        # без timeout (старый busybox) helper все равно запускается
        assert 'timeout -k 10 1 true 2>/dev/null' in f and '\n      bash "$f" </dev/null || rc=$?' in f, name
        assert "124|137)" in f, name
