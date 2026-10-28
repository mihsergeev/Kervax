"""Выкатка: новый под не встает и падает, пока его не починят следующей выкаткой. Перезапуски пода в
первые полчаса его жизни (а пока рядом работает прошлая ревизия - час) не алертят, и отбоя у них
нет. Под, который падает и после выдержки, - зависшая выкатка, алерт как раньше."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, podnames, restarts
from app.models import Server

NOW = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
CRASH = {"c": "app", "r": "Error", "x": 2, "log": "redis.exceptions.AuthenticationError: AUTH called without password"}


def _pod(name, restarts_=0, up=True, last=None):
    p = {"ns": "default", "name": name, "owner": "ReplicaSet", "ctrl": "deployment/app-celery", "node": "k8s-a",
         "phase": "Running", "ready": up, "restarts": restarts_}
    if not up:
        p["reason"] = "CrashLoopBackOff"
    if last:
        p["last"] = last
    return p


def _srv(pods, rs):
    return Server(id=1, name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900,
                  last_report={"kube": {"access": True, "pods": pods}}, alert_state={"restarts": rs}, alert_mutes=[],
                  cpu_alert_percent=0, mem_alert_percent=0, disk_warn_percent=0, disk_alert_percent=0,
                  disk_crit_percent=0, temp_alert_c=0, conntrack_alert_percent=0, disk_temp_alert_c=0,
                  db_conn_alert_percent=0, web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def _young(minutes_old, crashes, seen=True):
    """Состояние пода, которому minutes_old минут, с перезапусками в эти минуты его жизни."""
    born = NOW - timedelta(minutes=minutes_old)
    cs = {"rc": len(crashes), "ev": [[(born + timedelta(minutes=m)).isoformat(), 1] for m in crashes],
          "last": CRASH}
    if seen:
        cs["seen"] = born.isoformat()
    return cs


def test_revision_from_pod_name():
    assert podnames.revision("mail-worker-6b8b56c769-f4k8m") == "6b8b56c769"
    assert podnames.revision("api-gateway-7bcf47bfdc-pc472") == "7bcf47bfdc"
    assert podnames.revision("postgres-0") == "" and podnames.revision("node-exporter-x2k4q") == ""


def test_panel_remembers_when_pod_appeared():
    st = restarts.update(None, {"kube": {"pods": [_pod("app-celery-6b8b56c769-f4k8m")]}}, NOW)
    assert st["pod:default/app-celery-6b8b56c769-f4k8m"]["seen"] == NOW.isoformat()
    later = NOW + timedelta(hours=2)
    st = restarts.update(st, {"kube": {"pods": [_pod("app-celery-6b8b56c769-f4k8m", 3)]}}, later)
    assert st["pod:default/app-celery-6b8b56c769-f4k8m"]["seen"] == NOW.isoformat()
    # под, который панель видела до этой версии, - старый, отметки у него нет и не появится
    old = {"pod:default/app-celery-6b8b56c769-f4k8m": {"rc": 3, "ev": []}}
    assert "seen" not in restarts.update(old, {"kube": {"pods": [_pod("app-celery-6b8b56c769-f4k8m", 4)]}},
                                         later)["pod:default/app-celery-6b8b56c769-f4k8m"]
    # пересоздан под тем же именем (StatefulSet): счетчик с нуля - новый под
    assert restarts.step({"rc": 7, "ev": []}, 0, later, pod=True)["seen"] == later.isoformat()
    # у контейнеров Docker отметки нет
    assert "seen" not in restarts.step(None, 0, later)


def test_new_pod_crashing_in_first_half_hour_does_not_alert():
    pods = [_pod("app-celery-778dbcd6cd-wdqnz", 8, up=False, last=CRASH)]
    rs = {"pod:default/app-celery-778dbcd6cd-wdqnz": _young(20, [1, 2, 3, 5, 8, 13, 18, 19])}
    assert restarts.problems(_srv(pods, rs), NOW) == []
    kv = restarts.kube_view(_srv(pods, rs), NOW)["pods"][0]
    assert kv["n"] == 8 and not kv["alert"] and not kv["rollout"]
    assert kv["wait"] == (NOW + timedelta(minutes=10)).isoformat()
    # тот же под, но панель его видела раньше (старый стабильный): алерт как раньше
    rs_old = {"pod:default/app-celery-778dbcd6cd-wdqnz": _young(20, [1, 2, 3, 5, 8, 13, 18, 19], seen=False)}
    assert [x["key"] for x in restarts.problems(_srv(pods, rs_old), NOW)] == ["pod:default/app-celery"]


def test_rollout_with_previous_revision_alive_waits_an_hour():
    pods = [_pod("app-celery-5bc87d768b-c8h2s"),  # прошлая ревизия работает и готова
            _pod("app-celery-778dbcd6cd-wdqnz", 10, up=False, last=CRASH)]
    crashes = [1, 2, 3, 5, 8, 13, 18, 23, 28, 33, 38, 43, 48, 53]
    rs = {"pod:default/app-celery-5bc87d768b-c8h2s": {"rc": 0, "ev": []},
          "pod:default/app-celery-778dbcd6cd-wdqnz": _young(55, crashes)}
    assert restarts.problems(_srv(pods, rs), NOW) == []
    kv = {p["name"]: p for p in restarts.kube_view(_srv(pods, rs), NOW)["pods"]}
    assert kv["app-celery-778dbcd6cd-wdqnz"]["rollout"]
    assert kv["app-celery-778dbcd6cd-wdqnz"]["wait"] == (NOW + timedelta(minutes=5)).isoformat()
    # прошлую ревизию убрали, а новая падает: сервиса нет, ждем только полчаса
    rs2 = {"pod:default/app-celery-778dbcd6cd-wdqnz": _young(55, crashes)}
    assert [x["key"] for x in restarts.problems(_srv(pods[1:], rs2), NOW)] == ["pod:default/app-celery"]


def test_rollout_stuck_past_the_wait_alerts_with_all_restarts():
    pods = [_pod("app-celery-5bc87d768b-c8h2s"), _pod("app-celery-778dbcd6cd-wdqnz", 15, up=False, last=CRASH)]
    rs = {"pod:default/app-celery-778dbcd6cd-wdqnz": _young(64, [1, 2, 3, 5, 8, 13, 18, 23, 28, 33, 38, 43, 48, 53,
                                                                 58, 63])}
    pr = restarts.problems(_srv(pods, rs), NOW)
    assert len(pr) == 1 and "перезапускался 16 раз за сутки" in pr[0]["text"]


def test_crashes_at_start_then_calm_never_count():
    """Под упал пять раз на старте (ждал базу) и работает: ни сейчас, ни через час одиночный
    перезапуск не тянет за собой те пять."""
    pods = [_pod("app-celery-778dbcd6cd-wdqnz", 6, last=CRASH)]
    rs = {"pod:default/app-celery-778dbcd6cd-wdqnz": _young(180, [1, 2, 3, 5, 8, 170])}
    assert restarts.problems(_srv(pods, rs), NOW) == []
    assert not restarts.kube_view(_srv(pods, rs), NOW)["pods"][0]["wait"]


def test_pod_not_running_waits_while_previous_revision_serves():
    """"Под не работает" (kube_pod) о новой ревизии, пока рядом работает прошлая, тоже ждет час:
    ImagePullBackOff и CrashLoopBackOff на выкатке чинят следующей выкаткой."""
    new = _pod("app-celery-778dbcd6cd-wdqnz", 3, up=False, last=CRASH)
    old = _pod("app-celery-5bc87d768b-c8h2s")
    st = {"kube_pod_since": (NOW - timedelta(minutes=40)).isoformat(),
          "restarts": {"pod:default/app-celery-778dbcd6cd-wdqnz": _young(40, [1, 2, 3]),
                       "pod:default/app-celery-5bc87d768b-c8h2s": {"rc": 0, "ev": []}}}
    s = _srv([old, new], st["restarts"])
    s.alert_state = st
    assert collector._server_conditions(s, NOW)["kube_pod"][0] == 0
    assert not [p for p in collector.server_problems(s, NOW) if p["kind"] == "kube_pod"]
    # первый отчет с новым подом: панель его еще не запомнила - тоже новый
    s.alert_state = {"restarts": {"pod:default/app-celery-5bc87d768b-c8h2s": {"rc": 0, "ev": []}}}
    assert collector._server_conditions(s, NOW)["kube_pod"][1]["n"] == 0
    # больше часа - зависшая выкатка: снова "под не работает" (с обычной выдержкой)
    s.alert_state = {**st, "restarts": {**st["restarts"],
                                        "pod:default/app-celery-778dbcd6cd-wdqnz": _young(61, [1, 2, 3])}}
    assert collector._server_conditions(s, NOW)["kube_pod"][1]["n"] == 1
    # без прошлой ревизии новая ждет обычной выдержки
    s.alert_state = st
    s.last_report = {"kube": {"access": True, "pods": [new]}}
    assert collector._server_conditions(s, NOW)["kube_pod"][1]["n"] == 1


async def _run_rollout(tmp_path, monkeypatch, fixed_at: int | None, keep_old: bool, minutes: int) -> list[str]:
    """Поминутно: в 0 мин выкатили ревизию, которая падает в CrashLoopBackOff (перезапуски в 1, 2,
    3, 5, 8 мин, дальше раз в 5 минут), в fixed_at ее починили следующей выкаткой. keep_old - прошлая
    ревизия работает рядом, пока новая не встала. Что ушло в чат."""
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / f'ro-{fixed_at}-{keep_old}.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    t0 = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    old, bad, good = "app-celery-5bc87d768b-c8h2s", "app-celery-778dbcd6cd-wdqnz", "app-celery-86bc658754-rnfd8"
    async with factory() as s:
        s.add(Server(name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=t0,
                     cpu_alert_percent=0, mem_alert_percent=0, alert_sustain_seconds=900,
                     last_report={"uptime_seconds": 100000, "kube": {"access": True, "pods": [_pod(old)]}},
                     alert_state={"restarts": {f"pod:default/{old}": {"rc": 0, "ev": []}}}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")
    crash_at = [1, 2, 3, 5, 8] + list(range(13, 24 * 60, 5))
    for m in range(minutes):
        t = t0 + timedelta(minutes=m)
        if fixed_at is not None and m >= fixed_at:
            pods = [_pod(good)]
        else:
            rc = sum(1 for c in crash_at if c <= m)
            pods = ([_pod(old)] if keep_old else []) + [_pod(bad, rc, up=False, last=CRASH if rc else None)]
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = t
            srv.last_report = {"uptime_seconds": 100000 + m * 60, "kube": {"access": True, "pods": pods}}
            await s.commit()
        await collector.evaluate_servers(factory, cfg, t)
    await engine.dispose()
    return [m for m in sent if "k8s-a" in m]


async def test_broken_rollout_fixed_by_next_one_is_silent(tmp_path, monkeypatch):
    """Как было 28.10: ревизия падала, через 40 минут ее починили - ни алерта, ни отбоя."""
    assert await _run_rollout(tmp_path, monkeypatch, fixed_at=40, keep_old=True, minutes=150) == []


async def test_stuck_rollout_alerts_after_the_wait(tmp_path, monkeypatch):
    """Выкатка висит больше часа: перезапуски - алертом, "под не поднимается" - еще через обычную
    выдержку."""
    msgs = await _run_rollout(tmp_path, monkeypatch, fixed_at=None, keep_old=True, minutes=70)
    assert [m.split("\n")[1] for m in msgs] == ["<b>Под перезапускается</b>"], msgs


async def test_crashing_pod_without_old_revision_alerts_after_half_an_hour(tmp_path, monkeypatch):
    """Прошлой ревизии рядом нет - сервис лежит: "под не поднимается" через обычную выдержку, а
    перезапуски - когда под падает и после получаса."""
    msgs = await _run_rollout(tmp_path, monkeypatch, fixed_at=None, keep_old=False, minutes=40)
    assert [m.split("\n")[1] for m in msgs] == ["<b>Не поднимается под</b>", "<b>Под перезапускается</b>"], msgs
    msgs = await _run_rollout(tmp_path, monkeypatch, fixed_at=25, keep_old=False, minutes=120)
    assert not [m for m in msgs if "перезапуск" in m], msgs
