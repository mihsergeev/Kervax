"""Здоровье баз (helper dbstat-setup 0.5): реплика, долгие транзакции, блокировки, wraparound,
таблицы без VACUUM."""
import re
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
    assert "KERVAX_SETUP_VERSION=0.17  # " in src and "health_pg() {" in src and "health_mysql()" in src
    # 0.7: без роли postgres - под пользователем контейнера в явную базу, а не в базу с его именем
    assert 'psql -d "${d:-postgres}" -Atqc "$q" ${u:+-U "$u"}' in src
    assert "backend_xmin is not null" in src and "idle_xmin" in src
    assert "report.d/db-health.json" in src and "pid <> pg_backend_pid()" in src
    assert 'case "$v" in [0-9]*) ;; *) pod_fail pg "$c"; return 0 ;; esac' in src and 'case "$rec" in t|f)' in src
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
    assert "load_pg()" in src and 'ld=$(load_pg "$c" "$nm")' in src and "LOAD_DIR=/var/lib/kervax/db-load" in src
    # без текстов при снимке: файл текстов на одной ноде был 3.9 ГБ и читается целиком
    assert "from pg_stat_statements(false)" in src and "pg_stat_statements(true)" in src
    assert "QTEXT_BIG=1073741824" in src and "pgss_query_texts.stat" in src
    # тексты с паролями и секретами не уходят
    assert "~* '(passw|secret|token|identified|pgp_sym|encrypt|credential|access.?key|api.?key|private.?key)' then ''" in src
    # пустой первый файл: FNR == NR ловил строки второго файла как первого, топ выходил пустым
    # четвертый - команды Redis (0.10), пятый и шестой - нагрузка и запросы MySQL (0.11)
    assert "FNR == NR" not in src and src.count("FILENAME == ARGV[1]") == 6
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
    # диски ВМ не в счет: rotational у них выставляет гипервизор, на ВМ тип дисков неизвестен
    rep["extras"]["disk-health"]["disks"] = [{"dev": "sda", "rota": 1, "err": "virtual"}]
    assert db_health._ssd(rep) is None
    rep["extras"]["disk-health"]["disks"].append({"dev": "nvme0n1", "rota": 0})
    assert db_health._ssd(rep) is True
    assert db_health.view(_rep(_my()), NOW)[0]["advice"] == []


# -- Как включить pg_stat_statements (dbstat-setup 0.14) ---------------------------
def test_helper_reports_how_to_enable_pgss():
    src = (Path(__file__).resolve().parents[2] / "agent" / "dbstat-setup.sh").read_text(encoding="utf-8")
    # откуда задан список: -c в команде контейнера ALTER SYSTEM не перебивает; отдельный запрос, а не в
    # общей строке настроек - не суперпользователю параметр не виден, и ошибка стоила бы всех настроек
    assert "coalesce((select source from pg_settings where name = 'shared_preload_libraries'), '')" in src
    assert "current_user, current_setting('is_superuser')" in src
    assert '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' in src
    # юнит хостового экземпляра - по cgroup postmaster'а
    assert '/postmaster.pid" 2>/dev/null)")' in src and "/proc/$pid/cgroup" in src
    # был ли в снимке часовой давности список запросов
    assert 'printf "S|%.0f|%d|%d\\n", all, reset, havep' in src and '"pbase":%s' in src


def _pgss(pss="off", container="app-db-1", **cf):
    return _pg(container=container, load={"win": 3600, "pss": pss, "qtext": 0}, cfg=_cfg(**cf))


def _inv(container="app-db-1", dbs=("app", "postgres"), users=("app",)):
    return [{"engine": "pg", "container": container, "dbs": [{"name": d, "size": 1} for d in dbs], "users": list(users)}]


def _fix(item, inv=None):
    rep = _rep(item)
    rep["db_stats"] = inv if inv is not None else _inv(item.get("container") or "")
    return db_health.view(rep, NOW)[0]["pgss_fix"]


