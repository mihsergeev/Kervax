"""Утечка памяти: дно (суточный минимум) контейнера, пода или процесса растет суток подряд."""
from datetime import date, datetime, timedelta, timezone

from app import collector, mem_trend
from app.models import Server

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
MB = 2**20


def _rep(**kw):
    r = {"mem_total": 16 * 2**30, "mem_used": 8 * 2**30,
         "docker": {"access": True, "containers": [
             {"name": "worker", "image": "app/worker:1", "state": "running", "mem": 900 * MB},
             {"name": "db", "image": "mariadb:11", "state": "running", "mem": 4 * 2**30},
             {"name": "old", "image": "app/old:1", "state": "exited", "mem": 0}]},
         "kube": {"pods": [{"ns": "shop", "name": "api-7f9", "owner": "ReplicaSet", "mem": 300 * MB},
                           {"ns": "db", "name": "pg-0", "owner": "StatefulSet", "image": "postgres:16", "mem": 2**30},
                           {"ns": "shop", "name": "job-1", "owner": "Job", "mem": 50 * MB}]},
         "top_mem": [{"comm": "java", "user": "app", "rss": 1200 * MB, "shared": 200 * MB},
                     {"comm": "java", "user": "app", "rss": 300 * MB, "shared": 0},
                     {"comm": "mysqld", "user": "mysql", "rss": 3 * 2**30, "shared": 0}]}
    r.update(kw)
    return r


def _srv(state=None, mutes=None, rep=None):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_targets_skip_buffer_pools_jobs_and_stopped():
    assert mem_trend.targets(_rep()) == {"docker:worker": 900, "pod:shop/api-7f9": 300, "proc:java@app": 1000}


def test_daily_minimum_and_history():
    cs = mem_trend.step(None, 900, NOW)
    cs = mem_trend.step(cs, 1500, NOW + timedelta(hours=1))  # всплеск дно не двигает
    cs = mem_trend.step(cs, 850, NOW + timedelta(hours=2))
    assert cs == {"d": "2026-10-12", "m": 850, "h": []}
    cs = mem_trend.step(cs, 1000, NOW + timedelta(days=1))
    assert cs["h"] == [["2026-10-12", 850]] and cs["m"] == 1000
    # пропавшее из отчета держим сутки, потом забываем
    st = mem_trend.update({"docker:gone": {"d": "2026-10-11", "m": 5, "h": []},
                           "docker:older": {"d": "2026-10-09", "m": 5, "h": []}}, _rep(), NOW)
    assert "docker:gone" in st and "docker:older" not in st


def _hist(vals, last_day=date(2026, 10, 11)):
    days = [last_day - timedelta(days=len(vals) - 1 - i) for i in range(len(vals))]
    return {"d": "2026-10-12", "m": vals[-1], "h": [[d.isoformat(), v] for d, v in zip(days, vals)]}


def test_leak_needs_steady_growth():
    lk = mem_trend.leak(_hist([800, 900, 1010, 1100, 1210]))
    assert lk["first"] == 800 * MB and lk["last"] == 1210 * MB and lk["rate"] == (410 * MB) // 4
    assert mem_trend.leak(_hist([800, 900, 1010, 1100])) is None  # мало суток
    assert mem_trend.leak(_hist([800, 1100, 1220, 1270, 1290])) is None  # прогрев кэша затухает
    assert mem_trend.leak(_hist([800, 900, 890, 1100, 1210])) is None  # был день без роста
    assert mem_trend.leak(_hist([800, 810, 820, 830, 840])) is None  # за 4 суток +5%: шум
    gap = _hist([800, 900, 1010, 1100, 1210])
    gap["h"][2][0] = "2026-10-05"
    assert mem_trend.leak(gap) is None  # нода молчала, ряд не непрерывный


def _state(**series):
    return {"mem_trend": {k: _hist(v) for k, v in series.items()}}


def test_growth_with_plenty_of_memory_is_not_a_problem():
    """Prometheus +31 МБ в сутки на ноде с гигабайтами свободной памяти будил алертом в три ночи:
    рост виден в карточке, но беда - только если при таком росте памяти не хватит на неделю."""
    s = _srv(_state(**{"docker:worker": [500, 700, 900, 1100, 1300], "proc:java@app": [500, 700, 900, 1100, 1300]}))
    ps = mem_trend.problems(s, NOW)
    # процесс в контейнере виден и в топе процессов: говорим один раз, про контейнер
    assert [x["key"] for x in ps] == ["docker:worker"]
    # 8 ГБ свободно, +200 МБ в сутки: хватит на 41 сутки
    assert ps[0]["text"] == "память контейнера worker растет 4 суток подряд: 500 МБ -> 1.3 ГБ, +200 МБ в сутки"
    assert ps[0]["level"] == 0 and ps[0]["eta_days"] == 41
    assert collector._server_conditions(s, NOW)["mem_leak"][0] == 0
    assert [p for p in collector.server_problems(s, NOW) if p["kind"] == "mem_leak"] == []


