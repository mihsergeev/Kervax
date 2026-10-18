"""Установщик агента работает со старым curl: на Ubuntu 20.04 (curl 7.68) нет --retry-all-errors."""
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent"


def test_retry_all_errors_only_where_curl_knows_it():
    for name in ("install.sh", "install-ru.sh"):
        src = (AGENT / name).read_text(encoding="utf-8")
        assert "--retry-all-errors \\" not in src, name  # безусловно - curl 7.68 не запускался вовсе
        assert "grep -q -- '--retry-all-errors' && RETRY_ALL=" in src and "$RETRY_ALL" in src, name
        assert "\r" not in src, name


def test_agent_directory_does_not_depend_on_the_callers_umask():
    # sudo у пользователя с umask 077 создавал /opt/kervax-agent с 0700: агент от kervax
    # не мог запустить свой бинарь (status=203/EXEC, Permission denied)
    for name in ("install.sh", "install-ru.sh"):
        src = (AGENT / name).read_text(encoding="utf-8")
        root = src.index('if [ "$(id -u)" != "0" ]; then')
        assert src.index("umask 022", root) < src.index('mkdir -p "$BIN_DIR"'), name
        assert 'chmod 0755 /opt/kervax-agent "$BIN_DIR"' in src, name
        # конфиг с токеном по-прежнему 0600, а дальше umask снова 022
        conf = src.index("\numask 077\n")
        assert src.index('chmod 0600 "$CONF"', conf) < src.index("umask 022", conf), name
        assert 'if ! systemctl restart "$UNIT"; then' in src, name
