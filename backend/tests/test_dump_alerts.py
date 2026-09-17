"""Сломанный дамп на ноде без файлового бэкапа — алерт и предупреждение в карточке.

Живой случай (17.09.2026, kz-se-op-dtp): дамп ClickHouse включён, а restic на ноде нет —
helper снимает дамп своим таймером раз в сутки. Проверка «дамп не снимается» шла только от
времени последнего бэкапа, поэтому на такой ноде сломанный дамп не заметил бы никто: ни
алерта, ни предупреждения в карточке.
"""

from datetime import datetime, timedelta, timezone

from app import collector, settings_store
from app.api.servers import _backup_coverage
from app.models import Server

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())
H = 3600
TARGET = "k8s.default.sts.chi-clickhouse-main-0-0"
PODS = [{"ns": "default", "name": "chi-clickhouse-main-0-0-0", "phase": "Running",
         "owner": "StatefulSet", "image": "clickhouse/clickhouse-server:26.7.3.19"}]


def _dump(**kw):
    d = {"engine": "ch", "container": TARGET, "files": 1, "size_bytes": 3292,
         "last_ts": T - 2 * H, "keep": 2, "min_free_pct": 10, "skipped": False,
         "enabled_ts": T - 20 * H, "dir": "/backup/ch/" + TARGET}
    d.update(kw)
    return d


def _server(dumps, backup=None, state=None):
    bk = {"present": True, "manageable": True, "configured": False, "dumps": dumps}
    bk.update(backup or {})
    rep = {"agent_version": "2.9", "backup": bk, "kube": {"pods": PODS},
           "setup_versions": {"backup-setup": "0.28"},
           "extras": {"kube-dumps": {"v": 1, "ctl": "k0s", "exec": True}}}
    return Server(name="kz-se-op-dtp", token_hash="x", enabled=True, last_seen=NOW,
                  offline_after_seconds=120, last_report=rep, alert_state=state or {},
                  backup_not_required=True, created_at=NOW - timedelta(days=90))


def _cond(server, key="backup_dump"):
    return collector._server_conditions(server, NOW).get(key)


# состояние «условие держится час» — чтобы sustain выдал уровень, а не только отсчёт
HELD = {"backup_dump_since": (NOW - timedelta(hours=1)).isoformat()}


def test_local_dump_is_checked_by_the_clock():
    assert collector.dump_local_stale(_dump(), T) is False
    # одиночный пропуск ночи: следующей ночью наверстается — не будим
    assert collector.dump_local_stale(_dump(last_ts=T - 48 * H - 20 * 60), T) is False
    # две ночи подряд без файла — сломан
    assert collector.dump_local_stale(_dump(last_ts=T - 50 * H, enabled_ts=T - 90 * H), T) is True
    # включили вчера вечером — первый дамп ещё впереди
    assert collector.dump_local_stale(_dump(files=0, last_ts=0, enabled_ts=T - 20 * H), T) is False
    # включили три дня назад, а файла так и нет
    assert collector.dump_local_stale(_dump(files=0, last_ts=0, enabled_ts=T - 72 * H), T) is True
    # перенастроили (скрипт переписан) — отсрочка заново
    assert collector.dump_local_stale(_dump(last_ts=T - 80 * H, enabled_ts=T - 2 * H), T) is False
    # пропуск из-за места — свой алерт, не «сломан»
    assert collector.dump_local_stale(_dump(last_ts=T - 80 * H, enabled_ts=T - 90 * H, skipped=True), T) is False
    # старый helper без enabled_ts и без файлов: судить не по чему
    assert collector.dump_local_stale(_dump(files=0, last_ts=0, enabled_ts=0), T) is False


def test_alert_on_a_node_without_a_file_backup():
    fresh = _cond(_server([_dump()], state=HELD))
    assert fresh[0] == 0

    stale = _server([_dump(last_ts=T - 60 * H, enabled_ts=T - 90 * H)], state=HELD)
    lvl, ctx = _cond(stale)
    assert lvl == 1 and ctx["engines"] == f"ch@{TARGET}" and ctx["n"] == 1
    assert ctx["reason"] == collector._DUMP_REASON_LOCAL
    # без удержания — только отсчёт, не алерт (sustain, как у остальных условий)
    assert _cond(_server([_dump(last_ts=T - 60 * H, enabled_ts=T - 90 * H)]))[0] == 0

    # мало места — отдельный алерт, а «дамп не снимается» молчит
    space = _server([_dump(last_ts=T - 60 * H, enabled_ts=T - 90 * H, skipped=True,
                           skip_free_pct=4)], state=HELD)
    assert _cond(space)[0] == 0
    assert _cond(space, "backup_dump_space") == (1, {"engines": f"ch@{TARGET}", "free": 4})


def test_alert_closes_when_dumps_are_turned_off():
    # алерт висел, дампы выключили: условие обязано прийти с нулём, иначе не закроется
    s = _server([], state={"backup_dump": 1, **HELD})
    assert _cond(s)[0] == 0
    # нода без бэкапа и без дампов, алертов не было — условий нет вовсе, шума нет
    s = _server([])
    assert _cond(s) is None and _cond(s, "backup_dump_space") is None


def test_node_with_a_backup_keeps_its_check():
    # бэкап идёт (метрика restic), а дамп отстал больше чем на двое суток — как раньше
    bk = {"configured": True, "metric_present": True, "success": 1, "last_backup_ts": T - H}
    lvl, ctx = _cond(_server([_dump(last_ts=T - 60 * H)], backup=bk, state=HELD))
    assert lvl == 1 and ctx["reason"] == collector._DUMP_REASON_BACKUP
    # свежий дамп при идущем бэкапе — норма
    assert _cond(_server([_dump()], backup=bk, state=HELD))[0] == 0


def test_alert_text():
    default = settings_store.SERVER_ALERT_KINDS["backup_dump"][1]
    s = _server([])
    ctx = {"engines": f"ch@{TARGET}", "n": 1, "reason": collector._DUMP_REASON_LOCAL}
    assert collector._fmt_rule(default, s, ctx) == (
        f"дамп не обновляется: ch@{TARGET} (свежего дампа нет больше двух суток, "
        "а файлового бэкапа на ноде нет)"
    )
    # у кого в правилах сохранён прежний дефолт, получит новый текст, а не старый шаблон
    assert ("дамп не обновляется, хотя бэкап идёт: {engines} (файловый снапшот живой базы "
            "может не восстановиться)") in settings_store.LEGACY_SERVER_DEFAULTS


def test_card_warns_about_a_stale_local_dump():
    def ch(server):
        return next(a for a in _backup_coverage(server) if a.subject == "ClickHouse")

    ok = ch(_server([_dump()]))
    assert ok.kind == "db_ok" and "по своему таймеру" in ok.detail

    stale = ch(_server([_dump(last_ts=T - 60 * H, enabled_ts=T - 90 * H)]))
    assert stale.kind == "db" and "свежего файла нет больше двух суток" in stale.detail
    assert stale.can_dump and stale.dump_engine == "ch"

    # отсчёт от последнего отчёта ноды: замолчавшая нода — не «сломанный дамп»
    quiet = _server([_dump(last_ts=T - 60 * H, enabled_ts=T - 90 * H)])
    quiet.last_seen = NOW - timedelta(hours=40)
    assert ch(quiet).kind == "db_ok"
