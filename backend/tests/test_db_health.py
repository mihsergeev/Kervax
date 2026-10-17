"""Здоровье баз (helper dbstat-setup 0.5): реплика, долгие транзакции, блокировки, wraparound,
таблицы без VACUUM."""
from datetime import datetime, timezone
from pathlib import Path

from app import collector, db_health
from app.models import Server

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
T = int(NOW.timestamp())


def _pg(**kw):
    x = {"engine": "pg", "container": "pg", "role": "primary", "replicas": 1, "lag": 0, "receiver": "",
         "xact": 0, "xact_db": "", "xact_app": "", "idle_xact": 0, "idle_db": "", "idle_app": "",
         "lock_waits": 0, "lock_wait": 0, "xid": 5_000_000, "xid_db": "postgres", "dead": []}
    x.update(kw)
    return x


def _my(**kw):
    x = {"engine": "mysql", "container": "", "role": "replica", "io": "Yes", "sql": "Yes", "lag": 0, "err": "",
         "trx": 0, "xact": 0, "lock_waits": 0, "lock_wait": 0}
    x.update(kw)
    return x


def _rep(*items, ts=T):
    return {"extras": {"db-health": {"v": 1, "ts": ts, "items": list(items)}}}


def _srv(rep):
    return Server(id=1, name="n1", token_hash="x", enabled=True, backup_not_required=True, last_seen=NOW,
                  offline_after_seconds=120, alert_sustain_seconds=900, last_report=rep,
                  alert_state={}, alert_mutes=[], cpu_alert_percent=0, mem_alert_percent=0,
                  disk_warn_percent=0, disk_alert_percent=0, disk_crit_percent=0, temp_alert_c=0,
                  conntrack_alert_percent=0, disk_temp_alert_c=0, db_conn_alert_percent=0,
                  web_5xx_alert_percent=0, kube_expiry_warn_days=[])


def test_healthy_databases_are_quiet():
    assert db_health.problems(_rep(_pg(), _my()), NOW) == []
    assert db_health.problems(_rep(_pg(xact=7200), ts=T - 7200), NOW) == []  # helper давно молчит - не судим
    # ошибка docker exec вместо ответа базы (postgres-exporter без psql) - не база
    junk = _pg(container="postgres-exporter", xact_app='OCI runtime exec failed: exec failed: "psql": '
               'executable file not found in $PATH: unknown')
    assert db_health.view(_rep(_pg(), junk), NOW)[0]["container"] == "pg" and len(db_health.view(_rep(_pg(), junk), NOW)) == 1


def test_replication():
    assert db_health.problems(_rep(_pg(role="replica", receiver="none", lag=40)), NOW) == [
        (2, "PostgreSQL pg: реплика не получает WAL от основной (статус none)", "pg:pg:stream")]
    assert db_health.problems(_rep(_pg(role="replica", receiver="streaming", lag=600)), NOW) == [
        (1, "PostgreSQL pg: реплика отстает на 10 мин", "pg:pg:lag")]
    assert db_health.problems(_rep(_pg(lag=2000)), NOW)[0][:2] == (
        2, "PostgreSQL pg: реплика отстает от основной на 33 мин")
    assert db_health.problems(_rep(_my(sql="No", err="Error 1062: Duplicate entry")), NOW) == [
        (2, "MySQL на хосте: репликация стоит (IO Yes, SQL No): Error 1062: Duplicate entry", "mysql::stream")]
    assert db_health.problems(_rep(_my(lag=400)), NOW)[0][:2] == (1, "MySQL на хосте: реплика отстает на 6 мин")


def test_transactions_locks_wraparound_vacuum():
    rep = _rep(_pg(xact=8000, xact_db="shop", xact_app="report-job", idle_xact=2400, idle_db="shop", idle_app="api",
                   lock_waits=3, lock_wait=420, xid=1_200_000_000, xid_db="shop",
                   dead=[{"db": "shop", "table": "events", "dead": 5_400_000, "live": 9_000_000, "vac": -1},
                         {"db": "shop", "table": "fresh", "dead": 2_000_000, "live": 3_000_000, "vac": 3600}]))
    assert [t for _l, t, _k in db_health.problems(rep, NOW)] == [
        "PostgreSQL pg: транзакция идет 2 ч 13 мин (база shop, report-job)",
        "PostgreSQL pg: ждут блокировку 3 запр., дольше всех 7 мин, а транзакция простаивает 40 мин "
        "(база shop, api) - держит, скорее всего, она",
        "PostgreSQL pg: возраст транзакций базы shop 1.20 млрд, на 2.1 млрд PostgreSQL остановит запись - "
        "автоочистка не успевает замораживать строки",
        "PostgreSQL pg: в shop.events 5.4 млн мертвых строк, 60% от живых, VACUUM не было ни разу"]
    assert db_health.problems(_rep(_pg(xact=7 * 3600)), NOW)[0][0] == 2
    assert db_health.problems(_rep(_pg(xid=1_600_000_000)), NOW)[0][0] == 2


