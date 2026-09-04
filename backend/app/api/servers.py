import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import delete as sa_delete, select

from app import audit, geoip
from app.setup_scripts import (
    current_setup_versions as _current_setup_versions,
    setup_needed as _setup_needed,
    setup_scripts as _setup_scripts,
)
from app.config import get_settings
from app.deps import AdminUser, CurrentUser, SessionDep, group_allowed, scope_query
from app.models import (
    AgentProbe,
    Check,
    BackupCommand,
    BackupSetupJob,
    DockerCommand,
    KubeCommand,
    OomEvent,
    Server,
    ServerMetric,
    User,
)
from app.schemas import (
    AgentConfigOut,
    AgentReleaseOut,
    AgentReportIn,
    AgentUpdateCancel,
    AgentUpdateReq,
    AlertSnoozeIn,
    BackupCommandIn,
    BackupCommandOut,
    BackupAuditMuteIn,
    BackupRepoMuteIn,
    BackupCredsOut,
    BackupAudit,
    BackupResultIn,
    BackupServerDeployIn,
    BackupSetupIn,
    BackupSetupJobOut,
    DockerCommandIn,
    DockerCommandOut,
    DockerResultIn,
    HelperAdvice,
    KubeCommandIn,
    KubeCommandOut,
    KubeResultIn,
    OomEventOut,
    ServerCreate,
    ServerEnrollOut,
    ServerMetricOut,
    ServerOut,
    ServerUpdate,
    SnoozeIn,
)
from app.security import generate_agent_token, hash_agent_token

log = logging.getLogger("kervax.servers")

router = APIRouter(prefix="/servers", tags=["servers"])
agent_router = APIRouter(prefix="/agent", tags=["agent"])


def _is_online(server: Server, now: datetime) -> bool:
    if server.last_seen is None:
        return False
    last = server.last_seen
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() <= max(server.offline_after_seconds, 30)


def _parse_iso(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


# Требования агента, зависящие от systemd-ЮНИТА (не от бинаря — OTA юнит не меняет).
# Агент шлёт caps={"<ключ>": bool}; если false → показываем, что дописать в юнит.
# РАСШИРЯЕМО: будущая фича, которой нужен юнит → добавить сюда (+ в agent/install.sh
# + флаг в агенте) — панель сама подскажет фикс на нодах, где его ещё нет.
AGENT_UNIT_REQS: dict[str, dict[str, str]] = {
    "kmsg": {
        "title": "Имя OOM-жертвы — чтение kernel log (/dev/kmsg)",
        "unit": "AmbientCapabilities=CAP_SYSLOG",
    },
    # systemd сам поднимет зависший агент (Go-вотчдог однажды не сработал — инцидент
    # зависший агент). Флагуется ТОЛЬКО у агента 1.77+ (старые cap не шлют), а для 1.77
    # drop-in Type=notify безопасен — они уже шлют READY=1.
    "watchdog": {
        "title": "Само-восстановление зависшего агента (systemd watchdog)",
        "unit": "Type=notify\\nNotifyAccess=main\\nWatchdogSec=180",
    },
}


def _agent_advice(server: Server) -> tuple[list[str], str | None]:
    """По самодиагностике агента (last_report.caps) собираем: чего не хватает в юните
    (человекочитаемо) и ОДНУ команду-фикс (drop-in). Если агент старый и caps не шлёт
    (или всё ок) → пусто, ничего не мигаем."""
    rep = server.last_report or {}
    caps = rep.get("caps") or {}
    missing = [k for k in AGENT_UNIT_REQS if caps.get(k) is False]

    # Отказ самообновления показываем ЗДЕСЬ же. Агент отвергает неподписанный или
    # чужой релиз безопасно, но раньше молча: причина оставалась в journalctl
    # ноды, а в панели виднелось только расхождение «цель X / версия Y». Самая
    # частая причина — панель собрана не с тем пубключом, которым подписан релиз;
    # на парке в десятки нод искать это по логам каждой ноды невозможно.
    upd_err = str(rep.get("update_error") or "").strip()
    extra = [f"обновление агента отклонено — {upd_err}"] if upd_err else []

    if not missing:
        return extra, None
    titles = [AGENT_UNIT_REQS[k]["title"] for k in missing]
    lines = "\\n".join(AGENT_UNIT_REQS[k]["unit"] for k in missing)
    cmd = (
        "sudo install -d /etc/systemd/system/kervax-agent.service.d\n"
        f"printf '[Service]\\n{lines}\\n' | "
        "sudo tee /etc/systemd/system/kervax-agent.service.d/kervax-extra.conf\n"
        "sudo systemctl daemon-reload && sudo systemctl restart kervax-agent"
    )
    return extra + titles, cmd


def _out(
    server: Server, now: datetime, cur_versions: dict[str, int] | None = None
) -> ServerOut:
    o = ServerOut.model_validate(server)
    o.online = _is_online(server, now)
    o.agent_advice, o.agent_fix_command = _agent_advice(server)
    o.helper_advice = _helper_advice(
        server, cur_versions if cur_versions is not None else _current_setup_versions()
    )
    o.backup_audit = _backup_coverage(server)
    # Страна по IP — офлайн-таблицей (см. geoip). Берём адрес, которым нода реально
    # выходит в сеть: external_ip панель видит сама, agent_ip задан руками и может
    # оказаться внутренним. local_ip не смотрим — он приватный и страны не имеет.
    o.country = geoip.country_of(server.external_ip) or geoip.country_of(server.agent_ip)
    o.docker_alerts = _docker_alerts(server)
    return o


def _docker_alerts(server: Server) -> dict[str, str]:
    """Контейнеры, по которым алерт УЖЕ отправлен: {имя: 'down'|'loop'}.

    Панель подсвечивает ровно то, про что написала в телеграм. Считать проблему на
    фронте по RestartCount нельзя: этот счётчик копится за всё время жизни контейнера
    и сам по себе ничего не значит, а crash-loop определяется по ПРИРОСТУ счётчика в
    окне — история для этого есть только на бэкенде (alert_state)."""
    out: dict[str, str] = {}
    for name, cs in ((server.alert_state or {}).get("docker") or {}).items():
        if not isinstance(cs, dict):
            continue
        if cs.get("alerted_down"):
            out[name] = "down"
        elif cs.get("alerted_loop"):
            out[name] = "loop"
    return out


async def _get_or_404(server_id: int, session: SessionDep, user: User | None = None) -> Server:
    server = await session.get(Server, server_id)
    if server is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сервер не найден")
    # Нода вне групп учётки — отвечаем «не найден», а не «запрещено»: так по ответу
    # нельзя перебором узнать, какие ещё серверы есть в панели.
    if user is not None and not group_allowed(user, server.group_name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сервер не найден")
    return server


async def _write_agent_allowlist(session) -> None:
    """Пишет data/agent_allow_ips — адреса, с которых агенты ходят в панель.
    Хостовый ops/agent-firewall-sync.sh разрешает их в ufw/firewalld (если панель
    закрыта фаерволом). Пишем атомарно; пустой файл = разрешать нечего."""
    ips = [
        ip.strip()
        for ip in await session.scalars(
            select(Server.agent_ip).where(Server.agent_ip != "")
        )
        if ip and ip.strip()
    ]
    data_dir = get_settings().data_dir
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "agent_allow_ips")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(sorted(set(ips))) + ("\n" if ips else ""))
    os.replace(tmp, path)


def _install_cmd(token: str) -> str:
    base = get_settings().panel_url.rstrip("/") or "https://ПАНЕЛЬ"
    # --connect-timeout: закрытая фаерволом панель даёт быстрый явный отказ,
    # а не многоминутное зависание пайпа
    return (
        f"curl -fsSL --connect-timeout 15 {base}/api/agent/install.sh"
        f" | sh -s -- {base} {token}"
    )


