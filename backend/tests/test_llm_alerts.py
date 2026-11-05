"""LLM: доставленная тревога, подтвержденная норма и один доставленный отбой."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import collector
from app.config import Settings
from app.db import Base, create_engine_and_factory
from app.models import AlertEvent, Server
from tests.test_gpu import _llm


@pytest.fixture
async def env(tmp_path, monkeypatch):
    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'llm.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(Server(name="node-a", token_hash="x", enabled=True, backup_not_required=True,
                           alert_sustain_seconds=0, cpu_alert_percent=0, mem_alert_percent=0))
        await session.commit()
    sent, failures = [], []

    async def send(cfg, text, parse_mode=None):
        if failures and text.recovery:
            return ["Telegram: timeout"]
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(at, items, *, frozen=False, offline=False, state=None):
        async with factory() as session:
            srv = await session.scalar(select(Server))
            srv.last_seen = at - timedelta(hours=1) if offline else at
            report = {"uptime_seconds": 100000 + int(at.timestamp()), "clock_unix": at.timestamp()}
            if frozen:
                report = dict(srv.last_report)
            if items is not None:
                report["llm"] = items
            srv.last_report = report
            if state is not None:
                srv.alert_state = {**(srv.alert_state or {}), **state}
            await session.commit()
        await collector.evaluate_servers(factory, cfg, at)
        async with factory() as session:
            srv = await session.scalar(select(Server))
            events = list(await session.scalars(select(AlertEvent).where(AlertEvent.kind == "llm")
                                                .order_by(AlertEvent.id)))
            return dict(srv.alert_state or {}), events

    yield tick, sent, failures
    await engine.dispose()


@pytest.mark.calm
@pytest.mark.parametrize("bad", [
    _llm(ok=False, err="refused", win=None),
    _llm(waiting=20),
    _llm(kv=97, preempt=4),
])
async def test_llm_delivered_alert_recovers_once_after_confirmed_health(env, bad):
    tick, sent, failures = env
    start = datetime.now(timezone.utc) + timedelta(days=1)
    state, events = await tick(start, [bad])
    assert state["llm"] > 0 and [e.recovery for e in events] == [False]
    assert state["llm_who"] == ["vllm|container|vllm-qwen|8000"]
    if not bad["ok"]:
        assert sent[0].splitlines()[1:] == ["<b>LLM не отвечает</b>", "vllm-qwen недоступна"]
        assert "порт закрыт" in events[0].text  # подробности остаются в истории
    healthy = start + timedelta(minutes=1)
    for minute in range(15):
        state, events = await tick(healthy + timedelta(minutes=minute), [_llm()])
        assert state["llm"] > 0 and [e.recovery for e in events] == [False]
    # Неудачная доставка отбоя не закрывает алерт и не попадает в историю.
    failures.append(True)
    end = healthy + timedelta(seconds=collector._SETTLE_S)
    state, events = await tick(end, [_llm()])
    assert state["llm"] > 0 and [e.recovery for e in events] == [False]
    failures.clear()
    state, events = await tick(end + timedelta(minutes=1), [_llm()])
    assert state["llm"] == 0 and [e.recovery for e in events] == [False, True]
    assert "LLM снова отвечает" in sent[-1]
    state, events = await tick(end + timedelta(minutes=2), [_llm()])
    assert state["llm"] == 0 and len(sent) == 2 and len(events) == 2


@pytest.mark.calm
async def test_llm_unknown_telemetry_breaks_recovery_hold(env):
    tick, sent, _failures = env
    start = datetime.now(timezone.utc) + timedelta(days=1)
    await tick(start, [_llm(ok=False, err="refused")])
    at = start
    unknown = [
        (None, {}), ([], {}), ([_llm(name="another-model")], {}),
        ([_llm(ok=False, err="local")], {}), ([_llm(ok=False, err="noaddr")], {}),
        ([_llm(ok=False, err="http 401")], {}), ([_llm(no_metrics=True)], {}),
        ([_llm(ok=False, err="refused", up=30)], {}), ([_llm(ok=None)], {}),
        ([{}], {}),
        ([_llm()], {"offline": True}), ([_llm()], {"frozen": True}),
    ]
    for items, kwargs in unknown:
        at += timedelta(minutes=1)
        state, _events = await tick(at, [_llm()])
        assert state.get("llm_calm")
        # Старый незавершенный hold нельзя засчитать, пока источник неизвестен.
        at += timedelta(minutes=3) if kwargs.get("frozen") else timedelta(minutes=1)
        state, events = await tick(at, items, **kwargs)
        assert state["llm"] == 2 and not state.get("llm_calm")
        assert [e.recovery for e in events] == [False]
    at += timedelta(minutes=1)
    for minute in range(16):
        state, events = await tick(at + timedelta(minutes=minute), [_llm()])
    assert state["llm"] == 0 and [e.recovery for e in events] == [False, True]
    assert len([m for m in sent if m.kind == "llm"]) == 2


@pytest.mark.calm
async def test_llm_recovery_waits_for_every_announced_service_and_reason(env):
    tick, sent, _failures = env
    start = datetime.now(timezone.utc) + timedelta(days=1)
    state, _events = await tick(start, [_llm(ok=False, err="refused"), _llm(name="model-b", ok=False, err="timeout")])
    assert len(state["llm_who"]) == 2
    at = start + timedelta(minutes=1)
    for minute in range(17):
        state, events = await tick(at + timedelta(minutes=minute), [_llm()])
        assert state["llm"] == 2 and not state.get("llm_calm") and len(events) == 1
    at += timedelta(minutes=18)
    state, events = await tick(at, [_llm(waiting=20), _llm(name="model-b")])
    assert state["llm"] == 1 and not state.get("llm_calm") and len(events) == 1
    for minute in range(1, 17):
        state, events = await tick(at + timedelta(minutes=minute), [_llm(), _llm(name="model-b")])
    assert state["llm"] == 0 and len(events) == 2 and len(sent) == 2


@pytest.mark.calm
async def test_legacy_llm_alert_recovers_without_an_extra_hour(env):
    tick, _sent, _failures = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    state, events = await tick(at, [_llm()], state={
        "llm": 2, "llm_since": (at - timedelta(hours=2)).isoformat(),
        "llm_cool": (at - timedelta(minutes=10)).isoformat(),
    })
    assert state["llm"] == 2 and state.get("llm_calm") and not events and not state.get("llm_cool")
    for minute in range(1, 16):
        state, events = await tick(at + timedelta(minutes=minute), [_llm()])
    assert state["llm"] == 0 and [e.recovery for e in events] == [True]


@pytest.mark.calm
async def test_llm_known_service_recovers_with_unrelated_unobservable_services(env):
    tick, sent, _failures = env
    at = datetime.now(timezone.utc) + timedelta(days=1)
    unrelated = [_llm(engine="litellm", name="proxy", ok=False, err="http 401"),
                 _llm(engine="sglang", name="other-model", no_metrics=True)]
    state, events = await tick(at, [_llm(ok=False, err="refused"), *unrelated])
    assert state["llm_who"] == ["vllm|container|vllm-qwen|8000"]
    for minute in range(1, 17):
        state, events = await tick(at + timedelta(minutes=minute), [_llm(), *unrelated])
    assert state["llm"] == 0 and [e.recovery for e in events] == [False, True] and len(sent) == 2


def test_legacy_llm_recovery_needs_a_complete_healthy_selection():
    from app import gpu

    assert gpu.llm_recovery_confirmed({"llm": [_llm()]})
    for unknown in ([], [_llm(), {}], [_llm(), _llm(name="b", ok=False, err="http 401")],
                    [_llm(), _llm(name="b", no_metrics=True)]):
        assert not gpu.llm_recovery_confirmed({"llm": unknown})
