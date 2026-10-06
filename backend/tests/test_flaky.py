"""Сайт отвечает через раз: сбои вперемешку с успешными проверками, которых алерт "N неудач
подряд" не видит (один сайт висел так каждый день без единого алерта)."""
from sqlalchemy import select

from app import collector, flaky
from app.checks import CheckOutcome
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check, CheckIncident

U, D, G = "up", "down", "degraded"


def test_short_failures_skip_ordinary_outages():
    # одиночные сбои считаются, серия из трех (обычное падение при пороге 3) - нет
    assert flaky.short_failures([U, D, U, D, D, U, D, D, D, U], 3) == (3, 2)
    # медленно - не неудача
    assert flaky.short_failures([D, G, G, D, U], 3) == (2, 2)
    # длинная серия, начавшаяся до окна, не превращается в короткую на его краю
    hist = [D] * 3 + [U] * (flaky.WINDOW - 1)
    assert flaky.short_failures(hist, 3) == (0, 0)
    # окно - последние WINDOW проверок
    assert flaky.short_failures([D, U, D, U] + [U] * flaky.WINDOW, 3) == (0, 0)


def test_is_flaky_needs_several_separate_failures():
    base = [U] * 16
    assert not flaky.is_flaky(base + [D, U, D, D], 3, False)  # три сбоя
    assert not flaky.is_flaky(base + [D, D, U, D, D], 3, False)  # четыре, но двумя сериями
    assert flaky.is_flaky(base + [D, U, D, U, D, D], 3, False)  # четыре тремя сериями
    # гистерезис: держится, пока в окне больше одного сбоя
    tail = [D, U, D] + [U] * (flaky.WINDOW - 3)
    assert flaky.is_flaky(tail, 3, True)
    assert not flaky.is_flaky(tail + [U], 3, True)
    # во время обычного падения состояние не меняется
    assert flaky.is_flaky([U] * 10 + [D, D, D], 3, True)
    assert not flaky.is_flaky([D, U, D, U, D, U] + [D, D, D], 3, False)
    # порог 1: любой сбой - уже обычный алерт, через раз не бывает
    assert not flaky.is_flaky([D, U, D, U, D, U, D, U], 1, False)


async def _run(tmp_path, monkeypatch, seq, mutes=None):
    db = (tmp_path / "f.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        c = Check(
            name="site", type="http", target="https://x", interval_seconds=1, enabled=True,
            alert_after_failures=3, alert_mutes=mutes,
        )
        s.add(c)
        await s.commit()
        cid = c.id
    box = {"i": 0}

    async def fake_run(check):
        st = seq[box["i"]]
        box["i"] += 1
        return CheckOutcome(st, message="ReadTimeout" if st == D else "HTTP 200")

    async def no_expiry(check):
        return None

    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.checks.run_check", fake_run)
    monkeypatch.setattr("app.checks.probe_expiry", no_expiry)
    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    seen = []
    for _ in seq:
        async with factory() as s:
            row = await s.get(Check, cid)
            row.last_checked_at = None
            await s.commit()
        await collector.run_due_checks(factory, settings)
        async with factory() as s:
            seen.append((await s.get(Check, cid)).flaky_since is not None)
    async with factory() as s:
        row = await s.get(Check, cid)
        incidents = list(await s.scalars(select(CheckIncident)))
    await engine.dispose()
    return sent, seen, row, incidents


async def test_flaky_alert_and_recovery(tmp_path, monkeypatch):
    seq = [U] * 20 + [D, U, U, D, U, D, U, D] + [U] * (flaky.WINDOW + 2)
    sent, seen, row, incidents = await _run(tmp_path, monkeypatch, seq)
    # обычного алерта нет (подряд максимум один сбой), через раз - ровно один, и отбой
    assert len(sent) == 2, sent
    assert "🟠" in sent[0] and "отвечает через раз: 4 из 20" in sent[0]
    assert "ReadTimeout" in sent[0] and 'href="https://x"' in sent[0]
    assert "✅" in sent[1] and "снова отвечает стабильно" in sent[1]
    assert not any(i.notified for i in incidents)
    assert seen.index(True) == 27  # четвертый сбой
    assert seen.index(False, 27) == 45  # в окне остался один сбой
    assert not row.flaky_notified and row.flaky_since is None


async def test_ordinary_outage_is_not_flaky(tmp_path, monkeypatch):
    # полное падение ловит обычный алерт; его сбои не превращаются в "через раз" после подъема
    seq = [U] * 3 + [D] * 6 + [U] * 10
    sent, seen, _, _ = await _run(tmp_path, monkeypatch, seq)
    assert len(sent) == 2 and "🔴" in sent[0] and "✅" in sent[1]
    assert not any(seen)


async def test_flaky_alert_can_be_muted_per_monitor(tmp_path, monkeypatch):
    # тип заглушен у монитора: алерт не уходит, а состояние для интерфейса есть
    seq = [U] * 3 + [D, U, D, U, D, U, D] + [U] * 3
    sent, seen, row, _ = await _run(tmp_path, monkeypatch, seq, mutes=["flaky"])
    assert sent == []
    assert seen.index(True) == 9
    assert row.flaky_since is not None and not row.flaky_notified