def _available_agent_version() -> str:
    """Версия из подписанного релиз-манифеста, который раздаёт панель (или '')."""
    path = os.path.join(get_settings().agent_dist_dir, "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            return str(json.load(f).get("version", ""))[:32]
    except (OSError, ValueError):
        return ""


# Кеш проверки целостности релиза: ключ — (путь, размер, mtime) всех участвующих
# файлов. Пересчитываем только когда файлы сменились: sha256 двух бинарей по ~6 МБ
# на каждый запрос манифеста — лишняя работа на ровном месте.
_release_cache: tuple[tuple, str] | None = None


def _release_fingerprint(dist: str, names: list[str]) -> tuple:
    out = []
    for n in names:
        try:
            st = os.stat(os.path.join(dist, n))
            out.append((n, st.st_size, st.st_mtime_ns))
        except OSError:
            out.append((n, -1, 0))
    return tuple(out)


def agent_release_problem() -> str:
    """'' если раздаваемые бинари совпадают с подписанным манифестом, иначе описание беды.

    Зачем. Бинари агента панель собирает сама, а manifest.json/manifest.sig кладёт
    релизный скрипт. Если в agent-dist окажется посторонний бинарь (например, остаток
    прошлого релиза), раздаваться будет он, а подписан — другой: агент честно откажет
    по sha256, и обновление молча не поедет у ВСЕГО парка. Ловим это на панели.
    """
    global _release_cache
    dist = get_settings().agent_dist_dir
    names = ["manifest.json", "kervax-agent-amd64", "kervax-agent-arm64"]
    fp = _release_fingerprint(dist, names)
    if _release_cache is not None and _release_cache[0] == fp:
        return _release_cache[1]

    problem = ""
    try:
        with open(os.path.join(dist, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
    except (OSError, ValueError):
        man = None
    if man:
        bad: list[str] = []
        for arch, art in (man.get("artifacts") or {}).items():
            path = os.path.join(dist, f"kervax-agent-{arch}")
            try:
                with open(path, "rb") as fb:
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := fb.read(1 << 20):
                        digest.update(chunk)
                        size += len(chunk)
            except OSError:
                bad.append(f"{arch}: бинарь не найден")
                continue
            if size != art.get("size") or digest.hexdigest() != art.get("sha256"):
                bad.append(f"{arch}: раздаваемый бинарь не тот, что подписан")
        if bad:
            problem = (
                f"Релиз агента {man.get('version', '?')} собран неправильно — "
                + "; ".join(bad)
                + ". В agent-dist/ должны лежать ТОЛЬКО manifest.json и manifest.sig; "
                "уберите оттуда бинари и пересоберите панель."
            )
    _release_cache = (fp, problem)
    if problem:
        log.error("%s", problem)
    return problem


@router.get("", response_model=list[ServerOut])
async def list_servers(user: CurrentUser, session: SessionDep) -> list[ServerOut]:
    now = datetime.now(timezone.utc)
    # учётке с нарезанными группами показываем только их ноды
    servers = list(await session.scalars(
        scope_query(user, select(Server), Server).order_by(Server.id)
    ))
    cur = _current_setup_versions()  # читаем раздаваемые скрипты один раз на весь список
    return [_out(s, now, cur) for s in servers]


@router.post("", response_model=ServerEnrollOut, status_code=status.HTTP_201_CREATED)
async def create_server(
    body: ServerCreate, user: CurrentUser, session: SessionDep
) -> ServerEnrollOut:
    token = generate_agent_token()
    server = Server(**body.model_dump(), token_hash=hash_agent_token(token))
    session.add(server)
    await session.commit()
    await session.refresh(server)
    await _write_agent_allowlist(session)
    await audit.record(session, user.username, "server_create", server.name)
    return ServerEnrollOut(
        server=_out(server, datetime.now(timezone.utc)),
        token=token,
        install_cmd=_install_cmd(token),
    )


# --- управляемое обновление агентов (admin, аудит; регистрируем ДО /{server_id}) ---


_SETUP_LABEL = {
    "backup-setup": "Бэкап (клиент)",
    "backupserver-setup": "Бэкап-сервер",
    "kube-setup": "Kubernetes",
    "kubeexpiry-setup": "Сроки Kubernetes и Flux",
    "webserver-setup": "Веб-домены",
    "timesync-setup": "Синхронизация времени",
    "dbstat-setup": "Инвентарь СУБД",
    "agent-watchdog": "Вотчдог агента",
}


def _helper_advice(server: Server, cur: dict[str, str]) -> list[HelperAdvice]:
    """Устаревшие setup-скрипты (helper'ы) на ноде: агент шлёт установленные версии
    (setup_versions), сверяем с текущими раздаваемыми. Флагуем только helper'ы, которые
    нода ТОЧНО имеет (manageable / бэкап-сервер с репо / kube-доступ). Гейт: агент <1.46
    не шлёт setup_versions → молчим (не путаем старый агент со старым helper'ом)."""
    rep = server.last_report or {}
    try:
        if float(rep.get("agent_version") or 0) < 1.46:
            return []
    except (TypeError, ValueError):
        return []
    sv = rep.get("setup_versions") or {}
    out: list[HelperAdvice] = []
    # идём по тому, что панель РЕАЛЬНО раздаёт (cur), а не по списку в коде
    for name, c in sorted(cur.items()):
        if not _setup_needed(name, rep):
            continue
        inst = sv.get(name)  # None = helper до версионирования
        # агенты < 1.63 слали версии числами — приводим к строке, чтобы не спорить о типах
        inst = None if inst is None else str(inst)
        if inst is None or _ver_key(inst) < _ver_key(c):
            out.append(
                HelperAdvice(
                    name=name,
                    label=_SETUP_LABEL.get(name, name),
                    installed=inst,
                    current=c,
                )
            )
    return out


# ФС, которые бэкапить бессмысленно (виртуальные/эфемерные) — из аудита исключаем
_AUDIT_SKIP_MOUNTS = ("/proc", "/sys", "/dev", "/run", "/boot", "/snap", "/var/lib/lxcfs")
# Bind-монтирования, которые НЕ являются данными контейнера: их содержимое даёт ОС
# (пакеты ядра, docker, системные файлы). Бэкапить их бессмысленно, а восстанавливать
# поверх другого ядра — вредно. Контейнеры сетевого стека (WireGuard/AmneziaWG) почти
# всегда монтируют /lib/modules — без этого списка панель звала бэкапить модули ядра.
_AUDIT_SKIP_BINDS = (
    "/lib/modules", "/usr/lib/modules",  # модули ядра — ставятся с пакетом ядра
    "/usr/src",                          # заголовки ядра
    "/var/run",                          # сокеты (docker.sock и пр.); /run уже выше
    "/etc/localtime", "/etc/timezone", "/etc/resolv.conf",
    "/etc/hosts", "/etc/hostname", "/etc/machine-id", "/etc/os-release",
)


def _under(path: str, base: str) -> bool:
    """Путь лежит под base (по границе сегмента). Без этого /lib/modules-mydata
    молча попал бы под правило для /lib/modules — та же ловушка, что с /var/log-old."""
    base = base.rstrip("/")
    return path == base or path.startswith(base + "/")
_AUDIT_MIN_BYTES = 1 << 30  # мелочь (<1 ГБ) не считаем дырой — шума больше, чем пользы
# образы СУБД → движок. Имена движков совпадают с теми, что шлёт агент (db_engines),
# иначе одна и та же база показалась бы дважды: контейнером и процессом.
_DB_IMAGES = {
    "postgres": "PostgreSQL", "postgis": "PostgreSQL", "timescale": "PostgreSQL",
    "mysql": "MySQL/MariaDB", "mariadb": "MySQL/MariaDB", "percona": "MySQL/MariaDB",
    "mongo": "MongoDB", "clickhouse": "ClickHouse",
    "elasticsearch": "Elasticsearch", "opensearch": "Elasticsearch",
    "redis": "Redis", "valkey": "Valkey", "influxdb": "InfluxDB",
    "victoriametrics": "VictoriaMetrics", "etcd": "etcd", "cockroach": "CockroachDB",
    "cassandra": "Cassandra", "zookeeper": "ZooKeeper", "kafka": "Kafka",
    "neo4j": "Neo4j", "rabbitmq": "RabbitMQ", "couchdb": "CouchDB",
    "mssql": "MS SQL Server", "sqlserver": "MS SQL Server",
    "prometheus": "Prometheus", "minio": "MinIO", "vault": "HashiCorp Vault",
    "consul": "Consul", "grafana": "Grafana",
}
# НЕ базы, хотя образ содержит имя движка: prometheus-экспортеры (postgres-exporter,
# redis-exporter…) читают метрики, но данных не хранят; postgrest — REST-обёртка.
# Считать их СУБД = ложная находка «нужен дамп» (ловили 4 postgres-exporter'а как базы).
_NOT_DB_MARKERS = ("exporter", "postgrest")


def _db_engine_of(image: str, name: str = "") -> str | None:
    """Движок по образу контейнера/пода, или None. Отсекает экспортеры и обёртки —
    и по образу, и по имени контейнера (образ мог быть переопределён, а имя говорящее)."""
    img = (image or "").lower()
    nm = (name or "").lower()
    if any(m in img or m in nm for m in _NOT_DB_MARKERS):
        return None
    for key, eng in _DB_IMAGES.items():
        if key in img:
            return eng
    return None


# движки, для которых панель умеет включить локальный дамп (helper: dump-setup <code>)
_DUMP_ENGINE = {"PostgreSQL": "pg", "MySQL/MariaDB": "mysql", "ClickHouse": "ch",
                "Redis": "redis", "Valkey": "redis", "RabbitMQ": "rabbitmq",
                # etcd на контроллере = кластер: k0s/k3s умеют бэкап сами, без exec
                "etcd": "k8s",
                # Grafana держит состояние в SQLite (grafana.db) — снимаем штатным
                # онлайн-бэкапом прямо с файла: exec в под не нужен, база лежит в PVC
                # на диске ноды, а хелпер её находит сам
                "Grafana": "grafana",
                # Neo4j: дамп снимается ТОЛЬКО с остановленной базы (онлайн-backup —
                # привилегия Enterprise), поэтому хелпер гасит её на время дампа.
                # Предупреждаем об этом в тексте находки — см. _DUMP_DOWNTIME.
                "Neo4j": "neo4j"}
# Движки, у которых дамп требует кратковременной остановки: пользователь должен узнать
# об этом ДО того, как включит ежедневный дамп, а не из графика доступности.
_DUMP_DOWNTIME = {"neo4j": "потребуется кратковременная остановка базы: "
                           "онлайн-дамп есть только в Enterprise"}

# Страховка от расхождения: движок из _DUMP_ENGINE обязан проходить валидацию команды,
# иначе кнопка «включить дампы» молча отвечает 422 (так уже случилось с grafana —
# движок добавили сюда, а белый список в схеме не тронули). Ловим на импорте панели.
for _code in sorted(set(_DUMP_ENGINE.values())):
    BackupCommandIn(action="dump_setup", engine=_code)
# Движки БЕЗ собственного состояния: кэш живёт только в памяти, бэкапить нечего.
# Memcached данные на диск не пишет вовсе; после перезапуска приложение просто
# наполняет кэш заново. (Redis сюда НЕ входит: у него есть RDB/AOF и его часто
# используют как хранилище.)
_STATELESS_CACHES = {"Memcached"}
# чем бэкапить каждый движок — совет должен быть конкретным, иначе он бесполезен
_DB_HOWTO = {
    "PostgreSQL": "pg_dump/pg_basebackup", "MySQL/MariaDB": "mysqldump/mariabackup",
    "MongoDB": "mongodump", "ClickHouse": "BACKUP / clickhouse-backup",
    "Elasticsearch": "snapshot API", "Redis": "RDB/AOF (BGSAVE)",
    "Valkey": "RDB/AOF (BGSAVE)", "InfluxDB": "influxd backup",
    "VictoriaMetrics": "snapshot API", "etcd": "etcdctl snapshot save",
    "CockroachDB": "BACKUP",
    "Cassandra": "nodetool snapshot", "ZooKeeper": "snapshot + txlog",
    "Kafka": "реплики/MirrorMaker", "Neo4j": "neo4j-admin dump",
    "RabbitMQ": "rabbitmqctl export_definitions", "CouchDB": "couchdb-dump / реплика",
    "MS SQL Server": "BACKUP DATABASE", "Prometheus": "snapshot API (admin)",
    "MinIO": "mc mirror", "HashiCorp Vault": "vault operator raft snapshot",
    "Consul": "consul snapshot save", "Grafana": "grafana.db / API дашбордов",
}


def _path_covered(path: str, mode: str, inc: list[str], exc: list[str]) -> bool:
    """Покрыт ли путь бэкапом. include — должен лежать под одним из перечисленных;
    exclude — покрыт, пока не попал под исключение. Сравниваем по границе сегмента,
    иначе /var/log-old ошибочно считался бы исключённым из-за /var/log."""
    def under(p: str, base: str) -> bool:
        base = base.rstrip("/")
        return p == base or p.startswith(base + "/")

    if mode == "include":
        return any(under(path, p) for p in inc)
    return not any(under(path, p) for p in exc)


def _backup_coverage(server: Server) -> list[BackupAudit]:
    """Аудит: что на ноде рискует не восстановиться. Три источника — точки монтирования
    с данными, bind-mount'ы контейнеров (осознанно проброшенные хост-пути = почти всегда
    данные) и живые СУБД. Только показ: алерты тут намеренно НЕ заводим, пока не увидим,
    что находки не шумят."""
    rep = server.last_report or {}
    bk = rep.get("backup") or {}
    out: list[BackupAudit] = []
    # СУБД проверяем даже без конфига бэкапа: риск не в покрытии, а в консистентности.
    # Два источника, потому что каждый по отдельности слеп: контейнеры не видят нативных
    # установок, а скан процессов (db_engines) не знает имён контейнеров. Сводим по движку,
    # чтобы одна база не показалась дважды.
    where: dict[str, list[str]] = {}
    for c in ((rep.get("docker") or {}).get("containers") or []):
        if (c.get("state") or "") != "running":
            continue
        eng = _db_engine_of(c.get("image") or "", c.get("name") or "")
        if eng:
            where.setdefault(eng, []).append(c.get("name") or c.get("image"))
    # kubernetes: агент шлёт образ только у СУБД-подов. Нужен отдельно от скана процессов —
    # под может крутиться на воркере, где агента нет, и в /proc control-plane его не видно.
    kube_pods: dict[str, list[str]] = {}
    for p in ((rep.get("kube") or {}).get("pods") or []):
        if not p.get("image") or p.get("phase") != "Running":
            continue
        eng = _db_engine_of(p.get("image") or "", p.get("name") or "")
        if eng:
            kube_pods.setdefault(eng, []).append(f"{p.get('ns', '?')}/{p.get('name', '?')}")
            where.setdefault(eng, [])
    for eng in (rep.get("db_engines") or []):
        where.setdefault(eng, [])  # найдена по процессу; контейнер может и не быть
    # УЖЕ настроенные дампы: CronJob с образом СУБД (у него же и утилиты дампа) либо с
    # «backup/dump» в имени. Без этого панель ныла бы «настройте дамп» на нодах, где он
    # работает годами, — ровно тот шум, из-за которого перестают читать уведомления.
    existing: dict[str, str] = {}
    for cj in ((rep.get("kube") or {}).get("cronjobs") or []):
        if cj.get("suspend"):
            continue  # выключенный джоб бэкапом не считается
        img, nm = (cj.get("image") or "").lower(), (cj.get("name") or "").lower()
        looks_backup = any(w in nm for w in ("backup", "dump", "pgdump", "mysqldump"))
        for key, eng in _DB_IMAGES.items():
            if key in img and (looks_backup or key in nm):
                label = f"{cj.get('ns', '?')}/{cj.get('name', '?')}"
                sched = cj.get("schedule") or ""
                existing[eng] = f"{label}{f' ({sched})' if sched else ''}"
                break

    # дампы, которые панель уже снимает сама (helper кладёт их в /backup/<движок>).
    # Без этого включённый дамп не закрывал находку: карточка оставалась оранжевой,
    # а на главной висел пункт «нужен отдельный дамп» — при работающем дампе.
    # ключ — (движок, контейнер): на ноде бывает несколько баз одного типа, и дамп
    # первой НЕ означает, что покрыта вторая. Пока ключом был один движок, включение
    # дампа для kervax-db-1 закрывало находку целиком, и zabbix-postgres молча
    # пропадал из аудита — выглядел покрытым, не будучи им.
    panel_dumps = {
        (d.get("engine"), d.get("container") or ""): d
        for d in ((rep.get("backup") or {}).get("dumps") or [])
        if d.get("engine")
    }
    for eng in sorted(where):
        # Кэши в памяти состояния не хранят: файлов данных у них нет, команды дампа тоже,
        # после рестарта они законно пустые. Держать их в «восстановимости под вопросом»
        # значит показывать пункт, с которым инженер ничего сделать не может.
        if eng in _STATELESS_CACHES:
            continue
        names = sorted(set(where[eng]))
        pods = sorted(set(kube_pods.get(eng, [])))
        code = _DUMP_ENGINE.get(eng, "")
        howto = _DB_HOWTO.get(eng)
        if eng in existing:
            # дамп уже есть (CronJob в кластере) — сообщаем как факт, без кнопки.
            # container/pods отдаём и здесь: по ним «Сервисы» показывают, ГДЕ живёт
            # СУБД. Раньше эта ветка их не заполняла, и странице оставался только
            # текст находки — в итоге она рисовала в поле «где» фразу про CronJob.
            out.append(BackupAudit(
                kind="db_ok", subject=eng, gap=False,
                detail=f"дамп уже настроен: CronJob {existing[eng]}",
                container=names[0] if len(names) == 1 else "",
                pods=pods[:4],
            ))
            continue
        # экземпляры: каждый контейнер — отдельная находка. Поды и нативная установка
        # контейнерного имени не имеют, поэтому идут одной записью с пустым instance.
        insts: list[str] = list(names) if names else [""]
        for inst in insts:
            dump = panel_dumps.get((code, inst)) if code else None
            # старый helper (< v8) не различал контейнеры и слал дамп без имени —
            # засчитываем его первому экземпляру, иначе после обновления панели
            # уже работающий дамп показался бы выключенным
            if dump is None and code and inst and inst == insts[0]:
                dump = panel_dumps.get((code, ""))
            where_txt = (
                f"контейнер: {inst}" if inst
                else ("под kubernetes: " + ", ".join(pods[:4]) if pods else "процесс на хосте")
            )
            if dump and (dump.get("files") or 0) > 0:
                out.append(BackupAudit(
                    kind="db_ok", subject=eng, gap=False, instance=inst,
                    detail=f"{where_txt} — дамп снимает панель перед каждым бэкапом",
                    dump_engine=code, can_dump=True, container=inst,
                ))
                continue
            if dump:
                # Только что включили? Файлов ещё нет законно — первый дамп в ближайший
                # бэкап. «Проблема» лишь если бэкап уже прошёл ПОСЛЕ включения, а файла нет.
                enabled = dump.get("enabled_ts") or 0
                last_bk = (bk.get("last_backup_ts") or 0)
                if enabled and last_bk and last_bk > enabled:
                    out.append(BackupAudit(
                        kind="db", subject=eng, gap=False, instance=inst,
                        detail=f"{where_txt} — дамп включён, но после бэкапа файлов в /backup"
                               " нет, проверьте, снимается ли он",
                        dump_engine=code, can_dump=True, container=inst,
                    ))
                else:
                    # ждём первого бэкапа по расписанию — это норма, не проблема
                    out.append(BackupAudit(
                        kind="db_ok", subject=eng, gap=False, instance=inst,
                        detail=f"{where_txt} — дамп включён, первый снимется в ближайший бэкап",
                        dump_engine=code, can_dump=True, container=inst,
                    ))
                continue
            # кнопку дампа даём только там, где helper реально может его снять: docker exec
            # или локально. Для пода нужен `kubectl exec`, а у агента в RBAC нет pods/exec —
            # молча расширять права до выполнения команд в любом поде нельзя.
            can_dump = bool(inst) or not pods
            out.append(BackupAudit(
                kind="db", subject=eng, gap=False, instance=inst,
                detail=f"{where_txt} — файловый снапшот живой базы может не восстановиться"
                       + (f", бэкапьте отдельно: {howto}" if howto else "")
                       + ("" if can_dump else " — дамп из пода снимает CronJob, панель покажет манифест"),
                dump_engine=code, can_dump=can_dump,
                downtime=_DUMP_DOWNTIME.get(code, "") if can_dump else "",
                container=inst, pods=pods[:4],
            ))
    # покрытие путей знаем только для нод с helper'ом (у остальных нет конфига)
    if not bk.get("manageable"):
        return out
    mode = bk.get("mode") or "exclude"
    inc = [p for p in (bk.get("includes") or []) if p.startswith("/")]
    exc = [p for p in (bk.get("excludes") or []) if p.startswith("/")]
    if mode == "include" and not inc:
        return out  # конфиг ещё не прочитан — не выдумываем дыры
    for d in (rep.get("disks") or []):
        m = d.get("mount") or ""
        if not m.startswith("/") or any(_under(m, x) for x in _AUDIT_SKIP_MOUNTS):
            continue
        # в include-режиме корень НИКОГДА не покрыт — это суть режима, а не находка.
        # Отдельные диски (/data и т.п.) флагуем: их забыть перечислить как раз легко.
        if mode == "include" and m == "/":
            continue
        if (d.get("used") or 0) < _AUDIT_MIN_BYTES:
            continue
        if not _path_covered(m, mode, inc, exc):
            gb = (d.get("used") or 0) / 1e9
            out.append(BackupAudit(
                kind="mount", subject=m, gap=True,
                detail=f"смонтированная ФС с данными ({gb:.0f} ГБ) не попадает в бэкап",
            ))
    seen: set[str] = set()
    for c in ((rep.get("docker") or {}).get("containers") or []):
        for b in (c.get("binds") or []):
            if not b.startswith("/") or b in seen:
                continue
            if any(_under(b, x) for x in _AUDIT_SKIP_MOUNTS + _AUDIT_SKIP_BINDS):
                continue
            if not _path_covered(b, mode, inc, exc):
                seen.add(b)
                out.append(BackupAudit(
                    kind="bind", subject=b, gap=True,
                    detail=f"данные контейнера «{c.get('name') or '?'}» не попадают в бэкап",
                ))
    out.extend(_kube_volume_audit(server, rep, mode, inc, exc))
    # ключ = вид+предмет: устойчив к переформулировке текста находки, поэтому
    # приглушение не «слетает» после правки описания
    mutes = set(server.backup_audit_mutes or [])
    for a in out:
        a.key = f"{a.kind}:{a.subject}" + (f":{a.instance}" if a.instance else "")
        a.muted = a.key in mutes
    return out


def _kube_volume_audit(
    server: Server, rep: dict, mode: str, inc: list[str], exc: list[str],
) -> list[BackupAudit]:
    """Тома кластера. hostPath/local — обычный каталог на ноде: сверяем с бэкапом, как
    любой другой путь. nfs/csi — данные физически вне ноды, файловый бэкап их не заберёт
    никогда; такие сводим в ОДИН пункт, иначе на кластере с облачными дисками аудит
    превратился бы в простыню из десятков одинаковых строк."""
    out: list[BackupAudit] = []
    vols = (rep.get("kube") or {}).get("volumes") or []
    if not vols:
        return out  # нет прав (старый RBAC) или нет томов — молчим, а не выдумываем
    # имена этой ноды в кластере: том с nodeAffinity на ДРУГУЮ ноду тут не при чём,
    # иначе каждый узел ныл бы про тома всех соседей
    me = {x for x in (server.hostname, server.name) if x}
    remote: dict[str, int] = {}
    for v in vols:
        node = v.get("node") or ""
        if node and node not in me:
            continue
        path = v.get("path") or ""
        if not path:
            # nfs/csi/прочее — каталога на ноде нет, копить в сводку
            remote[v.get("kind") or "?"] = remote.get(v.get("kind") or "?", 0) + 1
            continue
        if _path_covered(path, mode, inc, exc):
            continue
        claim = v.get("claim") or v.get("name") or "?"
        cap = v.get("capacity") or ""
        out.append(BackupAudit(
            kind="kube_vol", subject=path, gap=True,
            detail=f"том kubernetes «{claim}»{f' ({cap})' if cap else ''} не попадает в бэкап",
        ))
    if remote:
        kinds = ", ".join(f"{k} × {n}" for k, n in sorted(remote.items()))
        out.append(BackupAudit(
            kind="kube_vol_remote", subject="kubernetes", gap=False,
            detail=f"тома вне ноды ({kinds}) — файловый бэкап их не заберёт, "
                   "нужен снапшот хранилища или дамп изнутри пода",
        ))
    return out


def _ver_key(v: object) -> tuple[int, ...]:
    """Версия setup-скрипта «мажор.минор» → кортеж для сравнения. ПОКОМПОНЕНТНО, а не
    как число: 0.13 новее 0.2, хотя как дробь — наоборот. Нечисловое → (0,), то есть
    «древнее всего»: лучше лишний раз предложить переустановку, чем пропустить старьё.
    Старые числовые версии (9) тоже сюда попадают и остаются меньше любой 0.x."""
    parts = str(v or "").strip().split(".")
    try:
        t = tuple(int(p) for p in parts)
    except ValueError:
        return (0,)
    # Версия БЕЗ точки — это прежние схемы (счётчик 9, дата 20260721). Все они древнее
    # любой «мажор.минор», поэтому уводим их в отрицательный разряд. Без этого 20260721
    # оказалась бы новее 0.12, и нода со старым helper'ом молча осталась бы непомеченной.
    return (-1, t[0]) if len(t) == 1 else t


@router.get("/agent-release", response_model=AgentReleaseOut)
async def agent_release(user: CurrentUser) -> AgentReleaseOut:
    """Доступная подписанная версия агента + текущие версии setup-скриптов (UI флагует
    устаревшие helper'ы на нодах). problem != '' — релиз собран криво, обновлять нечем."""
    return AgentReleaseOut(
        version=_available_agent_version(),
        setup_versions=_current_setup_versions(),
        problem=agent_release_problem(),
    )


@router.post("/agent-update", response_model=list[ServerOut])
async def agent_update(
    body: AgentUpdateReq, user: CurrentUser, session: SessionDep
) -> list[ServerOut]:
    """Выставляет target-версию (панель попросит агенты обновиться). Разрешаем ТОЛЬКО
    ту версию, что реально подписана и раздаётся — иначе агент всё равно откажет.
    server_ids=None → все включённые (иначе canary/подмножество)."""
    avail = _available_agent_version()
    if not avail:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "На панели нет подписанного релиза агента"
        )
    if problem := agent_release_problem():
        raise HTTPException(status.HTTP_409_CONFLICT, problem)
    if body.version != avail:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Доступна к раскатке только версия {avail}"
        )
    q = select(Server).where(Server.enabled.is_(True))
    if body.server_ids is not None:
        q = q.where(Server.id.in_(body.server_ids))
    servers = list(await session.scalars(q))
    targeted = []
    for s in servers:
        if s.agent_version == body.version:  # уже на целевой — не трогаем
            continue
        s.target_agent_version = body.version
        targeted.append(s.name)
    await session.commit()
    await audit.record(
        session, user.username, "agent_update", f"{body.version}: {targeted}"
    )
    now = datetime.now(timezone.utc)
    return [_out(s, now) for s in servers]


@router.post("/agent-update/cancel", response_model=list[ServerOut])
async def agent_update_cancel(
    body: AgentUpdateCancel, user: CurrentUser, session: SessionDep
) -> list[ServerOut]:
    """Снимает target-версию (остановить раскатку). server_ids=None → со всех."""
    q = select(Server)
    if body.server_ids is not None:
        q = q.where(Server.id.in_(body.server_ids))
    else:
        q = q.where(Server.target_agent_version != "")
    servers = list(await session.scalars(q))
    names = [s.name for s in servers]
    for s in servers:
        s.target_agent_version = ""
    await session.commit()
    await audit.record(session, user.username, "agent_update_cancel", str(names))
    now = datetime.now(timezone.utc)
    return [_out(s, now) for s in servers]


@router.get("/{server_id}", response_model=ServerOut)
async def get_server(server_id: int, user: CurrentUser, session: SessionDep) -> ServerOut:
    server = await _get_or_404(server_id, session, user)
    return _out(server, datetime.now(timezone.utc))


@router.post("/{server_id}/snooze", response_model=ServerOut)
async def snooze_server(
    server_id: int, body: SnoozeIn, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Быстро приглушить алерты сервера на N часов (0 = снять)."""
    server = await _get_or_404(server_id, session, user)
    server.snooze_until = (
        datetime.now(timezone.utc) + timedelta(hours=body.hours) if body.hours > 0 else None
    )
    await session.commit()
    await session.refresh(server)
    await audit.record(session, user.username, "server_snooze", server.name, f"{body.hours}ч")
    return _out(server, datetime.now(timezone.utc))


@router.post("/{server_id}/snooze-alert", response_model=ServerOut)
async def snooze_server_alert(
    server_id: int, body: AlertSnoozeIn, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Точечно приглушить ОДИН тип алерта сервера на N часов (0 = снять). Остальные
    типы (напр. offline) продолжают алертить."""
    server = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    # чистим истёкшие + текущий, затем ставим новый (если hours>0)
    snoozes = {
        k: v
        for k, v in (server.alert_snoozes or {}).items()
        if k != body.kind and _parse_iso(v) is not None and _parse_iso(v) > now
    }
    if body.hours > 0:
        snoozes[body.kind] = (now + timedelta(hours=body.hours)).isoformat()
    server.alert_snoozes = snoozes
    await session.commit()
    await session.refresh(server)
    await audit.record(
        session, user.username, "server_snooze_alert", server.name,
        f"{body.kind}={body.hours}ч",
    )
    return _out(server, datetime.now(timezone.utc))


@router.patch("/{server_id}", response_model=ServerOut)
async def update_server(
    server_id: int, body: ServerUpdate, user: CurrentUser, session: SessionDep
) -> ServerOut:
    server = await _get_or_404(server_id, session, user)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(server, field, value)
    await session.commit()
    await session.refresh(server)
    await _write_agent_allowlist(session)
    await audit.record(session, user.username, "server_update", server.name)
    return _out(server, datetime.now(timezone.utc))


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: int, user: CurrentUser, session: SessionDep
) -> None:
    server = await _get_or_404(server_id, session, user)
    name = server.name
    await session.execute(
        sa_delete(ServerMetric).where(ServerMetric.server_id == server_id)
    )
    await session.delete(server)
    await session.commit()
    await _write_agent_allowlist(session)
    await audit.record(session, user.username, "server_delete", name)


@router.post("/{server_id}/rotate", response_model=ServerEnrollOut)
async def rotate_token(
    server_id: int, user: CurrentUser, session: SessionDep
) -> ServerEnrollOut:
    server = await _get_or_404(server_id, session, user)
    token = generate_agent_token()
    server.token_hash = hash_agent_token(token)
    await session.commit()
    await audit.record(session, user.username, "server_rotate", server.name)
    return ServerEnrollOut(
        server=_out(server, datetime.now(timezone.utc)),
        token=token,
        install_cmd=_install_cmd(token),
    )


def _bin_metrics(rows: list[ServerMetric], hours: float) -> list[ServerMetricOut]:
    step = max(int(hours * 3600 // 300), 15)
    buckets: dict[int, list[ServerMetric]] = {}
    for r in rows:
        b = int(r.ts.timestamp() // step) * step
        buckets.setdefault(b, []).append(r)
    out: list[ServerMetricOut] = []

    def avg(vals: list[float]) -> float | None:
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def avg_cores(rows: list[ServerMetric]) -> list[float] | None:
        arrs = [r.cpu_cores_pct for r in rows if r.cpu_cores_pct]
        if not arrs:
            return None
        n = min(len(a) for a in arrs)
        return [round(sum(a[i] for a in arrs) / len(arrs), 1) for i in range(n)] or None

    def avg_named(
        rows: list[ServerMetric], attr: str, key: str, vals: tuple[str, ...], nd: int
    ) -> list[dict] | None:
        # усредняет список [{key: name, val: …}, …] по name через все строки бакета;
        # None-значения (напр. temp без датчика) пропускаются → остаются None, не 0
        acc: dict[str, dict[str, list[float]]] = {}
        for r in rows:
            for it in getattr(r, attr) or []:
                name = it.get(key)
                if not name:
                    continue
                slot = acc.setdefault(name, {v: [] for v in vals})
                for v in vals:
                    x = it.get(v)
                    if x is not None:
                        slot[v].append(x)
        out = []
        for name, slot in sorted(acc.items()):
            row: dict = {key: name}
            for v, xs in slot.items():
                row[v] = round(sum(xs) / len(xs), nd) if xs else None
            out.append(row)
        return out or None

    for b in sorted(buckets):
        grp = buckets[b]
        disks = next((x.disks for x in reversed(grp) if x.disks), None)
        out.append(
            ServerMetricOut(
                ts=datetime.fromtimestamp(b, timezone.utc),
                cpu_percent=avg([x.cpu_percent for x in grp]),
                mem_percent=avg([x.mem_percent for x in grp]),
                disk_percent=avg([x.disk_percent for x in grp]),
                load1=avg([x.load1 for x in grp]),
                net_rx=avg([x.net_rx for x in grp]),
                net_tx=avg([x.net_tx for x in grp]),
                disk_read=avg([x.disk_read for x in grp]),
                disk_write=avg([x.disk_write for x in grp]),
                disk_read_iops=avg([x.disk_read_iops for x in grp]),
                disk_write_iops=avg([x.disk_write_iops for x in grp]),
                cpu_user=avg([x.cpu_user for x in grp]),
                cpu_system=avg([x.cpu_system for x in grp]),
                cpu_iowait=avg([x.cpu_iowait for x in grp]),
                cpu_irq=avg([x.cpu_irq for x in grp]),
                cpu_cores_pct=avg_cores(grp),
                cpu_freq=avg([x.cpu_freq for x in grp]),
                cpu_temp=avg([x.cpu_temp for x in grp]),
                cpu_throttle=avg([x.cpu_throttle for x in grp]),
                # OOM-киллы — счётчик событий: в бакет даунсемпла СУММИРУЕМ, не усредняем
                oom_kill=(sum(x.oom_kill or 0 for x in grp) or None),
                mem_cache=avg([x.mem_cache for x in grp]),
                mem_free=avg([x.mem_free for x in grp]),
                swap_in=avg([x.swap_in for x in grp]),
                swap_out=avg([x.swap_out for x in grp]),
                mem_slab=avg([x.mem_slab for x in grp]),
                mem_dirty=avg([x.mem_dirty for x in grp]),
                mem_writeback=avg([x.mem_writeback for x in grp]),
                net_ifaces=avg_named(grp, "net_ifaces", "if", ("rx", "tx", "errs", "drops"), 2),
                disk_devs=avg_named(grp, "disk_devs", "dev", ("util", "await", "temp"), 1),
                conntrack_count=avg([x.conntrack_count for x in grp]),
                conntrack_max=avg([x.conntrack_max for x in grp]),
                sock_used=avg([x.sock_used for x in grp]),
                sock_tcp=avg([x.sock_tcp for x in grp]),
                sock_tcp_tw=avg([x.sock_tcp_tw for x in grp]),
                sock_udp=avg([x.sock_udp for x in grp]),
                disks=disks,
            )
        )
    return out


@router.get("/{server_id}/metrics", response_model=list[ServerMetricOut])
async def server_metrics(
    server_id: int,
    user: CurrentUser,
    session: SessionDep,
    hours: int = Query(default=6, ge=1, le=8760),
    from_ts: float | None = Query(default=None),  # unix-секунды — произвольный диапазон
    to_ts: float | None = Query(default=None),
) -> list[ServerMetricOut]:
    await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    if from_ts is not None and to_ts is not None and to_ts > from_ts:
        lo = datetime.fromtimestamp(from_ts, timezone.utc)
        hi = datetime.fromtimestamp(to_ts, timezone.utc)
        span_hours = max((to_ts - from_ts) / 3600, 0.02)
    else:
        lo, hi, span_hours = now - timedelta(hours=hours), now, float(hours)
    rows = list(
        await session.scalars(
            select(ServerMetric)
            .where(
                ServerMetric.server_id == server_id,
                ServerMetric.ts >= lo,
                ServerMetric.ts <= hi,
            )
            .order_by(ServerMetric.ts)
        )
    )
    return _bin_metrics(rows, span_hours)


@router.get("/{server_id}/oom-events", response_model=list[OomEventOut])
async def server_oom_events(
    server_id: int,
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[OomEventOut]:
    """Журнал OOM-киллов сервера (новые первыми): когда и кого убило ядро."""
    await _get_or_404(server_id, session, user)
    rows = list(
        await session.scalars(
            select(OomEvent)
            .where(OomEvent.server_id == server_id)
            .order_by(OomEvent.ts.desc())
            .limit(limit)
        )
    )
    return [OomEventOut.model_validate(r) for r in rows]


async def _take_docker_commands(session, server_id: int) -> list[dict]:
    """Забирает pending docker-команды сервера, помечает running (не переслать
    повторно), возвращает для агента. Общий код /report и /commands (НЕ коммитит)."""
    pending = list(
        await session.scalars(
            select(DockerCommand)
            .where(DockerCommand.server_id == server_id, DockerCommand.status == "pending")
            .order_by(DockerCommand.created_at)
            .limit(20)
        )
    )
    cmds = []
    for c in pending:
        c.status = "running"
        cmds.append({
            "id": c.id, "container": c.container, "action": c.action,
            "tail": c.tail, "since": c.since,
        })
    return cmds


def _docker_cmd_out(c: DockerCommand, now: datetime) -> DockerCommandOut:
    # застрявшие в running (агент не ответил) старше 90с → таймаут
    if c.status == "running" and (now - c.created_at.replace(
        tzinfo=c.created_at.tzinfo or timezone.utc
    )).total_seconds() > 90:
        c.status, c.ok, c.result = "error", False, "агент не ответил (таймаут)"
    return DockerCommandOut.model_validate(c)


@router.post("/{server_id}/docker/command", response_model=DockerCommandOut)
async def docker_command(
    server_id: int, body: DockerCommandIn, user: CurrentUser, session: SessionDep
) -> DockerCommandOut:
    """Поставить docker-действие в очередь (агент заберёт в ответе на отчёт, ~15с).
    Исполняется через read-only proxy: только restart/stop/start + logs."""
    await _get_or_404(server_id, session, user)
    c = DockerCommand(
        server_id=server_id, container=body.container, action=body.action,
        tail=body.tail, since=body.since,
    )
    session.add(c)
    await session.commit()
    await session.refresh(c)
    await audit.record(
        session, user.username, f"docker_{body.action}", body.container[:120], f"srv={server_id}"
    )
    return DockerCommandOut.model_validate(c)


@router.get("/{server_id}/docker/command/{cmd_id}", response_model=DockerCommandOut)
async def docker_command_status(
    server_id: int, cmd_id: int, user: CurrentUser, session: SessionDep
) -> DockerCommandOut:
    """Статус/результат команды — панель поллит после постановки."""
    c = await session.get(DockerCommand, cmd_id)
    if c is None or c.server_id != server_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Команда не найдена")
    out = _docker_cmd_out(c, datetime.now(timezone.utc))
    await session.commit()  # зафиксировать возможный таймаут-переход
    return out


async def _take_kube_commands(session, server_id: int) -> list[dict]:
    """Забирает pending kube-команды сервера, помечает running. Общий код /report и
    /commands (НЕ коммитит)."""
    pending = list(
        await session.scalars(
            select(KubeCommand)
            .where(KubeCommand.server_id == server_id, KubeCommand.status == "pending")
            .order_by(KubeCommand.created_at)
            .limit(20)
        )
    )
    cmds = []
    for c in pending:
        c.status = "running"
        cmds.append({
            "id": c.id, "ns": c.ns, "kind": c.kind, "name": c.name,
            "action": c.action, "tail": c.tail, "since": c.since,
        })
    return cmds


def _kube_cmd_out(c: KubeCommand, now: datetime) -> KubeCommandOut:
    if c.status == "running" and (now - c.created_at.replace(
        tzinfo=c.created_at.tzinfo or timezone.utc
    )).total_seconds() > 90:
        c.status, c.ok, c.result = "error", False, "агент не ответил (таймаут)"
    return KubeCommandOut.model_validate(c)


@router.post("/{server_id}/kube/command", response_model=KubeCommandOut)
async def kube_command(
    server_id: int, body: KubeCommandIn, user: CurrentUser, session: SessionDep
) -> KubeCommandOut:
    """Поставить kube-действие в очередь (агент заберёт ~1-15с). Исполняется по токену
    узкого SA: только rollout_restart (deploy/sts/ds), delete_pod, logs."""
    await _get_or_404(server_id, session, user)
    c = KubeCommand(
        server_id=server_id, ns=body.ns, kind=body.kind, name=body.name,
        action=body.action, tail=body.tail, since=body.since,
    )
    session.add(c)
    await session.commit()
    await session.refresh(c)
    await audit.record(
        session, user.username, f"kube_{body.action}",
        f"{body.ns}/{body.kind}/{body.name}"[:120], f"srv={server_id}"
    )
    return KubeCommandOut.model_validate(c)


@router.get("/{server_id}/kube/command/{cmd_id}", response_model=KubeCommandOut)
async def kube_command_status(
    server_id: int, cmd_id: int, user: CurrentUser, session: SessionDep
) -> KubeCommandOut:
    c = await session.get(KubeCommand, cmd_id)
    if c is None or c.server_id != server_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Команда не найдена")
    out = _kube_cmd_out(c, datetime.now(timezone.utc))
    await session.commit()
    return out


async def _take_backup_commands(session, server_id: int) -> list[dict]:
    """Забирает pending backup-команды сервера, помечает running. Общий код /report и
    /commands (НЕ коммитит)."""
    pending = list(
        await session.scalars(
            select(BackupCommand)
            .where(BackupCommand.server_id == server_id, BackupCommand.status == "pending")
            .order_by(BackupCommand.created_at)
            .limit(20)
        )
    )
    cmds = []
    for c in pending:
        c.status = "running"
        d = {
            "id": c.id, "action": c.action, "mode": c.mode,
            "paths": c.paths or [], "schedule": c.schedule,
        }
        if c.payload:  # доп. поля провижининга (repo_url/repopass/hpass/cacert/retention…)
            d.update(c.payload)
        cmds.append(d)
    return cmds


def _backup_cmd_out(c: BackupCommand, now: datetime) -> BackupCommandOut:
    if c.status == "running" and (now - c.created_at.replace(
        tzinfo=c.created_at.tzinfo or timezone.utc
    )).total_seconds() > 90:
        c.status, c.ok, c.result = "error", False, "агент не ответил (таймаут)"
    return BackupCommandOut.model_validate(c)


@router.post("/{server_id}/backup/command", response_model=BackupCommandOut)
async def backup_command(
    server_id: int, body: BackupCommandIn, user: CurrentUser, session: SessionDep
) -> BackupCommandOut:
    """Поставить backup-действие в очередь (агент заберёт ~1-15с). Исполняется через
    узкий helper: только set_paths / set_schedule / run_now."""
    await _get_or_404(server_id, session, user)
    # engine/container нужны только dump_setup — кладём в payload (агент мержит его
    # в команду верхним уровнем), чтобы не плодить колонки под разовые поля
    payload = None
    if body.action in ("dump_setup", "dump_remove"):
        if not body.engine:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "не указан движок дампа")
        payload = {"engine": body.engine, "container": body.container}
        if body.action == "dump_setup":
            # настройки едут только при включении; агент подставит дефолты для пустых
            payload["dump_dir"] = body.dump_dir
            payload["dump_keep"] = body.dump_keep
            payload["dump_minfree"] = body.dump_minfree
    c = BackupCommand(
        server_id=server_id, action=body.action, mode=body.mode,
        paths=body.paths, schedule=body.schedule, payload=payload,
    )
    session.add(c)
    await session.commit()
    await session.refresh(c)
    await audit.record(
        session, user.username, f"backup_{body.action}",
        (body.schedule or f"{body.mode}:{len(body.paths)}")[:120], f"srv={server_id}"
    )
    return BackupCommandOut.model_validate(c)


@router.get("/{server_id}/backup/command/{cmd_id}", response_model=BackupCommandOut)
async def backup_command_status(
    server_id: int, cmd_id: int, user: CurrentUser, session: SessionDep
) -> BackupCommandOut:
    c = await session.get(BackupCommand, cmd_id)
    if c is None or c.server_id != server_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Команда не найдена")
    out = _backup_cmd_out(c, datetime.now(timezone.utc))
    await session.commit()
    return out


@router.post("/{server_id}/backup/audit-mute", response_model=ServerOut)
async def backup_audit_mute(
    server_id: int, body: BackupAuditMuteIn, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Приглушить/вернуть ОДНУ находку покрытия («этот RabbitMQ бэкапить не нужно»),
    не глуша остальные. Находка остаётся видимой в свёрнутом списке."""
    s = await _get_or_404(server_id, session, user)
    mutes = set(s.backup_audit_mutes or [])
    if body.muted:
        mutes.add(body.key)
    else:
        mutes.discard(body.key)
    s.backup_audit_mutes = sorted(mutes)
    await session.commit()
    await session.refresh(s)
    await audit.record(
        session, user.username,
        "backup_audit_mute" if body.muted else "backup_audit_unmute",
        body.key[:120], f"srv={server_id}",
    )
    return _out(s, datetime.now(timezone.utc))


@router.post("/{server_id}/backup/repo-mute", response_model=ServerOut)
async def backup_repo_mute(
    server_id: int, body: BackupRepoMuteIn, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Заглушить/включить репозиторий бэкап-сервера (разовые/неактуальные) — не считать
    проблемой и не алертить на устаревание."""
    s = await _get_or_404(server_id, session, user)
    mutes = set(s.backup_repo_mutes or [])
    if body.muted:
        mutes.add(body.repo)
    else:
        mutes.discard(body.repo)
    s.backup_repo_mutes = sorted(mutes)
    await session.commit()
    await session.refresh(s)
    await audit.record(
        session, user.username, "backup_repo_mute" if body.muted else "backup_repo_unmute",
        body.repo[:120], f"srv={server_id}"
    )
    return _out(s, datetime.now(timezone.utc))


# дефолтный exclude-список — бэкапим «/» кроме мусора. НЕ исключаем данные
# (/etc /home /root /srv /opt /var/www /var/lib/<БД> /lib65 /var/lib/docker/volumes).
# Исключаем: псевдо-ФС, temp/кэш, переустанавливаемое, слои образов контейнеров.
DEFAULT_BACKUP_EXCLUDES = [
    "/proc", "/sys", "/dev", "/run", "/var/lib/lxcfs",
    "/tmp", "/var/tmp", "/var/cache", "/var/lib/apt/lists", "/var/lib/systemd/coredump",
    "/swapfile", "/swap.img",
    "/usr", "/boot", "/snap", "/lib/modules", "/lib/firmware",
    "/var/lib/docker/overlay2", "/var/lib/docker/containers", "/var/lib/docker/tmp",
    "/var/lib/docker/buildkit", "/var/lib/containerd",
    "/mnt", "/media", "/var/log",
]


def _gen_secret(n: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _san_backup_name(raw: str, fallback_id: int) -> str:
    s = "".join(c for c in (raw or "").lower() if c.isalnum() or c in "._-").strip("._-")
    return s or f"node{fallback_id}"


def _pick_ip(s: Server) -> str:
    return (s.external_ip or s.agent_ip or "").strip()


async def _bcmd_create(session_factory, server_id: int, action: str, payload: dict) -> int:
    """Ставит backup-команду в очередь (короткая сессия). mode/schedule/paths → колонки
    (агент читает top-level), прочее → payload."""
    p = dict(payload)
    async with session_factory() as s:
        c = BackupCommand(
            server_id=server_id, action=action,
            mode=p.pop("mode", "exclude"), schedule=p.pop("schedule", ""),
            paths=p.pop("paths", None), payload=(p or None),
        )
        s.add(c)
        await s.commit()
        return c.id


async def _bcmd_wait(session_factory, cid: int, timeout: int) -> tuple[bool, str]:
    """Ждёт результат агента, опрашивая СВЕЖЕЙ сессией каждую итерацию (новое соединение →
    гарантированно видит коммит агента; не держим долгое соединение/снапшот)."""
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    while datetime.now(timezone.utc) < deadline:
        await asyncio.sleep(1.5)
        async with session_factory() as s:
            cur = await s.get(BackupCommand, cid)
            if cur is not None and cur.status in ("done", "error"):
                return bool(cur.ok), cur.result or ""
    async with session_factory() as s:
        cur = await s.get(BackupCommand, cid)
        if cur is not None and cur.status not in ("done", "error"):
            cur.status, cur.ok, cur.result = "error", False, "таймаут ожидания агента"
            await s.commit()
    return False, "таймаут ожидания агента"


# держим сильные ссылки на фоновые задачи оркестрации: без этого asyncio.create_task
# может быть собрана GC на середине выполнения (документированная ловушка asyncio).
_setup_tasks: set = set()


# helper бэкап-сервера возвращает пароль СУЩЕСТВУЮЩЕГО репо (панель его не знает) — в БД и в
# UI он попасть не должен; живёт только в памяти оркестратора на время настройки клиента.
_SECRET_OUT_RE = re.compile(r"REPOPASS_B64=\S+")


def _job_runner(session_factory, job_id: int):
    """Общая машинерия фоновых job'ов настройки: список шагов + save/step.
    Короткие сессии на каждую операцию (долгая сессия отваливается на провижининге)."""
    steps: list[dict] = []

    async def save(status_=None, message=None):
        async with session_factory() as s:
            job = await s.get(BackupSetupJob, job_id)
            if job is None:
                return
            job.steps = list(steps)
            if status_:
                job.status = status_
            if message is not None:
                job.message = message
            await s.commit()

    async def step(label, sid, action, payload, timeout=110):
        cid = await _bcmd_create(session_factory, sid, action, payload)
        ok, out = await _bcmd_wait(session_factory, cid, timeout)
        # detail уезжает в БД и в UI → вычищаем секреты, которые helper вернул панели
        # (пароль существующего репо). Сырой out остаётся только в памяти оркестратора.
        detail = _SECRET_OUT_RE.sub("REPOPASS_B64=<скрыто>", out or "")[:300]
        steps.append({"step": label, "ok": ok, "detail": detail})
        await save()
        return ok, out

    return steps, save, step


async def _orchestrate_deploy_server(session_factory, job_id: int, params: dict) -> None:
    """Поднять rest-server на ноде с нуля (helper ставит docker/restic/htpasswd из штатных
    реп дистрибутива и запускает контейнер с --append-only --private-repos) + опц. TLS-фронт.
    Секретов на этом этапе нет: репозитории и пароли появятся позже, при настройке клиентов."""
    _steps, save, step = _job_runner(session_factory, job_id)
    try:
        # долгий шаг: apt update/install + docker pull образа
        ok, out = await step("deploy-server", params["node_id"], "deploy_server",
                             {"port": params["port"]}, 400)
        if not ok:
            hint = (out or "").replace("\r", " ").strip()
            return await save("error", hint[:250] or "не удалось развернуть rest-server")
        if params["tls"]:
            ok, out = await step("tls-front", params["node_id"], "deploy_tls_front",
                                 {"san_ip": params["node_ip"], "san_dns": params["node_hostname"]}, 300)
            if not ok:
                hint = (out or "").replace("\r", " ").strip()
                return await save("error", ("rest-server поднят, но TLS-фронт не удался: " + hint)[:250])
        await save("done", "бэкап-сервер развёрнут")
    except BaseException as e:  # noqa: BLE001 — как и в _orchestrate_setup, не теряем задачу молча
        import traceback
        print("backup_server_deploy orchestration FAILED:", repr(e), traceback.format_exc(), flush=True)
        try:
            await save("error", f"сбой оркестрации: {type(e).__name__}: {e}"[:250])
        except BaseException:  # noqa: BLE001
            pass


async def _orchestrate_enable_tls(session_factory, job_id: int, params: dict) -> None:
    """Поднять self-signed TLS-фронт (caddy :64101) на УЖЕ работающем HTTP rest-server —
    миграция существующего сервера на HTTPS. HTTP :64100 НЕ трогаем (старые клиенты живут).
    После этого клиентов переводим на https отдельно (пере-энролл с TLS → repo-URL + --cacert)."""
    _steps, save, step = _job_runner(session_factory, job_id)
    try:
        ok, out = await step("tls-front", params["node_id"], "deploy_tls_front",
                             {"san_ip": params["node_ip"], "san_dns": params["node_hostname"]}, 300)
        if not ok:
            hint = (out or "").replace("\r", " ").strip()
            return await save("error", ("не удалось поднять TLS-фронт: " + hint)[:250])
        await save("done", "HTTPS-фронт (self-signed) поднят на :64101 — клиентов можно переводить на https")
    except BaseException as e:  # noqa: BLE001
        import traceback
        print("enable_tls orchestration FAILED:", repr(e), traceback.format_exc(), flush=True)
        try:
            await save("error", f"сбой оркестрации: {type(e).__name__}: {e}"[:250])
        except BaseException:  # noqa: BLE001
            pass


async def _orchestrate_setup(session_factory, job_id: int, params: dict) -> None:
    """Фоновая оркестрация: короткие сессии на каждую операцию, обновляет job.steps/status.
    Провижининг долгий (caddy pull, restic install) → фронт поллит статус."""
    steps, save, step = _job_runner(session_factory, job_id)

    try:
        name = params["name"]
        server_ip, hpass, repopass = params["server_ip"], params["hpass"], params["repopass"]
        cacert_b64 = "-"
        if params["tls"]:
            ok, _ = await step("tls-front", params["bs_id"], "deploy_tls_front",
                               {"san_ip": server_ip, "san_dns": params["bs_hostname"]}, 200)
            if not ok:
                return await save("error", "не удалось поднять TLS-фронт")
            ok, cert = await step("get-cert", params["bs_id"], "get_cert", {})
            if not ok or not (cert or "").strip():
                return await save("error", "не удалось получить сертификат")
            cacert_b64 = cert.strip()
            repo_url = f"rest:https://{name}:{hpass}@{server_ip}:{params['tls_port']}/{name}"
        else:
            repo_url = f"rest:http://{name}:{hpass}@{server_ip}:{params['http_port']}/{name}"

        ok, prov_out = await step("provision-repo", params["bs_id"], "provision_client",
                                  {"name": name, "hpass": hpass, "repopass": repopass,
                                   "client_ip": params["client_ip"], **params["retention"]}, 150)
        if not ok:
            # сообщения helper'а тут действительно полезные («репо есть, но пароль не найден…»)
            hint = _SECRET_OUT_RE.sub("", prov_out or "").replace("\r", " ").strip()
            return await save("error", hint[:250] or "не удалось создать репозиторий на бэкап-сервере")
        # Репо уже существовало → сервер вернул ЕГО пароль: репозиторий навсегда остаётся на
        # первом пароле, и клиента надо настраивать именно на него (иначе «wrong password»).
        # История снапшотов сохраняется — ничего не пересоздаём и не удаляем.
        m = re.search(r"REPOPASS_B64=([A-Za-z0-9+/=]+)", prov_out or "")
        if m:
            try:
                repopass = base64.b64decode(m.group(1)).decode().strip()
            except (ValueError, UnicodeDecodeError):
                return await save("error", "бэкап-сервер вернул нечитаемый пароль существующего репозитория")
            if not repopass:
                return await save("error", "бэкап-сервер вернул пустой пароль существующего репозитория")
            steps.append({"step": "existing-repo", "ok": True, "detail": ""})
            await save()
        ok, _ = await step("provision-client", params["client_id"], "provision",
                           {"repo_url": repo_url, "repopass": repopass, "mode": params["mode"],
                            "schedule": params["schedule"], "paths": params["paths"], "delay": "1h",
                            # версию restic НЕ диктуем: её знает helper (RESTIC_TARGET_VER).
                            # Здесь была зашита 0.18.1 — свежая нода получала устаревший
                            # restic, и панель тут же предлагала «обновить до 0.19.1».
                            "cacert_b64": cacert_b64}, 200)
        if not ok:
            return await save("error", "не удалось настроить бэкап на клиенте")
        await step("first-backup", params["client_id"], "run_now", {}, 60)
        await save("done", "бэкап настроен")
    except BaseException as e:  # noqa: BLE001 — ловим и CancelledError, чтобы не терять задачу молча
        import traceback
        print("backup_setup orchestration FAILED:", repr(e), traceback.format_exc(), flush=True)
        try:
            await save("error", f"сбой оркестрации: {type(e).__name__}: {e}"[:250])
        except BaseException:  # noqa: BLE001
            pass
        raise


@router.post("/{server_id}/backup/setup", response_model=BackupSetupJobOut)
async def backup_setup(
    server_id: int, body: BackupSetupIn, request: Request, user: CurrentUser, session: SessionDep
) -> BackupSetupJobOut:
    """Запустить фоновую оркестрацию «настроить бэкап»: генерит секреты (в БД НЕ хранит),
    провижинит репо на бэкап-сервере (htpasswd/init/ufw/prune) и бэкап на клиенте
    (restic/env/timer). Возвращает job — фронт поллит /backup/setup/{job_id}."""
    client = await _get_or_404(server_id, session, user)
    bs = await _get_or_404(body.backup_server_id, session)
    crep = client.last_report or {}
    bk = crep.get("backup") or {}
    if bk.get("configured") or bk.get("metric_present"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "на этом сервере уже настроен бэкап")
    if not ((bs.last_report or {}).get("backup_server") or {}).get("present"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "выбранный сервер не является бэкап-сервером")
    client_ip, server_ip = _pick_ip(client), _pick_ip(bs)
    if not client_ip or not server_ip:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "не удалось определить IP клиента/сервера")
    paths = body.paths or (list(DEFAULT_BACKUP_EXCLUDES) if body.mode == "exclude" else [])
    if not paths:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "для режима include укажите пути")
    name = _san_backup_name(client.hostname or client.name, client.id)
    job = BackupSetupJob(server_id=client.id, backup_server_id=bs.id, status="running", steps=[])
    session.add(job)
    await session.commit()
    await audit.record(
        session, user.username, "backup_setup", f"{name}→{bs.name} ({body.mode})", f"srv={server_id}"
    )
    # Порты берём у САМОГО бэкап-сервера (агент шлёт их в отчёте), а не константами:
    # панель разрешает развернуть rest-server на своём порту, и тогда репозиторий
    # надо создавать там же. Раньше здесь стояли 64100/64101, и сервер на другом
    # порту разворачивался успешно, а настройка клиента падала на init.
    bs_rep = (bs.last_report or {}).get("backup_server") or {}
    params = {
        "client_id": client.id, "bs_id": bs.id, "bs_hostname": bs.hostname or "",
        "http_port": int(bs_rep.get("port") or 64100),
        "tls_port": int(bs_rep.get("tls_port") or 64101),
        "name": name, "client_ip": client_ip, "server_ip": server_ip,
        "hpass": _gen_secret(), "repopass": _gen_secret(),
        "mode": body.mode, "schedule": body.schedule, "paths": paths, "tls": body.tls,
        "retention": {"keep_last": body.keep_last, "keep_daily": body.keep_daily,
                      "keep_weekly": body.keep_weekly, "keep_monthly": body.keep_monthly},
    }
    task = asyncio.create_task(_orchestrate_setup(request.app.state.session_factory, job.id, params))
    _setup_tasks.add(task)
    task.add_done_callback(_setup_tasks.discard)
    return BackupSetupJobOut.model_validate(job)


@router.post("/{server_id}/backup-server/deploy", response_model=BackupSetupJobOut)
async def backup_server_deploy(
    server_id: int, body: BackupServerDeployIn, request: Request, user: CurrentUser,
    session: SessionDep,
) -> BackupSetupJobOut:
    """Развернуть rest-server на ноде с нуля: helper ставит docker/restic/htpasswd из штатных
    реп и поднимает контейнер с ЗАШИТЫМИ --append-only --private-repos (панель не может их
    ослабить). Идемпотентно; существующий compose helper не перезаписывает.
    Прогресс — через тот же GET /backup/setup/{job_id}."""
    node = await _get_or_404(server_id, session, user)
    rep = node.last_report or {}
    if ((rep.get("backup_server") or {}).get("present")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "на этой ноде rest-server уже развёрнут")
    # агент не отличает «helper есть, сервер не развёрнут» от «ничего нет» (backup_server=nil),
    # поэтому готовность определяем по версиям setup-скриптов
    if not (rep.get("setup_versions") or {}).get("backupserver-setup"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "на ноде нет helper'а бэкап-сервера — сначала выполните backupserver-setup.sh",
        )
    node_ip = _pick_ip(node)
    if not node_ip:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "не удалось определить IP ноды")
    job = BackupSetupJob(server_id=node.id, backup_server_id=node.id, status="running", steps=[])
    session.add(job)
    await session.commit()
    await audit.record(
        session, user.username, "backup_server_deploy", f"{node.name}:{body.port}", f"srv={server_id}"
    )
    params = {
        "node_id": node.id, "node_ip": node_ip, "node_hostname": node.hostname or "",
        "port": body.port, "tls": body.tls,
    }
    task = asyncio.create_task(
        _orchestrate_deploy_server(request.app.state.session_factory, job.id, params)
    )
    _setup_tasks.add(task)
    task.add_done_callback(_setup_tasks.discard)
    return BackupSetupJobOut.model_validate(job)


@router.post("/{server_id}/backup-server/enable-tls", response_model=BackupSetupJobOut)
async def backup_server_enable_tls(
    server_id: int, request: Request, user: CurrentUser, session: SessionDep,
) -> BackupSetupJobOut:
    """Миграция существующего HTTP rest-server на HTTPS: поднять self-signed TLS-фронт
    (caddy :64101) рядом, HTTP :64100 остаётся. Прогресс — GET /backup/setup/{job_id}."""
    node = await _get_or_404(server_id, session, user)
    rep = node.last_report or {}
    bs = rep.get("backup_server") or {}
    if not bs.get("present"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "на этой ноде нет rest-server")
    if bs.get("tls_front"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "TLS-фронт уже поднят")
    if not (rep.get("setup_versions") or {}).get("backupserver-setup"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "на ноде нет helper'а бэкап-сервера")
    node_ip = _pick_ip(node)
    if not node_ip:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "не удалось определить IP ноды")
    job = BackupSetupJob(server_id=node.id, backup_server_id=node.id, status="running", steps=[])
    session.add(job)
    await session.commit()
    await audit.record(
        session, user.username, "backup_server_enable_tls", node.name, f"srv={server_id}"
    )
    params = {"node_id": node.id, "node_ip": node_ip, "node_hostname": node.hostname or ""}
    task = asyncio.create_task(
        _orchestrate_enable_tls(request.app.state.session_factory, job.id, params)
    )
    _setup_tasks.add(task)
    task.add_done_callback(_setup_tasks.discard)
    return BackupSetupJobOut.model_validate(job)


@router.get("/{server_id}/backup/setup/{job_id}", response_model=BackupSetupJobOut)
async def backup_setup_status(
    server_id: int, job_id: int, user: CurrentUser, session: SessionDep
) -> BackupSetupJobOut:
    job = await session.get(BackupSetupJob, job_id)
    if job is None or job.server_id != server_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Задача не найдена")
    return BackupSetupJobOut.model_validate(job)


async def _backup_server_for(session, repo_url: str):
    """Бэкап-сервер, на который смотрит repo-URL клиента, и имя репо в нём."""
    m = re.match(r"rest:[a-z]+://([^:/]+)", repo_url or "")
    if not m:
        return None, ""
    host = m.group(1)
    name = (repo_url or "").rstrip("/").rsplit("/", 1)[-1]
    for cand in await session.scalars(select(Server).where(Server.enabled.is_(True))):
        if not ((cand.last_report or {}).get("backup_server") or {}).get("present"):
            continue
        if host in (cand.external_ip or "", cand.agent_ip or "", cand.hostname or ""):
            return cand, name
    return None, name


async def _fetch_cacert_pem(factory, session, repo_url: str) -> str:
    """САМ сертификат бэкап-сервера (PEM). Клиентский helper отдаёт лишь ПУТЬ к файлу
    на своей ноде — на чужой машине это бесполезно, а восстанавливаются как раз не на
    ней. Поэтому content берём у бэкап-сервера его же командой get_cert."""
    if not (repo_url or "").startswith("rest:https://"):
        return ""  # http-репо серт не требует
    bs, _ = await _backup_server_for(session, repo_url)
    if bs is None:
        return ""
    try:
        cid = await _bcmd_create(factory, bs.id, "get_cert", {})
        ok, out = await _bcmd_wait(factory, cid, 40)
        if ok and (out or "").strip():
            return base64.b64decode(out.strip()).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    return ""


@router.post("/{server_id}/backup/credentials", response_model=BackupCredsOut)
async def backup_credentials(
    server_id: int, request: Request, user: AdminUser, session: SessionDep
) -> BackupCredsOut:
    """Данные для восстановления (repo URL + пароль) — читаются С НОДЫ по запросу (helper
    читает env), в БД НЕ хранятся. Это ключ дешифровки бэкапа — показываем осознанно."""
    server = await _get_or_404(server_id, session, user)
    factory = request.app.state.session_factory

    def _decode(out: str) -> dict[str, str]:
        d: dict[str, str] = {}
        for ln in base64.b64decode(out).decode().splitlines():
            k, _, v = ln.partition("=")
            if k:
                d[k] = v
        return d

    # 1) пробуем достать с самого клиента (полный URL + пароль + cacert)
    cid = await _bcmd_create(factory, server_id, "get_creds", {})
    ok, out = await _bcmd_wait(factory, cid, 40)
    if ok and (out or "").strip():
        try:
            d = _decode(out)
            await audit.record(session, user.username, "backup_credentials_view", server.name, f"srv={server_id}")
            url = d.get("repo_url", "")
            return BackupCredsOut(
                repo_url=url, repopass=d.get("repopass", ""),
                cacert_file=d.get("cacert_file", ""),
                cacert_pem=await _fetch_cacert_pem(factory, session, url),
                source="client",
            )
        except Exception:  # noqa: BLE001
            pass

    # 2) FALLBACK (клиент мёртв/недоступен): достаём пароль с БЭКАП-СЕРВЕРА (prune-env
    #    хранит тот же repopass рядом с бэкапами). Сервер находим по repo_dest клиента.
    repo_dest = ((server.last_report or {}).get("backup") or {}).get("repo_dest") or ""
    m = re.match(r"rest:[a-z]+://([^:/]+)", repo_dest)
    if m:
        host = m.group(1)
        name = repo_dest.rstrip("/").rsplit("/", 1)[-1]
        bs = None
        for cand in await session.scalars(select(Server).where(Server.enabled.is_(True))):
            if not ((cand.last_report or {}).get("backup_server") or {}).get("present"):
                continue
            if host in (cand.external_ip or "", cand.agent_ip or "", cand.hostname or ""):
                bs = cand
                break
        if bs is not None and name:
            cid2 = await _bcmd_create(factory, bs.id, "get_client_creds", {"name": name})
            ok2, out2 = await _bcmd_wait(factory, cid2, 40)
            if ok2 and (out2 or "").strip():
                try:
                    d = _decode(out2)
                    await audit.record(session, user.username, "backup_credentials_view", f"{server.name} (via {bs.name})", f"srv={server_id}")
                    return BackupCredsOut(
                        repo_url=repo_dest, repopass=d.get("repopass", ""),
                        repo_local=d.get("repo_local", ""),
                        cacert_pem=await _fetch_cacert_pem(factory, session, repo_dest),
                        source="backup-server", server_name=bs.name,
                    )
                except Exception:  # noqa: BLE001
                    pass

    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        (out or "").strip() or "не удалось получить данные (клиент недоступен и на бэкап-сервере не нашлось; helper < 1.44?)",
    )


# --- раздача агента (публично: бинарь и скрипт не секретны, токен передаётся отдельно) ---


@agent_router.get("/install.sh")
async def install_script() -> PlainTextResponse:
    path = os.path.join(get_settings().agent_dist_dir, "install.sh")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "install.sh не найден")
    with open(path, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/x-shellscript")


@agent_router.get("/install-ru.sh")
async def install_script_ru() -> PlainTextResponse:
    """Тот же установщик с сообщениями на русском.

    Основной install.sh англоязычный — его видят все, кто ставит панель. Русская
    копия нужна там, где команду выполняет человек, которому так понятнее, и
    отличается она ровно языком вывода."""
    path = os.path.join(get_settings().agent_dist_dir, "install-ru.sh")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "install-ru.sh не найден")
    with open(path, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/x-shellscript")


@agent_router.get("/kube-setup.sh")
async def kube_setup_script() -> PlainTextResponse:
    """Скрипт включения k8s-доступа: создаёт узкий ServiceAccount + kube.json.
    Запускается root'ом на control-plane ноде (панель показывает команду)."""
    path = os.path.join(get_settings().agent_dist_dir, "kube-setup.sh")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "kube-setup.sh не найден")
    with open(path, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/x-shellscript")


@agent_router.get("/backup-setup.sh")
async def backup_setup_script() -> PlainTextResponse:
    """Скрипт включения управления бэкапом: ставит узкий helper в /lib65 + sudoers.
    Запускается root'ом на клиентской ноде (панель показывает команду)."""
    path = os.path.join(get_settings().agent_dist_dir, "backup-setup.sh")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backup-setup.sh не найден")
    with open(path, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/x-shellscript")


@agent_router.get("/backupserver-setup.sh")
async def backupserver_setup_script() -> PlainTextResponse:
    """Скрипт включения статистики сервера бэкапов: ставит read-only helper в /lib65
    + sudoers. Запускается root'ом на бэкап-сервере (панель показывает команду)."""
    path = os.path.join(get_settings().agent_dist_dir, "backupserver-setup.sh")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "backupserver-setup.sh не найден")
    with open(path, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/x-shellscript")


@agent_router.get("/setup/index")
async def setup_index() -> list[dict]:
    """Каталог setup-хелперов: имя, версия и признак «безопасен везде» (KERVAX_SETUP_ALWAYS).
    Нужен install.sh, чтобы ставить ВСЁ применимое самому, не таская у себя захардкоженный
    список: раньше новый хелпер приходилось дописывать в установщик, о нём забывали, и
    свежая нода сразу попадала в «Требует действий» с просьбой сходить руками. Теперь
    источник правды один — сами скрипты, а установщик просто читает этот каталог."""
    d = get_settings().agent_dist_dir
    out: list[dict] = []
    for name in _setup_scripts():
        try:
            with open(os.path.join(d, f"{name}.sh"), encoding="utf-8") as f:
                body = f.read()
        except OSError:
            continue
        ver = re.search(r"^KERVAX_SETUP_VERSION=([0-9.]+)", body, re.MULTILINE)
        always = re.search(r"^KERVAX_SETUP_ALWAYS=1", body, re.MULTILINE)
        # Условие применимости и признак «только явной установкой». Оба живут в самом
        # скрипте: установщик и ansible-плейбук читают одно и то же, и ни один из них
        # не носит у себя списка «что куда ставить» — такие списки расходятся молча.
        when = re.search(r'^KERVAX_SETUP_WHEN="([^"]+)"', body, re.MULTILINE)
        manual = re.search(r"^KERVAX_SETUP_MANUAL=1", body, re.MULTILINE)
        out.append({
            "name": name,
            "version": ver.group(1) if ver else "",
            "always": bool(always),
            "when": when.group(1) if when else "",
            "manual": bool(manual),
        })
    return out


@agent_router.get("/setup/{name}.sh")
async def setup_script(name: str) -> PlainTextResponse:
    """Любой setup-helper из каталога раздачи одним маршрутом. Три старых (kube/backup/
    backupserver) имеют свои эндпоинты по историческим причинам, а webserver-setup и
    timesync-setup панель ФЛАГОВАЛА как устаревшие, но раздать не могла — скачать их
    было неоткуда (только ansible из репо). Имя сверяем со списком: путь снаружи."""
    if name not in _setup_scripts():
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name}.sh не найден")
    path = os.path.join(get_settings().agent_dist_dir, f"{name}.sh")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{name}.sh не найден")
    with open(path, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/x-shellscript")


@agent_router.get("/download/{arch}")
async def download_agent(arch: str) -> FileResponse:
    if arch not in ("amd64", "arm64"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "нет такой архитектуры")
    path = os.path.join(get_settings().agent_dist_dir, f"kervax-agent-{arch}")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "бинарь агента не собран")
    return FileResponse(
        path, media_type="application/octet-stream", filename="kervax-agent"
    )


@agent_router.get("/manifest")
async def agent_manifest() -> FileResponse:
    # Раздаём ТОЧНЫЕ байты подписанного манифеста (агент проверяет подпись над ними).
    path = os.path.join(get_settings().agent_dist_dir, "manifest.json")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "релиз-манифест не найден")
    # Битый релиз не отдаём вовсе: иначе весь парк будет качать по 6 МБ и отвергать
    # их по sha256 — бесконечно и молча. Явный отказ виден и в журнале агента, и в панели.
    if problem := agent_release_problem():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, problem)
    return FileResponse(path, media_type="application/json")


