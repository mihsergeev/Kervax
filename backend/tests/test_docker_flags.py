"""Отметки "упал" и "в цикле" у контейнеров (alerted_down/alerted_loop) снимаются, даже если
алерт заглушили посреди аварии: по ним главная показывает "Что сломано", и заглушенный
контейнер иначе висел бы там вечно."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select


async def test_flags_clear_when_muted(tmp_path, monkeypatch):
    from app import collector
    from app.config import Settings
    from app.db import Base, create_engine_and_factory
    from app.models import Server

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'd.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, alert_sustain_seconds=0, last_report={}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(rc, state="running", mutes=None, step=timedelta(minutes=1)):
        nonlocal now
        now = now + step
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.last_report = {"clock_unix": int(now.timestamp()), "uptime_seconds": 10**6, "docker": {
                "present": True, "access": True,
                "containers": [{"name": "w", "state": state, "restarts": rc, "policy": "unless-stopped"}]}}
            if mutes is not None:
                srv.alert_mutes = mutes
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            return ((srv.alert_state or {}).get("docker") or {}).get("w") or {}

    for rc in range(5):  # счетчик растет каждую минуту - цикл
        st = await tick(rc)
    assert st.get("alerted_loop") and any("w" in m for m in sent)
    n = len(sent)
    # заглушили посреди аварии, перезапуски прекратились и вышли из окна
    st = await tick(4, mutes=["docker_loop"], step=timedelta(minutes=20))
    assert not st.get("alerted_loop"), "отметка цикла должна сняться и у заглушенного"
    assert len(sent) == n, "отбой по заглушенному алерту слать нельзя"

    # то же с "упал": алерт ушел, заглушили, контейнер поднялся
    st = await tick(4, state="exited", mutes=[])
    st = await tick(4, state="exited")
    assert st.get("alerted_down")
    n = len(sent)
    st = await tick(4, state="running", mutes=["docker_down"])
    assert not st.get("alerted_down") and len(sent) == n
    await engine.dispose()
