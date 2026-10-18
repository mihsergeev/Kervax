"""Воркеры под supervisor и pm2 (workers, helper workers-setup): что считается бедой, мьют, "Что сломано",
алерт, карточка."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import collector, workers as wk
from app.models import Server

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())


def _prog(name, state="RUNNING", **kw):
    return {"name": name, "state": state, "since": kw.pop("since", TS - 3600), "up": 3600 if state in ("RUNNING", "online") else 0,
            "pid": 7, "exit": kw.pop("exit", 0), "r15": kw.pop("r15", 0), "r60": kw.pop("r60", 0), **kw}


def _rep(progs=None, pm2=None, ts=TS, skipped=None):
    ds = [{"kind": "supervisor", "where": "container app-workers-1", "progs": progs or [_prog("queue-listen")]}]
    if pm2 is not None:
        ds.append({"kind": "pm2", "where": "host", "progs": pm2})
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": TS,
            "extras": {"workers": {"v": 1, "ts": ts, "daemons": ds, "skipped": skipped or []}}}


def _srv(rep=None, mutes=None):
    return Server(id=1, name="app-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state={}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_what_is_trouble():
    d = {"ts": TS}
    assert wk.trouble(d, _prog("a")) is None
    assert wk.trouble(d, _prog("a", "FATAL"))[0] == 2
    assert wk.trouble(d, _prog("a", "errored"))[0] == 2
    # BACKOFF - переходное: три минуты ждем
    assert wk.trouble(d, _prog("a", "BACKOFF", since=TS - 60)) is None
    assert wk.trouble(d, _prog("a", "BACKOFF", since=TS - 200))[0] == 2
    # одноразовая отработала с нулем - норма, с ошибкой - предупреждение
    assert wk.trouble(d, _prog("a", "EXITED", exit=0)) is None
    assert wk.trouble(d, _prog("a", "EXITED", exit=3)) == (1, "завершился с кодом 3 и не перезапускается")
    # остановлен руками - решение человека
    assert wk.trouble(d, _prog("a", "STOPPED")) is None and wk.trouble(d, _prog("a", "stopped")) is None
    # падает и поднимается
    assert wk.trouble(d, _prog("a", r15=2)) is None
    assert wk.trouble(d, _prog("a", r15=4)) == (1, "падает и поднимается: 4 перезапуска за 15 минут")
    assert wk.trouble(d, _prog("a", "online", r15=3))[0] == 1


def test_problems_text_mute_and_home():
    rep = _rep([_prog("queue-listen"), _prog("mailer", "FATAL", log="boom: cannot connect to db password=***")],
               pm2=[_prog("api", "online", r15=5)])
    s = _srv(rep)
    p = wk.problems(s, NOW)
    assert [x["key"] for x in p] == ["Supervisor, контейнер app-workers-1|mailer", "PM2, на хосте|api"]
    assert p[0]["text"] == ("воркер mailer (Supervisor, контейнер app-workers-1) не запускается, supervisor перестал "
                            "его поднимать\n↳ boom: cannot connect to db password=***")
    rows = [x for x in collector.server_problems(s, NOW) if x["kind"] == "workers"]
    assert [r["level"] for r in rows] == [2, 1] and rows[0]["mute"] == "worker:Supervisor, контейнер app-workers-1|mailer"
    # заглушить один воркер: насовсем и на время
    s.alert_mutes = ["worker:Supervisor, контейнер app-workers-1|mailer"]
    assert [x["name"] for x in wk.problems(s, NOW)] == ["api"]
    s.alert_mutes = []
    s.alert_snoozes = {"worker:PM2, на хосте|api": (NOW + timedelta(days=1)).isoformat()}
    assert [x["name"] for x in wk.problems(s, NOW)] == ["mailer"]
    v = wk.view(s, NOW)
    api = v[1]["progs"][0]
    assert api["mute"] == {"until": (NOW + timedelta(days=1)).isoformat()} and api["level"] == 1
    assert v[0]["progs"][1]["why"].startswith("не запускается")
    # срок снуза вышел - снова в беде
    s.alert_snoozes = {"worker:PM2, на хосте|api": (NOW - timedelta(minutes=1)).isoformat()}
    assert len(wk.problems(s, NOW)) == 2


def test_stale_block_is_not_judged():
    s = _srv(_rep([_prog("mailer", "FATAL")], ts=TS - 3600))
    assert wk.problems(s, NOW) == [] and wk.view(s, NOW) is None


def test_skipped_in_words():
    rep = _rep(skipped=[{"where": "container mailserver", "kind": "supervisor", "why": "norpc"}])
    assert wk.skipped(rep) == [{"where": "Supervisor, контейнер mailserver", "why": "у supervisord не включен сокет управления"}]


def test_snooze_accepts_a_worker_key():
    from app.schemas import AlertSnoozeIn
    k = "worker:Supervisor, контейнер app-workers-1|grp:w1"
    assert AlertSnoozeIn(kind=k, hours=24).kind == k


def test_own_unit_does_not_alert():
    assert collector.own_helper_unit("kervax-workers.service")


def test_helper_script():
    src = (Path(__file__).resolve().parents[2] / "agent" / "workers-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.1" in src and "KERVAX_SETUP_ALWAYS=1" in src and "\r" not in src
    # pm2 спрашиваем по rpc.sock, не командой pm2: она запускает новый демон, если не нашла старый
    assert "getMonitorData" in src and "pm2 jlist" in src and "rpc.sock" in src
    assert "getAllProcessInfo" in src and "/bin/sh -c" not in src
    # пароли из строк лога маскируются
    assert src.count("***") >= 2


def test_alert_condition():
    rep = _rep([_prog("mailer", "FATAL", log="boom")], pm2=[_prog("api", "online", r15=5)])
    lv, ctx = collector._server_conditions(_srv(rep), NOW)["workers"]
    assert lv == 2 and ctx["sig"] == ["Supervisor, контейнер app-workers-1|mailer", "PM2, на хосте|api"]
    assert ctx["detail"].startswith("воркер mailer (Supervisor, контейнер app-workers-1) не запускается")
    assert collector._server_conditions(_srv(_rep()), NOW)["workers"][0] == 0