@agent_router.get("/manifest.sig")
async def agent_manifest_sig() -> FileResponse:
    path = os.path.join(get_settings().agent_dist_dir, "manifest.sig")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "подпись манифеста не найдена")
    return FileResponse(path, media_type="text/plain")


# --- ingest от агента (авторизация по токену, не JWT) ---


def _client_ip(xff: str, xreal: str) -> str:
    # nginx кладёт исходный адрес агента в X-Forwarded-For (первый токен) / X-Real-IP
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first[:64]
    return xreal.strip()[:64]


@agent_router.post("/report", response_model=AgentConfigOut)
async def agent_report(
    body: AgentReportIn,
    session: SessionDep,
    authorization: str = Header(default=""),
    x_forwarded_for: str = Header(default=""),
    x_real_ip: str = Header(default=""),
) -> AgentConfigOut:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нет токена агента")
    server = await session.scalar(
        select(Server).where(Server.token_hash == hash_agent_token(token))
    )
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен агента")

    now = datetime.now(timezone.utc)
    mem_pct = (
        round(body.mem_used / body.mem_total * 100, 1) if body.mem_total else None
    )
    disks_pct = [
        {"mount": d["mount"], "pct": round(d["used"] / d["total"] * 100, 1)}
        for d in body.disks
        if d.get("total") and d.get("mount")
    ]
    disk_pct = max((d["pct"] for d in disks_pct), default=None)
    tot = body.mem_total or 0
    # В историю пишем не каждый отчёт: агент шлёт их часто (нужно для «онлайн» и
    # порогов), а графику достаточно точки раз в минуту. Оперативные значения
    # живут в last_report и обновляются каждым отчётом — эта экономия видна
    # только на длине таблицы, не на свежести данных.
    every = max(int(get_settings().server_metric_interval_seconds), 0)
    prev = server.metric_written_at
    write_metric = (
        every <= 0
        or prev is None
        or (now - (prev if prev.tzinfo else prev.replace(tzinfo=timezone.utc))).total_seconds()
        >= every
    )
    if write_metric:
        server.metric_written_at = now
        session.add(
            ServerMetric(
                server_id=server.id,
                cpu_percent=round(body.cpu_percent, 1),
                mem_percent=mem_pct,
                disk_percent=disk_pct,
                load1=body.load[0] if body.load else None,
                net_rx=round(body.net_rx),
                net_tx=round(body.net_tx),
                disk_read=round(body.disk_read),
                disk_write=round(body.disk_write),
                disk_read_iops=round(body.disk_read_iops, 1),
                disk_write_iops=round(body.disk_write_iops, 1),
                cpu_user=round(body.cpu_user, 1),
                cpu_system=round(body.cpu_system, 1),
                cpu_iowait=round(body.cpu_iowait, 1),
                cpu_irq=round(body.cpu_irq, 1),
                cpu_cores_pct=body.cpu_cores_pct or None,
                cpu_freq=round(body.cpu_freq) if body.cpu_freq else None,
                cpu_temp=round(body.cpu_temp, 1) if body.cpu_temp is not None else None,
                cpu_throttle=body.cpu_throttle,
                oom_kill=body.oom_kill,
                mem_cache=round(body.mem_cached / tot * 100, 1) if tot else None,
                mem_free=round(body.mem_free / tot * 100, 1) if tot else None,
                swap_in=round(body.swap_in),
                swap_out=round(body.swap_out),
                mem_slab=round(body.mem_slab),
                mem_dirty=round(body.mem_dirty),
                mem_writeback=round(body.mem_writeback),
                disks=disks_pct or None,
                net_ifaces=body.net_ifaces or None,
                disk_devs=body.disk_devs or None,
                conntrack_count=round(body.conntrack_count),
                conntrack_max=round(body.conntrack_max),
                sock_used=round(body.sock_used),
                sock_tcp=round(body.sock_tcp),
                sock_tcp_tw=round(body.sock_tcp_tw),
                sock_udp=round(body.sock_udp),
                ts=now,
            )
        )
    if body.hostname:
        server.hostname = body.hostname[:255]
    if body.os:
        server.os = body.os[:128]
    if body.local_ip:
        server.local_ip = body.local_ip[:64]
    ext = _client_ip(x_forwarded_for, x_real_ip)
    if ext:
        server.external_ip = ext
    server.agent_version = body.agent_version[:32]
    # детект перезагрузки: аптайм упал (с запасом 60с на дрожание) → отметка времени,
    # по которой collector пошлёт разовый алерт «сервер перезагружен».
    prev_up = (server.last_report or {}).get("uptime_seconds") or 0
    if prev_up and body.uptime_seconds and body.uptime_seconds < prev_up - 60:
        server.rebooted_at = now
    # OOM-киллы копим КУМУЛЯТИВНО тут (в ingest'е видим каждый отчёт), а не в
    # collector'е по last_report — дельта живёт лишь один тик и терялась при поллинге.
    oom_delta = round(body.oom_kill or 0)
    if oom_delta > 0:
        server.oom_total = (server.oom_total or 0) + oom_delta
        victim = (getattr(body, "oom_victim", "") or "")[:200]
        if victim:
            server.oom_victim = victim
        # журнал OOM-событий: одна запись на отчёт с киллом (когда + кого)
        session.add(OomEvent(server_id=server.id, ts=now, victim=victim, count=oom_delta))
    report_dict = body.model_dump()
    # сдвиг часов: локальное время ноды (на момент отправки) минус время панели на приёме.
    # Работает даже если у ноды нет доступа к NTP — агент до панели по HTTPS достучался.
    # Ошибка ≈ сетевая задержка (доли секунды); порог алерта её с запасом перекрывает.
    if body.clock_unix:
        report_dict["clock_skew_sec"] = int(now.timestamp()) - body.clock_unix
    server.last_report = report_dict
    server.last_seen = now

    # Сигнал обновления: только если админ выставил target и агент ещё не на ней.
    # Агент сам проверит подпись/хеш/анти-откат — панель лишь просит.
    upd = None
    if (
        server.target_agent_version
        and server.target_agent_version != server.agent_version
    ):
        upd = {"version": server.target_agent_version}

    # docker/kube/backup-команды из очереди (fallback; основной — быстрый /agent/commands)
    cmds = await _take_docker_commands(session, server.id)
    kcmds = await _take_kube_commands(session, server.id)
    bcmds = await _take_backup_commands(session, server.id)
    await _store_site_probes(session, server.id, body.site_probes or [], now)
    probes = await _site_probe_tasks(session, server.id)
    await session.commit()

    return AgentConfigOut(
        interval=get_settings().server_report_interval, checks=[], update=upd,
        docker_commands=cmds, kube_commands=kcmds, backup_commands=bcmds,
        site_probes=probes,
    )


