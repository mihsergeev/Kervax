"""Установщик агента работает со старым curl: на Ubuntu 20.04 (curl 7.68) нет --retry-all-errors."""
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent"


def test_retry_all_errors_only_where_curl_knows_it():
    for name in ("install.sh", "install-ru.sh"):
        src = (AGENT / name).read_text(encoding="utf-8")
        assert "--retry-all-errors \\" not in src, name  # безусловно - curl 7.68 не запускался вовсе
        assert "grep -q -- '--retry-all-errors' && RETRY_ALL=" in src and "$RETRY_ALL" in src, name
        assert "\r" not in src, name