def test_pgss_alter_appends_to_the_current_list():
    """Список дописывается, а не затирается, и каждая библиотека - своей строкой: 'a,b' одной строкой
    PostgreSQL принял бы за одну библиотеку с запятой в имени и не запустился бы"""
    fix = _fix(_pgss(role="app", su="on", spl_src="configuration file", spl="timescaledb, pg_cron"))
    assert [s["id"] for s in fix] == ["pgss_create", "pgss_alter", "pgss_restart", "pgss_check", "pgss_undo"]
    psql = "sudo docker exec app-db-1 psql -U app -d postgres"
    # расширение первым: работает и без загруженной библиотеки и проверяет, что она есть в сборке
    assert fix[0] == {"id": "pgss_create", "arg": "postgres", "cmd": f'{psql} -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"'}
    assert fix[1]["cmd"] == f"{psql} -c \"ALTER SYSTEM SET shared_preload_libraries = 'timescaledb', 'pg_cron', 'pg_stat_statements'\""
    assert fix[1]["arg"] == "timescaledb, pg_cron"
    assert fix[2]["cmd"] == "sudo docker restart -t 120 app-db-1"
    assert fix[3]["cmd"] == f'{psql} -c "SHOW shared_preload_libraries" -c "SELECT count(*) FROM pg_stat_statements"'
    # откат - прежний список, а не RESET: он мог быть задан тем же ALTER SYSTEM
    assert fix[4]["undo"] and fix[4]["cmd"] == (
        f"{psql} -c \"DROP EXTENSION IF EXISTS pg_stat_statements\" -c \"ALTER SYSTEM SET shared_preload_libraries = 'timescaledb', 'pg_cron'\"\n"
        "sudo docker restart -t 120 app-db-1")
    # пустой список: откат - RESET; роль postgres - системным пользователем postgres, как ходит helper
    fix = _fix(_pgss(role="postgres", su="on", spl_src="default", spl=""))
    assert fix[1]["cmd"] == "sudo docker exec -u postgres app-db-1 psql -d postgres -c \"ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements'\""
    assert fix[4]["cmd"].endswith('-c "ALTER SYSTEM RESET shared_preload_libraries"\nsudo docker restart -t 120 app-db-1')


def test_pgss_in_container_command_edits_compose():
    """-c shared_preload_libraries в command: ALTER SYSTEM его не перебьет, правится compose с копией"""
    cf = dict(role="app", su="on", spl_src="command line", spl="timescaledb", cproj="app", csvc="db",
              cfiles="/srv/app/compose.yml", cdir="/srv/app")
    fix = _fix(_pgss(**cf))
    assert [s["id"] for s in fix] == ["pgss_create", "pgss_compose", "pgss_compose_up", "pgss_check", "pgss_undo_compose"]
    up = "sudo docker compose -p app -f compose.yml up -d --no-deps -t 120 db"
    assert fix[1]["arg"] == "db" and fix[1]["cmd"].split("\n") == [
        "cd /srv/app", "sudo cp -p compose.yml compose.yml.bak-kervax",
        "sudo sed -i -E 's/(shared_preload_libraries=[^A-Za-z0-9_]?timescaledb)([^A-Za-z0-9_,]|$)/\\1,pg_stat_statements\\2/' compose.yml",
        "diff compose.yml.bak-kervax compose.yml",
        "grep -qE 'shared_preload_libraries=[^[:space:]]*pg_stat_statements' compose.yml"
        ' && echo "pg_stat_statements добавлен" || echo "НЕ ДОБАВЛЕНО: строка shared_preload_libraries другая, поправьте руками"']
    assert fix[2]["cmd"] == f"cd /srv/app\n{up}"
    assert fix[4]["cmd"].split("\n")[1:] == ["cd /srv/app", "sudo cp -p compose.yml.bak-kervax compose.yml", up]
    assert "ALTER SYSTEM" not in " ".join(s["cmd"] for s in fix)
    # несколько файлов (или имя, которое в sed не подставить) - найти строку и поправить руками
    fix = _fix(_pgss(**{**cf, "cfiles": "/srv/app/compose.yml,/srv/app/compose.prod.yml"}))
    assert fix[1] == {"id": "pgss_compose_grep", "arg": "timescaledb,pg_stat_statements",
                      "cmd": "cd /srv/app\ngrep -n shared_preload_libraries compose.yml compose.prod.yml"}
    assert fix[2]["cmd"].endswith("-f compose.yml -f compose.prod.yml up -d --no-deps -t 120 db")
    # не из compose: только показать команду контейнера, пересоздать его панель не подскажет
    fix = _fix(_pgss(**{**cf, "cproj": "", "cfiles": "", "cdir": "", "csvc": ""}))
    assert [s["id"] for s in fix] == ["pgss_create", "pgss_cmdline", "pgss_check"]
    assert fix[1] == {"id": "pgss_cmdline", "arg": "timescaledb,pg_stat_statements",
                      "cmd": "sudo docker inspect -f '{{json .Config.Cmd}}' app-db-1"}