async def _store_site_probes(session, server_id: int, results: list, now) -> None:
    """Кладёт присланные агентом результаты локальных проверок.

    Только для мониторов, которым ЭТОТ сервер назначен проверяющим: иначе агент
    (или тот, кто добыл его токен) мог бы объявить любой чужой монитор упавшим."""
    if not results:
        return
    ids = [int(r.get("id") or 0) for r in results if r.get("id")]
    if not ids:
        return
    mine = set(
        await session.scalars(
            select(Check.id).where(Check.id.in_(ids), Check.probe_server_id == server_id)
        )
    )
    for r in results:
        cid = int(r.get("id") or 0)
        if cid not in mine:
            continue
        row = await session.get(AgentProbe, cid)
        if row is None:
            row = AgentProbe(check_id=cid, server_id=server_id, ts=now)
            session.add(row)
        row.server_id = server_id
        row.ts = now
        row.code = int(r.get("code") or 0)
        lat = r.get("latency_ms")
        row.latency_ms = int(lat) if isinstance(lat, (int, float)) else None
        row.error = str(r.get("error") or "")[:512]
        row.kw_up_found = bool(r.get("kw_up_found", True))
        row.kw_down_found = bool(r.get("kw_down_found", False))
        try:
            row.cert_expires = int(r.get("cert_expires") or 0)
        except (TypeError, ValueError):
            row.cert_expires = 0
        row.cert_issuer = str(r.get("cert_issuer") or "")[:128]