def test_alert_home_and_view():
    s = _srv(_rep(_pg(role="replica", receiver="none"), _my(xact=4000)))
    lvl, ctx = collector._server_conditions(s, NOW)["db_health"]
    assert lvl == 2 and ctx["sig"] == ["pg:pg:stream", "mysql::xact"]
    rows = [(p["level"], p.get("section")) for p in collector.server_problems(s, NOW) if p["kind"] == "db_health"]
    assert rows == [(2, "services"), (1, "services")]
    v = db_health.view(s.last_report, NOW)
    assert v[0]["role"] == "replica" and v[0]["problems"][0]["level"] == 2 and v[1]["problems"][0]["level"] == 1
    assert collector._server_conditions(_srv(_rep(_pg())), NOW)["db_health"][0] == 0
    assert "db_health" not in collector._server_conditions(_srv({}), NOW)


def test_helper_writes_health():
    src = (Path(__file__).resolve().parents[2] / "agent" / "dbstat-setup.sh").read_text(encoding="utf-8")
    assert "KERVAX_SETUP_VERSION=0.9" in src and "health_pg()" in src and "health_mysql()" in src
    # 0.7: без роли postgres - под пользователем контейнера в явную базу, а не в базу с его именем
    assert 'psql -d "${d:-postgres}" -Atqc "$q" ${u:+-U "$u"}' in src
    assert "backend_xmin is not null" in src and "idle_xmin" in src
    assert "report.d/db-health.json" in src and "pid <> pg_backend_pid()" in src
    assert 'case "$v" in [0-9]*) ;; *) return 0 ;; esac' in src and 'case "$rec" in t|f)' in src
    assert "\r" not in src


def test_vacuum_only_for_a_transaction_holding_a_snapshot():
    """Две простаивающие: старшая держит advisory lock без снимка, младшая после SELECT держит
    снимок. VACUUM мешает только вторая"""
    rep = _rep(_pg(idle_xact=27600, idle_db="shop", idle_app="worker", idle_xmin=26880, idle_xmin_db="shop",
                   idle_xmin_app="worker"))
    assert [t for _l, t, _k in db_health.problems(rep, NOW)] == [
        "PostgreSQL pg: транзакция открыта и простаивает 7 ч 28 мин (база shop, worker): держит снимок, "
        "VACUUM не может чистить мертвые строки"]
    # без снимка и без ждущих она никому не мешает: воркер сутками держит один advisory lock
    lock_only = _rep(_pg(idle_xact=71332, idle_db="shop", idle_app="worker"))
    assert db_health.problems(lock_only, NOW) == []
    # одна и та же транзакция - одна строка, про VACUUM
    one = _rep(_pg(idle_xact=4000, idle_xmin=4000))
    assert [k for _l, _t, k in db_health.problems(one, NOW)] == ["pg:pg:idle"]


def test_a_dump_holds_its_transaction_by_design():
    """Ночной pg_dump большой базы шел четыре часа и давал алерт "транзакция идет 1 ч": дамп
    держит одну транзакцию от начала до конца. Про него - только если идет больше 12 часов."""
    from datetime import datetime, timezone
    from app import db_health
    now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)

    def rep(xact, app):
        return {"extras": {"db-health": {"v": 1, "ts": now.timestamp(), "items": [
            {"engine": "pg", "container": "pg", "role": "primary", "xact": xact, "xact_db": "feed", "xact_app": app}]}}}
    assert db_health.problems(rep(4 * 3600, "pg_dump"), now) == []
    assert db_health.problems(rep(4 * 3600, "pg_basebackup"), now) == []
    assert [k for _l, _t, k in db_health.problems(rep(13 * 3600, "pg_dump"), now)] == ["pg:pg:xact"]
    assert [k for _l, _t, k in db_health.problems(rep(2 * 3600, "worker"), now)] == ["pg:pg:xact"]


