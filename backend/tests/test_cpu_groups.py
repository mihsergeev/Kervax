"""Кто ест CPU (агент 2.21): группы процессов с владельцем, алерт "процессы в пустом цикле",
причина CPU-алерта и с какого момента видна каждая проблема ("Что сломано": "2 дня").

Повод - k8s-a-prc: 47 зависших chrome одного пода крутили по ядру 2-186 дней, 45 ядер из 96,
а CPU-алерт молчал, потому что общий CPU до порога не доходил."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector
from app.models import Server

UID8 = "56279ecf"
POD = "ads-com/ads-retargeting-monitor-c56fcfcd7-cgrtn"
CHROME = {"comm": "chrome", "kind": "pod", "owner": f"pod {UID8}", "n": 47, "cpu": 4510.4,
          "spin": 45, "spin_age": 186 * 86400}
CONTAINERD = {"comm": "containerd", "kind": "unit", "owner": "k0sworker.service", "n": 1, "cpu": 127.1,
              "spin": 1, "spin_age": 84 * 86400}


def test_texts():
    assert collector._cores(4510.4) == "45 ядер"
    assert collector._cores(2100) == "21 ядро"
    assert collector._cores(250) == "2.5 ядра"
    names = {UID8: POD}
    assert collector.spin_text([CHROME], names) == (
        f"45 процессов chrome (под {POD}) крутят по ядру вхолостую: 45 ядер, старшему 186 дн")
    # имени нет (контроллер кластера не в этой панели) - хотя бы uid
    assert f"(под {UID8})" in collector.spin_text([CHROME])
    assert collector.groups_text([CHROME, CONTAINERD], names) == (
        f"chrome x47 (под {POD}) 45 ядер, containerd (юнит k0sworker.service) 1.3 ядра")


def _srv(groups, **kw):
    now = datetime.now(timezone.utc)
    rep = {"cpu_percent": 55, "clock_unix": int(now.timestamp())}
    if groups is not None:
        rep["cpu_groups"] = groups
    return Server(name="k8s-a-prc", token_hash="x", enabled=True, last_seen=now,
                  last_report=rep, offline_after_seconds=120, cpu_alert_percent=90, **kw)


def test_condition_and_problem():
    now = datetime.now(timezone.utc)
    lvl, ctx = collector._server_conditions(_srv([CHROME, CONTAINERD]), now, None, {UID8: POD})["cpu_spin"]
    assert lvl == 1 and ctx["detail"].startswith("45 процессов chrome (под ads-com/")
    # одиночный занятой демон - не повод: нужно хотя бы три одинаковых процесса
    assert collector._server_conditions(_srv([CONTAINERD]), now)["cpu_spin"][0] == 0
    # агент старше 2.21 групп не шлет - и ключа нет
    assert "cpu_spin" not in collector._server_conditions(_srv(None), now)

    probs = [p for p in collector.server_problems(_srv([CHROME]), now, {UID8: POD}) if p["kind"] == "cpu_spin"]
    assert len(probs) == 1 and probs[0]["level"] == 1 and probs[0]["sec"] == "cpueat"
    # возраст в строке "Что сломано" не повторяем: там длительность и так стоит в конце
    assert probs[0]["text"] == f"45 процессов chrome (под {POD}) крутят по ядру вхолостую: 45 ядер"
    # начало - по возрасту старшего процесса: крутится почти всю жизнь
    since = datetime.fromisoformat(probs[0]["since"])
    assert abs((now - since).total_seconds() - 186 * 86400) < 5


def test_cause_text_names_groups():
    rep = {"cpu_groups": [CHROME, CONTAINERD], "top_cpu": [{"comm": "chrome", "cpu": 100}] * 8}
    txt = collector.cause_text(rep, "cpu", {}, None, {UID8: POD})
    # из топа вышло бы "chrome 800%", а это 45 ядер одного пода
    assert f"больше всего CPU у chrome x47 (под {POD}) 45 ядер" in txt
    # без групп (агент старше) - как раньше, по топу процессов
    assert "chrome 800%" in collector.cause_text({"top_cpu": rep["top_cpu"]}, "cpu", {})


def test_since_maps_and_pod_names():
    s = _srv([CHROME], alert_state={
        "disk_health_from": "2026-10-04T10:00:00+00:00",
        "cpu_from": "2026-10-05T10:05:00+00:00", "cpu_since": "2026-10-05T10:00:00+00:00",
        "units_from": None, "flap_since": "2026-10-01T00:00:00+00:00",
        # бэкап настроили, условие больше не считается, а ключ остался с июля
        "backup_missing_since": "2026-07-27T16:58:18+00:00",
        "docker": {"web": {"down_since": "2026-10-05T09:00:00+00:00"}, "ok": {"down_since": None}},
    })
    since = collector.alert_since(s)
    # у порога CPU - начало превышения, а не алерта; погасшее и служебное - не отдаем
    assert since == {"disk_health": "2026-10-04T10:00:00+00:00", "cpu": "2026-10-05T10:00:00+00:00"}
    assert collector.docker_since(s) == {"web": "2026-10-05T09:00:00+00:00"}
    ctl = Server(name="node-35", token_hash="x", last_report={"kube": {"pods": [
        {"ns": "ads-com", "name": "ads-retargeting-monitor-c56fcfcd7-cgrtn", "u": UID8},
        {"ns": "default", "name": "other", "u": "aaaaaaaa"}]}})
    names = collector.pod_uid_names([ctl, s])
    assert names[UID8] == POD
    # серверу отдаем только имена его подов
    assert collector.needed_pod_names(s, names) == {UID8: POD}


async def test_cpu_spin_alert_lifecycle(tmp_path, monkeypatch):
    """Крутятся -> одно сообщение; снова то же -> молчим; перестали -> отбой. Начало проблемы
    (cpu_spin_from) ставится при первом срабатывании и снимается при отбое. Имя пода берется
    из отчета контроллера кластера."""
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'c.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    ctl_rep = {"kube": {"present": True, "access": True, "pods": [
        {"ns": "ads-com", "name": "ads-retargeting-monitor-c56fcfcd7-cgrtn", "u": UID8}]}}
    async with factory() as s:
        s.add(Server(name="k8s-a-prc", token_hash="x", enabled=True, backup_not_required=True,
                     last_seen=now, cpu_alert_percent=0, mem_alert_percent=0, last_report={}))
        s.add(Server(name="node-35", token_hash="y", enabled=True, backup_not_required=True,
                     last_seen=now, cpu_alert_percent=0, mem_alert_percent=0, last_report=ctl_rep))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(groups):
        nonlocal now
        now = now + timedelta(minutes=2)
        async with factory() as s:
            for srv in (await s.execute(select(Server))).scalars():
                srv.last_seen = now
                if srv.name == "k8s-a-prc":
                    srv.last_report = {"clock_unix": int(now.timestamp()), "uptime_seconds": 10**7,
                                       "cpu_percent": 55, "cpu_groups": groups}
                else:
                    srv.last_report = dict(ctl_rep, clock_unix=int(now.timestamp()), uptime_seconds=10**7)
            await s.commit()
        await collector.evaluate_servers(factory, cfg, now)
        async with factory() as s:
            prc = (await s.execute(select(Server).where(Server.name == "k8s-a-prc"))).scalars().one()
            return prc.alert_state or {}

    st = await tick([CHROME, CONTAINERD])
    spin = [m for m in sent if "пустом цикле" in m or "крутят по ядру" in m]
    assert len(spin) == 1 and "🌀" in spin[0] and f"(под {POD})" in spin[0], sent
    assert st.get("cpu_spin_from")
    st = await tick([CHROME, CONTAINERD])
    assert len([m for m in sent if "крутят по ядру" in m]) == 1
    st = await tick([CONTAINERD])
    assert any(m.startswith("✅") and "крутивших CPU вхолостую" in m for m in sent), sent
    assert not st.get("cpu_spin_from")
    await engine.dispose()
