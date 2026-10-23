"""Мигающий сайт: одна тревога и один отбой на эпизод, а не сообщение на каждый поворот
(app/site_flap.py). Последовательности проверок гоняются через record_outcome с подставным
временем: сколько и каких сообщений уходит."""
from datetime import datetime, timedelta, timezone

from app import collector, flaky, site_flap
from app.checks import CheckOutcome
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import Check

U, D, G = "up", "down", "degraded"
T0 = datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc)


def _what(text: str) -> str:
    if "Не отвечает" in text:
        return "down"
    if "Отвечает через раз" in text:
        return "flaky"
    if text.endswith("снова отвечает стабильно"):
        return "stable"
    if text.endswith("снова отвечает"):
        return "up"
    return text


async def _play(tmp_path, monkeypatch, seq, *, interval=200, thr=3, gaps=None):
    """Прогоняет статусы по одному на интервал. Возвращает [(номер проверки, что ушло)]."""
    db = (tmp_path / "f.db").as_posix()
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{db}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        c = Check(name="example.com", type="http", target="https://example.com", enabled=True,
                  interval_seconds=interval, alert_after_failures=thr, degraded_after_failures=5)
        s.add(c)
        await s.commit()
        cid = c.id
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    settings = Settings(alert_webhook="http://hook")
    out: list[tuple[int, str]] = []
    now = T0
    for i, st in enumerate(seq):
        now += timedelta(seconds=interval + (gaps or {}).get(i, 0))
        pending: list = []
        msg = {"down": "сервер принял запрос, но не ответил за 10 с", "degraded": "медленно: 9000 мс"}.get(st, "HTTP 200")
        async with factory() as s:
            row = await s.get(Check, cid)
            await collector.record_outcome(s, row, CheckOutcome(st, message=msg, latency_ms=500), now, pending)
            await s.commit()
        before = len(sent)
        if pending:
            await collector._send_alerts(factory, settings, pending, now)
        out += [(i, _what(t)) for t in sent[before:]]
    await engine.dispose()
    return out


def test_flapping_and_stable_rules():
    t = T0
    hist = [(t + timedelta(minutes=m), st) for m, st in [(0, D), (5, U), (10, D), (15, U)]]
    assert site_flap.flapping(hist, t + timedelta(minutes=15))
    assert not site_flap.flapping(hist[2:], t + timedelta(minutes=15))  # одно падение
    # первая серия старше часа - уже не мигание
    assert not site_flap.flapping(hist, t + timedelta(minutes=70))
    ups = [(t + timedelta(minutes=20 + m), U) for m in range(0, 30, 5)]
    assert not site_flap.stable(hist + ups, t + timedelta(minutes=39))  # 29 минут без падений
    assert site_flap.stable(hist + ups, t + timedelta(minutes=40))
    # медленно - не падение, но последние проверки должны пройти
    assert not site_flap.stable(hist + ups + [(t + timedelta(minutes=41), G)], t + timedelta(minutes=41))
    assert site_flap.cutoff(site_flap.closed(t)) == t and site_flap.cutoff(None) is None
    assert site_flap.level(site_flap.opened("down", t)) == "down" and site_flap.level(site_flap.closed(t)) is None


async def test_first_outage_alerts_and_recovers_at_once(tmp_path, monkeypatch):
    # первое падение - на пороге монитора, отбой - на первой удачной проверке, как раньше
    out = await _play(tmp_path, monkeypatch, [U] * 10 + [D] * 3 + [U] * 5)
    assert out == [(12, "down"), (13, "up")]


async def test_cfm_night_one_alert_one_recovery(tmp_path, monkeypatch):
    # ночь 24.10 с прода: тормоза, падение, 6 минут ответов, снова падение. Было три сообщения
    # ("недоступен", "снова отвечает", "через раз") и шло к серии, теперь одна тревога и один
    # отбой, когда сайт продержится полчаса
    night = [U, G, U, G, U, U, U, U, U, U, G, U, G, U, D, G, D, D, U, D, U, U, D, U, D, D, U, U]
    out = await _play(tmp_path, monkeypatch, [U] * 10 + night + [U] * 12)
    assert [w for _, w in out] == ["down", "stable"], out
    first_d = 10 + night.index(D)
    assert out[0][0] == first_d + 2  # тревога на третьей неудаче подряд, как и была
    last_d = 10 + len(night) - 1 - night[::-1].index(D)
    # отбой: 30 минут без падений при интервале 200 с - на девятой проверке после последнего
    assert out[1][0] == last_d + 9


async def test_alternating_failures_one_flaky_one_recovery(tmp_path, monkeypatch):
    # сбой, ок, сбой, ок... два часа подряд: одно "через раз" и один отбой, когда прошло
    seq = [U] * 20 + [D, U] * 36 + [U] * (flaky.WINDOW + 2)
    out = await _play(tmp_path, monkeypatch, seq)
    # отбой "через раз" - когда в окне остался один сбой, текст тот же "снова отвечает стабильно"
    assert out == [(20 + 6, "flaky"), (20 + 71 + flaky.WINDOW - 3, "stable")], out


async def test_flaky_then_long_outage_escalates_once(tmp_path, monkeypatch):
    # через раз -> лежит: одно сообщение, когда падение продержалось WORSE, дальше молчим
    seq = [U] * 20 + [D, U] * 4 + [D] * 15 + [U, D, U] + [U] * 12
    out = await _play(tmp_path, monkeypatch, seq)
    assert [w for _, w in out] == ["flaky", "down", "stable"], out
    start = 20 + 8
    # 10 минут при интервале 200 с - четвертая неудача подряд (порог монитора три)
    assert out[1][0] == start + 3


async def test_real_outage_after_closed_episode_alerts_fast(tmp_path, monkeypatch):
    # первый отбой ушел сразу (падение было одно), сайт снова упал - это новая тревога, и уже
    # мигание: отбой после получаса. Эпизод закрыт - следующее падение снова приходит сразу по
    # порогу, а спустя час после прошлых сбоев и отбой у него сразу
    seq = [U] * 10 + [D, D, D, U, D, D, D, U] + [U] * 12 + [D] * 5 + [U]
    out = await _play(tmp_path, monkeypatch, seq)
    assert out == [(12, "down"), (13, "up"), (16, "down"), (25, "stable"), (32, "down"), (35, "up")], out


async def test_refall_while_flapping_is_silent(tmp_path, monkeypatch):
    # мигает, отбоя не было - следующие падения, даже долгие, того же эпизода не повторяются
    seq = [U] * 10 + [D, U, D, D, D, U, D, D, D, U, U, D, D, D, D, D, D, D, D, U] + [U] * 12
    out = await _play(tmp_path, monkeypatch, seq)
    # было бы шесть: недоступен, снова отвечает, и так три раза
    assert out == [(14, "down"), (37, "stable")], out


async def test_episode_forgotten_after_long_pause(tmp_path, monkeypatch):
    # проверок не было дольше STABLE (монитор выключали) - старый эпизод не глушит падение
    seq = [U] * 10 + [D, U, D, D, D, U] + [D, D, D]
    pause = int(site_flap.STABLE.total_seconds()) + 60
    out = await _play(tmp_path, monkeypatch, seq, gaps={16: pause})
    assert out == [(14, "down"), (18, "down")], out