def test_pgss_on_the_host():
    inv = _inv(container="", dbs=("shop",), users=("postgres", "shop"))
    item = _pgss(container="", role="postgres", su="on", spl_src="configuration file", spl="", unit="postgresql@16-main.service")
    fix = _fix(item, inv)
    # от postgres через runuser: sudo -u postgres просит пароль, если sudo разрешен только как root;
    # базы postgres нет - в самой большой, ее helper проверяет первой
    assert fix[0]["cmd"] == 'sudo runuser -u postgres -- psql -d shop -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"'
    assert fix[2] == {"id": "pgss_restart_host", "arg": "postgresql@16-main.service",
                      "cmd": "sudo systemctl restart postgresql@16-main.service"}
    item["cfg"]["unit"] = ""
    assert _fix(item, inv)[2]["cmd"] == "sudo systemctl restart postgresql"


def test_pgss_without_helper_data_looks_first():
    """helper до 0.14 не говорит, что уже в списке и откуда он: первым шагом посмотреть, затем руками"""
    item = _pg(container="app-db-1", load={"win": 3600, "pss": "off", "qtext": 0}, cfg={"sb": 1})
    fix = _fix(item)
    assert [s["id"] for s in fix] == ["pgss_look", "pgss_create", "pgss_alter_hand", "pgss_restart",
                                      "pgss_compose_hand", "pgss_check", "pgss_undo_hand"]
    # helper входил под единственной ролью с правом входа
    assert fix[0]["cmd"].split("\n") == [
        "sudo docker exec app-db-1 psql -U app -d postgres -c \"SELECT setting, source FROM pg_settings WHERE name = 'shared_preload_libraries'\"",
        "sudo docker inspect -f '{{json .Config.Cmd}} {{index .Config.Labels \"com.docker.compose.project.working_dir\"}} "
        "{{index .Config.Labels \"com.docker.compose.project\"}} {{index .Config.Labels \"com.docker.compose.service\"}}' app-db-1"]
    assert fix[4]["alt"] and fix[6]["undo"]
    # ролей несколько и postgres среди них нет - как helper, под POSTGRES_USER контейнера
    fix = _fix(item, _inv(users=("admin", "app")))
    assert fix[1]["cmd"].startswith('sudo docker exec app-db-1 psql -U "$(sudo docker exec app-db-1 printenv POSTGRES_USER)" -d postgres ')
    # не суперпользователь или имя, которое не подставить - тоже руками
    assert _fix(_pgss(role="app", su="off", spl_src="default", spl=""))[0]["id"] == "pgss_look"
    assert _fix(_pgss(role="app", su="on", spl_src="default", spl='"$libdir/x"'))[0]["id"] == "pgss_look"


def test_pgss_loaded_without_extension_and_enabled():
    assert [s["id"] for s in _fix(_pgss("noext", role="app", su="on", spl_src="default", spl="pg_stat_statements"))] == [
        "pgss_create", "pgss_check"]
    assert _fix(_pgss("on")) == [] and _fix(_pg(container="app-db-1")) == []
    assert "pgss_fix" not in db_health.view(_rep(_my()), NOW)[0]
    # это подсказка в карточке, а не поломка: ни строки проблемы, ни "Что сломано"
    rep = _rep(_pgss(role="app", su="on", spl_src="default", spl=""))
    assert db_health.problems(rep, NOW) == []
    assert [p for p in collector.server_problems(_srv(rep), NOW) if p["kind"] == "db_health"] == []


def test_load_view_says_when_the_base_had_no_statements():
    """Сразу после включения в снимке часовой давности запросов нет: топ пуст час, и это не простой базы"""
    v = db_health.view(_rep(_pg(load={"win": 3100, "pss": "on", "qtext": 0, "ms_all": 0, "pbase": 0})), NOW)[0]["load"]
    assert v["pbase"] == 0
    assert "pbase" not in db_health.view(_rep(_pg(load={"win": 3100, "pss": "on", "qtext": 0})), NOW)[0]["load"]


def _ch(**kw):
    x = {"engine": "clickhouse", "container": "ch", "role": "", "replicas": 0, "lag": 0,
         "parts": {"max": 12, "tbl": "shop.events", "part": "202610", "delay": 1000, "throw": 3000},
         "mut": {"n": 0, "age": 0, "tbl": "", "err": ""},
         "rep": {"n": 0, "ro": 0, "delay": 0, "queue": 0, "ro_tbl": "", "q_n": 0, "q_age": 0, "q_err": ""},
         "detached": 0, "merges": 1, "merge_max": 3, "ins": {"win": 900, "n": 400, "fail": 0, "rej": 0, "del": 0},
         "qlog": True, "qerr": 0, "top": [{"n": 16, "ms": 589, "rd": 14584845, "mem": 5853296, "q": "SELECT count() FROM t WHERE a = ?"}],
         "cut": 0}
    for k, v in kw.items():
        if isinstance(v, dict) and isinstance(x.get(k), dict):
            x[k] = {**x[k], **v}
        else:
            x[k] = v
    return x


