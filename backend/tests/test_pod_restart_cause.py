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
        "похоже, подвисает нода k8s-a: пробы и запросы к kube-api не успевают, смотрите нагрузку и память ноды")
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