def test_helper_collects_load():
    """0.8: что грузит PostgreSQL - снимки счетчиков, тексты только для топа и из кэша"""
    src = (Path(__file__).resolve().parents[2] / "agent" / "dbstat-setup.sh").read_text(encoding="utf-8")
    assert "load_pg()" in src and 'ld=$(load_pg "$c")' in src and "LOAD_DIR=/var/lib/kervax/db-load" in src
    # без текстов при снимке: файл текстов на одной ноде был 3.9 ГБ и читается целиком
    assert "from pg_stat_statements(false)" in src and "pg_stat_statements(true)" in src
    assert "QTEXT_BIG=1073741824" in src and "pgss_query_texts.stat" in src
    # тексты с паролями и секретами не уходят
    assert "~* '(passw|secret|token|identified|pgp_sym|encrypt)' then ''" in src
    # пустой первый файл: FNR == NR ловил строки второго файла как первого, топ выходил пустым
    assert "FNR == NR" not in src and src.count("FILENAME == ARGV[1]") == 3
    # отчет больше 256 КБ агент выбрасывает целиком - бюджет на топы
    assert "LOAD_BUDGET=131072" in src and '\\"cut\\":1}' in src


def test_load_view_per_hour_and_merged_top():
    ld = {"win": 1800, "pss": "on", "qtext": 3_900_000_000, "temp_b": 1000, "temp_f": 3, "xact": 18000, "rd": 50,
          "wal_rec": 1000, "fpi": 40, "wal_b": 2000, "wbf": 10, "ck_t": 1, "ck_r": 0, "ms_all": 1000,
          "top": [
              {"id": "1", "db": "shop", "q": "select * from t order by id offset $1", "calls": 2, "ms": 300, "temp": 100, "rd": 0},
              {"id": "-2", "db": "shop", "q": "select * from t order by id offset $1", "calls": 4, "ms": 200, "temp": 50, "rd": 0},
              {"id": "3", "db": "shop", "q": "", "calls": 1, "ms": 400, "temp": 0, "rd": 8},
              "junk"]}
    v = db_health.view(_rep(_pg(load=ld)), NOW)[0]["load"]
    assert v["win"] == 1800 and v["temp_b"] == 2000 and v["xact"] == 36000 and v["ck_r"] == 0 and v["ms_all"] == 2000
    # один текст в одной базе - одна строка (у PostgreSQL отдельная запись на роль и типы параметров)
    assert [(r["q"][:6], r["calls"], r["ms"], r["share"]) for r in v["top"]] == [("select", 12, 1000, 50), ("", 2, 800, 40)]
    assert v["top"][0]["avg_ms"] == 83.3 and v["top"][0]["temp"] == 300
    assert v["qtext"] == 3_900_000_000 and v["cut"] is False


def test_load_view_old_versions_and_first_run():
    # первый прогон: снимков еще нет
    assert db_health.view(_rep(_pg(load={"win": 0, "pss": "noext", "qtext": 0})), NOW)[0]["load"] == {
        "win": 0, "pss": "noext", "qtext": 0, "cut": False, "top": []}
    # PostgreSQL до 14 не считает WAL (-1), helper 0.7 вообще без load
    v = db_health.view(_rep(_pg(load={"win": 3600, "pss": "off", "qtext": 0, "wal_b": -1, "temp_b": 0})), NOW)[0]["load"]
    assert v["wal_b"] is None and v["temp_b"] == 0 and v["top"] == []
    assert db_health.view(_rep(_pg()), NOW)[0]["load"] is None
    # нагрузка - не проблема: ни строк, ни алерта
    assert db_health.problems(_rep(_pg(load={"win": 3600, "pss": "on", "temp_b": 10**13, "ms_all": 10**9})), NOW) == []


G = 1 << 30
M = 1 << 20


def _cfg(**kw):
    c = {"v": 180000, "sb": 32 * G, "wb": 32 * M, "mws": 64 * G, "wm": 40 * M, "mwm": 2 * G, "ecs": 90 * G, "ckt": 1800,
         "cct": 0.9, "mc": 1000, "walc": "lz4", "hp": "try", "hps": "off", "hpn": 17119, "rpc": 1.1, "mem": 125 * G,
         "mem_lim": False, "hpt": 0, "hpsz": 2 * M, "pt": 2_600_000_000, "stop_to": -1, "stop_sig": "SIGINT"}
    c.update(kw)
    return c


def test_helper_reports_settings():
    """0.9: настройки и то, чего из базы не видно: память, huge pages ядра, таймаут остановки docker"""
    src = (Path(__file__).resolve().parents[2] / "agent" / "dbstat-setup.sh").read_text(encoding="utf-8")
    assert "cfg_pg()" in src and 'cf=$(cfg_pg "$c")' in src and "pg_size_bytes(current_setting('shared_buffers'))" in src
    assert "current_setting('huge_pages_status', true)" in src and "/^HugePages_Total:/" in src
    assert "{{.Config.StopTimeout}}" in src and "{{.HostConfig.Memory}}" in src