def _rd(**kw):
    x = {"engine": "redis", "container": "cache", "role": "primary", "replicas": 0, "lag": 0, "link": "", "down": 0,
         "mem": 300 << 20, "maxmem": 0, "policy": "noeviction", "rdb": "ok", "rdb_changes": 2, "rdb_age": 600,
         "aof": 0, "aof_status": "ok", "stop_writes": "yes", "win": 900, "evicted": 0, "rejected": 0,
         "cmds": [{"c": "get", "n": 1200, "us": 4000}]}
    x.update(kw)
    return x


def _probs(*items):
    return db_health.problems(_rep(*items), NOW)


def test_helper_watches_clickhouse_and_redis():
    src = (Path(__file__).resolve().parents[2] / "agent" / "dbstat-setup.sh").read_text(encoding="utf-8")
    assert "health_ch()" in src and "health_redis()" in src and 'health_ch "$c"' in src and 'health_redis "$c"' in src
    # одна команда docker exec на все запросы ClickHouse, ответ - ровно десять строк
    assert "clickhouse-client -n --format TSV" in src and '= 10 ] || return 0' in src
    assert "parts_to_throw_insert" in src and "RejectedInserts" in src and "normalizeQuery" in src
    # пароль Redis уходит в redis-cli через stdin, не в аргументах (его видно в ps)
    assert "read -r REDISCLI_AUTH; export REDISCLI_AUTH" in src and "--requirepass" in src
    # в поде (0.17) пароль ставится в окружение redis-cli уже внутри контейнера, тоже не аргументом
    assert "REDISCLI_AUTH=" not in src.replace("read -r REDISCLI_AUTH", "").replace("REDISCLI_AUTH=$P; export REDISCLI_AUTH", "")
    assert "cmdstat_" in src and "stop-writes-on-bgsave-error" in src


def test_clickhouse_quiet_when_healthy():
    assert _probs(_ch()) == []


def test_clickhouse_parts_inserts_replicas_mutations():
    p = _probs(_ch(parts={"max": 2600}))
    assert p[0][0] == 2 and "в shop.events (партиция 202610) 2600 кусков, при 3000 вставки начнут отклоняться" in p[0][1]
    p = _probs(_ch(parts={"max": 1200}))
    assert p[0][0] == 1 and "с 1000 вставки замедляются" in p[0][1]
    # старая версия: пороги 150 и 300
    assert _probs(_ch(parts={"max": 250, "delay": 150, "throw": 300}))[0][0] == 2
    p = _probs(_ch(ins={"rej": 37}))
    assert p[0] == (2, "ClickHouse ch: за 15 мин отклонено вставок: 37 - слишком много кусков в партиции (TOO_MANY_PARTS)",
                    "clickhouse:ch:rej")
    assert _probs(_ch(ins={"del": 5}))[0][0] == 1
    p = _probs(_ch(rep={"n": 3, "ro": 2, "ro_tbl": "shop.orders"}))
    assert p[0][0] == 2 and "shop.orders в режиме только чтения (еще таких: 1)" in p[0][1]
    assert _probs(_ch(rep={"n": 3, "delay": 2000}))[0][:2] == (2, "ClickHouse ch: реплика отстает на 33 мин")
    p = _probs(_ch(rep={"n": 3, "q_n": 4, "q_age": 7200, "q_err": "Code: 999. Keeper session expired"}))
    assert p[0][0] == 1 and "очередь репликации стоит, самой старой задаче 2 ч 0 мин: Code: 999" in p[0][1]
    p = _probs(_ch(mut={"n": 1, "age": 120, "tbl": "shop.events", "err": "Code: 341. Cannot parse"}))
    assert p[0] == (1, "ClickHouse ch: мутация в shop.events не выполняется: Code: 341. Cannot parse", "clickhouse:ch:mut")
    assert _probs(_ch(mut={"n": 1, "age": 7 * 3600, "tbl": "shop.events"}))[0][1].endswith("идет 7 ч 0 мин")
    # без окна (первый прогон или перезапуск) счетчики вставок не судим
    assert _probs(_ch(ins={"win": 0, "rej": 5})) == []


