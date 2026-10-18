"""PHP-FPM (fpm_pools, helper fpm-setup): пул подолгу упирается в pm.max_children или рвет запросы по
таймауту, мьют, "Что сломано", алерт, раздел."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import collector, fpm_pools as fp
from app.models import Server

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
TS = int(NOW.timestamp())


def _pool(where, pool="www", mx=5, sat=0, reached=0, tout=0, slow=0, **kw):
    p = {"where": where, "pool": pool, "listen": "9000", "pm": "dynamic", "max": mx, "children": 3, "conns": 0,
         "queue": 0, "sat": sat, "span": 86400, "reached": reached, "reached_d": reached * 10, "slow": slow,
         "slow_d": slow * 10, "tout": tout, "tout_d": tout * 10, "slowlog": 0, "terminate": 0, "status": 0}
    p.update(kw)
    return p


def _rep(pools=None, ts=TS):
    return {"mem_total": 8 * 2**30, "mem_used": 2**30, "clock_unix": TS,
            "extras": {"fpm": {"v": 1, "ts": ts, "skipped": [],
                               "pools": pools if pools is not None else [_pool("container shop-php")]}}}


def _srv(rep=None, mutes=None, state=None):
    return Server(id=1, name="web-a", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep or _rep(),
                  alert_state=state or {}, alert_mutes=mutes or [], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_level_rules():
    # минута-другая очереди и редкие упоры - не беда
    assert fp.level(_pool("container a", sat=5, reached=10)) == 0
    # запросы ждали четверть часа из часа - предупреждение, три четверти - проблема
    assert fp.level(_pool("container a", sat=15)) == 1
    assert fp.level(_pool("container a", sat=45)) == 2
    # пул упирается в предел раз в минуту, хотя выборки очередь не застали
    assert fp.level(_pool("container a", reached=59)) == 0
    assert fp.level(_pool("container a", reached=60)) == 1
    # убитые по таймауту запросы - это 502 у пользователей
    assert fp.level(_pool("container a", tout=19)) == 0
    assert fp.level(_pool("container a", tout=20)) == 1
    assert fp.level(_pool("container a", tout=100)) == 2
    # медленные только показываются
    assert fp.level(_pool("container a", slow=5000)) == 0
    # после ушедшего алерта отбой при половине порогов
    assert fp.level(_pool("container a", sat=8)) == 0
    assert fp.level(_pool("container a", sat=8), held=True) == 1
    assert fp.level(_pool("container a", sat=7, reached=29, tout=9), held=True) == 0


def test_problems_text_mute_and_home():
    ex = "script '/app/index.php' (request: \"POST /api/pay?token=***\") execution timed out (30.01 sec), terminating"
    rep = _rep([_pool("container shop-php", sat=20, reached=80, tout=25, terminate=30, tout_last=ex),
                _pool("pod shop/web-0", pool="api", sat=2)])
    s = _srv(rep)
    p = fp.problems(s, NOW)
    assert [x["key"] for x in p] == ["контейнер shop-php|www"]
    assert p[0]["text"] == ("PHP-FPM контейнер shop-php, пул www: запросы ждали свободного воркера 20 мин из последнего"
                            " часа (воркеров 5), за час пул 80 раз уперся в pm.max_children = 5, запросов, убитых по"
                            " таймауту 30 с, за час: 25\n↳ " + ex)
    assert fp.why(_pool("container a", reached=62)) == "за час пул 62 раза уперся в pm.max_children = 5"
    assert fp.why(_pool("container a", tout=21)) == "запросов, убитых по таймауту, за час: 21"
    rows = [x for x in collector.server_problems(s, NOW) if x["kind"] == "fpm"]
    assert [r["level"] for r in rows] == [1] and rows[0]["mute"] == "fpm:контейнер shop-php|www"
    # заглушить один пул: насовсем и на время
    s.alert_mutes = ["fpm:контейнер shop-php|www"]
    assert fp.problems(s, NOW) == []
    s.alert_mutes = []
    s.alert_snoozes = {"fpm:контейнер shop-php|www": (NOW + timedelta(days=1)).isoformat()}
    assert fp.problems(s, NOW) == []
    v = fp.view(s, NOW)
    a, b = v["pools"]
    assert a["mute"] == {"until": (NOW + timedelta(days=1)).isoformat()} and a["level"] == 1 and a["tout_last"] == ex
    assert b["label"] == "под shop/web-0, пул api" and b["key"] == "под shop/web-0|api" and "level" not in b
    assert b["max"] == 5 and b["listen"] == "9000"
    # срок снуза вышел - снова в беде
    s.alert_snoozes = {"fpm:контейнер shop-php|www": (NOW - timedelta(minutes=1)).isoformat()}
    assert len(fp.problems(s, NOW)) == 1


def test_example_falls_back_to_slow():
    s = _srv(_rep([_pool("host", sat=50, slow_last="script '/x.php' (request: \"GET /x.php\") executing too slow (5.2 sec), logging")]))
    p = fp.problems(s, NOW)
    assert p[0]["level"] == 2 and p[0]["text"].endswith("\n↳ script '/x.php' (request: \"GET /x.php\") executing too slow (5.2 sec), logging")
    assert p[0]["label"] == "на хосте, пул www"


def test_stale_block_is_not_judged():
    s = _srv(_rep([_pool("container a", sat=60)], ts=TS - 3600))
    assert fp.problems(s, NOW) == [] and fp.view(s, NOW) is None


def test_held_comes_from_the_sent_alert():
    x = _pool("container a", sat=10)
    s = _srv(_rep([x]), state={"fpm_sig": ["контейнер a|www"]})
    assert [p["level"] for p in fp.problems(s, NOW)] == [1]
    assert fp.problems(s, NOW)[0]["why"] == "запросы ждали свободного воркера 10 мин из последнего часа (воркеров 5)"
    assert fp.problems(_srv(_rep([x])), NOW) == []


def test_snooze_accepts_a_pool_key():
    from app.schemas import AlertSnoozeIn
    k = "fpm:контейнер shop-php|www"
    assert AlertSnoozeIn(kind=k, hours=24).kind == k


def test_own_unit_does_not_alert():
    assert collector.own_helper_unit("kervax-fpm.service")


def test_alert_condition():
    rep = _rep([_pool("container a", sat=50), _pool("container b", tout=30)])
    lv, ctx = collector._server_conditions(_srv(rep), NOW)["fpm"]
    assert lv == 2 and ctx["sig"] == ["контейнер a|www", "контейнер b|www"]
    assert ctx["detail"].startswith("PHP-FPM контейнер a, пул www: запросы ждали свободного воркера 50 мин")
    assert collector._server_conditions(_srv(_rep()), NOW)["fpm"][0] == 0


def test_helper_script():
    src = (Path(__file__).resolve().parents[2] / "agent" / "fpm-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.1" in src and "KERVAX_SETUP_ALWAYS=1" in src and "\r" not in src
    # обратный слэш в строке лога удваивается так, что mawk его не теряет: иначе отчет не JSON
    assert '_jn = split(s, _jp, "\\\\")' in src
    # смещения больше 2 ГиБ: mawk пишет 7.1983e+09 в строке и режет %d на 2^31; strtonum в mawk нет
    assert 'sprintf("%.0f", from + 1)' in src and 'function hex(s,' in src and "strtonum" not in src
    # include разворачивается на своем месте: [www] после include перекрывает пулы из include
    assert 'for f in "$d"/${inc##*/}; do [ -f "$f" ] && { cat "$f"; echo; } >> "$TMPD/conf"; done' in src
    # путь из конфига контейнера не уводит за его корень и не попадает в команду как есть
    assert src.count("*..*|*[!A-Za-z0-9._/@+-]*") >= 2 and "elog !~ /\\.\\./" in src
    # конфиг с CRLF: имена секций без \r
    assert '{ gsub(/\\r/, "") }' in src
