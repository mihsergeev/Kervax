"""Кроны хоста и контейнеров (cron_watch, helper cron-setup): наслоение, зависание, мьют, "Что сломано",
алерт, раздел."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import collector, cron_watch as cw
from app.models import Server

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())


def _job(cmd, ages=(), cpu=0, **kw):
    run = [{"pid": 100 + i, "age": a, "cpu": cpu, "procs": 2} for i, a in enumerate(sorted(ages, reverse=True))]
    return {"where": kw.pop("where", "host"), "kind": "cron", "user": "root", "sched": kw.pop("sched", "* * * * *"),
            "src": "/etc/cron.d/app", "cmd": cmd, "last": TS - 30, "runs": kw.pop("runs", 1440),
            "max": kw.pop("max", 0), "done": kw.pop("done", 0), "n": kw.pop("n", len(run)), "run": run, **kw}


def _rep(jobs=None, ts=TS, disc=0):
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": TS,
            "extras": {"cron": {"v": 1, "ts": ts, "journal": True, "disc24": disc,
                                "daemons": [{"where": "host", "kind": "cron", "mta": False, "jobs": len(jobs or [])}],
                                "jobs": jobs or [_job("/lib65/scripts/sync.sh")]}}}


def _srv(rep=None, mutes=None, state=None):
    return Server(id=1, name="app-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_overlap():
    # не идет - не о чем говорить; одна копия минуту - норма
    assert cw.trouble(_job("a")) is None
    assert cw.trouble(_job("a", [50])) is None
    # задача раз в минуту, которая иногда работает 70 секунд: вторая копия идет секунды - не наслоение
    assert cw.trouble(_job("a", [70, 10])) is None
    # вторая копия идет четверть часа - наслоение, предупреждение
    assert cw.trouble(_job("a", [1000, 940])) == (1, "наслаивается: идут 2 копии, самая старая 16 мин")
    # пять копий и больше - копятся, проблема
    assert cw.trouble(_job("a", [3600, 3540, 3480, 3420, 3360]))[0] == 2
    # всех копий больше, чем перечислено: судим по n
    assert cw.trouble(_job("a", [1000, 940], n=7)) == (2, "наслаивается: идут 7 копий, самая старая 16 мин")
    # после ушедшего алерта отбой с запасом: держится, пока вторая копия старше половины порога
    assert cw.trouble(_job("a", [600, 500])) is None
    assert cw.trouble(_job("a", [600, 500]), held=True)[0] == 1
    assert cw.trouble(_job("a", [600, 400]), held=True) is None


def test_hung():
    # обычная длительность известна: висит, когда в разы дольше (не меньше часа)
    j = _job("backup", [5 * 3600], max=3600, done=4, sched="0 3 * * *", runs=1)
    assert cw.usual(j) == (3 * 3600, 3600)
    assert cw.trouble(j) == (1, "висит 5 ч 0 мин, обычно до 1 ч 0 мин")
    assert cw.trouble(_job("backup", [2 * 3600], max=3600, done=4)) is None
    # короткая задача, законченных копий видели много: висит после часа
    j = _job("short", [3700], max=20, done=5)
    assert cw.usual(j) == (cw.HUNG_MIN, 1)
    assert cw.trouble(j) == (1, "висит 1 ч 1 мин, обычно меньше минуты")
    # истории нет - только совсем долгое
    assert cw.trouble(_job("new", [3 * 3600])) is None
    assert cw.trouble(_job("new", [7 * 3600]))[1] == "висит 7 ч 0 мин"
    # висит и крутит процессор (в среднем ядро и больше) - проблема, сколько съела - в тексте
    t = cw.trouble(_job("short", [7200], cpu=9000, max=20, done=5))
    assert t == (2, "висит 2 ч 0 мин, обычно меньше минуты, съела 2 ч 30 мин процессора")
    # и наслаивается, и висит - одной строкой
    t = cw.trouble(_job("short", [7200, 7140], max=20, done=5))
    assert t == (1, "наслаивается: идут 2 копии, самая старая 2 ч 0 мин, обычно меньше минуты")


def test_problems_text_mute_and_home():
    rep = _rep([_job("/lib65/scripts/sync.sh"), _job("php /app/yii cron/min5", [3600, 3540, 3480, 3420, 3360],
                                                   where="container app-cron-1", sched="*/5 * * * *")])
    s = _srv(rep)
    p = cw.problems(s, NOW)
    assert [x["key"] for x in p] == ["контейнер app-cron-1|php /app/yii cron/min5"]
    assert p[0]["text"] == ("крон контейнер app-cron-1 (*/5 * * * *) наслаивается: идут 5 копий, самая старая 1 ч 0 мин\n"
                            "↳ php /app/yii cron/min5")
    assert p[0]["since"] == datetime.fromtimestamp(TS - 3600, timezone.utc).isoformat()
    rows = [x for x in collector.server_problems(s, NOW) if x["kind"] == "cron_stuck"]
    assert [r["level"] for r in rows] == [2] and rows[0]["mute"] == "cron:контейнер app-cron-1|php /app/yii cron/min5"
    # заглушить одну задачу: насовсем и на время
    s.alert_mutes = ["cron:контейнер app-cron-1|php /app/yii cron/min5"]
    assert cw.problems(s, NOW) == []
    s.alert_mutes = []
    s.alert_snoozes = {"cron:контейнер app-cron-1|php /app/yii cron/min5": (NOW + timedelta(days=1)).isoformat()}
    assert cw.problems(s, NOW) == []
    v = cw.view(s, NOW)
    row = [j for j in v["jobs"] if j["n"]][0]
    assert row["mute"] == {"until": (NOW + timedelta(days=1)).isoformat()} and row["level"] == 2
    assert row["where"] == "контейнер app-cron-1" and row["runs"] == 1440 and row["usual"] == 0
    # срок снуза вышел - снова в беде
    s.alert_snoozes = {"cron:контейнер app-cron-1|php /app/yii cron/min5": (NOW - timedelta(minutes=1)).isoformat()}
    assert len(cw.problems(s, NOW)) == 1


def test_view_lost_output_and_daemons():
    rep = _rep([_job("docker exec -i app bash -c x 2>&1 >> /var/log/x.log", lost="err", order=1),
                _job("/lib65/scripts/a.sh", lost="both", max=90, done=2)], disc=28)
    v = cw.view(_srv(rep), NOW)
    assert v["disc24"] == 28 and v["journal"] is True
    assert v["daemons"] == [{"where": "на хосте", "kind": "cron", "mta": False, "jobs": 2}]
    a, b = v["jobs"]
    assert a["lost"] == "err" and a["order"] is True
    assert b["lost"] == "both" and "order" not in b and b["usual"] == 90


def test_stale_block_is_not_judged():
    s = _srv(_rep([_job("a", [1000, 940])], ts=TS - 3600))
    assert cw.problems(s, NOW) == [] and cw.view(s, NOW) is None


def test_held_comes_from_the_sent_alert():
    j = _job("a", [600, 500])
    s = _srv(_rep([j]), state={"cron_stuck_sig": ["на хосте|a"]})
    assert [x["level"] for x in cw.problems(s, NOW)] == [1]
    assert cw.problems(_srv(_rep([j])), NOW) == []


def test_snooze_accepts_a_cron_key():
    from app.schemas import AlertSnoozeIn
    k = "cron:на хосте|/usr/bin/docker exec -i app bash -c './yii cron/min5' 2>&1 >> /var/log/x.log"
    assert AlertSnoozeIn(kind=k, hours=24).kind == k


def test_own_unit_does_not_alert():
    assert collector.own_helper_unit("kervax-cron.service")


def test_alert_condition():
    rep = _rep([_job("a", [3600, 3540, 3480, 3420, 3360]), _job("b", [1000, 940])])
    lv, ctx = collector._server_conditions(_srv(rep), NOW)["cron_stuck"]
    assert lv == 2 and ctx["sig"] == ["на хосте|a", "на хосте|b"]
    assert ctx["detail"].startswith("крон на хосте (* * * * *) наслаивается: идут 5 копий")
    assert collector._server_conditions(_srv(_rep()), NOW)["cron_stuck"][0] == 0


def test_helper_script():
    src = (Path(__file__).resolve().parents[2] / "agent" / "cron-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.1" in src and "KERVAX_SETUP_ALWAYS=1" in src and "\r" not in src
    # обратный слэш в команде удваивается так, что mawk его не теряет: иначе отчет не JSON
    assert '_jn = split(s, _jp, "\\\\")' in src
    # пароли в командах маскируются: password=, mysql -p, user:pass@ в адресе, curl -u, Bearer
    assert "function cmask" in src and '" -p***"' in src and ":***@" in src and '" ***"' in src
    # журнал читается по курсору, после сбоя курсора - с прошлого чтения, не за сутки снова
    assert "--after-cursor" in src and "--show-cursor" in src and 'since=$cts' in src
    # имя контейнера: docker ps один раз в файл (where_of идет в $(...), переменная бы не дожила)
    assert '"$TMPD/dps"' in src and "DNAME" not in src
    # порядок перенаправлений: 2>&1 до >> file отдает stderr в cron
    assert "ordbug" in src and '\\"order\\":1' in src