def test_redis_writes_memory_replica():
    assert _probs(_rd()) == []
    p = _probs(_rd(rdb="err"))
    assert p[0][0] == 2 and "отклоняет запись (MISCONF)" in p[0][1]
    p = _probs(_rd(rdb="err", stop_writes="no"))
    assert p[0][0] == 1 and "при перезапуске пропадет" in p[0][1]
    assert _probs(_rd(aof=1, aof_status="err"))[0][0] == 2
    p = _probs(_rd(maxmem=1 << 30, mem=int(0.99 * (1 << 30))))
    assert p[0][0] == 2 and "память кончилась (1014 МБ из 1.0 ГБ), политика noeviction" in p[0][1]
    assert _probs(_rd(maxmem=1 << 30, mem=int(0.92 * (1 << 30))))[0][0] == 1
    # с вытеснением упор в maxmemory - так и задумано
    assert _probs(_rd(maxmem=1 << 30, mem=1 << 30, policy="allkeys-lru", evicted=500)) == []
    p = _probs(_rd(role="replica", link="down", down=600))
    assert p[0] == (2, "Redis cache: реплика потеряла связь с основной 10 мин назад", "redis:cache:link")
    assert _probs(_rd(role="replica", link="down", down=30)) == []
    assert _probs(_rd(rejected=12))[0][0] == 1


def test_view_passes_clickhouse_and_redis_facts():
    v = db_health.view(_rep(_ch(), _rd(rdb="err")), NOW)
    ch, rd = v
    assert ch["ch"]["parts"]["max"] == 12 and ch["ch"]["top"][0]["q"].startswith("SELECT") and ch["problems"] == []
    assert rd["rd"]["cmds"][0]["c"] == "get" and rd["problems"][0]["level"] == 2 and "ch" not in rd
    # в алерт и "Что сломано" идут через тот же db_health
    s = _srv(_rep(_rd(rdb="err")))
    lvl, ctx = collector._server_conditions(s, NOW)["db_health"]
    assert lvl == 2 and "Redis cache: не может сохранить снимок" in ctx["detail"]



# ── MySQL / MariaDB (dbstat-setup 0.11) ────────────────────────────────────────
def test_helper_reaches_mysql_in_pods_and_behind_a_password():
    src = (Path(__file__).resolve().parents[2] / "agent" / "dbstat-setup.sh").read_text(encoding="utf-8")
    # под: клиент из образа внутри его пространств, от uid сервера, окружение контейнера из config.json среды
    # (с 0.17 общий вход pod_run для всех движков, *_FILE читается внутри контейнера)
    assert 'nsenter -t "$p" -m -n -p -S "$u"' in src and "io.containerd.runtime.v2.task" in src
    assert 'pod_run "${c#pid:}" "$MY_SH" $fl -e "$q"' in src and "MARIADB_ROOT_PASSWORD_FILE" in src
    # хост: файлы с учеткой раньше сокета - отказ root по сокету копит Aborted_connects
    assert 'for f in /etc/mysql/debian.cnf /root/.my.cnf ""; do' in src
    # реплика по именам полей, без \G
    assert 'my_run "$c" -B "SHOW REPLICA STATUS"' in src and "STATUS\\G" not in src
    # нагрузка, запросы без своих, настройки
    assert "load_my()" in src and "events_statements_summary_by_digest" in src and "cfg_my()" in src
    assert "not like 'SHOW %'" in src and "lower(digest_text) regexp 'passw|secret" in src
    assert 'find "$LOAD_DIR" -mindepth 1 -maxdepth 1 -mtime +2' in src


