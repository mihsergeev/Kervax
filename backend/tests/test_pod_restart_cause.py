"""Перезапуски подов: в алерте причина прошлого завершения (агент 2.43, поле last), порог - по ней,
а пачки (поды разных ворклоадов разом) - одной строкой про ноду. Алерт ведет в раздел
"Перезапуски" кластера."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, restarts
from app.models import Server

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _pod(name, ctrl, last=None, node="k8s-a"):
    p = {"ns": "default", "name": name, "owner": "ReplicaSet", "ctrl": f"deployment/{ctrl}", "phase": "Running",
         "ready": True, "restarts": 9, "node": node}
    if last:
        p["last"] = last
    return p


def _ev(*minutes_ago):
    return {"rc": 9, "ev": [[(NOW - timedelta(minutes=m)).isoformat(), 1] for m in minutes_ago]}


def _srv(pods, rs, mutes=None):
    return Server(id=1, name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report={"kube": {"pods": pods}},
                  alert_state={"restarts": rs}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_cause_text():
    assert restarts.cause(None) == ("", "")
    assert restarts.cause({"c": "app", "r": "OOMKilled", "x": 137, "lim": "128Mi"}) == (
        "oom", "OOMKilled при лимите 128Mi")
    assert restarts.cause({"c": "app", "r": "OOMKilled", "x": 137})[1] == "OOMKilled, лимита нет: кончилась память ноды"
    assert restarts.cause({"c": "app", "r": "Error", "x": 143, "probe": "liveness",
                           "pmsg": 'Get "http://10.0.0.5:8081/healthz": context deadline exceeded'}) == (
        "probe", 'убит по liveness-пробе (Get "http://10.0.0.5:8081/healthz": context deadline exceeded)')
    assert restarts.cause({"c": "app", "r": "Completed", "x": 0}) == ("exit0", "сам вышел с кодом 0")
    # с нулем выходят и после потери lease: это не плановый выход
    assert restarts.cause({"c": "app", "r": "Completed", "x": 0, "log": 'error unable to start provider {"error": "leader election lost"}'}) == (
        "crash", 'exit 0: error unable to start provider {"error": "leader election lost"}')
    assert restarts.cause({"c": "app", "r": "Error", "x": 1, "log": 'ERROR setup problem running manager {"error": "leader election lost"}'}) == (
        "crash", 'exit 1: ERROR setup problem running manager {"error": "leader election lost"}')
    assert restarts.cause({"c": "app", "r": "Error", "x": 137}) == ("crash", "exit 137 (SIGKILL)")
    assert restarts.cause({"c": "app", "r": "ContainerCannotRun", "x": 128}) == ("crash", "exit 128 (ContainerCannotRun)")
    # message контейнера (terminationMessagePath) точнее строки лога
    assert restarts.cause({"c": "app", "r": "Error", "x": 2, "msg": "config not found", "log": "bye"})[1] == "exit 2: config not found"


def test_threshold_follows_the_cause():
    pods = [_pod("app-a-6b8b56c769-f4k8m", "app-a", {"c": "app", "r": "OOMKilled", "x": 137, "lim": "128Mi"}),
            _pod("app-b-5d88689b5d-rg5d7", "app-b", {"c": "app", "r": "Completed", "x": 0}),
            _pod("app-c-649ccd4dfd-zz4k8", "app-c", {"c": "app", "r": "Error", "x": 1, "log": "panic: nil map"}),
            _pod("app-d-795d8cc8c-fttx8", "app-d", {"c": "app", "r": "Error", "x": 1})]
    rs = {"pod:default/app-a-6b8b56c769-f4k8m": _ev(10, 200, 400),            # OOM трижды - уже повод
          "pod:default/app-b-5d88689b5d-rg5d7": _ev(*range(30, 600, 60)),   # 10 раз, но выходит с кодом 0
          "pod:default/app-c-649ccd4dfd-zz4k8": _ev(15, 75, 135, 195, 255),
          "pod:default/app-d-795d8cc8c-fttx8": _ev(20, 80, 140, 200)}       # падает, но реже пяти
    pr = restarts.problems(_srv(pods, rs), NOW)
    assert [(x["key"], x["text"]) for x in pr] == [
        ("pod:default/app-a", "под default/app-a-6b8b56c769-f4k8m перезапускался 3 раза за сутки, последний 10 мин назад: "
                              "OOMKilled при лимите 128Mi"),
        ("pod:default/app-c", "под default/app-c-649ccd4dfd-zz4k8 перезапускался 5 раз за сутки, последний 15 мин назад: "
                              "exit 1: panic: nil map")]
    # в разделе видны все четыре: с причиной и пометкой, повод ли это для алерта
    kv = restarts.kube_view(_srv(pods, rs), NOW)
    assert [(p["name"], p["n"], p["kind"], p["alert"]) for p in kv["pods"]] == [
        ("app-c-649ccd4dfd-zz4k8", 5, "crash", True), ("app-a-6b8b56c769-f4k8m", 3, "oom", True),
        ("app-b-5d88689b5d-rg5d7", 10, "exit0", False), ("app-d-795d8cc8c-fttx8", 4, "crash", False)]
    assert kv["pods"][1]["last"]["lim"] == "128Mi" and kv["bursts"] == []
    assert kv["pods"][1]["why"] == "OOMKilled при лимите 128Mi" and kv["pods"][1]["mute"] == "restarts:pod:default/app-a"
    assert len(kv["pods"][2]["times"]) == 10
    # мьют деплоймента виден в разделе
    kvm = restarts.kube_view(_srv(pods, rs, mutes=["restarts:pod:default/app-c"]), NOW)
    assert [p["muted"] for p in kvm["pods"]][:2] == [True, False]


def _stall_case(n_bursts):
    """Три контроллера на одной ноде перезапускаются в одни и те же минуты, плюс у одного свои"""
    pods = [_pod("app-a-6b8b56c769-f4k8m", "app-a", {"c": "manager", "r": "Error", "x": 1, "log": "leader election lost"}),
            _pod("app-b-5d88689b5d-rg5d7", "app-b", {"c": "frr", "r": "Error", "x": 143, "probe": "liveness"}),
            _pod("app-c-649ccd4dfd-zz4k8", "app-c")]
    stall = [30 + 120 * i for i in range(n_bursts)]
    rs = {"pod:default/app-a-6b8b56c769-f4k8m": _ev(*stall, 1000, 1100),
          "pod:default/app-b-5d88689b5d-rg5d7": _ev(*stall),
          "pod:default/app-c-649ccd4dfd-zz4k8": _ev(*stall)}
    return pods, rs


def test_bursts_are_one_line_about_the_node():
    pods, rs = _stall_case(3)
    pr = restarts.problems(_srv(pods, rs), NOW)
    # каждый под набрал бы 3-5 перезапусков, но они в пачках: о подах ни строки, одна - про ноду
    assert [x["key"] for x in pr] == ["bursts"]
    assert pr[0]["text"] == (
        "поды перезапускаются пачками: 3 раза за сутки по 3 разом, последний 30 мин назад (app-a, app-b, app-c) - "
        "подвисает нода k8s-a: пробы и запросы к kube-api не дождались ответа, смотрите, что грузит ноду")
    kv = restarts.kube_view(_srv(pods, rs), NOW)
    assert len(kv["bursts"]) == 3 and kv["bursts"][-1]["pods"] == [
        "default/app-a-6b8b56c769-f4k8m", "default/app-b-5d88689b5d-rg5d7", "default/app-c-649ccd4dfd-zz4k8"]
    a = kv["pods"][0]
    assert (a["name"], a["n"], a["solo"], a["alert"]) == ("app-a-6b8b56c769-f4k8m", 5, 2, False)
    # две пачки за сутки (перезагрузка, разовый затык) - не повод
    pods2, rs2 = _stall_case(2)
    assert restarts.problems(_srv(pods2, rs2), NOW) == []
    # заглушить строку про пачки
    assert restarts.problems(_srv(pods, rs, mutes=["restarts:bursts"]), NOW) == []
    # поды на разных нодах - под подозрением kube-api
    pods3 = [dict(p, node=f"k8s-{i}") for i, p in enumerate(pods)]
    assert "подвисает kube-api или etcd" in restarts.problems(_srv(pods3, rs), NOW)[0]["text"]
    # строка в "Что сломано" ведет в раздел "Перезапуски" и глушится по ключу bursts
    rows = [(p["level"], p.get("sec"), p["mute"]) for p in collector.server_problems(_srv(pods, rs), NOW)
            if p["kind"] == "kube_restarts"]
    assert rows == [(1, "restarts", "restarts:bursts")]


def test_workers_of_one_app_exiting_together_are_not_a_burst():
    """Воркеры одного приложения работают по 20 минут и выходят сами с кодом 0 в одну и ту же
    минуту: 57 "пачек" за сутки на ноде, где давления нет. Это не подвисание и не беда вовсе."""
    names = [("app-a-6f678d8d6d-jxrpd", "app-a"), ("app-b-649b7b9b57-z2gcf", "app-b"),
             ("app-c-74fc666fc6-b6rn5", "app-c"), ("app-d-6c4b7b84cc-8lswh", "app-d")]
    pods = [_pod(n, c, {"c": "app", "r": "Completed", "x": 0}) for n, c in names]
    rs = {f"pod:default/{n}": _ev(*range(5, 1400, 20)) for n, _c in names}
    s = _srv(pods, rs)
    assert restarts.problems(s, NOW) == []
    kv = restarts.kube_view(s, NOW)
    assert kv["bursts"] == [] and {p["kind"] for p in kv["pods"]} == {"exit0"}
    # те же поды заглушены руками - тоже не пачка, даже если причина неизвестна (старый агент)
    pods_old = [_pod(n, c) for n, c in names]
    muted = [f"restarts:pod:default/{c}" for _n, c in names]
    assert restarts.problems(_srv(pods_old, rs, mutes=muted), NOW) == []


def test_apps_failing_together_without_node_stall_count_per_pod():
    """Три деплоймента падают в одни минуты с ошибкой приложения (общая база), а нода в порядке:
    не "подвисает нода", а каждый под своей строкой со своей причиной."""
    pods = [_pod(f"app-{x}-6b8b56c769-f4k8m", f"app-{x}", {"c": "app", "r": "Error", "x": 1,
                                                          "log": "SQLSTATE[08006] connection to db refused"})
            for x in "abc"]
    rs = {f"pod:default/app-{x}-6b8b56c769-f4k8m": _ev(10, 130, 250, 370, 490) for x in "abc"}
    rs[restarts.PSI_KEY] = [[(NOW - timedelta(minutes=m)).isoformat(), {"c": 3, "ms": 0, "mf": 0, "is": 1, "if": 0}]
                            for m in (10, 130, 250, 370, 490)]
    pr = restarts.problems(_srv(pods, rs), NOW)
    assert [x["key"] for x in pr] == ["pod:default/app-a", "pod:default/app-b", "pod:default/app-c"]
    assert pr[0]["text"].endswith(": exit 1: SQLSTATE[08006] connection to db refused")
    assert restarts.kube_view(_srv(pods, rs), NOW)["bursts"] == []


def test_node_stall_confirmed_by_pressure():
    """Причина у подов неизвестна (агент старше 2.43), но в минуты пачек на ноде было давление"""
    pods = [_pod(f"app-{x}-6b8b56c769-f4k8m", f"app-{x}") for x in "abc"]
    rs = {f"pod:default/app-{x}-6b8b56c769-f4k8m": _ev(10, 130, 250) for x in "abc"}
    calm = {"c": 2, "ms": 0, "mf": 0, "is": 0, "if": 0}
    rs[restarts.PSI_KEY] = [[(NOW - timedelta(minutes=m)).isoformat(), calm] for m in (10, 130, 250)]
    assert restarts.problems(_srv(pods, rs), NOW) == []  # давления нет - и пачки нет, и по подам мало
    rs[restarts.PSI_KEY][1][1] = {"c": 40, "ms": 12, "mf": 7.6, "is": 13, "if": 5.8}
    rs[restarts.PSI_KEY][2][1] = {"c": 30, "ms": 2, "mf": 0, "is": 1, "if": 0}
    rs[restarts.PSI_KEY][0][1] = {"c": 26, "ms": 0, "mf": 0, "is": 0, "if": 0}
    pr = restarts.problems(_srv(pods, rs), NOW)
    assert [x["key"] for x in pr] == ["bursts"]
    assert pr[0]["text"] == (
        "поды перезапускаются пачками: 3 раза за сутки по 3 разом, последний 10 мин назад (app-a, app-b, app-c) - "
        "подвисает нода k8s-a: в эти минуты процессы ждали CPU 40%, память 12%, диск 13% времени, "
        "смотрите, что грузит ноду")
    assert restarts.kube_view(_srv(pods, rs), NOW)["bursts"][-1]["psi"]["c"] == 26


def test_update_keeps_node_pressure_of_restart_minutes():
    rep = {"psi": {"cpu_some_60": 41.2, "mem_full_60": 7.64, "mem_some_60": 11.8, "io_some_60": 13.2, "io_full_60": 5.8},
           "kube": {"pods": [_pod("app-a-6b8b56c769-f4k8m", "app-a")]}}
    st = restarts.update(None, dict(rep, kube={"pods": [dict(rep["kube"]["pods"][0], restarts=3)]}), NOW)
    assert restarts.PSI_KEY not in st  # первая точка - не перезапуск
    t1 = NOW + timedelta(minutes=1)
    st = restarts.update(st, dict(rep, kube={"pods": [dict(rep["kube"]["pods"][0], restarts=3)]}), t1)
    assert restarts.PSI_KEY not in st  # перезапусков не было - давление не пишем
    t2 = NOW + timedelta(minutes=2)
    st = restarts.update(st, dict(rep, kube={"pods": [dict(rep["kube"]["pods"][0], restarts=4)]}), t2)
    assert st[restarts.PSI_KEY] == [[t2.isoformat(), {"c": 41.2, "ms": 11.8, "mf": 7.6, "is": 13.2, "if": 5.8}]]
    # служебный ключ не ломает подсчет для разделов
    assert restarts.view(_srv([], st), t2) == {"pod:default/app-a-6b8b56c769-f4k8m": 1}


async def test_alert_has_the_cause_and_leads_to_restarts(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'pr.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    pods = [_pod("app-a-6b8b56c769-f4k8m", "app-a", {"c": "app", "r": "OOMKilled", "x": 137, "lim": "256Mi"}),
            _pod("app-b-5d88689b5d-rg5d7", "app-b", {"c": "app", "r": "Error", "x": 1, "log": "fatal: db is gone"})]
    rs = {"pod:default/app-a-6b8b56c769-f4k8m": {"rc": 3, "ev": [[(now - timedelta(minutes=m)).isoformat(), 1]
                                                                for m in (2, 100, 200)]},
          "pod:default/app-b-5d88689b5d-rg5d7": {"rc": 5, "ev": [[(now - timedelta(minutes=m)).isoformat(), 1]
                                                                for m in (7, 60, 120, 180, 240)]}}
    async with factory() as s:
        s.add(Server(name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, alert_state={"restarts": rs},
                     last_report={"uptime_seconds": 100000, "kube": {"pods": pods}}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    await collector.evaluate_servers(factory, Settings(alert_webhook="http://hook", panel_url="https://kervax.test"), now)
    hits = [m for m in sent if m.startswith("🔄☸️")]
    assert len(hits) == 1, sent
    msg = hits[0]
    assert "ktab=restarts" in msg
    # каждый под своей строкой и с причиной
    assert ("под default/app-a-6b8b56c769-f4k8m перезапускался 3 раза за сутки, последний 2 мин назад: "
            "OOMKilled при лимите 256Mi\nпод default/app-b-5d88689b5d-rg5d7 перезапускался 5 раз за сутки, "
            "последний 7 мин назад: exit 1: fatal: db is gone") in msg
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        assert srv.alert_state["kube_restarts_sig"] == ["pod:default/app-a", "pod:default/app-b"]
    await engine.dispose()


def test_logs_prev_is_an_allowed_command():
    from app.schemas import KubeCommandIn
    c = KubeCommandIn(ns="default", kind="pod", name="app-a-6b8b56c769-f4k8m", action="logs_prev", tail=100)
    assert c.action == "logs_prev"


def test_planned_exit_is_remembered_through_a_rollout():
    """Воркеры по плану выходят с кодом 0 раз в 20 минут. На выкладке у удаляемого пода kubelet
    стирает прошлый контейнер, и причина (Last State) пропадает из отчета: панель видела "причина
    неизвестна" и давала алерт, а через минуту, когда старых подов уже нет, - отбой. Теперь панель
    помнит последнюю известную причину пода."""
    names = [("app-a-6f678d8d6d-jxrpd", "app-a"), ("app-b-649b7b9b57-z2gcf", "app-b"),
             ("app-c-74fc666fc6-b6rn5", "app-c")]
    done = {"c": "app", "r": "Completed", "x": 0, "t": 1, "log": "deadline reached, exiting"}
    st = None
    for i in range(31):  # тридцать плановых выходов каждого за 10 часов, все в одну минуту
        t = NOW - timedelta(minutes=20 * (30 - i) + 1)
        st = restarts.update(st, {"kube": {"pods": [dict(_pod(n, c, done if i else None), restarts=i)
                                                    for n, c in names]}}, t)
    # минута выкладки: поды еще в отчете, но прошлого запуска у них уже нет
    gone = [dict(_pod(n, c), restarts=30) for n, c in names]
    st = restarts.update(st, {"kube": {"pods": gone}}, NOW)
    assert st["pod:default/app-a-6f678d8d6d-jxrpd"]["last"]["r"] == "Completed"
    s = _srv(gone, st)
    assert restarts.problems(s, NOW) == []
    kv = restarts.kube_view(s, NOW)
    assert kv["bursts"] == [] and {(p["n"], p["kind"]) for p in kv["pods"]} == {(30, "exit0")}
    assert kv["pods"][0]["last"]["log"] == "deadline reached, exiting"
    # новая причина из отчета вытесняет запомненную: под упал по-настоящему
    crash = {"c": "app", "r": "Error", "x": 1, "log": "PDOException: no connection"}
    st2 = restarts.update(st, {"kube": {"pods": [dict(_pod(n, c, crash), restarts=31) for n, c in names]}},
                          NOW + timedelta(minutes=1))
    assert st2["pod:default/app-a-6f678d8d6d-jxrpd"]["last"]["x"] == 1
    # под пересоздали (счетчик с нуля) - прошлая причина ему не принадлежит
    assert "last" not in restarts.step(st2["pod:default/app-a-6f678d8d6d-jxrpd"], 0, NOW + timedelta(minutes=2))


async def test_recovery_waits_half_an_hour(tmp_path, monkeypatch):
    """Выкладка уносит поды вместе с историей перезапусков, и отбой приходил на следующем же
    отчете. Теперь отбой - после получаса без перезапусков, а вернулось внутри получаса - та же
    история, без нового сообщения."""
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'rh.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(timezone.utc)
    pod = _pod("app-b-5d88689b5d-rg5d7", "app-b", {"c": "app", "r": "Error", "x": 1, "log": "fatal: db is gone"})
    hot = {"pod:default/app-b-5d88689b5d-rg5d7": {"rc": 5, "ev": [[(now - timedelta(minutes=m)).isoformat(), 1]
                                                                 for m in (7, 60, 120, 180, 240)]}}
    async with factory() as s:
        s.add(Server(name="k8s-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=now,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report={"uptime_seconds": 100000}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(minutes, up):
        t = now + timedelta(minutes=minutes)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = t
            srv.last_report = {"uptime_seconds": 100000, "kube": {"pods": [pod] if up else []}}
            srv.alert_state = {**(srv.alert_state or {}), "restarts": hot if up else {}}
            await s.commit()
        await collector.evaluate_servers(factory, cfg, t)
        return [m for m in sent if "k8s-a" in m]

    assert len(await tick(0, True)) == 1
    assert len(await tick(1, False)) == 1           # выкладка унесла под: отбоя пока нет
    assert len(await tick(20, True)) == 1           # снова перезапускается - та же история
    assert len(await tick(21, False)) == 1
    assert len(await tick(50, False)) == 1          # 29 минут без перезапусков
    msgs = await tick(52, False)
    assert len(msgs) == 2 and "поды восстановлены" in msgs[1]
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        assert not srv.alert_state.get("kube_restarts") and not srv.alert_state.get("kube_restarts_hold")
    await engine.dispose()
