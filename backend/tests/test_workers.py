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
    assert "KERVAX_SETUP_VERSION=0.4" in src and "KERVAX_SETUP_ALWAYS=1" in src and "\r" not in src
    # 0.4: ничего, на что указал процесс, не запускается от root: supervisord спрашивает curl хоста,
    # node pm2 - от владельца демона, root контейнера - docker exec или nobody
    assert '-m -n -- "$py"' not in src and '-m -n -- "$exe"' not in src
    assert "--unix-socket" in src and "-K -" in src and "u=65534; g=65534" in src and "docker exec -u" in src
    # 0.3: выход с кодом 0 после минуты работы - плановый перезапуск, не падение; старые времена сброшены
    assert 'if ($9 == "0" && $6 - cs[k] >= 60) pt = pt' in src and '\\"p60\\":%d' in src
    assert 'rt = cv >= 3 ? hour(crt[k]) : ""' in src
    # pm2 спрашиваем по rpc.sock, не командой pm2: она запускает новый демон, если не нашла старый
    assert "getMonitorData" in src and "pm2 jlist" in src and "rpc.sock" in src
    assert "getAllProcessInfo" in src and "/bin/sh -c" not in src
    # пароли из строк лога маскируются
    assert src.count("***") >= 2
    # 0.2: обратный слэш в логе (App\Jobs\Mail) удваивается так, что mawk его не теряет: иначе отчет не JSON
    assert '_jn = split(s, _jp, "\\\\")' in src


def test_alert_condition():
    rep = _rep([_prog("mailer", "FATAL", log="boom")], pm2=[_prog("api", "online", r15=5)])
    lv, ctx = collector._server_conditions(_srv(rep), NOW)["workers"]
    assert lv == 2 and ctx["sig"] == ["Supervisor, контейнер app-workers-1|mailer", "PM2, на хосте|api"]
    assert ctx["detail"].startswith("воркер mailer (Supervisor, контейнер app-workers-1) не запускается")
    assert collector._server_conditions(_srv(_rep()), NOW)["workers"][0] == 0


def _copies(*bad, n=15, **kw):
    return [_prog(f"listen:listen_{i:02d}", r15=5 if f"{i:02d}" in bad else 0, **kw) for i in range(n)]


def test_copies_of_a_program_are_one_trouble():
    """Слушатель очереди в пятнадцати копиях: падают по очереди - одна беда с ключом программы"""
    s = _srv(_rep(_copies("00", "03", "07") + [_prog("grp:mailer", "FATAL"), _prog("one:one_00", r15=4)]))
    p = wk.problems(s, NOW)
    assert [x["key"] for x in p] == ["Supervisor, контейнер app-workers-1|grp:mailer",
                                     "Supervisor, контейнер app-workers-1|listen",
                                     "Supervisor, контейнер app-workers-1|one:one_00"]
    assert p[1]["text"] == ("воркер listen (Supervisor, контейнер app-workers-1) падает и поднимается: 5 перезапусков "
                            "за 15 минут - так 3 копии из 15")
    # одна копия в беде - имя копии, ключ все равно программы: sig не скачет
    one = wk.problems(_srv(_rep(_copies("04"))), NOW)
    assert one[0]["key"] == "Supervisor, контейнер app-workers-1|listen" and "воркер listen:listen_04 (" in one[0]["text"]
    assert wk.program_key("Supervisor, контейнер app-workers-1|listen:listen_04") == "Supervisor, контейнер app-workers-1|listen"
    assert wk.program_key("PM2, на хосте|api:3") == "PM2, на хосте|api"
    assert wk.program_key("Supervisor, контейнер app-workers-1|grp:mailer") == "Supervisor, контейнер app-workers-1|grp:mailer"


def test_copies_mute_by_program_and_old_copy_mutes():
    s = _srv(_rep(_copies("00", "03")), mutes=["worker:Supervisor, контейнер app-workers-1|listen"])
    assert wk.problems(s, NOW) == []
    v = wk.view(s, NOW)
    row = v[0]["progs"][0]
    assert row["mkey"] == "Supervisor, контейнер app-workers-1|listen" and row["mute"] == {"perm": True}
    # мьюты по копиям до 1.4.273: глушат, когда заглушена каждая копия в беде
    old = ["worker:Supervisor, контейнер app-workers-1|listen:listen_00", "worker:Supervisor, контейнер app-workers-1|listen:listen_03"]
    assert wk.problems(_srv(_rep(_copies("00", "03")), mutes=old), NOW) == []
    assert len(wk.problems(_srv(_rep(_copies("00", "05")), mutes=old), NOW)) == 1


def test_planned_restarts_are_not_trouble():
    # helper 0.3: выходы с кодом 0 после минуты работы - в p60, в r15 их нет
    s = _srv(_rep([_prog("listen:listen_00", p60=8), _prog("listen:listen_01", p60=6)]))
    assert wk.problems(s, NOW) == []
    assert [x["p60"] for x in wk.view(s, NOW)[0]["progs"]] == [8, 6]