def test_mysql_purge_backlog_idle_transaction_refused_connections():
    x = _my(role="primary", xact=7200, xact_db="shop", xact_app="app", hist=3_400_000,
            idle_xact=7200, idle_sleep=5400, idle_db="shop", idle_app="app",
            load={"win": 1800, "connection_errors_max_connections": 12}, cfg={"max_connections": "151"})
    # одна беда - одна строка: простаивающая транзакция и есть самая старая, очередь из-за нее
    assert [t for _l, t, _k in db_health.problems(_rep(x), NOW)] == [
        "MySQL на хосте: транзакция открыта 2 ч 0 мин и простаивает 1 ч 30 мин (база shop, app): держит блокировки "
        "строк и не дает InnoDB удалять старые версии; очередь очистки InnoDB уже 3.4 млн версий строк",
        "MySQL на хосте: за 30 мин отказано в подключении: 12 - упор в max_connections (151)",
    ]
    # работающая долгая транзакция и очередь - тоже одна строка
    busy = _my(role="primary", xact=7200, xact_db="shop", xact_app="app", hist=3_400_000)
    assert [t for _l, t, _k in db_health.problems(_rep(busy), NOW)] == [
        "MySQL на хосте: транзакция идет 2 ч 0 мин (база shop, app); очередь очистки InnoDB уже 3.4 млн версий строк"]
    # очередь без долгой транзакции - своя строка; MariaDB называется по версии
    q = _my(role="primary", container="db", xact=900, xact_app="app", hist=1_200_000, cfg={"version": "11.4.8-MariaDB"})
    assert [t for _l, t, _k in db_health.problems(_rep(q), NOW)] == [
        "MariaDB db: очередь очистки InnoDB 1.2 млн версий строк - старые версии не удаляются, запросы пробираются "
        "через них и замедляются; самая долгая транзакция идет 15 мин (app) - держит, скорее всего, она"]
    # дамп с --single-transaction держит транзакцию по природе: до 12 часов молчим
    dump = _my(role="primary", xact=4 * 3600, xact_app="mysqldump")
    assert db_health.problems(_rep(dump), NOW) == []
    # тихий сервер - тишина
    assert db_health.problems(_rep(_my(role="primary", hist=120, load={"win": 3600})), NOW) == []


def _myload(**kw):
    ld = {"win": 1800, "ps": "on", "questions": 90_000, "com_select": 60_000, "com_insert": 5000, "com_update": 2000,
          "com_delete": 500, "slow_queries": 3, "created_tmp_tables": 4000, "created_tmp_disk_tables": 900,
          "innodb_buffer_pool_reads": 40_000, "innodb_buffer_pool_read_requests": 2_000_000,
          "innodb_os_log_written": 3 * (1 << 30), "innodb_log_waits": 80, "innodb_row_lock_waits": 10,
          "innodb_row_lock_time": 5000, "im_deadlocks": 1, "aborted_connects": 40,
          "connection_errors_max_connections": 0, "page": 16384, "run": 3, "max_used": 40, "ms_all": 600_000,
          "top": [{"id": "a1", "db": "shop", "q": "SELECT * FROM `orders` WHERE `note` LIKE ?", "calls": 100,
                   "ms": 400_000, "rows": 5_000_000, "tmp": 0, "noidx": 100},
                  {"id": "b2", "db": "shop", "q": "", "calls": 10, "ms": 50_000, "rows": 10, "tmp": 5, "noidx": 0}]}
    ld.update(kw)
    return ld


def test_mysql_load_view_per_hour():
    v = db_health.my_load_view(_my(load=_myload()))
    assert v["win"] == 1800 and v["ps"] == "on" and v["q"] == 180_000 and v["wr"] == 15_000
    assert v["rd"] == 40_000 * 16384 * 2 and v["hit"] == 98.0 and v["redo"] == 6 * (1 << 30)
    assert v["deadlocks"] == 2 and v["tmp_disk"] == 1800 and v["refused"] == 0 and v["max_used"] == 40
    assert [(r["calls"], r["ms"], r["share"], r["rows_call"], r["noidx"]) for r in v["top"]] == [
        (200, 800_000, 67, 50_000, 200), (20, 100_000, 8, 1, 0)]
    # счетчика нет в этой версии - None, а не 0; первый прогон - без окна
    assert db_health.my_load_view(_my(load={"win": 600, "questions": 10}))["redo"] is None
    first = db_health.my_load_view(_my(load={"win": 0, "ps": "off"}))
    assert first["win"] == 0 and first["ps"] == "off" and "q" not in first


def _mycfg(**kw):
    c = {"version": "8.0.36", "innodb_buffer_pool_size": str(128 << 20), "innodb_redo_log_capacity": str(100 << 20),
         "innodb_log_buffer_size": str(16 << 20), "tmp_table_size": str(16 << 20),
         "max_heap_table_size": str(16 << 20), "log_bin": "ON", "binlog_expire_logs_seconds": "2592000",
         "expire_logs_days": "0", "mem": 32 << 30, "mem_lim": False, "stop_to": -1, "binlog_b": 0, "rt": "host"}
    c.update(kw)
    return c