async def _site_probe_tasks(session, server_id: int) -> list[dict]:
    """Задания агенту: что проверять изнутри сервера.

    Агент ходит ТОЛЬКО на localhost (адрес он подменяет сам), поэтому URL здесь —
    это не «куда пойти», а «чьим именем представиться»: Host и SNI. Панель не может
    послать агента ни на какой другой хост, и заставить его сканировать сеть тоже
    не может — даже будучи захваченной."""
    rows = list(
        await session.scalars(
            select(Check).where(
                Check.enabled.is_(True),
                Check.probe_server_id == server_id,
                Check.type == "http",
            )
        )
    )
    out: list[dict] = []
    for c in rows:
        out.append({
            "id": c.id,
            "url": c.target,
            "method": c.method or "GET",
            "timeout_ms": c.timeout_ms,
            "interval": c.interval_seconds,
            "expected_status": c.expected_status,
            "keyword_up": c.keyword_up,
            "keyword_down": c.keyword_down,
            "headers": c.http_headers or "",
            "auth_user": c.auth_user if c.auth_method == "basic" else "",
            "auth_pass": c.auth_pass if c.auth_method == "basic" else "",
            "ignore_tls": bool(c.ignore_tls),
        })
    return out


@agent_router.get("/commands")
async def agent_commands(
    session: SessionDep, authorization: str = Header(default="")
) -> dict:
    """Быстрый опрос очереди docker-команд (агент дёргает чаще, чем шлёт метрики) —
    чтобы restart/logs исполнялись за ~3с, а не за интервал отчёта. Тело крошечное."""
    token = authorization.removeprefix("Bearer ").strip()
    server = await session.scalar(
        select(Server).where(Server.token_hash == hash_agent_token(token))
    )
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен агента")
    cmds = await _take_docker_commands(session, server.id)
    kcmds = await _take_kube_commands(session, server.id)
    bcmds = await _take_backup_commands(session, server.id)
    await session.commit()
    return {"docker_commands": cmds, "kube_commands": kcmds, "backup_commands": bcmds}


