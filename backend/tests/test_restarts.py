"""Часто перезапускается: контейнер или под, который падает раз в час-другой. Быстрый цикл ловит
docker_loop, а медленный - приросты RestartCount за сутки."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import collector, restarts
from app.alerts import full_text
from app.models import Server

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _rep(rc=0, pod_rc=0, access=True):
    return {"docker": {"access": access, "containers": [{"name": "web", "state": "running", "restarts": rc}]},
            "kube": {"pods": [{"ns": "shop", "name": "api-7f9", "owner": "ReplicaSet", "phase": "Running",
                               "ready": True, "restarts": pod_rc},
                              {"ns": "shop", "name": "migrate-x", "owner": "Job", "restarts": 9}]}}


def _srv(state=None, mutes=None, rep=None):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_step_counts_increments_and_forgets():
    cs = restarts.step(None, 3, NOW)
    assert cs == {"rc": 3, "ev": []}  # первая точка - отсчет, прошлые перезапуски не в счет
    cs = restarts.step(cs, 5, NOW + timedelta(hours=1))
    cs = restarts.step(cs, 5, NOW + timedelta(hours=2))
    cs = restarts.step(cs, 1, NOW + timedelta(hours=3))  # пересоздали при деплое - не падение
    cs = restarts.step(cs, 2, NOW + timedelta(hours=4))
    assert restarts.count(cs, NOW + timedelta(hours=4))[0] == 3
    # через сутки после первого прироста он уходит из окна
    later = NOW + timedelta(hours=25, minutes=1)
    assert restarts.count(restarts.step(cs, 2, later), later)[0] == 1


def test_update_keeps_counters_when_the_source_is_missing():
    st = restarts.update(None, _rep(rc=1, pod_rc=2), NOW)
    assert set(st) == {"docker:web", "pod:shop/api-7f9"}  # под Job - не в счет
    st = restarts.update(st, _rep(rc=2, access=False), NOW + timedelta(minutes=1))
    assert st["docker:web"]["rc"] == 1  # доступа к Docker нет - счетчик держим
    st = restarts.update(st, {"docker": {"access": True, "containers": []}, "kube": {"pods": []}},
                         NOW + timedelta(minutes=2))
    assert st == {}  # контейнер и под пропали из отчета - забываем


def _state(n_docker, n_pod, alerted_loop=False):
    rs = {"docker:web": {"rc": 50, "ev": [[(NOW - timedelta(hours=i + 1)).isoformat(), 1] for i in range(n_docker)]},
          "pod:shop/api-7f9": {"rc": 9, "ev": [[(NOW - timedelta(minutes=30)).isoformat(), n_pod]]}}
    return {"restarts": rs, "docker": {"web": {"alerted_loop": alerted_loop}}}


def test_problems_alert_and_home_rows():
    s = _srv(_state(6, 22))
    assert [x["text"] for x in restarts.problems(s, NOW)] == [
        "контейнер web перезапускался 6 раз за сутки, последний 60 мин назад",
        "под shop/api-7f9 перезапускался 22 раза за сутки, последний 30 мин назад"]
    conds = collector._server_conditions(s, NOW)
    assert conds["docker_restarts"][0] == 1 and conds["docker_restarts"][1]["sig"] == ["docker:web"]
    assert conds["kube_restarts"][0] == 1
    rows = [(p["kind"], p["level"], p.get("section"), p["mute"]) for p in collector.server_problems(s, NOW)
            if p["kind"].endswith("_restarts")]
    assert rows == [("docker_restarts", 1, "docker", "restarts:docker:web"),
                    ("kube_restarts", 1, "kuber", "restarts:pod:shop/api-7f9")]
    assert restarts.view(s, NOW) == {"docker:web": 6, "pod:shop/api-7f9": 22}
    # реже пяти раз в сутки - не беда; заглушенный - молчим; быстрый цикл уже сказан docker_loop
    assert restarts.problems(_srv(_state(4, 0)), NOW) == []
    assert [x["key"] for x in restarts.problems(_srv(_state(6, 22), mutes=["restarts:docker:web"]), NOW)] == [
        "pod:shop/api-7f9"]
    assert [x["key"] for x in restarts.problems(_srv(_state(6, 22, alerted_loop=True)), NOW)] == ["pod:shop/api-7f9"]
    assert collector._recovery_detail("docker_restarts", {}, {}) == "контейнеры восстановлены, частых перезапусков нет"
    assert collector._recovery_detail("kube_restarts", {}, {}) == "поды восстановлены, частых перезапусков нет"


async def test_alert_after_five_restarts_a_day(tmp_path, monkeypatch):
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 'r.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    t0 = datetime.now(timezone.utc) - timedelta(hours=6)
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=t0,
                     cpu_alert_percent=0, mem_alert_percent=0,
                     last_report={"uptime_seconds": 100000, **_rep(rc=0)}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")
    # контейнер падает раз в час: шесть часов подряд по одному перезапуску
    for i in range(8):
        t = t0 + timedelta(hours=i)
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen = t
            srv.last_report = {"uptime_seconds": 100000, **_rep(rc=min(i, 6))}
            await s.commit()
        await collector.evaluate_servers(factory, cfg, t)
    hits = [m for m in sent if getattr(m, "kind", "") == "docker_restarts"]
    assert len(hits) == 1 and "контейнер web перезапускался 5 раз за сутки" in full_text(hits[0]), sent
    assert hits[0].startswith("⚠️🐳 ") and hits[0].split("\n")[2] == "5 раз за сутки", hits[0]
    await engine.dispose()
    # имя контейнера как есть: "web часто перезапускается", а не "Web часто перезапускается"
    assert hits[0].split("\n")[1] == "<b>web часто перезапускается</b>", hits[0]


def test_pod_names_come_down_to_their_workload():
    from app import podnames
    assert podnames.workload("mail-worker-6b8b56c769-f4k8m") == "mail-worker"   # деплоймент
    assert podnames.workload("scheduler-9d79c684f-6z4n7") == "scheduler"
    assert podnames.workload("node-exporter-x7k2p") == "node-exporter"                  # демонсет
    assert podnames.workload("rabbitmq-0") == "rabbitmq"                                # стейтфулсет
    assert podnames.workload("api-7f9") == "api-7f9"                                    # не похоже - как есть
    assert podnames.workload("whatever-abcde", "deployment/front") == "front"           # ctrl агента главнее
    # мьют, поставленный на под до деплоя, глушит новые поды того же деплоймента
    old = {"pod:shop/mail-worker-6b8b56c769-f4k8m"}
    assert podnames.muted(old, "pod:shop/mail-worker-789cd65bb8-nkftn")
    assert podnames.muted(old, "pod:shop/mail-worker", "deployment/mail-worker")
    assert not podnames.muted(old, "pod:shop2/mail-worker-789cd65bb8-nkftn")       # другой namespace
    assert not podnames.muted(old, "pod:shop/crawler-649ccd4dfd-tn87l")


def _dev_rep(*pods):
    return {"kube": {"pods": [{"ns": "shop", "name": n, "owner": "ReplicaSet", "ctrl": f"deployment/{c}",
                               "phase": "Running", "ready": True, "restarts": 5} for n, c in pods]}}


def _five(*keys):
    return {"restarts": {k: {"rc": 5, "ev": [[(NOW - timedelta(minutes=10 + i)).isoformat(), 1] for i in range(5)]}
                         for k in keys}}


def test_pods_of_one_deployment_are_one_line_and_mutes_survive_a_deploy():
    """На одной ноде перезапуски подов заглушили руками, деплоймент перекатился, у подов новые
    имена - и заглушенное снова пришло алертом, а два пода одного деплоймента шли двумя строками."""
    rep = _dev_rep(("mail-worker-789cd65bb8-nkftn", "mail-worker"),
                      ("mail-worker-789cd65bb8-xmszd", "mail-worker"),
                      ("importer-795d8cc8c-fttx8", "importer"))
    st = _five("pod:shop/mail-worker-789cd65bb8-nkftn", "pod:shop/mail-worker-789cd65bb8-xmszd",
               "pod:shop/importer-795d8cc8c-fttx8")
    pr = restarts.problems(_srv(st, rep=rep), NOW)
    assert [(x["key"], x["text"]) for x in pr] == [
        ("pod:shop/importer", "под shop/importer-795d8cc8c-fttx8 перезапускался 5 раз за сутки, последний 10 мин назад"),
        ("pod:shop/mail-worker", "поды shop/mail-worker (2 шт.) перезапускались 10 раз за сутки, последний 10 мин назад")]
    # мьюты, поставленные на поды прошлой выкатки
    old = ["restarts:pod:shop/mail-worker-6b8b56c769-f4k8m", "restarts:pod:shop/importer-5bfc9d5574-csqbv"]
    assert restarts.problems(_srv(st, mutes=old, rep=rep), NOW) == []
    # новый мьют ставится на деплоймент (ключ строки в "Что сломано")
    rows = [p["mute"] for p in collector.server_problems(_srv(st, rep=rep), NOW) if p["kind"] == "kube_restarts"]
    assert rows == ["restarts:pod:shop/importer", "restarts:pod:shop/mail-worker"]


async def test_another_deployment_within_the_hour_is_the_same_story(tmp_path, monkeypatch):
    """Перезапуски считаются за сутки: еще один деплоймент перешел порог через минуту после
    сообщения - не второе сообщение. Старый sig по подам не дает "нового". Заглушили все -
    отбоя "частых перезапусков нет" нет: ничего не стихло."""
    from app.config import Settings
    from app.db import Base, create_engine_and_factory

    engine, factory = create_engine_and_factory(f"sqlite+aiosqlite:///{(tmp_path / 's.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    t0 = datetime.now(timezone.utc)
    pods = [("mail-worker-789cd65bb8-nkftn", "mail-worker"), ("importer-795d8cc8c-fttx8", "importer"),
            ("scheduler-5d88689b5d-rg5d7", "scheduler"), ("crawler-649ccd4dfd-zz4k8", "crawler")]
    async with factory() as s:
        s.add(Server(name="node", token_hash="x", enabled=True, backup_not_required=True, last_seen=t0,
                     cpu_alert_percent=0, mem_alert_percent=0, last_report={"uptime_seconds": 100000}))
        await s.commit()
    sent: list[str] = []

    async def fake_send(cfg, text, parse_mode=None):
        sent.append(text)
        return []

    monkeypatch.setattr("app.alerts.send_alert", fake_send)
    cfg = Settings(alert_webhook="http://hook")

    async def tick(t, hot, state=None, mutes=None):
        async with factory() as s:
            srv = (await s.execute(select(Server))).scalars().one()
            srv.last_seen, srv.last_report = t, {"uptime_seconds": 100000, **_dev_rep(*pods)}
            st = dict(srv.alert_state or {}) if state is None else state
            # у каждого деплоймента свои минуты: три разом в одну минуту - уже пачка (restarts.bursts)
            st["restarts"] = {f"pod:shop/{n}": {"rc": 5, "ev": [[(t - timedelta(minutes=5 + 20 * i + 3 * j)).isoformat(), 1]
                                                                 for i in range(5)]}
                              for j, (n, _c) in enumerate(hot)}
            srv.alert_state = st
            if mutes is not None:
                srv.alert_mutes = mutes
            await s.commit()
        await collector.evaluate_servers(factory, cfg, t)

    # sig старой панели - по подам: тот же под под ключом деплоймента не новость
    await tick(t0, pods[:1], state={"kube_restarts": 1, "kube_restarts_sig": ["pod:shop/mail-worker-789cd65bb8-nkftn"]})
    assert sent == []
    await tick(t0 + timedelta(minutes=1), pods[:2])        # та же история, хотя сообщения от этой панели не было
    assert len(sent) == 1
    await tick(t0 + timedelta(minutes=2), pods[:3])        # через минуту после сообщения - молчим
    assert len(sent) == 1
    await tick(t0 + timedelta(minutes=70), pods[:4])       # через час - уже новое
    assert len(sent) == 2 and "crawler" in sent[1]
    await tick(t0 + timedelta(minutes=72), pods[:4],
               mutes=[f"restarts:pod:shop/{c}" for _n, c in pods])
    assert len(sent) == 2                                    # заглушили все: отбоя нет
    async with factory() as s:
        srv = (await s.execute(select(Server))).scalars().one()
        assert int(srv.alert_state.get("kube_restarts", 0)) == 0
    await engine.dispose()