def test_mysql_advice():
    x = _my(role="primary", cfg=_mycfg())
    lv = db_health.my_load_view(_my(load=_myload()))
    ks = {a["k"]: a for a in db_health.my_advice(x, lv, db_size=20 << 30)}
    # буфер по умолчанию при 20 ГБ данных и чтениях с диска; redo 6 ГБ в час на 100 МБ журнала
    assert ks["my_bp_low"]["bp"] == 128 << 20 and ks["my_bp_low"]["rd"] == lv["rd"]
    assert ks["my_redo"]["rec"] == 8 << 30 and ks["my_redo"]["var"] == "innodb_redo_log_capacity" and ks["my_redo"]["online"]
    assert ks["my_log_buffer"]["n"] == 160 and ks["my_tmp"]["n"] == 1800
    assert "my_binlog" not in ks and "my_stop_timeout" not in ks
    # бинлоги без срока, docker с 10 секундами на остановку, буфер больше памяти контейнера
    y = _my(role="primary", container="db", cfg=_mycfg(version="10.6.18-MariaDB", innodb_buffer_pool_size=str(6 << 30),
                                                       mem=6 << 30, mem_lim=True, binlog_expire_logs_seconds="0",
                                                       expire_logs_days="0.000000", binlog_b=40 << 30, rt="docker",
                                                       innodb_log_file_size=str(96 << 20)))
    del y["cfg"]["innodb_redo_log_capacity"]  # у MariaDB его нет
    ky = {a["k"]: a for a in db_health.my_advice(y, lv, db_size=1 << 30)}
    assert ky["my_bp_high"]["lim"] is True and ky["my_binlog"]["b"] == 40 << 30 and ky["my_stop_timeout"]["to"] == 10
    assert ky["my_redo"]["var"] == "innodb_log_file_size" and ky["my_redo"]["online"] is False
    # несколько MySQL на машине - про долю памяти не советуем; под - без совета про docker
    assert "my_bp_low" not in {a["k"] for a in db_health.my_advice(x, lv, my_count=2, db_size=20 << 30)}
    pod = _my(container="default/db-0", cfg=_mycfg(innodb_buffer_pool_size=str(4 << 30), rt="pod"))
    assert "my_stop_timeout" not in {a["k"] for a in db_health.my_advice(pod, lv)}


def test_mysql_in_view():
    st = {"engine": "mysql", "container": "default/db-0", "dbs": [{"name": "shop", "size": 20 << 30}]}
    rep = _rep(_my(role="primary", container="default/db-0", load=_myload(), cfg=_mycfg()))
    rep["db_stats"] = [st]
    v = db_health.view(rep, NOW)[0]
    assert v["load"] is None and v["my"]["q"] == 180_000 and {a["k"] for a in v["advice"]} >= {"my_bp_low", "my_redo"}


def test_mysql_refused_connections_not_twice_with_the_conn_alert():
    x = _my(role="primary", container="wp", load={"win": 3600, "connection_errors_max_connections": 37},
            cfg={"max_connections": "151"})
    rep = _rep(x)
    rep["db_stats"] = [{"engine": "mysql", "container": "wp", "conn_used": 150, "conn_max": 151}]
    s = _srv(rep)
    assert [k for _l, _t, k in collector.db_health_problems(s, rep, NOW)] == ["mysql:wp:conn"]
    # алерт коннектов включен и горит по этому экземпляру - строка "занято 150 из 151" уже говорит о том же
    s.db_conn_alert_percent = 80
    assert collector.db_health_problems(s, rep, NOW) == []


def test_pgss_fix_in_a_pod_gives_kubectl():
    """База в поде (dbstat-setup 0.17): docker exec там не к чему, команды - kubectl exec в контейнер базы,
    перезапуск - удалить под, контроллер создаст его заново"""
    item = _pgss(container="default/postgres-0", role="app", su="on", spl_src="default", spl="", rt="pod", kc="postgres")
    fix = _fix(item)
    assert [x["id"] for x in fix] == ["pgss_create", "pgss_alter", "pgss_restart_pod", "pgss_check", "pgss_undo"]
    psql = "kubectl -n default exec postgres-0 -c postgres -- psql -U app -d postgres"
    assert fix[0]["cmd"] == f'{psql} -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"'
    assert fix[2] == {"id": "pgss_restart_pod", "arg": "default/postgres-0", "cmd": "kubectl -n default delete pod postgres-0"}
    assert fix[4]["cmd"].endswith("\nkubectl -n default delete pod postgres-0")
    assert not any("docker" in x["cmd"] for x in fix)
    # задан в args контейнера: ALTER SYSTEM не перебьет, показать текущие command и args
    fix = _fix(_pgss(container="db/pg-0", role="postgres", su="on", spl_src="command line", spl="timescaledb", rt="pod"))
    assert [x["id"] for x in fix] == ["pgss_create", "pgss_cmdline_pod", "pgss_check"]
    assert fix[1]["arg"] == "timescaledb,pg_stat_statements"
    assert fix[1]["cmd"].startswith("kubectl -n db get pod pg-0 -o jsonpath=")
    # не суперпользователь: откуда задан список, не видно - сначала посмотреть
    fix = _fix(_pgss(container="db/pg-0", role="app", su="off", spl_src="", rt="pod"))
    assert fix[0]["id"] == "pgss_look" and fix[0]["arg"] == "pod" and "kubectl -n db get pod pg-0" in fix[0]["cmd"]
    # docker-контейнер с образом под другим именем helper тоже опрашивает через nsenter (rt=pod), но имя у него
    # docker'а - команды docker
    fix = _fix(_pgss(container="shop-db", role="app", su="on", spl_src="default", spl="", rt="pod"))
    assert fix[0]["cmd"].startswith("sudo docker exec shop-db psql")


