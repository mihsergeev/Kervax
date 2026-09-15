"""Дамп базы из пода kubernetes — одной кнопкой, как у docker-контейнера.

Живой случай (15.09.2026, kz-se-op-dtp, k0s): панель печатала CronJob с именами,
выведенными из имени пода, — его приходилось править руками, а после передеплоя он
переставал попадать в базу. Заодно под clickhouse-operator числился вторым ClickHouse.
Теперь дамп снимает helper на ноде через kubectl exec, а цель — контроллер пода: сам под
helper находит заново при каждом запуске.
"""

from app.api.servers import _backup_coverage, _kube_workload
from app.models import Server
from app.schemas import BackupCommandIn

PODS = [
    {"ns": "default", "name": "chi-clickhouse-main-0-0-0", "phase": "Running",
     "owner": "StatefulSet", "image": "clickhouse/clickhouse-server:26.7.3.19"},
    # оператор управляет базой, но данных не держит
    {"ns": "default", "name": "clickhouse-operator-altinity-clickhouse-operator-6c7bb5ff42rwzf",
     "phase": "Running", "owner": "ReplicaSet", "image": "altinity/clickhouse-operator:0.25.3"},
    {"ns": "default", "name": "rabbitmq-0", "phase": "Running", "owner": "StatefulSet",
     "image": "docker.io/rabbitmq:4.3.4-management"},
    {"ns": "shop", "name": "mariadb-7d9f8b6c5d-x2x7l", "phase": "Running", "owner": "ReplicaSet",
     "image": "mariadb:11"},
]


def _server(pods=None, **rep_kw):
    rep = {
        "agent_version": "2.8",
        "backup": {"present": True, "manageable": True, "configured": False},
        "kube": {"pods": PODS if pods is None else pods},
        "setup_versions": {"backup-setup": "0.27"},
        "extras": {"kube-dumps": {"v": 1, "ctl": "k0s", "exec": True}},
    }
    rep.update(rep_kw)
    return Server(name="kz-se-op-dtp", token_hash="x", enabled=True, last_report=rep,
                  backup_not_required=False)


def _audit(server):
    return {(a.subject, a.instance): a for a in _backup_coverage(server) if a.kind.startswith("db")}


def test_workload_of_a_pod():
    def wl(name, owner, ns="default"):
        return _kube_workload({"ns": ns, "name": name, "owner": owner})

    assert wl("chi-clickhouse-main-0-0-0", "StatefulSet") == "k8s.default.sts.chi-clickhouse-main-0-0"
    assert wl("postgres-12", "StatefulSet") == "k8s.default.sts.postgres"
    # хеш ReplicaSet меняется с каждым выкатом — в цель он не попадает
    assert wl("wordpress-mariadb-b65684cf4-p2z7l", "ReplicaSet") == "k8s.default.deploy.wordpress-mariadb"
    assert wl("redis-node-8xk2p", "DaemonSet", "cache") == "k8s.cache.ds.redis-node"
    # без контроллера под после пересоздания не найти
    assert wl("debug-shell", "") == ""
    assert wl("job-x-1", "Job") == ""


def test_pod_databases_get_the_dump_button():
    audit = _audit(_server())
    ch = audit[("ClickHouse", "k8s.default.sts.chi-clickhouse-main-0-0")]
    assert ch.can_dump and ch.dump_engine == "ch" and ch.container == ch.instance
    assert ch.pods == ["default/chi-clickhouse-main-0-0-0"]
    assert "StatefulSet default/chi-clickhouse-main-0-0" in ch.detail
    assert "манифест" not in ch.detail and "CronJob" not in ch.detail
    # оператор ClickHouse — не вторая база
    assert [k for k in audit if k[0] == "ClickHouse"] == [("ClickHouse", "k8s.default.sts.chi-clickhouse-main-0-0")]

    assert audit[("RabbitMQ", "k8s.default.sts.rabbitmq")].can_dump
    maria = audit[("MySQL/MariaDB", "k8s.shop.deploy.mariadb")]
    assert maria.can_dump and maria.dump_engine == "mysql"


def test_why_the_button_is_not_offered():
    old = _audit(_server(setup_versions={"backup-setup": "0.26"}))
    ch = old[("ClickHouse", "k8s.default.sts.chi-clickhouse-main-0-0")]
    assert not ch.can_dump and "0.27" in ch.detail

    worker = _audit(_server(extras={"kube-dumps": {"v": 1, "ctl": "", "exec": False}}))
    ch = worker[("ClickHouse", "k8s.default.sts.chi-clickhouse-main-0-0")]
    assert not ch.can_dump and "управляющей ноде" in ch.detail

    # под без контроллера: найти его заново не по чему
    bare = _audit(_server(pods=[{"ns": "default", "name": "pg-debug", "phase": "Running",
                                 "owner": "", "image": "postgres:17"}]))
    pg = bare[("PostgreSQL", "")]
    assert not pg.can_dump and pg.pods == ["default/pg-debug"]


def test_enabled_pod_dump_closes_the_finding():
    dump = {"engine": "rabbitmq", "container": "k8s.default.sts.rabbitmq", "files": 2,
            "last_ts": 1789480127, "keep": 2, "dir": "/backup/rabbitmq/k8s.default.sts.rabbitmq"}
    s = _server(backup={"present": True, "manageable": True, "configured": False, "dumps": [dump]})
    rabbit = _audit(s)[("RabbitMQ", "k8s.default.sts.rabbitmq")]
    assert rabbit.kind == "db_ok" and rabbit.can_dump


def test_the_command_accepts_a_workload():
    cmd = BackupCommandIn(action="dump_setup", engine="ch",
                          container="k8s.default.sts.chi-clickhouse-main-0-0")
    assert cmd.container == "k8s.default.sts.chi-clickhouse-main-0-0"
