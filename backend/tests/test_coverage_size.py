"""Примерный объем бэкапа в "Покрытии" (dbstat-setup 0.15).

Повод - вопрос оператора: в "Под вопросом" четырнадцать баз, и непонятно, бэкапить там 100 МБ или 2 ТБ.
Объем берется из того, что helper уже присылает: инвентарь баз (db_stats) и extras["db-sizes"]
(индексы PostgreSQL/MySQL и du каталогов данных остальных движков).
"""

from pathlib import Path

from app.api.servers import _backup_coverage
from app.models import Server

GB = 1 << 30
NOW = 1_800_000_000


def _srv(containers, db_stats=(), sizes=(), procs=()):
    rep = {
        "clock_unix": NOW,
        "docker": {"containers": [{"name": n, "image": img, "state": "running"} for n, img in containers]},
        "db_engines": list(procs),
        "db_stats": list(db_stats),
        "extras": {"db-sizes": {"v": 1, "ts": NOW, "items": list(sizes)}},
    }
    return Server(name="node-1", token_hash="x", last_report=rep)


def _row(rows, subject, inst=""):
    got = [a for a in rows if a.subject == subject and a.instance == inst]
    assert len(got) == 1, [(a.subject, a.instance) for a in rows]
    return got[0]


def test_postgres_dump_without_indexes_and_largest_db():
    s = _srv([("pg-main", "postgres:16")],
             db_stats=[{"engine": "pg", "container": "pg-main", "dbs": [
                 {"name": "app", "size": 30 * GB}, {"name": "audit", "size": 9 * GB}, {"name": "postgres", "size": GB}]}],
             sizes=[{"key": "pg", "container": "pg-main", "idx": 15 * GB, "ts": NOW - 3600}])
    a = _row(_backup_coverage(s), "PostgreSQL", "pg-main")
    assert a.kind == "db"
    assert a.size_bytes == 25 * GB  # база 40 ГБ минус индексы 15 ГБ
    assert a.size_db == 40 * GB
    assert (a.size_top, a.size_top_bytes) == ("app", 30 * GB)


def test_postgres_without_index_measure_shows_whole_base():
    s = _srv([("pg-main", "postgres:16")],
             db_stats=[{"engine": "pg", "container": "pg-main", "dbs": [{"name": "app", "size": 2 * GB}]}])
    a = _row(_backup_coverage(s), "PostgreSQL", "pg-main")
    assert a.size_bytes == 2 * GB
    assert a.size_db == 0 and a.size_top == ""  # одна база: "крупнейшая" ничего не добавит


def test_small_largest_db_not_named():
    # сотня мелких баз: крупнейшая из них объем не объясняет
    dbs = [{"name": f"site{i}", "size": 100 << 20} for i in range(20)]
    s = _srv([("pg-main", "postgres:16")], db_stats=[{"engine": "pg", "container": "pg-main", "dbs": dbs}])
    a = _row(_backup_coverage(s), "PostgreSQL", "pg-main")
    assert a.size_bytes == 20 * (100 << 20) and a.size_top == ""


def test_minio_and_prometheus_by_data_dir():
    s = _srv([("minio-1", "minio/minio:RELEASE.2025"), ("minio-2", "minio/minio:RELEASE.2025")],
             procs=["Prometheus"],
             sizes=[{"key": "minio", "container": "minio-1", "bytes": 3 * GB, "ts": NOW - 600},
                    {"key": "prometheus", "container": "", "bytes": 7 * GB, "ts": NOW - 600}])
    rows = _backup_coverage(s)
    assert _row(rows, "MinIO", "minio-1").size_bytes == 3 * GB
    assert _row(rows, "MinIO", "minio-2").size_bytes == 0  # не замерен - строка молчит
    assert _row(rows, "Prometheus").size_bytes == 7 * GB  # нативная установка


def test_redis_takes_rdb_dir_not_key_count():
    # у Redis в инвентаре size - число ключей, а не байты
    s = _srv([("redis", "redis:7")],
             db_stats=[{"engine": "redis", "container": "redis", "dbs": [{"name": "db0", "size": 123456}]}],
             sizes=[{"key": "redis", "container": "redis", "bytes": 512 << 20, "ts": NOW}])
    assert _row(_backup_coverage(s), "Redis", "redis").size_bytes == 512 << 20


def test_rabbitmq_is_small_whatever_the_queues():
    s = _srv([("rabbitmq", "rabbitmq:3-management")],
             sizes=[{"key": "rabbitmq", "container": "rabbitmq", "bytes": 40 * GB, "ts": NOW}])
    a = _row(_backup_coverage(s), "RabbitMQ", "rabbitmq")
    assert a.size_small and a.size_bytes == 0


def test_stale_measure_ignored():
    # helper замер бросил (снят или старый): старому числу не верим
    s = _srv([("minio-1", "minio/minio")],
             sizes=[{"key": "minio", "container": "minio-1", "bytes": 3 * GB, "ts": NOW - 4 * 86400}])
    assert _row(_backup_coverage(s), "MinIO", "minio-1").size_bytes == 0


def test_index_bigger_than_base_ignored():
    # индексы со вчерашнего замера, базу с тех пор почистили: вычитать нельзя
    s = _srv([("pg-main", "postgres:16")],
             db_stats=[{"engine": "pg", "container": "pg-main", "dbs": [{"name": "app", "size": GB}]}],
             sizes=[{"key": "pg", "container": "pg-main", "idx": 2 * GB, "ts": NOW}])
    a = _row(_backup_coverage(s), "PostgreSQL", "pg-main")
    assert a.size_bytes == GB and a.size_db == 0


def test_no_helper_block_no_sizes():
    s = _srv([("minio-1", "minio/minio")])
    s.last_report.pop("extras")
    assert _row(_backup_coverage(s), "MinIO", "minio-1").size_bytes == 0


def test_helper_measures_sizes():
    src = (Path(__file__).resolve().parents[2] / "agent" / "dbstat-setup.sh").read_text(encoding="utf-8")
    assert "\r" not in src
    assert "report.d/db-sizes.json" in src
    # du на большом томе: простой приоритет, предел по времени, раз в сутки и бюджет на прогон
    assert "nice -n 19 $IONICE du -sxB1" in src and 'timeout "$SIZE_TO"' in src
    assert "SIZE_EVERY=86400" in src and "SIZE_BUDGET=" in src
    # индексы в дамп не идут: их размер меряется отдельно
    assert "pg_relation_size(indexrelid)" in src and "sum(index_length)" in src
    # сначала инвентарь и здоровье, потом медленный замер
    assert src.index('mv -f "$HOUT.tmp.$$" "$HOUT"') < src.index("sizes_native\n")
