"""Остановленный контейнер с restart-policy - авария, только если докер не смог его запустить
(State.Error, агент 2.22) или кончились перезапуски on-failure. docker stop restart-policy не
снимает, и до 1.4.98 панель поднимала тревогу на воркеры, которые разработчик погасил руками."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.collector import docker_down_why, docker_exit_code


def _c(**kw):
    base = {"name": "w", "state": "exited", "policy": "unless-stopped", "restarts": 0}
    return {**base, **kw}


def test_stopped_by_hand_is_not_an_accident():
    # агент 2.22: ошибки нет, код 0 - так выглядит docker compose stop
    assert docker_down_why(_c(exit=0, status="Exited (0) 17 minutes ago")) is None
    assert docker_down_why(_c(policy="always", exit=137)) is None
    # без restart-policy контейнер никто не обязан держать запущенным
    assert docker_down_why(_c(policy="no", exit=1, err="boom")) is None
    assert docker_down_why(_c(state="running")) is None


def test_start_failure_is_an_accident():
    why = docker_down_why(_c(exit=128, err="driver failed programming external connectivity: "
                                           "Bind for 0.0.0.0:80 failed: port is already allocated"))
    assert why and "port is already allocated" in why
    # compose up не смог запустить: контейнер так и остался created, причина в State.Error
    assert docker_down_why(_c(state="created", exit=128, err="no such file or directory")) is not None
    assert docker_down_why(_c(state="created", exit=0)) is None
    assert docker_down_why(_c(state="dead")) is not None


def test_on_failure():
    # отработал и вышел сам
    assert docker_down_why(_c(policy="on-failure", exit=0, max_retry=3)) is None
    # перезапуски кончились - авария, с причиной
    why = docker_down_why(_c(policy="on-failure", exit=1, max_retry=3, restarts=3, oom=True))
    assert why and "3 из 3" in why and "OOM" in why
    # перезапуски не кончились, а он лежит - значит остановили руками
    assert docker_down_why(_c(policy="on-failure", exit=1, max_retry=3, restarts=1)) is None


def test_old_agent_falls_back_to_exit_code():
    # агент до 2.22 не знает State.Error: судим по коду из строки статуса
    assert docker_down_why(_c(status="Exited (0) 17 minutes ago")) is None
    assert docker_down_why(_c(status="Exited (143) 2 hours ago")) is None
    assert docker_down_why(_c(status="Exited (128) 5 minutes ago")) == "остановился с кодом 128"
    assert docker_exit_code({"status": "Exited (255) 4 years ago"}) == 255
    assert docker_exit_code({"status": "Up 3 hours"}) is None
    assert docker_exit_code({"exit": 0, "status": "Exited (1) now"}) == 0


async def test_lifecycle(tmp_path, monkeypatch):
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

    async def tick(containers, state=None):
        nonlocal now
        now = now + timedelta(minutes=1)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = now
            srv.last_report = {"clock_unix": int(now.timestamp()), "uptime_seconds": 10**6, "docker": {
                "present": True, "access": True, "containers": containers}}
            if state is not None:
                srv.alert_state = state
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            return (srv.alert_state or {}).get("docker") or {}

    stopped = _c(name="workers", exit=0, status="Exited (0) 1 minute ago")
    failed = _c(name="web", exit=128, err="Bind for 0.0.0.0:80 failed: port is already allocated")
    for _ in range(3):
        st = await tick([stopped, failed])
    # погашенный руками: ни тревоги, ни отметки "лежит с"
    assert not st["workers"].get("alerted_down") and not st["workers"].get("down_since")
    assert not any("workers" in m for m in sent)
    # не запустился: тревога с причиной от докера
    assert st["web"].get("alerted_down")
    assert any("web" in m and "port is already allocated" in m for m in sent)

    # тревога, поднятая старой логикой, снимается без слов "снова работает"
    n = len(sent)
    st = await tick([stopped], state={"docker": {"workers": {"alerted_down": True, "down_since": now.isoformat()}}})
    assert not st["workers"].get("alerted_down")
    assert len(sent) == n + 1 and "остановлен штатно" in sent[-1] and "снова работает" not in sent[-1]
    await engine.dispose()
