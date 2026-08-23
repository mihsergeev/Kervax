from types import SimpleNamespace

from app import checks as checks_mod
from app.checks import CheckOutcome
from app.config import Settings


def _no_delay(monkeypatch):
    monkeypatch.setattr("app.checks.get_settings", lambda: Settings(check_retry_delay_ms=0))


def _seq_runner(monkeypatch, statuses):
    box = {"i": 0}

    async def fake_once(check):
        s = statuses[box["i"]]
        box["i"] += 1
        return CheckOutcome(s, message=s)

    monkeypatch.setattr("app.checks._run_once", fake_once)
    return box


async def test_retry_recovers_on_later_attempt(monkeypatch):
    _no_delay(monkeypatch)
    box = _seq_runner(monkeypatch, ["down", "down", "up"])
    r = await checks_mod.run_check(SimpleNamespace(type="http", retries=2))
    assert r.status == "up" and box["i"] == 3  # успех на 3-й попытке, дальше не пробуем


async def test_retry_all_fail_stays_down(monkeypatch):
    _no_delay(monkeypatch)
    box = _seq_runner(monkeypatch, ["down", "down", "down"])
    r = await checks_mod.run_check(SimpleNamespace(type="http", retries=2))
    assert r.status == "down" and box["i"] == 3  # 1 + 2 повтора


async def test_no_retry_when_first_up(monkeypatch):
    _no_delay(monkeypatch)
    box = _seq_runner(monkeypatch, ["up", "down", "down"])
    r = await checks_mod.run_check(SimpleNamespace(type="http", retries=2))
    assert r.status == "up" and box["i"] == 1  # первая же успешна — повторов нет


async def test_zero_retries_single_attempt(monkeypatch):
    _no_delay(monkeypatch)
    box = _seq_runner(monkeypatch, ["down", "up"])
    r = await checks_mod.run_check(SimpleNamespace(type="http", retries=0))
    assert r.status == "down" and box["i"] == 1  # без повторов — один прогон
