"""Ошибки в логах контейнеров и подов (log_errors, helper logerr-setup): всплеск против обычного часа,
мьют, "Что сломано", алерт, раздел."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import collector, log_errors as le
from app.models import Server

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())


def _src(where, h1, d1, span=86400, container="", last="ERROR boom", lines1=None):
    return {"where": where, "container": container, "h1": h1, "d1": d1, "lines1": lines1 if lines1 is not None else h1 * 3,
            "span": span, "last_ts": TS - 60, "last": last}


def _rep(sources=None, ts=TS):
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": TS,
            "extras": {"logerr": {"v": 1, "ts": ts, "logs": 40, "read": 7000000, "skipped": 0,
                                  "sources": sources if sources is not None else [_src("container shop-php", 3, 40)]}}}


def _srv(rep=None, mutes=None, state=None):
    return Server(id=1, name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_spike_rules():
    # мелочь не шлем: меньше 200 за час, даже если обычно ноль
    assert le.spike(_src("container a", 150, 150)) == 0
    # обычный час: сутки без последнего часа на 23 часа
    assert le.base(_src("container a", 300, 300 + 23 * 10)) == 10
    assert le.spike(_src("container a", 300, 300 + 23 * 10)) == 1
    # лог, где ошибок всегда много, не алертит, пока их не стало в разы больше
    assert le.spike(_src("container a", 600, 600 + 23 * 500)) == 0
    assert le.spike(_src("container a", 2600, 2600 + 23 * 500)) == 1
    # в 20 раз больше обычного и от тысячи - проблема
    assert le.spike(_src("container a", 4000, 4000 + 23 * 100)) == 2
    assert le.spike(_src("container a", 900, 900 + 23 * 10)) == 1
    # истории меньше 4 часов: обычного не знаем, нужно 20000 - постоянный поток ошибок без истории
    # не отличить от всплеска (ZooKeeper, который всегда сыпет BindException)
    assert le.base(_src("container a", 1500, 1500, span=3 * 3600)) is None
    assert le.spike(_src("container a", 2054, 2054, span=3 * 3600)) == 0
    assert le.spike(_src("container a", 25000, 25000, span=3 * 3600)) == 1
    # после ушедшего алерта отбой при половине порогов
    assert le.spike(_src("container a", 150, 150 + 23 * 10)) == 0
    assert le.spike(_src("container a", 150, 150 + 23 * 10), held=True) == 1
    assert le.spike(_src("container a", 90, 90 + 23 * 10), held=True) == 0


def test_problems_text_mute_and_home():
    rep = _rep([_src("container shop-php", 1240, 1240 + 23 * 30, last="PHP Fatal error: Uncaught PDOException password=***"),
                _src("pod shop/web-0", 40, 400, container="php")])
    s = _srv(rep)
    p = le.problems(s, NOW)
    assert [x["key"] for x in p] == ["контейнер shop-php|"]
    assert p[0]["text"] == ("ошибки в логе: контейнер shop-php, 1 240 строк с ошибками за час, обычно около 30 в час\n"
                            "↳ PHP Fatal error: Uncaught PDOException password=***")
    rows = [x for x in collector.server_problems(s, NOW) if x["kind"] == "log_errors"]
    assert [r["level"] for r in rows] == [2] and rows[0]["mute"] == "logerr:контейнер shop-php|"
    # заглушить один лог: насовсем и на время
    s.alert_mutes = ["logerr:контейнер shop-php|"]
    assert le.problems(s, NOW) == []
    s.alert_mutes = []
    s.alert_snoozes = {"logerr:контейнер shop-php|": (NOW + timedelta(days=1)).isoformat()}
    assert le.problems(s, NOW) == []
    v = le.view(s, NOW)
    a, b = v["sources"]
    assert a["mute"] == {"until": (NOW + timedelta(days=1)).isoformat()} and a["level"] == 2 and a["base"] == 30
    assert b["label"] == "под shop/web-0 (php)" and b["key"] == "под shop/web-0|php" and "level" not in b
    # срок снуза вышел - снова в беде
    s.alert_snoozes = {"logerr:контейнер shop-php|": (NOW - timedelta(minutes=1)).isoformat()}
    assert len(le.problems(s, NOW)) == 1


def test_no_history_says_so():
    s = _srv(_rep([_src("container a", 25000, 25000, span=1800)]))
    assert le.problems(s, NOW)[0]["why"] == "25 000 строк с ошибками за час, обычного еще не знаем: истории меньше 4 часов"
    assert le.why(_src("container a", 300, 300)) == "300 строк с ошибками за час, обычно их почти нет"


def test_stale_block_is_not_judged():
    s = _srv(_rep([_src("container a", 5000, 5000)], ts=TS - 3600))
    assert le.problems(s, NOW) == [] and le.view(s, NOW) is None


def test_held_comes_from_the_sent_alert():
    x = _src("container a", 150, 150 + 23 * 10)
    assert [p["level"] for p in le.problems(_srv(_rep([x]), state={"log_errors_sig": ["контейнер a|"]}), NOW)] == [1]
    assert le.problems(_srv(_rep([x])), NOW) == []


def test_snooze_accepts_a_log_key():
    from app.schemas import AlertSnoozeIn
    k = "logerr:под shop/web-0|php"
    assert AlertSnoozeIn(kind=k, hours=24).kind == k


def test_own_unit_does_not_alert():
    assert collector.own_helper_unit("kervax-logerr.service")


def test_alert_condition():
    rep = _rep([_src("container a", 4000, 4000 + 23 * 100), _src("container b", 300, 300 + 23 * 10)])
    lv, ctx = collector._server_conditions(_srv(rep), NOW)["log_errors"]
    assert lv == 2 and ctx["sig"] == ["контейнер a|", "контейнер b|"]
    assert ctx["detail"].startswith("ошибки в логе: контейнер a, 4 000 строк с ошибками за час")
    assert collector._server_conditions(_srv(_rep()), NOW)["log_errors"][0] == 0


def test_helper_script():
    src = (Path(__file__).resolve().parents[2] / "agent" / "logerr-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.2" in src and "KERVAX_SETUP_ALWAYS=1" in src and "\r" not in src
    # 0.2: строка стека (ClickHouse "3. DB::Exception... @ 0x", Java "at ...(", PHP "#3 ", "Caused by:") - не ошибка
    assert "if (l ~ RE && msg(l) !~ FR) { errs++; last = l }" in src and "@ 0x[0-9a-fA-F]+" in src
    # обратный слэш в строке лога удваивается так, что mawk его не теряет: иначе отчет не JSON
    assert '_jn = split(s, _jp, "\\\\")' in src
    # смещения и размеры больше 2 ГиБ: mawk пишет 7.1983e+09 в строке и режет %d на 2^31
    assert 'sprintf("%.0f", from + 1)' in src and '%.0f\\n", f, no[f], nf[f]' in src
    # новый лог - с конца, обрезанный или сменившийся - с начала, читать не больше CAP за раз
    assert "a new log: from its end" in src and "CAP=" in src and "BUDGET=" in src
    # логи подов тоже, контейнеры кубера под dockershim - только оттуда
    assert "/var/log/pods" in src and "k8s_" in src


def test_dbstat_asks_as_postgres_user_first():
    # 0.13: в контейнере, где суперпользователь задан POSTGRES_USER, роли postgres нет: вход под ней
    # оставлял в логе базы десятки FATAL каждые 15 минут
    src = (Path(__file__).resolve().parents[2] / "agent" / "dbstat-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.13" in src
    assert src.count('if [ -n "$u" ] && [ "$u" != postgres ]; then') == 2
    assert src.count("case $? in 2|12[5-7])") == 4