def test_problems_alert_and_home_rows():
    # свободно 1 ГБ, +200 МБ в сутки: на 5 суток - за неделю никто не встает ночью, видно в карточке
    s = _srv(_state(**{"docker:worker": [500, 700, 900, 1100, 1300]}), rep=_rep(mem_used=15 * 2**30))
    ps = mem_trend.problems(s, NOW)
    assert ps[0]["level"] == 0 and ps[0]["eta_days"] == 5.1
    assert collector._server_conditions(s, NOW)["mem_leak"][0] == 0
    assert [p for p in collector.server_problems(s, NOW) if p["kind"] == "mem_leak"] == []
    # меньше чем на трое суток - алерт и "Что сломано"
    s = _srv(_state(**{"docker:worker": [500, 700, 900, 1100, 1300]}), rep=_rep(mem_used=16 * 2**30 - 500 * MB))
    ps = mem_trend.problems(s, NOW)
    assert ps[0]["level"] == 2 and ps[0]["eta_days"] == 2.5
    assert ps[0]["text"] == ("память контейнера worker растет 4 суток подряд: 500 МБ -> 1.3 ГБ, +200 МБ в сутки, "
                             "свободной памяти сервера (500 МБ) при таком росте хватит примерно на 2.5 сут")
    lvl, ctx = collector._server_conditions(s, NOW)["mem_leak"]
    assert lvl == 2 and ctx["sig"] == ["docker:worker"]
    rows = [(p["level"], p.get("section"), p["mute"]) for p in collector.server_problems(s, NOW)
            if p["kind"] == "mem_leak"]
    assert rows == [(2, "docker", "leak:docker:worker")]
    # без контейнера - про процесс; памяти на сутки с небольшим - уже проблема
    tight = _rep(mem_used=16 * 2**30 - 300 * MB)
    s2 = _srv(_state(**{"proc:java@app": [500, 700, 900, 1100, 1300]}), rep=tight)
    p2 = mem_trend.problems(s2, NOW)[0]
    assert p2["text"].startswith("память процесса java (пользователь app) растет 4 суток подряд")
    assert p2["level"] == 2 and p2["eta_days"] == 1.5
    assert [(p["section"] if "section" in p else None, p["sec"]) for p in collector.server_problems(s2, NOW)
            if p["kind"] == "mem_leak"] == [(None, "memleak")]
    # заглушенный - молчим
    assert mem_trend.problems(_srv(_state(**{"docker:worker": [500, 700, 900, 1100, 1300]}),
                                   mutes=["leak:docker:worker"]), NOW) == []
    assert collector._recovery_detail("mem_leak", {}, {}) == "память больше не растет день за днем"


def test_memory_is_shared_by_everything_that_grows():
    # свободно 1 ГБ: одному контейнеру с +200 МБ в сутки хватило бы на 5 суток, двоим - на 2.6
    two = _state(**{"docker:worker": [500, 700, 900, 1100, 1300], "docker:api": [500, 700, 900, 1100, 1300]})
    ps = mem_trend.problems(_srv(two, rep=_rep(mem_used=15 * 2**30)), NOW)
    assert [(x["key"], x["level"], x["eta_days"]) for x in ps] == [("docker:api", 2, 2.6), ("docker:worker", 2, 2.6)]
    one = _state(**{"docker:worker": [500, 700, 900, 1100, 1300]})
    assert mem_trend.problems(_srv(one, rep=_rep(mem_used=15 * 2**30)), NOW)[0]["level"] == 0


def test_no_false_recovery_when_still_growing():
    """Алерт ушел по старому правилу (любой рост), а теперь памяти хватает: растет как росло, и
    "память больше не растет" было бы неправдой - отбой молча."""
    st = _state(**{"docker:worker": [500, 700, 900, 1100, 1300]})
    s = _srv(st)
    assert collector._sig_all_quiet(s, "mem_leak", {"docker:worker"}, NOW)
    # перестал расти - это настоящий отбой
    calm = _state(**{"docker:worker": [500, 700, 700, 700, 700]})
    assert not collector._sig_all_quiet(_srv(calm), "mem_leak", {"docker:worker"}, NOW)


def test_pod_mute_survives_a_deploy():
    """Мьют роста памяти ставится на под, а после деплоя у пода новое имя: глушим ворклоад."""
    st = _state(**{"pod:shop/mail-worker-789cd65bb8-nkftn": [500, 700, 900, 1100, 1300]})
    assert [x["key"] for x in mem_trend.problems(_srv(st), NOW)] == ["pod:shop/mail-worker-789cd65bb8-nkftn"]
    assert mem_trend.problems(_srv(st, mutes=["leak:pod:shop/mail-worker-6b8b56c769-f4k8m"]), NOW) == []
    assert mem_trend.problems(_srv(st, mutes=["leak:pod:shop2/mail-worker-6b8b56c769-f4k8m"]), NOW) != []