def test_advice_for_a_busy_feed_database():
    """Как было на одной ноде: 32 ГБ буферов без huge pages, буфер WAL переполняется, временные файлы
    терабайтами, docker ждет при остановке 10 секунд"""
    lv = {"win": 3600, "wal_b": 40 * G, "wal_rec": 277_000_000, "fpi": 2_900_000, "ck_t": 2, "ck_r": 0, "wbf": 150_000,
          "temp_b": 2000 * G}
    adv = db_health.advice({"container": "postgresql", "cfg": _cfg()}, lv, ssd=True, db_size=300 * G)
    by = {a["k"]: a for a in adv}
    assert list(by) == ["hugepages", "wal_buffers", "stop_timeout", "temp"]
    assert by["hugepages"]["need"] == 17119 and by["hugepages"]["have"] == 0
    assert by["wal_buffers"] == {"k": "wal_buffers", "cur": 32 * M, "n": 150_000}
    assert by["stop_timeout"]["to"] == 10 and by["temp"]["mc"] == 1000
    # после правок советов нет: huge pages взяты, wal_buffers 128 МБ, контейнеру дали 10 минут
    done = _cfg(hps="on", hpt=17200, wb=128 * M, stop_to=600)
    assert [a["k"] for a in db_health.advice({"container": "postgresql", "cfg": done}, lv, ssd=True, db_size=300 * G)] == ["temp"]


def test_advice_for_defaults_on_a_big_machine():
    lv = {"win": 3600, "wal_b": 8 * G, "wal_rec": 1000, "fpi": 10, "ck_t": 4, "ck_r": 6, "wbf": 0, "temp_b": 0}
    cfg = _cfg(sb=128 * M, mem=64 * G, ecs=4 * G, rpc=4, walc="pglz", v=160000, mws=G, ckt=300, stop_to=30,
               stop_sig="SIGTERM", hps="", hpn=0)
    by = {a["k"]: a for a in db_health.advice({"container": "pg", "cfg": cfg}, lv, ssd=True, db_size=50 * G)}
    assert list(by) == ["max_wal_size", "wal_compression", "stop_timeout", "random_page_cost", "effective_cache_size",
                        "shared_buffers_low"]
    # 8 ГБ WAL в час, цикл 5 минут, чекпоинт по объему на max_wal_size / 1.9: хватит 2 ГБ (не меньше двойного)
    assert by["max_wal_size"]["rec"] == 2 * G and by["stop_timeout"]["sig"] == "SIGTERM"
    # несколько инстансов на машине - проценты от памяти не советуем; на HDD и маленькой базе - random_page_cost
    keys = [a["k"] for a in db_health.advice({"container": "pg", "cfg": cfg}, lv, pg_count=2, ssd=False, db_size=50 * G)]
    assert "effective_cache_size" not in keys and "shared_buffers_low" not in keys and "random_page_cost" not in keys
    # PostgreSQL до 15: lz4 нет, pglz не трогаем; на хосте (не в docker) таймаута остановки нет
    keys = [a["k"] for a in db_health.advice({"container": "", "cfg": _cfg(v=140000, walc="on", hps="", hpt=17200)}, lv)]
    assert "wal_compression" not in keys and "stop_timeout" not in keys and "hugepages" not in keys
    # внеплановые чекпоинты при 48 МБ WAL в час - не от объема WAL, max_wal_size ни при чем
    small = dict(lv, wal_b=48 * M, ck_t=4, ck_r=8)
    assert "max_wal_size" not in [a["k"] for a in db_health.advice({"container": "pg", "cfg": cfg}, small)]
    assert db_health.advice({"container": "pg"}, lv) == []


def test_advice_in_view_uses_disks_and_db_sizes():
    item = _pg(container="pg", cfg=_cfg(sb=128 * M, mem=64 * G, ecs=4 * G, rpc=4, stop_to=600, hps="", hpn=0))
    rep = _rep(item)
    rep["db_stats"] = [{"engine": "pg", "container": "pg", "dbs": [{"name": "shop", "size": 40 * G}]}]
    rep["extras"]["disk-health"] = {"disks": [{"dev": "nvme0n1", "rota": 0}, {"dev": "nvme1n1", "rota": 0}]}
    keys = [a["k"] for a in db_health.view(rep, NOW)[0]["advice"]]
    assert keys == ["random_page_cost", "effective_cache_size", "shared_buffers_low"]
    rep["extras"]["disk-health"]["disks"][1]["rota"] = 1  # есть HDD - про random_page_cost молчим
    assert "random_page_cost" not in [a["k"] for a in db_health.view(rep, NOW)[0]["advice"]]
    assert db_health.view(_rep(_my()), NOW)[0]["advice"] == []