@agent_router.post("/docker-result")
async def agent_docker_result(
    body: DockerResultIn,
    session: SessionDep,
    authorization: str = Header(default=""),
) -> dict:
    """Агент постит результат docker-команды (по своему токену). Логи кап 128КБ."""
    token = authorization.removeprefix("Bearer ").strip()
    server = await session.scalar(
        select(Server).where(Server.token_hash == hash_agent_token(token))
    )
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен агента")
    c = await session.get(DockerCommand, body.id)
    if c is None or c.server_id != server.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Команда не найдена")
    c.ok = body.ok
    c.result = (body.output or "")[-20_000_000:]  # хвост, если очень длинно (кап ~20МБ)
    c.status = "done" if body.ok else "error"
    c.done_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


@agent_router.post("/kube-result")
async def agent_kube_result(
    body: KubeResultIn,
    session: SessionDep,
    authorization: str = Header(default=""),
) -> dict:
    """Агент постит результат kube-команды (по своему токену)."""
    token = authorization.removeprefix("Bearer ").strip()
    server = await session.scalar(
        select(Server).where(Server.token_hash == hash_agent_token(token))
    )
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен агента")
    c = await session.get(KubeCommand, body.id)
    if c is None or c.server_id != server.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Команда не найдена")
    c.ok = body.ok
    c.result = (body.output or "")[-20_000_000:]
    c.status = "done" if body.ok else "error"
    c.done_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True}


@agent_router.post("/backup-result")
async def agent_backup_result(
    body: BackupResultIn,
    session: SessionDep,
    authorization: str = Header(default=""),
) -> dict:
    """Агент постит результат backup-команды (по своему токену)."""
    token = authorization.removeprefix("Bearer ").strip()
    server = await session.scalar(
        select(Server).where(Server.token_hash == hash_agent_token(token))
    )
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен агента")
    c = await session.get(BackupCommand, body.id)
    if c is None or c.server_id != server.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Команда не найдена")
    c.ok = body.ok
    c.result = (body.output or "")[-100_000:]
    c.status = "done" if body.ok else "error"
    c.done_at = datetime.now(timezone.utc)
    c.payload = None  # секреты (repopass/hpass) в БД не задерживаем — стираем сразу
    await session.commit()
    return {"ok": True}