def test_fails_of_databases_in_pods():
    """Почему helper не опросил базу в поде: панель пишет конкретно, а не общее "не дотягивается"."""
    rep = _rep(_pg(container="default/postgres-0"))
    rep["extras"]["db-health"]["fails"] = [
        {"engine": "redis", "container": "default/redis-0", "why": "auth", "err": "NOAUTH Authentication required. "},
        {"engine": "pg", "container": "db/pg-1", "why": "strange", "err": ""},
        {"engine": "pg", "container": "", "why": "client"},
        "junk"]
    assert db_health.fails(rep, NOW) == [
        {"engine": "redis", "container": "default/redis-0", "why": "auth", "err": "NOAUTH Authentication required."},
        {"engine": "pg", "container": "db/pg-1", "why": "conn", "err": ""}]
    # helper до 0.17 поля не пишет, старый блок не судим
    assert db_health.fails(_rep(_pg()), NOW) is None
    rep["extras"]["db-health"]["ts"] = T - 7200
    assert db_health.fails(rep, NOW) is None


def test_helper_enters_pods_of_every_engine():
    """0.17: PostgreSQL, ClickHouse и Redis в подах - как MySQL с 0.11, через nsenter своей ноды, без доступа
    к кластеру. Окружение контейнера идет в контейнер по трубе: в аргументах (ps) его нет, хостовые
    timeout и nsenter его не получают (LD_PRELOAD контейнера не доходит до процесса root)"""
    src = (Path(__file__).resolve().parents[2] / "agent" / "dbstat-setup.sh").read_text(encoding="utf-8")
    body = src.split("HELPER_EOF")[1]
    assert not re.search(r"^[^#\n]*\bkubectl\b", body, re.M) and "admin.conf" not in src
    assert ("| timeout $TO nsenter -t \"$p\" -m -n -p -S \"$u\" -G \"${g:-$u}\" -- /bin/sh -c 'eval \"$(cat)\"; '\"$s\""
            in src)
    assert src.count("timeout $TO nsenter") == 1  # один вход в контейнер для всех движков
    for eng in ("PG_POD", "CH_POD", "RD_POD", "MY_SH"):
        assert f'pod_run "${{c#pid:}}" "${eng}"' in src or f'pod_run "${{1#pid:}}" "${eng}"' in src, eng
    # uid сервера базы, не root: у клиента нет capabilities
    assert "case \"$u\" in ''|0) u=65534; g=65534 ;; esac" in src
    # пароль из *_FILE читается внутри контейнера, а не с хоста через /proc/<pid>/root
    assert '"/root" + fp' not in src and "my_pod_pw" not in src
    # обход по процессам: постмастер, а не сотни его бэкендов; docker-контейнеры, опрошенные по образу, не повторяются
    for x in ("sweep pg pg postgres", "sweep mysql mysql mysqld mariadbd", "sweep ch clickhouse clickhouse-serv",
              "sweep redis redis redis-server valkey-server", 'case " $DONE_IDS $SEEN " in', "c[p[x]] != c[x]"):
        assert x in src, x
    # причины отказа - в db-health.json
    assert '"fails":[%s]}' in src and "why=nsenter" in src and "124) why=timeout" in src and "127) why=client" in src
    # имя пода, а не pid: снимки нагрузки и размеры не теряются при перезапуске пода
    assert 'n=$(inst_name "$c")' in src and 'health_pg "$c" "$n"' in src and 'idx_pg "$c" "$n"' in src
