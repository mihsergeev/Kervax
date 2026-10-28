import asyncio
import base64
import contextlib
import gzip
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import string
import tempfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import delete as sa_delete, func, select, update as sa_update

from app import backup_unit
from app import backup_verify
from app import gre as gre_mod, relay as relay_mod
from app import disk_wear
from app import request_watch
from app import install_watch
from app import panel_errors
from app import db_health
from app import gpu as gpu_mod
from app import queue_watch
from app import workers as workers_mod
from app import cron_watch
from app import going_down
from app import log_errors
from app import fpm_pools
from app import gitlab as gitlab_mod
from app import mem_trend
from app import mem_limit
from app import restarts as srv_restarts
from app import access_audit
from app import docker_risk
from app import reboot_survive
from app import ssh_audit
from app import web_certs
from app import cf_realip
from app import autoban as autoban_mod
from app import web_upstream
from app import web_config
from app import webfiles as webfiles_mod
from app import changes as srv_changes
from app import known
from app import kube_expiry
from app import onboard
from app import parents as parents_mod
from app import boot_warmup
from app import timeline as timeline_mod
from app import packages as packages_mod
from app import netlinks
from app import traffic as traffic_mod
from app import usage as usage_mod
from app import home_mute
from app import audit, backup_growth, backup_state, backup_target, bsrv_cleanup, custom_backups, docker_exposure, geoip, kube_coverage, manual_probe, open_ports, os_support
from app import dump_reason
from app import fleet_rollout
from app import oom as oom_mod
from app import software as software_mod
from app.collector import (
    _OFFLINE_ALERT_EXTRA, _backup_problem_repos, alert_since, awaiting_after_pause, backup_cadence, backup_rotation_items, backup_unmonitored,
    bsrv_extra, bsrv_restic_old, clock_skew_window, cron_job_issues, disk_analyze_ok, prune_problems, repo_reason, docker_since,
    backup_run_start, dump_local_stale, needed_pod_names, panel_server_names, pod_uid_names,
    disk_health_block, send_alerts_soon, send_autofix_note, server_problems, web_5xx_total, web_breakdown,
    web_error_window, web_label_key, web_log_label, web_rate_total, panel_started_at,
)
from app.setup_scripts import (
    current_setup_versions as _current_setup_versions,
    setup_needed as _setup_needed,
    setup_scripts as _setup_scripts,
)
from app.config import get_settings
from app.deps import LOGS_CLOSED, AdminUser, CurrentUser, SessionDep, group_allowed, scope_query, viewer_command_ok
from app.models import (
    AgentPathProbe,
    AgentProbe,
    Check,
    BackupCommand,
    BackupSetupJob,
    DockerCommand,
    KubeCommand,
    OomEvent,
    Server,
    ServerMetric,
    ServerTraffic,
    ServerUsage,
    User,
    WebErrorSample,
)
from app.schemas import (
    agent_ip_valid,
    dump_dir_default,
    safe_hostname,
    KnownIn,
    NoteIn,
    AgentConfigOut,
    AgentReleaseOut,
    AgentPackagesIn,
    AgentGoingDownIn,
    AgentReportIn,
    AgentSiteProbesIn,
    AgentRunOut,
    AgentUpdateCancel,
    AgentUpdateReq,
    FleetRolloutOut,
    HelperRolloutReq,
    HelperRunOut,
    AlertSnoozeIn,
    BackupCommandIn,
    AutobanCmdIn,
    BackupCommandOut,
    BackupAuditMuteIn,
    CustomBackupIgnoreIn,
    BackupRepoArchiveIn,
    BackupRepoMuteIn,
    BackupCredsOut,
    BackupAudit,
    BackupResultIn,
    BackupServerDeployIn,
    BackupToHttpsIn,
    BackupToHttpsJobOut,
    BackupSetupIn,
    BackupSetupJobOut,
    DockerCommandIn,
    DockerCommandOut,
    DockerResultIn,
    HelperAdvice,
    KubeCommandIn,
    KubeExpiryIgnoreIn,
    KubeNodeIgnoreIn,
    KubeNodeRef,
    KubeCommandOut,
    KubeResultIn,
    OomEventOut,
    WebErrorOut,
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
# счетчики байт с загрузки (агент 2.31) нужны только трафику за месяц (app/traffic.py), в
# истории метрик им не место
_IFACE_TOTALS = ("rx_total", "tx_total", "phys")


def _is_online(server: Server, now: datetime) -> bool:
    if server.last_seen is None:
        return False
    # панель только что поднялась после простоя: отчет этого сервера еще в пути, а не пропал
    if awaiting_after_pause(server, now):
        return True
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


# Маркер временной неудачи в тексте агента: скачанное сохранено, попытка продолжится.
_UPD_RESUME = "продолжим с этого места"


def agent_update_note(rep: dict) -> str:
    """«докачивается 59% (3.4 из 5.8 МБ)» - чтобы было видно, что процесс идёт, а не
    встал. Пусто, если докачки нет. Текст готов к показу как есть: фронт только
    подставляет имя ноды, иначе строку пришлось бы собирать в двух местах."""
    err = str(rep.get("update_error") or "").strip()
    if _UPD_RESUME not in err:
        return ""
    m = re.search(r"взято (\d+) из (\d+)", err)
    if not m:
        return "докачивается"
    got, total = int(m.group(1)), int(m.group(2))
    pct = round(got / total * 100) if total else 0
    return f"докачивается {pct}% ({got / 1e6:.1f} из {total / 1e6:.1f} МБ)"


def agent_update_error(rep: dict) -> str:
    """Причина, по которой агент отверг обновление; пусто, если отказа нет. Докачка на
    медленном канале - не отказ (это agent_update_note)."""
    err = str(rep.get("update_error") or "").strip()
    return "" if _UPD_RESUME in err else err


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
    upd_err = agent_update_error(rep)
    # Медленная докачка - НЕ отказ: набранное лежит на ноде, следующая попытка идёт
    # дальше с того же места. Такой текст в «Требует действий» звал чинить то, что
    # чинится само: на РФ-нодах за DPI 5.8 МБ набираются за несколько попыток по
    # 10 минут, и панель показывала три пункта «обновление отклонено» подряд.
    extra = [] if not upd_err else [
        f"обновление агента отклонено — {upd_err}"
    ]

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


# Что в отчете и в разборах карточки - строки логов и командные строки процессов: там бывают
# пароли (`mysql -p...`, `--requirepass`, токены в URL), ради этого и есть галка "может читать
# логи". Учетке только для просмотра без галки эти поля не отдаем: раньше она читала их в
# last_report и разборах карточки, обходя галку
_LOG_KEYS = frozenset({"cmdline", "log", "pmsg", "msg", "crash_last", "lines"})


def _strip_logs(v, depth: int = 0):
    if depth > 12:
        return v
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if k in _LOG_KEYS and (isinstance(x, str) or (
                    isinstance(x, list) and all(isinstance(i, str) for i in x))):
                continue
            out[k] = _strip_logs(x, depth + 1)
        if isinstance(out.get("sources"), list):
            # пример строки лога у helper'а ошибок в логах (logerr)
            out["sources"] = [{k: x for k, x in s.items() if k != "last"} if isinstance(s, dict) else s
                              for s in out["sources"]]
        return out
    if isinstance(v, list):
        return [_strip_logs(x, depth + 1) for x in v]
    return v


def _logs_for(o: ServerOut, user) -> ServerOut:
    if user.role != "viewer" or user.can_read_logs:
        return o
    for name in type(o).model_fields:
        val = getattr(o, name, None)
        if isinstance(val, (dict, list)):
            setattr(o, name, _strip_logs(val))
    return o


def _out(
    server: Server, now: datetime, cur_versions: dict[str, int] | None = None,
    pod_names: dict[str, str] | None = None,
    web_err: tuple[float, float, int, dict, float] | None = None,
) -> ServerOut:
    o = ServerOut.model_validate(server)
    o.online = _is_online(server, now)
    o.agent_advice, o.agent_fix_command = _agent_advice(server)
    o.agent_update_note = agent_update_note(server.last_report or {}) or None
    o.agent_update_error = agent_update_error(server.last_report or {}) or None
    o.helper_advice = _helper_advice(
        server, cur_versions if cur_versions is not None else _current_setup_versions()
    )
    o.relay = relay_mod.view(server, now)
    o.gre = gre_mod.view(server, now)
    o.gre_need = gre_mod.need(server.last_report or {})
    o.helper_rollout = helper_rollout(server, o.helper_advice)
    o.backup_audit = _backup_coverage(server)
    o.custom_backups = custom_backups.views(server, now)
    # Страна по IP — офлайн-таблицей (см. geoip). Берём адрес, которым нода реально
    # выходит в сеть: external_ip панель видит сама, agent_ip задан руками и может
    # оказаться внутренним. local_ip не смотрим — он приватный и страны не имеет.
    o.country = geoip.country_of(server.external_ip) or geoip.country_of(server.agent_ip)
    o.docker_alerts = _docker_alerts(server)
    o.docker_exposed = docker_exposure.view(server.last_report or {}, _fresh_node(server, now))
    o.docker_risk = docker_risk.view(server, fix=True)
    o.exposure = open_ports.view(server)
    if o.exposure is not None:
        # веб-серверы, чьи белые списки обходятся подменой CF-Connecting-IP (app/cf_realip.py)
        o.exposure["realip"] = cf_realip.view(server, now) or []
    o.os_advice = os_support.advice(server.last_report or {}, now, server.alert_mutes)
    o.sw_advice = software_mod.advice(server.last_report or {}, server.sw_host, now, server.alert_mutes)
    o.changes = srv_changes.view(server)
    o.backup_verify = backup_verify.view(server.last_report or {}, now)
    o.backup_target = backup_target.view(server.last_report or {}, server.alert_mutes)
    o.restarts_day = srv_restarts.view(server, now) or None
    o.kube_expiry_ignored = kube_expiry.view(server)
    o.kube_expiry_unused = kube_expiry.unused_view(server)
    o.kube_restarts = srv_restarts.kube_view(server, now)
    o.mem_leaks = mem_trend.problems(server, now) or None
    o.mem_limits = mem_limit.view(server, now, pod_names)
    o.oom = oom_mod.view(server, now, pod_names)
    o.ssh = ssh_audit.view(server, now)
    o.ssh_advice = ssh_audit.advice(server, now) or None
    o.noboot = reboot_survive.view(server, now)
    o.noboot_advice = reboot_survive.advice(server, now)
    o.access = access_audit.view(server, now)
    o.access_advice = access_audit.advice(server, now)
    o.db_health = db_health.view(server.last_report or {}, now)
    o.gpu = gpu_mod.view(server.last_report or {}, now)
    o.gpu_sum = gpu_mod.summary(server.last_report or {}, now)
    o.llm = gpu_mod.llm_view(server.last_report or {}, (server.alert_state or {}).get("llm_base"),
                             (server.alert_state or {}).get("llm_lat"), now) or None
    o.queue_watch = queue_watch.view(server, now) or None
    o.queue_brokers = queue_watch.helper_view(server) or None
    o.queue_skipped = queue_watch.redis_skipped(server.last_report or {}) or None
    o.queue_db_skipped = queue_watch.db_skipped(server.last_report or {}) or None
    o.queue_mutes = queue_watch.mute_view(server, now) or None
    o.queue_fail_mutes = queue_watch.mute_view(server, now, queue_watch.FAIL_PREFIX) or None
    o.workers = workers_mod.view(server, now)
    o.workers_skipped = workers_mod.skipped(server.last_report or {}) or None
    o.cron = cron_watch.view(server, now)
    o.log_errors = log_errors.view(server, now)
    o.fpm = fpm_pools.view(server, now)
    o.gitlab = gitlab_mod.view(server, now)
    o.web_config = web_config.view(server.last_report or {}, now)
    o.web_certs = web_certs.view(server.last_report or {}, now)
    o.webfiles = webfiles_mod.view(server.webfiles, server.last_report or {}, now)
    # старые образы прокси с решением "оставить как есть" и шагами обновления под ноду
    o.proxy_outdated = docker_exposure.old_view(server.last_report or {}, now.timestamp(), server.alert_mutes)
    o.disk_analyze = disk_analyze_ok(server.last_report or {})
    o.fresh = _fresh_node(server, now)
    # недоступен: что ответил сервер панели в момент алерта (app/liveness.py)
    probe = (server.alert_state or {}).get("offline_probe")
    o.offline_probe = probe if isinstance(probe, dict) and not o.online else None
    # молчит после штатной перезагрузки: ждем его или он не вернулся (app/going_down.py)
    o.going_down = going_down.view(server.last_report, o.online, now)
    if onboard.active(server, now):
        o.onboard = {"until": onboard.until(server).isoformat()}
    o.problems = server_problems(server, now, pod_names, web_err)
    if ((server.last_report or {}).get("kube") or {}).get("access"):
        o.kube_cron_issues = {**cron_job_issues(server.last_report, now, True, server.alert_state),
                              **cron_job_issues(server.last_report, now, False, server.alert_state)}
    o.alert_since = alert_since(server)
    o.docker_since = docker_since(server)
    o.backup_growth = backup_growth.jump(server, now)
    o.backup_state = backup_state.view(server)
    o.bsrv_rotation = backup_rotation_items(server, now)
    # вкладка "Чистка": что не так с чисткой и готовые шаги под этот сервер и репозитории
    o.bsrv_cleanup = bsrv_cleanup.view(server, now)
    o.bsrv_restic_old = bsrv_restic_old(server.last_report or {})
    o.wear_forecast = disk_wear.card(disk_health_block(server.last_report or {}), server.disk_wear)
    o.bsrv_cadence = _cadences(server.last_report or {})
    bs = (server.last_report or {}).get("backup_server") or {}
    if bs.get("present"):
        o.bsrv_prune = {n: why for n, (_k, why) in prune_problems(
            bs, bsrv_extra(server.last_report or {}), now, set(server.backup_repo_mutes or []),
            server.backup_repo_archive or {}).items()}
    o.pod_names = needed_pod_names(server, pod_names)
    return o


def _docker_alerts(server: Server) -> dict[str, str]:
    """Контейнеры, которые лежат или крутятся в цикле по правилам алерта: {имя: 'down'|'loop'}.

    Считать проблему на фронте по RestartCount нельзя: этот счётчик копится за всё время
    жизни контейнера и сам по себе ничего не значит, а crash-loop определяется по ПРИРОСТУ
    счётчика в окне — история для этого есть только на бэкенде (alert_state). Состояние
    ("down"/"loop") ведется и без отправленного алерта: под мьютом, при выключенном правиле
    упавший контейнер раньше не был виден нигде; alerted_* - у состояний, записанных раньше."""
    out: dict[str, str] = {}
    for name, cs in ((server.alert_state or {}).get("docker") or {}).items():
        if not isinstance(cs, dict):
            continue
        if cs.get("down") or cs.get("alerted_down"):
            out[name] = "down"
        elif cs.get("loop") or cs.get("alerted_loop"):
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
    закрыта фаерволом). Пустой файл = разрешать нечего.

    Только одиночные адреса: подсеть 0.0.0.0/0 в "IP сервера" открывала 80/443 хоста панели
    всем (agent_ip теперь проверяется и на приеме, а здесь - то, что записано раньше).
    Пишем через свой временный файл: у трех воркеров uvicorn общее имя .tmp давало гонку,
    и PATCH сервера отвечал 500 уже после коммита. Ошибка записи ответ не роняет: изменения
    в базе, файл перепишет следующая правка."""
    ips = sorted({
        ip.strip()
        for ip in await session.scalars(
            select(Server.agent_ip).where(Server.agent_ip != "")
        )
        if ip and agent_ip_valid(ip.strip())
    })
    data_dir = get_settings().data_dir
    tmp = ""
    try:
        os.makedirs(data_dir, exist_ok=True)
        path = os.path.join(data_dir, "agent_allow_ips")
        fd, tmp = tempfile.mkstemp(dir=data_dir, prefix="agent_allow_ips.")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(ips) + ("\n" if ips else ""))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        log.warning("не удалось записать agent_allow_ips", exc_info=True)
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


_PEM_CERT = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----")
# переменная оболочки с временным файлом корня на время установки (install.sh потом копирует
# его в хранилище ОС)
PANEL_CA_VAR = "KERVAX_CA"


def panel_ca_pem() -> str:
    """Корень, которым подписан сертификат панели, если он не из публичного CA: data/panel-ca.crt.

    Панель в локальной сети (quickstart --local-ca) получает сертификат от внутреннего CA caddy:
    Let's Encrypt до приватного адреса не достучится. Агенту этот CA неизвестен, и первая же
    curl команды установки падала на проверке сертификата. Файл кладет quickstart (корень caddy),
    или сам админ (корпоративный CA). Пусто - сертификат публичный, ничего не нужно."""
    path = os.path.join(get_settings().data_dir, "panel-ca.crt")
    try:
        with open(path, encoding="ascii") as f:
            body = f.read(65536)
    except (OSError, UnicodeDecodeError):
        return ""
    certs = _PEM_CERT.findall(body)
    if not certs:
        log.warning("%s без сертификата в PEM - команда установки идет без корня", path)
        return ""
    return "\n".join(certs) + "\n"


# Что печатает команда установки, пока ждет панель. Фронт подменяет их в английском интерфейсе
# (installProxy.ts), поэтому одинарных кавычек и фигурных скобок в них нет.
INSTALL_WAIT_MSG = "Панель пока закрыта для этого сервера, ждем 10 с (curl $?, попытка $i/24)"
INSTALL_GIVEUP_MSG = "Панель так и не ответила: проверьте IP сервера в ее настройках и исходящий 443 (или прокси)"


def _install_cmd(token: str) -> str:
    """Команда установки агента: одна строка, ее копируют из окна "Сервер создан".

    Новая нода стучится в панель раньше, чем та откроет ей фаервол (ops/agent-firewall-sync.sh),
    поэтому команда сама повторяет попытки: 24 раза через 10 с, потом честно сдается. Установщик
    сначала скачивается в файл целиком (-m: зависшая передача тоже ошибка) и только потом
    запускается: через `curl | sh` на канале с DPI, который обрывает передачу посреди файла, sh
    выполнял половину установщика и ждал остаток вечно. --compressed: сжатый установщик меньше
    порога, на котором такой DPI режет соединение.

    sh -c: аргументы установщика идут последними, и то, что окно предлагает дописать в конец
    (--no-auto, имя инстанса), попадает к нему. Токен - в KERVAX_TOKEN перед sh -c, а не
    аргументом: аргументы видит в ps любой пользователь ноды все минуты установки, а с токеном
    можно забирать команды вместо ноды. Присваивание перед командой в argv не попадает, а
    окружение процесса читает только его владелец; exec sh передает его install.sh. Прокси окно
    передает так же, переменной KERVAX_PROXY перед sh -c: ее берут и этот curl, и install.sh.
    Путь через GRE - переменной KERVAX_CONNECT (адрес панели в туннеле): curl идет на него с
    именем панели в TLS и SNI (--resolve), install.sh так же, и пишет агенту connect=."""
    base = get_settings().panel_url.rstrip("/") or "https://ПАНЕЛЬ"
    ca = panel_ca_pem()
    # с корнем аргументы начинаются с "--ca-file <файл>": curl берет тот же файл вторым аргументом
    cacert = ' --cacert "$2"' if ca else ""
    u = urlsplit(base)
    hostport = f"{u.hostname or 'ПАНЕЛЬ'}:{u.port or (80 if u.scheme == 'http' else 443)}"
    script = (
        "f=$(mktemp) && for i in $(seq 24); do"
        f" curl -fsL --compressed --connect-timeout 10 -m 120{cacert}"
        ' ${KERVAX_PROXY:+--proxy "$KERVAX_PROXY"}'
        f' ${{KERVAX_CONNECT:+--resolve "{hostport}:$KERVAX_CONNECT"}}'
        f' -o "$f" {base}/api/agent/install.sh && exec sh "$f" "$@";'
        f' echo "{INSTALL_WAIT_MSG}" >&2; sleep 10; done;'
        f' echo "{INSTALL_GIVEUP_MSG}" >&2; exit 1'
    )
    if not ca:
        return f"KERVAX_TOKEN={token} sh -c '{script}' - {base}"
    # Корень едет в самой команде: скачать его с панели нечем, https к ней без корня и не
    # проверить, а отключать проверку (-k) нельзя. curl проверяет панель этим корнем, install.sh
    # доверяет ему в ОС (агент и helper'ы ходят в панель системным хранилищем). --ca-file стоит
    # ДО адреса: третий позиционный аргумент (имя инстанса) UI дописывает в самый конец.
    # Файл - через mktemp: фиксированное имя в /tmp можно было подложить заранее своим и
    # получить свой CA в доверенных ноды (install.sh еще и проверяет владельца файла)
    b64 = base64.b64encode(ca.encode("ascii")).decode("ascii")
    return (
        f'{PANEL_CA_VAR}=$(mktemp) && echo {b64} | base64 -d > "${PANEL_CA_VAR}" && '
        f"KERVAX_TOKEN={token} sh -c '{script}' - --ca-file \"${PANEL_CA_VAR}\" {base}"
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
    # Ноды кластеров сверяем со ВСЕМИ серверами панели: нода, которую смотрит сервер из
    # чужой группы, все равно под присмотром. Без нарезки по группам это тот же список.
    # список, а не результат запроса: его обходят три раза, а результат запроса отдается один раз
    every = servers if not (user.server_groups or []) else list(await session.scalars(select(Server)))
    index = kube_coverage.build_index(every)
    names = pod_uid_names(every)
    panel = panel_server_names(every)
    # ошибки 5xx за 15 минут по всем нодам - двумя запросами на весь список, для "Что сломано"
    web_err = await web_error_window(session, now)
    bogus = await request_watch.server_issues(session)
    # выпуски GitLab собирает планировщик, веб берет их из app_settings (раз в 5 минут)
    await gitlab_mod.load_feed(session)
    # куда пойдут алерты серверов на подключении: строка чек-листа в карточке
    cfg = None
    if any(onboard.active(s, now) for s in servers):
        from app import settings_store
        cfg = await settings_store.get_alert_config(session, get_settings())
    outs = []
    for s in servers:
        o = _out(s, now, cur, names, web_err.get(s.id))
        if o.onboard is not None and cfg is not None:
            o.onboard |= onboard.destinations(cfg, s.group_name or "")
        if s.id in bogus:
            o.problems = [*o.problems, bogus[s.id]]
        o.kube_unmonitored = _unmonitored(s, index)
        # ключи заглушки до нового для пунктов "Требует действий" (app/home_mute.py): нужны только
        # главной, а она берет этот список
        o.home_mutes = home_mute.keys(s, now, index)
        # бэкап по http: на какой сервер бэкапов переводить на https и что мешает (кнопка в окне)
        o.backup_https = backup_target.https_view(s, every)
        bs = (s.last_report or {}).get("backup_server") or {}
        if bs.get("present"):
            arch = s.backup_repo_archive or {}
            o.bsrv_unmonitored = backup_unmonitored(bs, set(s.backup_repo_mutes or []), panel, now, arch)
            ext = bsrv_extra(s.last_report or {})
            byname = {r.get("name"): r for r in bs.get("repos") or []}
            o.bsrv_repo_reasons = {
                n: repo_reason(byname[n], now, ext, panel)
                for n in _backup_problem_repos(bs, set(s.backup_repo_mutes or []), now, ext, panel, arch)}
        outs.append(_logs_for(o, user))
    return outs


@router.post("", response_model=ServerEnrollOut, status_code=status.HTTP_201_CREATED)
async def create_server(
    body: ServerCreate, user: CurrentUser, session: SessionDep
) -> ServerEnrollOut:
    name = body.name.strip()
    # в чужую группу не заводим: учетка с группами подкладывала бы ноду соседям
    if not group_allowed(user, body.group_name):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Группа недоступна для этой учётной записи")
    await _name_free(session, name, user=user)
    token = generate_agent_token()
    # подключение открыто (app/onboard.py): пока его не закроют, от сервера уходит алертом только
    # офлайн и критичный диск. Метка в состоянии - чтобы сборщик знал, что было что закрывать
    server = Server(**(body.model_dump() | {"name": name}), token_hash=hash_agent_token(token),
                    onboard_done_at=None, alert_state={"onboard": 1})
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


async def _name_free(session, name: str, exclude_id: int | None = None, user=None) -> None:
    """Проверяет, что имя еще не занято: панель различает ноды по имени.

    Две записи с одним именем в списке не различить, их метрики и алерты идут вперемешку,
    а вторая обычно пустая: ту же ноду завели второй раз. Так в парке появился второй
    stats-a (18.09.2026) - панель молча создала его рядом с рабочим."""
    dup = await session.scalar(
        select(Server).where(func.lower(func.trim(Server.name)) == name.strip().lower())
    )
    if dup is None or dup.id == exclude_id:
        return
    if user is not None and not group_allowed(user, dup.group_name):
        # сервер чужой группы не называем и не подтверждаем его имя дословно
        raise HTTPException(status.HTTP_409_CONFLICT, "Это имя уже занято, выберите другое.")
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        f"Сервер {dup.name} уже есть в панели. Другой ноде дайте другое имя, "
        "а ту же ноду второй раз заводить не нужно.",
    )


# --- управляемое обновление агентов (admin, аудит; регистрируем ДО /{server_id}) ---


_SETUP_LABEL = {
    "backup-setup": "Бэкап (клиент)",
    "backupserver-setup": "Бэкап-сервер",
    "kube-setup": "Kubernetes",
    "kubeexpiry-setup": "Сроки Kubernetes и Flux",
    "webserver-setup": "Веб-домены",
    "timesync-setup": "Синхронизация времени",
    "diskusage-setup": "Разбор места на диске",
    "diskhealth-setup": "Здоровье дисков",
    "units-setup": "Упавшие юниты systemd",
    "dbstat-setup": "Инвентарь СУБД",
    "agent-watchdog": "Вотчдог агента",
    "dockerproxy-setup": "Доступ к Docker (прокси)",
    "changes-setup": "Изменения на сервере",
    "gpu-setup": "GPU NVIDIA",
    "queues-setup": "Очереди задач в Redis и таблицах баз",
    "workers-setup": "Воркеры supervisor и pm2",
    "cron-setup": "Кроны на хосте и в контейнерах",
    "logerr-setup": "Ошибки в логах контейнеров",
    "fpm-setup": "Пулы PHP-FPM",
    "gitlab-setup": "GitLab: версия, бэкап, миграции",
    "sockfix-setup": "Compose внешнего прокси: сокет и образ",
    "relay-setup": "Ретранслятор для агентов",
    "gre-setup": "GRE-туннель до панели",
    "webfiles-setup": "Файлы сайтов: признаки взлома",
    "autoban-setup": "Автобан сканеров",
}


def helper_rollout(server: Server, advice: list[HelperAdvice]) -> bool:
    """Поменяет ли прогон kervax_helpers.yml что-то на ноде: устарел helper или агент
    говорит, что watchdog не включен (его ставит helper agent-watchdog)."""
    caps = (server.last_report or {}).get("caps") or {}
    return bool(advice) or caps.get("watchdog") is False


# Первые минуты после первого отчета установщик еще ставит helper'ы и прокси Docker: агент он
# запускает раньше них. Их отсутствие тогда не повод звать "поставьте" - плашки пропадали сами
# через пару минут и сбивали с толку. Установщик, упавший на полпути, проявится после паузы.
FRESH_NODE = timedelta(minutes=20)


def _fresh_node(server: Server, now: datetime | None = None) -> bool:
    """Нода только что поставлена: с первого отчета агента не прошло FRESH_NODE."""
    t = server.first_report_at
    if t is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - (t if t.tzinfo else t.replace(tzinfo=timezone.utc)) < FRESH_NODE


def _helper_advice(server: Server, cur: dict[str, str]) -> list[HelperAdvice]:
    """Устаревшие setup-скрипты (helper'ы) на ноде: агент шлёт установленные версии
    (setup_versions), сверяем с текущими раздаваемыми. Флагуем только helper'ы, которые
    нода ТОЧНО имеет (manageable / бэкап-сервер с репо / kube-доступ). Гейт: агент <1.46
    не шлёт setup_versions → молчим (не путаем старый агент со старым helper'ом).

    У только что поставленной ноды отсутствующий helper не флагуем: установщик его еще ставит
    (_fresh_node). Устаревший флагуем: установщик берет helper'ы у панели, со старым не приходит."""
    fresh = _fresh_node(server)
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
        if not _setup_needed(name, rep, server.alert_mutes, relay=server.relay_enabled, gre=server.gre_enabled):
            continue
        inst = sv.get(name)  # None = helper до версионирования
        # агенты < 1.63 слали версии числами — приводим к строке, чтобы не спорить о типах
        inst = None if inst is None else str(inst)
        # relay-setup и gre-setup - не про установщик: галочку ставят уже на живой ноде, и
        # установщик, прошедший раньше, его не принесет. Спрятать его на 20 минут значило
        # оставить kervax_outdated пустым как раз тогда, когда человек пошел гонять плейбук
        if inst is None and fresh and name not in ("relay-setup", "gre-setup"):
            continue
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
# Операторы (altinity/clickhouse-operator, postgres-operator…) базами управляют, но данных
# не держат: на k8s-c под clickhouse-operator числился второй ClickHouse.
_NOT_DB_MARKERS = ("exporter", "postgrest", "operator")


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


# Дамп базы из пода снимает helper на ноде: kubectl exec с админским доступом самой ноды.
# Цель — не под, а его StatefulSet/Deployment/DaemonSet: под helper каждый раз находит заново,
# потому что поды пересоздаются под новыми именами (у Deployment хеш меняется при каждом
# выкате). Раньше панель печатала CronJob с именами, выведенными из имени пода, — его
# приходилось править руками, и после передеплоя он переставал попадать в базу.
_KUBE_DUMP_ENGINES = {"pg", "mysql", "ch", "redis", "rabbitmq"}
_KUBE_DUMP_HELPER = "0.27"
# ClickHouse helper 0.27 дампил, входя пользователем default, и на k8s-c упёрся в
# пароль, которого нет в окружении пода. С 0.28 схему читает из файлов metadata — без входа.
_KUBE_DUMP_HELPER_FOR = {"ch": "0.28"}
_KUBE_KIND = {"sts": "StatefulSet", "deploy": "Deployment", "ds": "DaemonSet"}


def _kube_workload(pod: dict) -> str:
    """Контроллер пода как цель дампа: «k8s.<ns>.<sts|deploy|ds>.<имя>» ("" — под без
    контроллера, найти его заново после пересоздания не по чему).

    Имя контроллера выводим из имени пода — так их именует сам Kubernetes: StatefulSet
    даёт <имя>-<номер>, Deployment через ReplicaSet — <имя>-<хеш>-<суффикс>, DaemonSet —
    <имя>-<суффикс>. Helper на ноде по этому имени берёт селектор и ищет живой под."""
    ns, name, owner = pod.get("ns") or "", pod.get("name") or "", pod.get("owner") or ""
    rules = {
        "StatefulSet": ("sts", r"^(.+)-\d+$"),
        "ReplicaSet": ("deploy", r"^(.+)-[a-z0-9]{6,10}-[a-z0-9]{5}$"),
        "DaemonSet": ("ds", r"^(.+)-[a-z0-9]{5}$"),
    }
    if owner not in rules or not ns:
        return ""
    kind, rx = rules[owner]
    m = re.match(rx, name)
    return f"k8s.{ns}.{kind}.{m.group(1)}" if m else ""


def _kube_dump_block(rep: dict, code: str) -> str:
    """Почему дамп из пода на этой ноде не включить ("" — можно)."""
    if code not in _KUBE_DUMP_ENGINES:
        return "дамп этого движка из пода панель не снимает"
    need = _KUBE_DUMP_HELPER_FOR.get(code, _KUBE_DUMP_HELPER)
    have = str((rep.get("setup_versions") or {}).get("backup-setup") or "")
    if not have or _ver_key(have) < _ver_key(need):
        return (f"дамп из пода снимает helper backup-setup {need} и новее — "
                "обновите его на ноде")
    if not ((rep.get("extras") or {}).get("kube-dumps") or {}).get("exec"):
        return ("у helper на этой ноде нет доступа к кластеру: дамп из пода включается на "
                "управляющей ноде (k0s, k3s, microk8s или kubeadm с admin.conf)")
    return ""


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


# Движки с инвентарем баз (dbstat-setup): код движка в db_stats. Их объем - сумма баз, а не каталог:
# каталог PostgreSQL держит еще WAL, и du по нему не нужен вовсе
_SIZE_INV = {"PostgreSQL": "pg", "MySQL/MariaDB": "mysql", "ClickHouse": "clickhouse"}
# Индексы в дамп не идут (pg_dump, mysqldump): их размер helper меряет раз в сутки
_SIZE_IDX = {"pg", "mysql"}
_SIZE_SMALL = {"RabbitMQ"}  # export_definitions: килобайты при любом объеме очередей
# Замер каталогов раз в сутки, отчет каждые 15 минут: старше трех суток - helper замер бросил
_SIZE_MAX_AGE = 3 * 86400


class _CoverageSizes:
    """Примерный объем, который придется бэкапить, для строк "Покрытия". Источники - то, что root-helper
    dbstat-setup уже присылает: инвентарь баз (db_stats) и с 0.15 extras["db-sizes"] (индексы и du
    каталогов данных). Без замера строка молчит, а не пишет "неизвестно"."""

    def __init__(self, rep: dict):
        self.inv: dict[tuple[str, str], list[tuple[str, int]]] = {}
        for e in rep.get("db_stats") or []:
            if not isinstance(e, dict) or e.get("engine") not in _SIZE_INV.values():
                continue
            dbs = [(str(d.get("name") or ""), int(d.get("size") or 0))
                   for d in e.get("dbs") or [] if isinstance(d, dict)]
            self.inv[(e["engine"], str(e.get("container") or ""))] = [x for x in dbs if x[1] > 0]
        self.idx: dict[tuple[str, str], int] = {}
        self.du: dict[str, int] = {}  # контейнер -> байты
        self.native: dict[str, int] = {}  # движок нативной установки -> байты
        block = (rep.get("extras") or {}).get("db-sizes")
        items = block.get("items") if isinstance(block, dict) else None
        ref = float(rep.get("clock_unix") or 0) or datetime.now(timezone.utc).timestamp()
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict) or ref - float(it.get("ts") or 0) > _SIZE_MAX_AGE:
                continue
            key, cont = str(it.get("key") or ""), str(it.get("container") or "")
            if int(it.get("idx") or 0) > 0:
                self.idx[(key, cont)] = int(it["idx"])
            b = int(it.get("bytes") or 0)
            if b <= 0:
                continue
            if cont:
                self.du[cont] = b
            elif key in _DB_IMAGES:
                self.native[_DB_IMAGES[key]] = self.native.get(_DB_IMAGES[key], 0) + b

    def fill(self, a: BackupAudit) -> None:
        # поды kubernetes helper не меряет; у CronJob-ветки instance пустой, но поды есть
        if a.kind not in ("db", "db_ok") or a.pods or a.instance.startswith("k8s."):
            return
        if a.subject in _SIZE_SMALL:
            a.size_small = True
            return
        code = _SIZE_INV.get(a.subject)
        dbs = self.inv.get((code, a.instance)) if code else None
        if dbs:
            total = sum(s for _, s in dbs)
            idx = self.idx.get((code, a.instance), 0) if code in _SIZE_IDX else 0
            # индексов больше базы не бывает: такой замер с другого дня, лучше без него
            a.size_bytes = total - idx if 0 < idx < total else total
            if a.size_bytes < total * 0.85:
                a.size_db = total
            if len(dbs) > 1:
                name, top = max(dbs, key=lambda x: x[1])
                if top >= total * 0.3:  # крупнейшая из сотни мелких ничего не объясняет
                    a.size_top, a.size_top_bytes = name, top
            return
        a.size_bytes = self.du.get(a.instance, 0) if a.instance else self.native.get(a.subject, 0)


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
    # движок → цель дампа (k8s.<ns>.<вид>.<имя>) → её поды. Поды без контроллера — отдельно
    kube_wl: dict[str, dict[str, list[str]]] = {}
    kube_bare: dict[str, list[str]] = {}
    # Под, который ДОЛЖЕН работать, но не поднялся, раньше просто выпадал из аудита, и
    # карточка писала «процесс на хосте»: на ноде остаётся процесс оператора базы.
    # Живой случай - k8s-b, 23.09.2026: ClickHouse 6 часов в
    # CreateContainerConfigError (нет секрета), а панель предлагала включить ему дамп.
    down_pods: dict[str, str] = {}
    for p in ((rep.get("kube") or {}).get("pods") or []):
        # Succeeded/Failed - это отработавшие поды Job'ов, живой базой они не были
        if not p.get("image") or p.get("phase") not in ("Running", "Pending"):
            continue
        eng = _db_engine_of(p.get("image") or "", p.get("name") or "")
        if eng:
            ref = f"{p.get('ns', '?')}/{p.get('name', '?')}"
            # ready is False, а не falsy: старый агент поля не шлёт вовсе, и на нём
            # «не готов» означало бы «все поды лежат» - карточка врала бы наоборот
            if p.get("phase") != "Running" or p.get("ready") is False:
                down_pods[ref] = str(p.get("reason") or "не запущен")
            kube_pods.setdefault(eng, []).append(ref)
            wl = _kube_workload(p)
            if wl:
                kube_wl.setdefault(eng, {}).setdefault(wl, []).append(ref)
            else:
                kube_bare.setdefault(eng, []).append(ref)
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
            # Дамп настроен, но все поды базы лежат: «дамп уже настроен» читалось бы как
            # «всё в порядке». Живой случай - postgres-0 семь часов в CrashLoopBackOff
            # (нет TLS-сертификата), а карточка оставалась зелёной.
            all_down = [down_pods[x] for x in pods if x in down_pods]
            if pods and len(all_down) == len(pods):
                out.append(BackupAudit(
                    kind="db", subject=eng, gap=False, pods=pods[:4],
                    detail=f"под не запущен ({all_down[0]}) — база не работает, "
                           f"дамп настроен: CronJob {existing[eng]}",
                    note=f"под не запущен ({all_down[0]}), база не работает; дамп: CronJob {existing[eng]}",
                ))
                continue
            # дамп уже есть (CronJob в кластере) — сообщаем как факт, без кнопки.
            # container/pods отдаём и здесь: по ним «Сервисы» показывают, ГДЕ живёт
            # СУБД. Раньше эта ветка их не заполняла, и странице оставался только
            # текст находки — в итоге она рисовала в поле «где» фразу про CronJob.
            out.append(BackupAudit(
                kind="db_ok", subject=eng, gap=False,
                detail=f"дамп уже настроен: CronJob {existing[eng]}",
                note=f"дамп: CronJob {existing[eng]}",
                container=names[0] if len(names) == 1 else "",
                pods=pods[:4],
            ))
            continue
        # экземпляры: каждый контейнер и каждый контроллер kubernetes — отдельная находка.
        # Нативная установка и поды без контроллера имени не имеют — одна запись с пустым
        # instance. Процесс базы из пода виден и в /proc хоста, поэтому «нативную» запись
        # заводим, только если ни контейнеров, ни контроллеров не нашлось.
        wls = kube_wl.get(eng, {})
        bare = sorted(set(kube_bare.get(eng, [])))
        insts: list[str] = list(names) + sorted(wls)
        if not insts or bare:
            insts.append("")
        now_c = datetime.now(timezone.utc)
        for inst in insts:
            kube = inst.startswith("k8s.")
            inst_pods = sorted(set(wls.get(inst, []))) if kube else ([] if inst else bare)
            if kube:
                _, ns, kind, wname = inst.split(".", 3)
                where_txt = (f"{_KUBE_KIND.get(kind, kind)} {ns}/{wname} в kubernetes"
                             f" (под {', '.join(x.split('/', 1)[1] for x in inst_pods[:3])})")
            else:
                where_txt = (
                    f"контейнер: {inst}" if inst
                    else ("под kubernetes: " + ", ".join(bare[:4]) if bare else "процесс на хосте")
                )
            # Свой бэкап ноды (cron, таймер, скрипт с метриками), который бэкапит именно этот
            # экземпляр. Раньше панель его не видела и звала «настройте дамп» там, где дамп
            # годами снимает ансибл-роль.
            # Все поды этого экземпляра лежат - дамп снимать не с чего, и звать
            # «включите дамп» бессмысленно: сначала надо поднять базу.
            down = [down_pods[x] for x in inst_pods if x in down_pods]
            if inst_pods and len(down) == len(inst_pods):
                out.append(BackupAudit(
                    kind="db", subject=eng, gap=False, instance=inst,
                    detail=f"{where_txt} — под не запущен ({down[0]}), "
                           "дамп снять не с чего: сначала поднимите базу",
                    note=f"под не запущен ({down[0]}), дамп снять не с чего: сначала поднимите базу",
                    container=inst, pods=inst_pods[:4],
                ))
                continue
            own = custom_backups.db_cover(server, now_c, eng, inst, len(insts))
            if own is not None:
                out.append(BackupAudit(
                    kind="db_ok", subject=eng, gap=False, instance=inst,
                    detail=f"{where_txt} — свой бэкап: {custom_backups.label(own)}",
                    note=f"свой бэкап: {custom_backups.label(own)}",
                    container=inst, pods=inst_pods[:4],
                ))
                continue
            dump = panel_dumps.get((code, inst)) if code else None
            # старый helper (< v8) не различал контейнеры и слал дамп без имени —
            # засчитываем его первому экземпляру, иначе после обновления панели
            # уже работающий дамп показался бы выключенным
            if dump is None and code and inst and inst == insts[0]:
                dump = panel_dumps.get((code, ""))
            # Перед бэкапом дамп идёт, только если бэкап на ноде есть: helper цепляет дамп к
            # его сервису. Без бэкапа helper ставит свой суточный таймер, и копия остаётся на
            # самой ноде (k8s-c). configured — агент нашёл таймер бэкапа.
            own_timer = not bk.get("configured")
            # На такой ноде сломанный дамп виден только по часам — тот же признак, что у
            # алерта backup_dump. Отсчёт от последнего отчёта ноды: у замолчавшей ноды файлы
            # не «стареют», про неё есть свой алерт.
            seen_ts = (server.last_seen.replace(tzinfo=server.last_seen.tzinfo or timezone.utc).timestamp()
                       if server.last_seen else datetime.now(timezone.utc).timestamp())
            if dump and own_timer and dump_local_stale(dump, seen_ts):
                out.append(BackupAudit(
                    kind="db", subject=eng, gap=False, instance=inst,
                    detail=f"{where_txt} — дамп включён, но свежего файла нет больше двух суток,"
                           " проверьте, снимается ли он",
                    note="свежего файла дампа нет больше двух суток, проверьте, снимается ли он",
                    dump_engine=code, can_dump=True, container=inst, pods=inst_pods[:4],
                ))
                continue
            if dump and (dump.get("files") or 0) > 0:
                out.append(BackupAudit(
                    kind="db_ok", subject=eng, gap=False, instance=inst,
                    detail=(f"{where_txt} — дамп снимается раз в сутки по своему таймеру, "
                            "копия только на этой ноде" if own_timer
                            else f"{where_txt} — дамп снимает панель перед каждым бэкапом"),
                    note=("дамп раз в сутки по своему таймеру, копия только на ноде" if own_timer
                          else "дамп перед каждым бэкапом"),
                    dump_engine=code, can_dump=True, container=inst, pods=inst_pods[:4],
                ))
                continue
            if dump:
                # Только что включили? Файлов ещё нет законно — первый дамп в ближайший
                # бэкап. «Проблема» лишь если бэкап уже НАЧАЛСЯ после включения, а файла нет:
                # дампы снимаются в начале прогона, включенный посреди долгого бэкапа ждет
                # следующего (backup_run_start).
                enabled = dump.get("enabled_ts") or 0
                if enabled and backup_run_start(bk) > enabled:
                    out.append(BackupAudit(
                        kind="db", subject=eng, gap=False, instance=inst,
                        detail=f"{where_txt} — дамп включён, но после бэкапа файлов в /backup"
                               " нет, проверьте, снимается ли он",
                        note="после бэкапа файлов дампа нет, проверьте, снимается ли он",
                        dump_engine=code, can_dump=True, container=inst, pods=inst_pods[:4],
                    ))
                else:
                    # ждём первого бэкапа по расписанию — это норма, не проблема
                    out.append(BackupAudit(
                        kind="db_ok", subject=eng, gap=False, instance=inst,
                        detail=(f"{where_txt} — дамп включён, первый снимется ночью по своему таймеру"
                                if own_timer
                                else f"{where_txt} — дамп включён, первый снимется в ближайший бэкап"),
                        note=("первый дамп снимется ночью по своему таймеру" if own_timer
                              else "первый дамп снимется в ближайший бэкап"),
                        dump_engine=code, can_dump=True, container=inst, pods=inst_pods[:4],
                    ))
                continue
            # Кнопку дампа даём только там, где helper реально может его снять: docker exec,
            # локально или kubectl exec с админским доступом самой ноды. Агенту pods/exec
            # по-прежнему не выдаём — exec делает root-helper по белому списку движков.
            if kube:
                block = _kube_dump_block(rep, code) if code else ""
                can_dump = bool(code) and not block
            else:
                block = ("не удалось определить StatefulSet или Deployment пода — дамп из него панель "
                         "не снимает") if bare and not inst else ""
                can_dump = not block
            out.append(BackupAudit(
                kind="db", subject=eng, gap=False, instance=inst,
                detail=f"{where_txt} — файловый снапшот живой базы может не восстановиться"
                       + (f", бэкапьте отдельно: {howto}" if howto else "")
                       + (f" — {block}" if block else ""),
                note=", ".join(x for x in (f"бэкапить отдельно: {howto}" if howto else "", block) if x),
                dump_engine=code, can_dump=can_dump,
                downtime=_DUMP_DOWNTIME.get(code, "") if can_dump and not kube else "",
                container=inst, pods=inst_pods[:4],
            ))
    sizes = _CoverageSizes(rep)
    for a in out:
        sizes.fill(a)
    # покрытие путей знаем только для нод с helper'ом (у остальных нет конфига)
    if not bk.get("manageable"):
        return out
    # старый runner, юнит не разобран: что бэкапится, неизвестно - дыр не выдумываем
    if not backup_unit.paths_known(bk):
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
            # ГБ двоичные, как в карточке дисков: десятичные давали на ~7% больше
            out.append(BackupAudit(
                kind="mount", subject=m, gap=True,
                detail=f"смонтированная ФС с данными ({backup_growth.fmt_size(d.get('used') or 0)}) не попадает в бэкап",
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
        if not a.note and a.kind != "db":
            a.note = a.detail
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
    # только серверы групп учетки: редактор группы не раскатывает агент на чужие
    q = scope_query(user, select(Server).where(Server.enabled.is_(True)), Server)
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
    if body.track and servers:
        await fleet_rollout.track_agent(session, body.version, [s.id for s in servers])
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
    q = scope_query(user, select(Server), Server)
    if body.server_ids is not None:
        q = q.where(Server.id.in_(body.server_ids))
    else:
        q = q.where(Server.target_agent_version != "")
    servers = list(await session.scalars(q))
    names = [s.name for s in servers]
    for s in servers:
        s.target_agent_version = ""
    await session.commit()
    if body.untrack:
        await fleet_rollout.untrack(session, "agent", ids={s.id for s in servers})
    await audit.record(session, user.username, "agent_update_cancel", str(names))
    now = datetime.now(timezone.utc)
    return [_out(s, now) for s in servers]


async def _scoped_nodes(user, session) -> tuple[set[int], set[str]]:
    """id и хосты (как их зовет ansible: hostname, иначе имя) серверов групп учетки."""
    rows = (await session.execute(
        scope_query(user, select(Server.id, Server.hostname, Server.name), Server))).all()
    return {r.id for r in rows}, {r.hostname or r.name for r in rows}


@router.get("/fleet-rollout", response_model=FleetRolloutOut)
async def get_fleet_rollout(user: CurrentUser, session: SessionDep) -> FleetRolloutOut:
    """Что запущено из "Обновлений" и когда (fleet_rollout.py). Чужие группы не видны:
    редактор группы получает только свои ноды, пусто - части нет."""
    data = await fleet_rollout.load(session)
    ids, hosts = await _scoped_nodes(user, session)
    out = FleetRolloutOut()
    if (a := data["agent"]) and (mine := [i for i in a["ids"] if i in ids]):
        out.agent = AgentRunOut(v=a["v"], ids=mine, at=datetime.fromtimestamp(a["at"], timezone.utc))
    if (h := data["helpers"]) and (mine_h := [x for x in h["hosts"] if x in hosts]):
        out.helpers = HelperRunOut(hosts=mine_h, at=datetime.fromtimestamp(h["at"], timezone.utc))
    return out


@router.post("/fleet-rollout/helpers", response_model=FleetRolloutOut)
async def start_helper_rollout(
    body: HelperRolloutReq, user: CurrentUser, session: SessionDep
) -> FleetRolloutOut:
    """Команду ansible для helper'ов скопировали: панель ее не выполняет, только ждет, пока
    хосты перестанут требовать прогона (helper_rollout), и показывает прогресс."""
    _, hosts = await _scoped_nodes(user, session)
    mine = sorted({h for h in body.hosts if h in hosts})
    if mine:
        await fleet_rollout.track_helpers(session, mine)
        await audit.record(session, user.username, "helper_rollout", str(mine))
    return await get_fleet_rollout(user, session)


@router.delete("/fleet-rollout/{part}", response_model=FleetRolloutOut)
async def close_fleet_rollout(part: str, user: CurrentUser, session: SessionDep) -> FleetRolloutOut:
    """Строка ушла (все обновились) или ее скрыли: из хода раскатки уходят ноды учетки.
    Цели агента не снимаются - для этого "стоп" (agent-update/cancel)."""
    if part not in ("agent", "helpers"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Нет такой части")
    ids, hosts = await _scoped_nodes(user, session)
    await fleet_rollout.untrack(session, part, ids=ids, hosts=hosts)
    return await get_fleet_rollout(user, session)


@router.get("/{server_id}", response_model=ServerOut)
async def get_server(server_id: int, user: CurrentUser, session: SessionDep) -> ServerOut:
    server = await _get_or_404(server_id, session, user)
    await gitlab_mod.load_feed(session)
    o = _out(server, datetime.now(timezone.utc))
    bogus = (await request_watch.server_issues(session)).get(server.id)
    if bogus:
        o.problems = [*o.problems, bogus]
    return _logs_for(o, user)


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


@router.put("/{server_id}/note", response_model=ServerOut)
async def set_server_note(
    server_id: int, body: NoteIn, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Заметка о сервере: видна всем, кто видит сервер, и едет строкой в его алертах."""
    server = await _get_or_404(server_id, session, user)
    text = body.text.strip()
    now = datetime.now(timezone.utc)
    server.note = text
    server.note_by = user.username if text else ""
    server.note_at = now if text else None
    await session.commit()
    await session.refresh(server)
    await audit.record(session, user.username, "server_note", server.name, text[:200])
    return _out(server, now)


@router.put("/{server_id}/known/{kind}", response_model=ServerOut)
async def set_server_known(
    server_id: int, kind: str, body: KnownIn, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Отметить проблему сервера известной: алерт по ней молчит, пока не станет хуже, а
    когда она уйдет сама, отметка закроется (app/known.py)."""
    server = await _get_or_404(server_id, session, user)
    if kind == "offline":
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Недоступность так не отмечается: для работ на сервере нужен снуз")
    if not (server.alert_state or {}).get(f"{kind}_from"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Этой проблемы сейчас нет")
    now = datetime.now(timezone.utc)
    cur = dict(server.known_issues or {})
    cur[kind] = known.new_entry(body.text, user.username, body.days, now)
    server.known_issues = cur
    await session.commit()
    await session.refresh(server)
    await audit.record(session, user.username, "known_set", server.name, f"{kind}: {body.text[:200]}")
    return _out(server, now)


@router.post("/{server_id}/onboard/done", response_model=ServerOut)
async def finish_onboard(server_id: int, user: CurrentUser, session: SessionDep) -> ServerOut:
    """Закрыть подключение: что сейчас сломано, на следующем тике станет известной проблемой
    ("было при подключении"), дальше алерты как у всех (app/onboard.py)."""
    server = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    if server.onboard_done_at is None:
        server.onboard_done_at, server.onboard_by = now, user.username
        await session.commit()
        await session.refresh(server)
        await audit.record(session, user.username, "onboard_done", server.name)
    return _out(server, now)


@router.delete("/{server_id}/known/{kind}", response_model=ServerOut)
async def drop_server_known(
    server_id: int, kind: str, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Снять отметку: если проблема осталась, алерт уйдет на ближайшем тике."""
    server = await _get_or_404(server_id, session, user)
    cur = dict(server.known_issues or {})
    if cur.pop(kind, None) is not None:
        server.known_issues = cur or None
        await session.commit()
        await session.refresh(server)
        await audit.record(session, user.username, "known_drop", server.name, kind)
    return _out(server, datetime.now(timezone.utc))


@router.post("/{server_id}/exposure/scan", response_model=ServerOut)
async def exposure_scan(server_id: int, user: CurrentUser, session: SessionDep) -> ServerOut:
    """Проверить снаружи, что открыто в интернет, не дожидаясь планового прогона (раз в 6 часов):
    например, сразу после правки файрвола."""
    s = await _get_or_404(server_id, session, user)
    if not (s.last_report or {}).get("listen"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Агент на ноде старше 2.26 и не сообщает, какие порты слушает")
    now = datetime.now(timezone.utc)
    last = open_ports.scanned_at(s)
    if last is not None and now - last < timedelta(minutes=1):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Проверка только что была, подождите минуту")
    proxy, via = await open_ports.pick_proxy(session)
    s.exposure = await open_ports.scan_server(s, proxy, via, now)
    await session.commit()
    await session.refresh(s)
    await audit.record(session, user.username, "exposure_scan", s.name, via)
    return _out(s, datetime.now(timezone.utc))


@router.get("/{server_id}/packages")
async def server_packages(server_id: int, user: CurrentUser, session: SessionDep,
                          q: str = Query("", max_length=100)) -> dict:
    """Установленные пакеты сервера (агент 2.28), с фильтром по имени и версии."""
    await _get_or_404(server_id, session, user)
    return await packages_mod.list_for(session, server_id, q)


@router.get("/{server_id}/software")
async def server_software(server_id: int, user: CurrentUser, session: SessionDep) -> dict:
    """Версии ПО сервера и сроки их поддержки (app/software.py): все найденные, образы без версии
    в теге и образы, которые больше года не пересобирались; у кончившихся и скорых - как исправить
    (fix) и решение человека (mkey, muted)."""
    s = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    inv = software_mod.inventory(s.last_report or {}, s.sw_host, now, s.alert_mutes)
    for g in inv["items"]:
        g["text"] = software_mod.item_text(g) if g["state"] != "ok" else ""
        if g["state"] != "ok":
            g["fix"] = software_mod.fix(g, s.last_report or {}, now)
    tbl = software_mod.table()
    # когда был отчет и как часто они идут: после шагов "как исправить" видно, свежие ли это данные
    return inv | {"built": tbl.get("built"), "source": tbl.get("source"),
                  "agent_ok": _ver_key(s.agent_version) >= (2, 35), "pkgs": s.sw_host is not None,
                  "report_at": _aware_dt(s.last_seen).isoformat() if s.last_seen else None,
                  "every": get_settings().server_report_interval}


@router.get("/{server_id}/vulns")
async def server_vulns(server_id: int, user: CurrentUser, session: SessionDep) -> dict:
    """Важные уязвимости в пакетах сервера (app/vulns.py): critical и уже эксплуатируемые, только
    те, что закрываются обновлением."""
    from app import vulns as vulns_mod
    s = await _get_or_404(server_id, session, user)
    return await vulns_mod.for_server(session, s)


@router.get("/{server_id}/timeline")
async def server_timeline(
    server_id: int, user: CurrentUser, session: SessionDep, days: int = Query(7, ge=1, le=90)
) -> dict:
    """Хронология сервера одной лентой (app/timeline.py): что менялось и что с ним было."""
    from app import settings_store

    s = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    cfg = await settings_store.get_alert_config(session, get_settings())
    return await timeline_mod.view(session, s, user, now - timedelta(days=days), now,
                                   cfg.get("tz") or "Europe/Moscow")


@router.post("/{server_id}/changes/ack", response_model=ServerOut)
async def changes_ack(server_id: int, user: CurrentUser, session: SessionDep) -> ServerOut:
    """Изменения доступа на сервере сделали свои: строка "изменился доступ" уходит с главной.
    Лента остается как есть, новое изменение снова покажется и придет алертом."""
    s = await _get_or_404(server_id, session, user)
    if user.role == "viewer":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужны права оператора")
    cur = dict(s.changes or {}) if isinstance(s.changes, dict) else {}
    cur["ack_ts"] = datetime.now(timezone.utc).isoformat()
    cur["ack_by"] = user.username
    s.changes = cur
    await session.commit()
    await session.refresh(s)
    await audit.record(session, user.username, "changes_ack", s.name, "")
    return _out(s, datetime.now(timezone.utc))


@router.post("/{server_id}/webfiles-accept", response_model=ServerOut)
async def webfiles_accept(server_id: int, body: dict, user: CurrentUser, session: SessionDep) -> ServerOut:
    """Улики в веб-корне - свои (app/webfiles.py): root - какой корень (пусто - все), key - php или
    пусто (edit - от старых версий панели). Признак уходит, следующая улика снова видна."""
    s = await _get_or_404(server_id, session, user)
    if user.role == "viewer":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Нужны права оператора")
    root, key = str(body.get("root") or ""), str(body.get("key") or "")
    if key not in ("", "php", "edit"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Принимать нечего")
    s.webfiles = webfiles_mod.accept(s.webfiles, root, key)
    await session.commit()
    await session.refresh(s)
    await audit.record(session, user.username, "webfiles_accept", s.name, f"{root} {key}".strip())
    return _out(s, datetime.now(timezone.utc))


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
    fields = body.model_dump(exclude_unset=True)
    autofix = fields.pop("disk_autofix", None)
    autofix_changed = autofix is not None and autofix != server.disk_autofix
    # панель начнет сама запускать удаление на ноде: решение админа, не редактора
    if autofix_changed and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Авто-очистку диска включает только админ")
    if autofix_changed:
        fields["disk_autofix"] = autofix
    if "group_name" in fields and fields["group_name"] != server.group_name and (
            not group_allowed(user, fields["group_name"])):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Группа недоступна для этой учётной записи")
    if fields.get("name") is not None:
        fields["name"] = fields["name"].strip()
        await _name_free(session, fields["name"], exclude_id=server.id, user=user)
    parent_changed = "parent_id" in fields and fields["parent_id"] != server.parent_id
    if parent_changed and fields["parent_id"] is not None:
        pid = fields["parent_id"]
        if pid == server.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Сервер не может стоять сам за собой")
        parent = await _get_or_404(pid, session, user)  # чужую группу не выбрать
        chain = {i: p for i, p in (await session.execute(select(Server.id, Server.parent_id))).all() if p}
        if parents_mod.would_cycle(server.id, pid, chain):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"{parent.name} сам стоит за {server.name}: такая связь замкнет цепочку")
    for field, value in fields.items():
        setattr(server, field, value)
    await session.commit()
    await session.refresh(server)
    await _write_agent_allowlist(session)
    await audit.record(session, user.username, "server_update", server.name)
    if parent_changed:
        pname = (await session.scalar(select(Server.name).where(Server.id == server.parent_id))
                 if server.parent_id else None)
        await audit.record(session, user.username, "server_parent", server.name,
                           f"за {pname}" if pname else "ни за кем")
    if autofix_changed:
        await audit.record(session, user.username,
                           "disk_autofix_on" if autofix else "disk_autofix_off", server.name)
    return _out(server, datetime.now(timezone.utc))


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(
    server_id: int, user: CurrentUser, session: SessionDep
) -> None:
    server = await _get_or_404(server_id, session, user)
    name, had_gre = server.name, server.gre_enabled
    await session.execute(
        sa_delete(ServerMetric).where(ServerMetric.server_id == server_id)
    )
    # кто стоял за этим сервером или зависел от него вручную - теперь ни за кем, а мониторы
    # снова ищут родителя сами
    await session.execute(sa_update(Server).where(Server.parent_id == server_id).values(parent_id=None))
    # ходившие через ретранслятор на этом сервере - больше ни через какой
    await session.execute(sa_update(Server).where(Server.relay_via_id == server_id).values(relay_via_id=None))
    await session.execute(
        sa_update(Check).where(Check.parent_server_id == server_id)
        .values(parent_server_id=None, parent_mode="auto"))
    await timeline_mod.forget(session, server_id)
    await netlinks.forget(session, server_id)
    await session.execute(sa_delete(ServerTraffic).where(ServerTraffic.server_id == server_id))
    await session.execute(sa_delete(ServerUsage).where(ServerUsage.server_id == server_id))
    await session.delete(server)
    await session.commit()
    await _write_agent_allowlist(session)
    if had_gre:  # хост панели снимает его туннель
        await gre_mod.write_tunnels(session)
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

    def avg_parts(
        rows: list[ServerMetric], attr: str, key: str, vals: tuple[str, ...]
    ) -> list[dict] | None:
        # Как avg_named, но для разбивки "пятерка за минуту": лог, выпавший из пятерки в
        # какую-то минуту бакета, в ней считается нулем, и делим на все минуты с разбивкой.
        # Усреднение только по минутам, где лог был, завышало его: полосы стека вылезали
        # выше общего числа запросов.
        have = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
        if not have:
            return None
        acc: dict[str, dict[str, float]] = {}
        for items in have:
            for it in items:
                name = it.get(key)
                if not name:
                    continue
                slot = acc.setdefault(name, {v: 0.0 for v in vals})
                for v in vals:
                    slot[v] += float(it.get(v) or 0)
        return [{key: name, **{v: round(x / len(have), 1) for v, x in slot.items()}}
                for name, slot in sorted(acc.items())]

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
                web_rpm=avg([x.web_rpm for x in grp]),
                web_5xx=avg([x.web_5xx for x in grp]),
                web_top=avg_parts(grp, "web_top", "k", ("r", "e")),
                web_codes=avg_parts(grp, "web_codes", "c", ("n",)),
                gpu=avg_named(grp, "gpu", "g", ("u", "m", "t", "p"), 1),
                llm=avg_named(grp, "llm", "k", ("run", "wait", "kv", "gen", "req", "ttft", "tpot", "e2e"), 4),
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


async def _store_web_errors(session, server: Server, body, now: datetime) -> None:
    """Минуты с ошибками 5xx - в историю, по логу на строку. Блок хелпер обновляет раз в
    минуту, а агент шлёт его с каждым отчётом: повтор отсекаем по ts блока на ноде."""
    if web_rate_total(body.extras, now, body.clock_unix) is None:
        return  # блока нет или он протух
    block = (body.extras or {}).get("web-rate") or {}
    src = int(block.get("ts") or 0)
    bad = [x for x in (block.get("logs") or []) if isinstance(x, dict) and (x.get("e5") or 0) > 0]
    if not bad or not src:
        return
    dup = await session.scalar(
        select(WebErrorSample.id).where(
            WebErrorSample.server_id == server.id, WebErrorSample.src_ts == src
        ).limit(1)
    )
    if dup is not None:
        return
    kube = getattr(body, "kube", None) or {}
    namespaces = {str(p.get("ns")) for k in ("pods", "workloads") for p in kube.get(k) or []
                  if isinstance(p, dict) and p.get("ns")}
    for x in bad[:20]:
        codes = x.get("c5") if isinstance(x.get("c5"), dict) else None
        paths = [p for p in (x.get("p5") or []) if isinstance(p, dict)][:8] or None
        lines = [str(v)[:500] for v in (x.get("l5") or []) if isinstance(v, str)][:5] or None
        # "-" - строки, из которых домен не достался: в сводке "по доменам" это не домен
        hosts = [{"h": str(h.get("h"))[:120], "n": int(h.get("e5") or 0)}
                 for h in (x.get("hosts") or [])
                 if isinstance(h, dict) and h.get("h") and h.get("h") != "-" and (h.get("e5") or 0) > 0][:10] or None
        session.add(WebErrorSample(
            server_id=server.id, ts=now, src_ts=src,
            log=str(x.get("log") or "")[:512], label=web_log_label(x)[:255],
            e5=int(x.get("e5") or 0), rpm=int(x.get("rpm") or 0),
            codes=codes, paths=paths, lines=lines, hosts=hosts,
            ups=_web_ups(x, codes, lines, namespaces),
        ))


def _web_ups(x: dict, codes, lines, namespaces: set[str]) -> list[dict] | None:
    """Почему 5xx минуты лога. 503, на которые ingress-nginx ответил сам (нет живых подов), helper
    0.28 отдает видом noep; у старого helper их видно только по строкам - берем оттуда."""
    ups = web_upstream.clean(x.get("up")) or []
    if not any(it["k"] in ("noep", "limit") for it in ups):
        ups += web_upstream.from_lines(lines, codes, int(x.get("e5") or 0), namespaces) or []
    for it in ups:
        if it["k"] in ("noep", "limit") and "s" not in it and (sv := web_upstream.svc_of(it["u"], namespaces)):
            it["s"] = sv
    return ups[:web_upstream.UP_MAX] or None


@router.get("/{server_id}/web-errors", response_model=list[WebErrorOut])
async def server_web_errors(
    server_id: int,
    user: CurrentUser,
    session: SessionDep,
    hours: int = Query(default=24, ge=1, le=720),
) -> list[WebErrorOut]:
    """Ошибки 5xx по логам за окно: где, сколько, какие коды и пути. Сюда ведёт алерт."""
    server = await _get_or_404(server_id, session, user)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    # какие логи нода пишет сейчас: остальные за окно - история (сайты разнесли по логам)
    web = ((server.last_report or {}).get("extras") or {}).get("web-rate")
    # ключ как у сохраненных строк: подпись обрезана до 255, без подписи - путь лога (до 512)
    live = ({web_label_key(web_log_label(x)[:255] or str(x.get("log") or "")[:512])
             for x in (web.get("logs") or []) if isinstance(x, dict)} if isinstance(web, dict) else None)
    rows = list(await session.scalars(
        select(WebErrorSample)
        .where(WebErrorSample.server_id == server_id, WebErrorSample.ts >= since)
        .order_by(WebErrorSample.ts)
    ))
    acc: dict[str, dict] = {}
    seen: dict[str, set] = {}
    lines: dict[str, list[str]] = {}
    ups: dict[str, dict] = {}  # почему 502/504 (helper 0.26): {(адрес, вид): ...} по логу
    for r in rows:
        # Сводим по доменам, а не по ключу лога: см. web_label_key - иначе один и тот же
        # контейнер после обновления хелпера показывался двумя строками.
        k = web_label_key(r.label or r.log)
        a = acc.setdefault(k, {"log": r.log, "label": r.label, "errors": 0, "minutes": 0,
                               "peak": 0, "first_ts": r.ts, "last_ts": r.ts,
                               "codes": {}, "paths": {}, "hosts": {}})
        web_upstream.add(ups.setdefault(k, {}), r.ups)
        a["errors"] += int(r.e5 or 0)
        seen.setdefault(k, set()).add(r.ts)
        a["minutes"] = len(seen[k])
        a["peak"] = max(a["peak"], int(r.e5 or 0))
        a["log"] = r.log
        a["last_ts"] = r.ts
        a["label"] = r.label or a["label"]
        lines.setdefault(k, []).extend(x for x in (r.lines or []) if isinstance(x, str))
        for c, n in (r.codes or {}).items():
            a["codes"][str(c)] = a["codes"].get(str(c), 0) + int(n or 0)
        for it in (r.paths or []):
            if isinstance(it, dict) and it.get("p"):
                a["paths"][str(it["p"])] = a["paths"].get(str(it["p"]), 0) + int(it.get("n") or 0)
        for it in (r.hosts or []):
            if isinstance(it, dict) and it.get("h") and it.get("h") != "-":
                a["hosts"][str(it["h"])] = a["hosts"].get(str(it["h"]), 0) + int(it.get("n") or 0)
    out = []
    for k, a in sorted(acc.items(), key=lambda kv: -kv[1]["errors"]):
        top = sorted(a["paths"].items(), key=lambda kv: -kv[1])[:8]
        hosts = sorted(a["hosts"].items(), key=lambda kv: -kv[1])[:10]
        got = lines.get(k, [])
        out.append(WebErrorOut(**{**a, "key": k, "paths": [{"p": p, "n": n} for p, n in top],
                                  "hosts": [{"h": h, "n": n} for h, n in hosts],
                                  "lines": got[-5:], "lines_n": len(got),
                                  "ups": web_upstream.top(ups.get(k) or {}),
                                  "gone": live is not None and k not in live}))
    return out


@router.get("/{server_id}/autoban")
async def server_autoban(server_id: int, user: CurrentUser, session: SessionDep,
                         days: int = Query(default=30, ge=1, le=90)) -> dict:
    """Блок "Автобан" раздела Веб: режим, активные баны, кого бы забанило, свои адреса в ловушках,
    где бан не работает, и история банов за days дней."""
    server = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    return {"view": autoban_mod.view(server, now),
            "history": await autoban_mod.history(session, server.id, days=days),
            "helper": str(((server.last_report or {}).get("setup_versions") or {}).get("autoban-setup") or "")}


@router.post("/{server_id}/autoban", response_model=BackupCommandOut)
async def server_autoban_cmd(server_id: int, body: AutobanCmdIn, user: AdminUser,
                             session: SessionDep) -> BackupCommandOut:
    """Переключить режим автобана на ноде или снять бан. Только админ: это правка файрвола ноды,
    хоть и только в сторону "пускать". Через спул helper'а autoban-setup, как кнопки units-setup."""
    server = await _get_or_404(server_id, session, user)
    if not autoban_mod.agent_ok(server):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"агент {server.agent_version or '?'} не умеет команды автобана, нужен {autoban_mod.AGENT_MIN}+")
    payload = None
    if body.op == "unban":
        try:
            ip = str(ipaddress.ip_address(body.ip.strip()))
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "не адрес") from None
        payload = {"name": ip}
        await autoban_mod.mark_unban(session, server.id, ip)
    c = BackupCommand(server_id=server_id, action="autoban", mode=body.op, payload=payload, created_by=user.username)
    session.add(c)
    await session.commit()
    await session.refresh(c)
    await audit.record(session, user.username, "autoban_unban" if body.op == "unban" else "autoban_mode",
                       (payload or {}).get("name") or body.op, f"srv={server_id}")
    return BackupCommandOut.model_validate(c)


@router.get("/{server_id}/web-errors/lines", response_model=list[str])
async def server_web_error_lines(
    server_id: int,
    user: CurrentUser,
    session: SessionDep,
    key: str = Query(min_length=1, max_length=300),
    hours: int = Query(default=24, ge=1, le=720),
) -> list[str]:
    """Все сохраненные строки с 5xx одного лога за окно, старые сверху: из них панель
    собирает .txt, чтобы не искать ошибки на сервере. Это строки access-лога (в URL бывают
    токены): учетке только для просмотра - только с галкой "может читать логи"."""
    if user.role == "viewer" and not user.can_read_logs:
        raise HTTPException(status.HTTP_403_FORBIDDEN, LOGS_CLOSED)
    await _get_or_404(server_id, session, user)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = await session.scalars(
        select(WebErrorSample)
        .where(WebErrorSample.server_id == server_id, WebErrorSample.ts >= since)
        .order_by(WebErrorSample.ts, WebErrorSample.id)
    )
    out: list[str] = []
    for r in rows:
        if r.lines and web_label_key(r.label or r.log) == key:
            out.extend(x for x in r.lines if isinstance(x, str))
    return out[-5000:]


@router.get("/{server_id}/oom")
async def server_oom(server_id: int, user: CurrentUser, session: SessionDep) -> dict:
    """Эпизоды OOM за 30 дней (новые первыми): кого убили, кто занимал память, Mem-Info, какие
    юниты задело. Из записанного в базу и свежего блока helper'а (если отчет еще не записан)."""
    server = await _get_or_404(server_id, session, user)
    recs = await oom_mod.load(session, server_id)
    rep = server.last_report or {}
    have = {oom_mod.rec_key(r) for r in recs}
    recs += [r for r in oom_mod.records(oom_mod.block(rep)) if oom_mod.rec_key(r) not in have]
    eps = oom_mod.episodes(recs)
    # поды по имени: нужны отчеты нод с kube-api, их читаем только если OOM был в поде
    pods = None
    if any("kubepods" in str(r.get("memcg") or "") for r in recs):
        pods = pod_uid_names(list(await session.scalars(select(Server))))
    conts = oom_mod.names_of(rep, pods, recs)
    failed = oom_mod.units_failed_now(rep)
    mutes = oom_mod.mutes_now(server, datetime.now(timezone.utc))
    return {"episodes": [oom_mod.episode_view(e, conts, failed, mutes) for e in reversed(eps)],
            "helper": oom_mod.block(rep) is not None}


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


def _cmd_visible(user, c) -> bool:
    """Результат команды (в нем бывает лог) учетке только для просмотра - только своей и только
    с галкой "может читать логи": иначе чужой лог, запрошенный админом, она прочитала бы по
    номеру команды, а свой - уже после того, как галку сняли."""
    return user.role in ("admin", "editor") or (user.can_read_logs and c.created_by == user.username)


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
    out = DockerCommandOut.model_validate(c)
    # 403 на действие отвечает не докер, а прокси docker-сокета: на ноде прокси только для
    # чтения (tecnativa с POST=0). Говорим, в чем дело, вместо голого кода; в БД - как было.
    if out.status == "error" and (out.result or "").strip() == "docker вернул 403":
        out.result = "прокси Docker на ноде пропускает только чтение (403)"
    return out


@router.post("/{server_id}/docker/command", response_model=DockerCommandOut)
async def docker_command(
    server_id: int, body: DockerCommandIn, user: CurrentUser, session: SessionDep
) -> DockerCommandOut:
    """Поставить docker-действие в очередь (агент заберёт в ответе на отчёт, ~15с).
    Исполняется через read-only proxy: только restart/stop/start + logs."""
    viewer_command_ok(user, "docker", body.action)
    await _get_or_404(server_id, session, user)
    c = DockerCommand(
        server_id=server_id, container=body.container, action=body.action,
        tail=body.tail, since=body.since, created_by=user.username,
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
    server_id: int, cmd_id: int, user: CurrentUser, session: SessionDep, request: Request
) -> DockerCommandOut:
    """Статус/результат команды — панель поллит после постановки."""
    await _get_or_404(server_id, session, user)
    c = await session.get(DockerCommand, cmd_id)
    if c is None or c.server_id != server_id or not _cmd_visible(user, c):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Команда не найдена")
    was = c.status
    out = _docker_cmd_out(c, datetime.now(timezone.utc))
    await session.commit()  # зафиксировать возможный таймаут-переход
    if was != "error" and c.status == "error":  # агент не ответил: человек ждал зря
        panel_errors.command_failed(request.app.state.session_factory, "docker", c)
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


@router.post("/{server_id}/kube/expiry-ignore", response_model=ServerOut)
async def kube_expiry_ignore(
    server_id: int, body: KubeExpiryIgnoreIn, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Срок кластера "не нужен, не следить" (старый секрет, который никто не использует) - или
    вернуть его. Отметка привязана к сроку и отпечатку из отчета: обновят секрет - снова следим.
    Сам секрет панель не трогает: она ничего не меняет в кластере."""
    s = await _get_or_404(server_id, session, user)
    now = datetime.now(timezone.utc)
    k = kube_expiry.key({"kind": body.kind, "where": body.where})
    cur = dict(s.kube_expiry_ignored or {})
    if body.ignored:
        it = next((x for x in (s.last_report or {}).get("kube_expiry") or [] if kube_expiry.key(x) == k), None)
        if it is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Такого срока в отчете нет")
        if k not in cur and len(cur) >= kube_expiry.MAX_ITEMS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Слишком много отметок")
        cur[k] = kube_expiry.entry(it, user.username, now)
    else:
        cur.pop(k, None)
    s.kube_expiry_ignored = cur or None
    await session.commit()
    await session.refresh(s)
    await audit.record(
        session, user.username,
        "kube_expiry_ignore" if body.ignored else "kube_expiry_watch",
        s.name, f"{body.kind} {body.where}",
    )
    return _out(s, now)


@router.post("/{server_id}/kube/node-ignore", response_model=ServerOut)
async def kube_node_ignore(
    server_id: int, body: KubeNodeIgnoreIn, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Нода кластера, на которой агент не нужен (чужая, временная), - или вернуть ее в
    подсказку "без агента". Нода остается в списке нод, просто не просит агента."""
    s = await _get_or_404(server_id, session, user)
    names = set(s.kube_node_ignored or [])
    if body.ignored:
        if len(names) >= 1000:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Слишком много скрытых нод")
        names.add(body.node)
    else:
        names.discard(body.node)
    s.kube_node_ignored = sorted(names)
    await session.commit()
    await session.refresh(s)
    await audit.record(
        session, user.username,
        "kube_node_ignore" if body.ignored else "kube_node_watch",
        body.node, f"srv={server_id}",
    )
    o = _out(s, datetime.now(timezone.utc))
    o.kube_unmonitored = _unmonitored(s, kube_coverage.build_index(await session.scalars(select(Server))))
    return o


def _unmonitored(server: Server, index: tuple[set[str], set[str]]) -> list[KubeNodeRef]:
    """Ноды кластера сервера без агента панели - в виде схемы ответа."""
    return [KubeNodeRef(**n) for n in kube_coverage.unmonitored(server, index)]


@router.post("/{server_id}/kube/command", response_model=KubeCommandOut)
async def kube_command(
    server_id: int, body: KubeCommandIn, user: CurrentUser, session: SessionDep
) -> KubeCommandOut:
    """Поставить kube-действие в очередь (агент заберёт ~1-15с). Исполняется по токену
    узкого SA: только rollout_restart (deploy/sts/ds), delete_pod, logs."""
    viewer_command_ok(user, "kube", body.action)
    await _get_or_404(server_id, session, user)
    c = KubeCommand(
        server_id=server_id, ns=body.ns, kind=body.kind, name=body.name,
        action=body.action, tail=body.tail, since=body.since, created_by=user.username,
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
    server_id: int, cmd_id: int, user: CurrentUser, session: SessionDep, request: Request
) -> KubeCommandOut:
    await _get_or_404(server_id, session, user)
    c = await session.get(KubeCommand, cmd_id)
    if c is None or c.server_id != server_id or not _cmd_visible(user, c):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Команда не найдена")
    was = c.status
    out = _kube_cmd_out(c, datetime.now(timezone.utc))
    await session.commit()
    if was != "error" and c.status == "error":  # агент не ответил: человек ждал зря
        panel_errors.command_failed(request.app.state.session_factory, "kube", c)
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


# Сколько ждать ответа агента, прежде чем считать команду пропавшей. disk_fix дольше:
# docker builder prune по кэшу в десятки ГБ идет минутами, агент ждет helper до 15 минут.
_CMD_TIMEOUT = {"disk_fix": 16 * 60, "unit_fix": 7 * 60, "unit_journal": 2 * 60, "autoban": 3 * 60}


def _backup_cmd_out(c: BackupCommand, now: datetime) -> BackupCommandOut:
    if c.status == "running" and (now - c.created_at.replace(
        tzinfo=c.created_at.tzinfo or timezone.utc
    )).total_seconds() > _CMD_TIMEOUT.get(c.action, 90):
        c.status, c.ok, c.result = "error", False, "агент не ответил (таймаут)"
    return BackupCommandOut.model_validate(c)


@router.post("/{server_id}/backup/command", response_model=BackupCommandOut)
async def backup_command(
    server_id: int, body: BackupCommandIn, user: CurrentUser, session: SessionDep
) -> BackupCommandOut:
    """Поставить backup-действие в очередь (агент заберёт ~1-15с). Исполняется через
    узкий helper: только set_paths / set_schedule / run_now."""
    viewer_command_ok(user, "backup", body.action)
    srv = await _get_or_404(server_id, session, user)
    # engine/container нужны только dump_setup — кладём в payload (агент мержит его
    # в команду верхним уровнем), чтобы не плодить колонки под разовые поля
    payload = None
    if body.action == "disk_fix" and body.fix == "analyze":
        # Разобрать раздел, на который helper сам не смотрит (ниже 75%): только чтение (du,
        # docker system df), поэтому не одному админу. Раздел - из тех, что нода сообщает сама.
        if user.role == "viewer":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
        mounts = {d.get("mount") for d in (srv.last_report or {}).get("disks") or [] if isinstance(d, dict)}
        if body.mode != "run" or not body.mount or body.mount not in mounts:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "такого раздела на ноде нет")
        payload = {"name": "analyze", "container": "", "mount": body.mount}
    elif body.action == "disk_fix":
        # Root-действие на ноде (удаляет файлы): только админ и только из каталога. Путь
        # к логу контейнера панель не передает - helper берет его у docker по имени.
        if user.role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Требуются права администратора")
        if not body.fix or body.mode not in ("preview", "run"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "не указано действие или режим")
        if body.fix == "container-log" and not body.container:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "не указан контейнер")
        payload = {"name": body.fix,
                   "container": body.container if body.fix == "container-log" else ""}
    elif body.action == "unit_fix":
        # Перезапустить упавший юнит или сбросить его отметку. Рабочий сервис так не тронуть:
        # helper на ноде делает это, только пока юнит упал.
        if body.mode not in ("restart", "reset") or not body.unit or body.unit.startswith("_"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "не указан юнит или действие")
        payload = {"name": body.unit}
    elif body.action == "unit_journal":
        # Журнал юнита, всей системы (_all) или ядра (_kernel): только чтение, через тот же
        # helper units-setup (0.4+). Viewer'у - только с галкой "может читать логи" (deps.py).
        if not body.unit:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "не указан юнит")
        payload = {"name": body.unit, "since": body.since_min, "lines": body.lines, "prio": body.prio}
    elif body.mode in ("preview", "run", "restart", "reset"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "этот режим - только у disk_fix и unit_fix")
    elif body.action == "set_paths" and not backup_unit.paths_editable((srv.last_report or {}).get("backup")):
        # restic в ExecStart юнита: правка скрипта панели не применится (helper 0.45 тоже откажет)
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "пути заданы в юните бэкапа, панель их не меняет - сначала переведите бэкап "
                            f"на runner панели: {backup_unit.ADOPT_CMD}")
    elif body.action == "deprovision":
        # "Удалить настройку": снять с клиента то, что ставит "Настроить бэкап" (юниты, env с
        # паролем, скрипт, restic панели). Репозиторий на бэкап-сервере не трогается. Живой
        # бэкап так не снести: кнопка только у упавшей настройки и у следов старого бэкапа
        if backup_state.backup_alive((srv.last_report or {}).get("backup")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "на сервере работает бэкап - удалять его настройку отсюда нельзя")
    elif body.action in ("dump_setup", "dump_remove"):
        if not body.engine:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "не указан движок дампа")
        payload = {"engine": body.engine, "container": body.container}
        if body.action == "dump_setup":
            # свой каталог дампов - root-действие на ноде (helper создает его 0700, выключение
            # дампа удаляет): как "Освободить место", только админ. /backup и пусто - всем
            if not dump_dir_default(body.dump_dir) and user.role != "admin":
                raise HTTPException(status.HTTP_403_FORBIDDEN,
                                    "Каталог дампов не в /backup задает только администратор")
            # настройки едут только при включении; агент подставит дефолты для пустых
            payload["dump_dir"] = body.dump_dir
            payload["dump_keep"] = body.dump_keep
            payload["dump_minfree"] = body.dump_minfree
    c = BackupCommand(
        server_id=server_id, action=body.action, mode=body.mode,
        paths=body.paths, schedule=body.schedule, payload=payload, created_by=user.username,
    )
    session.add(c)
    await session.commit()
    await session.refresh(c)
    target = (body.schedule or f"{body.mode}:{len(body.paths)}")[:120]
    if body.action == "disk_fix":
        target = (f"{body.fix}:{body.mode}" + (f":{body.container}" if payload["container"] else "")
                  + (f":{body.mount}" if body.fix == "analyze" else ""))[:120]
    if body.action == "unit_fix":
        target = f"{body.unit}:{body.mode}"[:120]
    if body.action == "unit_journal":
        target = f"{body.unit}:journal:{body.since_min}m:{body.prio}"[:120]
    await audit.record(session, user.username, f"backup_{body.action}", target, f"srv={server_id}")
    return BackupCommandOut.model_validate(c)


@router.get("/{server_id}/backup/command/{cmd_id}", response_model=BackupCommandOut)
async def backup_command_status(
    server_id: int, cmd_id: int, user: CurrentUser, session: SessionDep, request: Request
) -> BackupCommandOut:
    await _get_or_404(server_id, session, user)
    c = await session.get(BackupCommand, cmd_id)
    if c is None or c.server_id != server_id or not _cmd_visible(user, c):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Команда не найдена")
    was = c.status
    out = _backup_cmd_out(c, datetime.now(timezone.utc))
    await session.commit()
    if was != "error" and c.status == "error":  # агент не ответил: человек ждал зря
        panel_errors.command_failed(request.app.state.session_factory, "backup", c)
    return out


@router.post("/{server_id}/backup/custom-ignore", response_model=ServerOut)
async def custom_backup_ignore(
    server_id: int, body: CustomBackupIgnoreIn, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Не отслеживать найденный на ноде бэкап («это не бэкап», «старый скрипт») — или
    вернуть. Задание остаётся видимым в списке, но не алертит и не закрывает покрытие."""
    s = await _get_or_404(server_id, session, user)
    ids = set(s.custom_backup_ignored or [])
    if body.ignored:
        ids.add(body.id)
    else:
        ids.discard(body.id)
    s.custom_backup_ignored = sorted(ids)
    await session.commit()
    await session.refresh(s)
    await audit.record(
        session, user.username,
        "custom_backup_ignore" if body.ignored else "custom_backup_watch",
        body.id[:120], f"srv={server_id}",
    )
    return _out(s, datetime.now(timezone.utc))


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


def _cadences(rep: dict) -> dict[str, int]:
    """Обычный интервал бэкапов по репозиториям бэкап-сервера, где он ясен."""
    out = {}
    for name, o in ((bsrv_extra(rep).get("repos") or {}).items()):
        c = backup_cadence(o.get("recent")) if isinstance(o, dict) else 0
        if c:
            out[name] = c
    return out


@router.post("/{server_id}/backup/repo-archive", response_model=ServerOut)
async def backup_repo_archive(
    server_id: int, body: BackupRepoArchiveIn, user: CurrentUser, session: SessionDep
) -> ServerOut:
    """Убрать в архив или вернуть репозиторий бэкап-сервера (с host - одну старую группу его
    снапшотов). Архив хранится как есть: не устаревает, не проблема, проверка целостности идет.
    На сервере ничего не меняется: чистка по политике у такого репозитория и так ничего не
    удаляет, а группу удаляют только руками (forget-group)."""
    s = await _get_or_404(server_id, session, user)
    key = f"{body.repo}|{body.host}" if body.host else body.repo
    arch = dict(s.backup_repo_archive or {})
    if body.archived:
        arch[key] = {"note": body.note.strip(), "ts": datetime.now(timezone.utc).isoformat(),
                     "by": user.username}
    else:
        arch.pop(key, None)
    s.backup_repo_archive = arch
    await session.commit()
    await session.refresh(s)
    await audit.record(
        session, user.username, "backup_repo_archive" if body.archived else "backup_repo_unarchive",
        key[:120], f"srv={server_id}"
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


def _job_runner(session_factory, job_id: int, user: str = "", what: str = ""):
    """Общая машинерия фоновых job'ов настройки: список шагов + save/step.
    Короткие сессии на каждую операцию (долгая сессия отваливается на провижининге).
    user и what - кто запустил и что: неудача идет в журнал ошибок панели."""
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
            # итог настройки клиента - на сервер: упавшая выносит его в "Проблемы" раздела
            # "Бэкапы" (app/backup_state.py), удачная это снимает
            if what == "setup" and status_ in ("error", "done"):
                srv = await s.get(Server, job.server_id)
                if srv is not None:
                    srv.backup_setup_err = None if status_ == "done" else {
                        "ts": datetime.now(timezone.utc).isoformat(), "msg": (message or "")[:250], "job": job.id}
            await s.commit()
            srv_id = job.server_id
        if status_ == "error" and user:
            await panel_errors.record(
                session_factory, kind="command", action=f"backup {what or 'setup'}",
                message=message or "настройка не удалась", username=user, section="backups",
                server_id=srv_id, detail="\n".join(
                    f"{'ok' if x.get('ok') else 'FAIL'} {x.get('step')}: {x.get('detail') or ''}" for x in steps),
                extra={"job_id": job_id},
            )

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
    _steps, save, step = _job_runner(session_factory, job_id, params.get("_user", ""), "deploy_server")
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
    _steps, save, step = _job_runner(session_factory, job_id, params.get("_user", ""), "enable_tls")
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
    steps, save, step = _job_runner(session_factory, job_id, params.get("_user", ""), "setup")

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
    # сервер бэкапов - тоже из групп учетки: настройка ставит на нем htpasswd, репозиторий и ufw
    bs = await _get_or_404(body.backup_server_id, session, user)
    crep = client.last_report or {}
    # Отказываем только при ЖИВОМ бэкапе и без согласия переустановить поверх. Метрика,
    # оставшаяся от снесенного бэкапа, и упавшая на полпути настройка - не бэкап: раньше
    # форма отказывала по одной метрике, и такую ноду нельзя было настроить вовсе
    if backup_state.backup_alive(crep.get("backup")) and not body.reinstall:
        raise HTTPException(status.HTTP_409_CONFLICT, backup_state.BACKUP_EXISTS)
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
        session, user.username, "backup_setup",
        f"{name}→{bs.name} ({body.mode}{', поверх' if body.reinstall else ''})", f"srv={server_id}"
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
        "_user": user.username,
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
        "port": body.port, "tls": body.tls, "_user": user.username,
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
    params = {"node_id": node.id, "node_ip": node_ip, "node_hostname": node.hostname or "",
              "_user": user.username}
    task = asyncio.create_task(
        _orchestrate_enable_tls(request.app.state.session_factory, job.id, params)
    )
    _setup_tasks.add(task)
    task.add_done_callback(_setup_tasks.discard)
    return BackupSetupJobOut.model_validate(job)


async def _orchestrate_to_https(session_factory, job_id: int, params: dict) -> None:
    """Перевести бэкап клиента с rest:http на TLS rest-server того же сервера бэкапов. Логин и
    пароль не проходят через панель: https-адрес helper клиента собирает из своего env. Порядок:
    TLS rest-server на сервере бэкапов (если его еще нет), его сертификат, TLS-порт клиенту в ufw,
    затем клиент читает репозиторий по https и только при успехе переключается. Неудача на любом
    шаге оставляет бэкап на http."""
    _steps, save, step = _job_runner(session_factory, job_id, params.get("_user", ""), "to_https")
    try:
        bs_id = params["bs_id"]
        if not params["tls_front"]:
            ok, out = await step("tls-front", bs_id, "deploy_tls_front",
                                 {"san_ip": params["san_ip"], "san_dns": params["bs_hostname"]}, 300)
            if not ok:
                return await save("error", ("не удалось поднять TLS rest-server: " + (out or "").replace("\r", " ").strip())[:250])
        ok, cert = await step("get-cert", bs_id, "get_cert", {})
        if not ok or not (cert or "").strip():
            return await save("error", "не удалось получить сертификат сервера бэкапов")
        ok, out = await step("allow-tls", bs_id, "allow_tls", {"client_ip": params["client_ip"]})
        if not ok:
            return await save("error", ("не удалось открыть TLS-порт клиенту: " + (out or "").replace("\r", " ").strip())[:250])
        ok, out = await step("to-https", params["client_id"], "to_https",
                             {"port": params["tls_port"], "cacert_b64": cert.strip()}, 280)
        if not ok:
            return await save("error", ((out or "").replace("\r", " ").strip() or "клиент не перешел на https, бэкап остался на http")[:250])
        await save("done", "бэкап идет по https")
    except BaseException as e:  # noqa: BLE001
        import traceback
        print("to_https orchestration FAILED:", repr(e), traceback.format_exc(), flush=True)
        try:
            await save("error", f"сбой оркестрации: {type(e).__name__}: {e}"[:250])
        except BaseException:  # noqa: BLE001
            pass


async def _to_https_job(session, user, client: Server, bs: Server | None) -> tuple[BackupSetupJob, dict]:
    """Проверки и задача перевода одного клиента; 400 с причиной, если сейчас нельзя."""
    why = backup_target.https_blockers(client, bs)
    if why:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{client.name}: " + "; ".join(why))
    client_ip = _pick_ip(client)
    if not client_ip:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{client.name}: не удалось определить IP клиента")
    t = backup_target.view(client.last_report or {}) or {}
    bs_rep = (bs.last_report or {}).get("backup_server") or {}
    host = t.get("host", "")
    job = BackupSetupJob(server_id=client.id, backup_server_id=bs.id, status="running", steps=[])
    session.add(job)
    await session.commit()
    await audit.record(session, user.username, "backup_to_https", f"{client.name}->{bs.name}", f"srv={client.id}")
    params = {
        "client_id": client.id, "client_ip": client_ip, "bs_id": bs.id, "bs_hostname": bs.hostname or "",
        "tls_front": bool(bs_rep.get("tls_front")), "tls_port": int(bs_rep.get("tls_port") or 64101),
        # сертификат нового TLS rest-server - на тот адрес, по которому клиент уже ходит
        "san_ip": host if backup_target.is_ip(host) else _pick_ip(bs), "_user": user.username,
    }
    return job, params


@router.post("/{server_id}/backup/to-https", response_model=BackupSetupJobOut)
async def backup_to_https(
    server_id: int, request: Request, user: CurrentUser, session: SessionDep,
) -> BackupSetupJobOut:
    """Перевести бэкап клиента с http на https того же сервера бэкапов. Прогресс - GET
    /backup/setup/{job_id}."""
    client = await _get_or_404(server_id, session, user)
    bs = backup_target.find_backup_server(client, list(await session.scalars(select(Server))))
    job, params = await _to_https_job(session, user, client, bs)
    task = asyncio.create_task(_orchestrate_to_https(request.app.state.session_factory, job.id, params))
    _setup_tasks.add(task)
    task.add_done_callback(_setup_tasks.discard)
    return BackupSetupJobOut.model_validate(job)


@router.post("/{server_id}/backup-server/to-https", response_model=list[BackupToHttpsJobOut])
async def backup_server_to_https(
    server_id: int, body: BackupToHttpsIn, request: Request, user: CurrentUser, session: SessionDep,
) -> list[BackupToHttpsJobOut]:
    """Перевести на https выбранных клиентов сервера бэкапов, по очереди: следующий начинается,
    когда предыдущий закончился (удачно или нет). Задачи создаются сразу, фронт поллит каждую.
    Клиент, которого сейчас не перевести, отказывает всю пачку с причиной - до начала."""
    bs = await _get_or_404(server_id, session, user)
    every = list(await session.scalars(select(Server)))
    by_id = {s.id: s for s in every}
    todo = []
    for cid in dict.fromkeys(body.client_ids):
        client = by_id.get(cid)
        if client is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"сервер {cid} не найден")
        await _get_or_404(cid, session, user)  # нарезка по группам: чужой клиент - 404
        if backup_target.find_backup_server(client, every) is not bs:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{client.name}: бэкап идет не на {bs.name}")
        why = backup_target.https_blockers(client, bs)
        if why:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{client.name}: " + "; ".join(why))
        todo.append(client)
    runs = [await _to_https_job(session, user, c, bs) for c in todo]
    factory = request.app.state.session_factory

    async def chain():
        tls_front = runs[0][1]["tls_front"]
        for job, params in runs:
            # TLS rest-server поднимает только первый в очереди
            params["tls_front"] = tls_front
            await _orchestrate_to_https(factory, job.id, params)
            tls_front = True

    task = asyncio.create_task(chain())
    _setup_tasks.add(task)
    task.add_done_callback(_setup_tasks.discard)
    return [BackupToHttpsJobOut.model_validate(j) for j, _ in runs]


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


_PUSH_OK = frozenset({"", "up", "ok", "0", "success"})
_PUSH_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,64}")


@agent_router.api_route("/push/{token}", methods=["GET", "POST", "HEAD"])
async def push_signal(token: str, session: SessionDep, status_: str = Query("", alias="status"),
                      msg: str = Query("", max_length=500)) -> dict:
    """Сигнал push-монитора: скрипт или крон стучится сюда после прогона. Без логина: адрес
    и есть секрет. ?status=down (или ненулевой код выхода, ?status=$?) - прогон не удался,
    ?msg=... - что сказать в алерте. Путь под /api/agent, его белый список caddy пропускает."""
    if not _PUSH_TOKEN.fullmatch(token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown push token")
    check = await session.scalar(select(Check).where(Check.push_token == token, Check.type == "push"))
    if check is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown push token")
    st = status_.strip().lower()
    check.push_at = datetime.now(timezone.utc)
    check.push_fail = "" if st in _PUSH_OK else ((msg.strip() or f"status={st}")[:255])
    await session.commit()
    return {"ok": True}


@agent_router.get("/install.sh")
async def install_script(x_forwarded_for: str = Header(default=""), x_real_ip: str = Header(default="")) -> PlainTextResponse:
    install_watch.note(_client_ip(x_forwarded_for, x_real_ip), "script")
    path = os.path.join(get_settings().agent_dist_dir, "install.sh")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "install.sh не найден")
    with open(path, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/x-shellscript")


@agent_router.get("/ca.crt")
async def panel_ca() -> PlainTextResponse:
    """Корень CA панели (data/panel-ca.crt), если сертификат у нее не публичный: импортировать
    в браузер или в хранилище ОС на машине без команды установки. Сертификат открытый, путь
    под /api/agent, его белый список caddy пропускает."""
    pem = panel_ca_pem()
    if not pem:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "panel CA is not set (data/panel-ca.crt)")
    return PlainTextResponse(pem, media_type="application/x-pem-file",
                             headers={"Content-Disposition": 'attachment; filename="kervax-panel-ca.crt"'})


@agent_router.get("/install-ru.sh")
async def install_script_ru(x_forwarded_for: str = Header(default=""), x_real_ip: str = Header(default="")) -> PlainTextResponse:
    """Тот же установщик с сообщениями на русском.

    Основной install.sh англоязычный — его видят все, кто ставит панель. Русская
    копия нужна там, где команду выполняет человек, которому так понятнее, и
    отличается она ровно языком вывода."""
    install_watch.note(_client_ip(x_forwarded_for, x_real_ip), "script")
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
async def download_agent(arch: str, x_forwarded_for: str = Header(default=""),
                         x_real_ip: str = Header(default="")) -> FileResponse:
    if arch not in ("amd64", "arm64"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "нет такой архитектуры")
    install_watch.note(_client_ip(x_forwarded_for, x_real_ip), "agent")
    path = os.path.join(get_settings().agent_dist_dir, f"kervax-agent-{arch}")
    if not os.path.exists(path):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "бинарь агента не собран")
    return FileResponse(
        path, media_type="application/octet-stream", filename="kervax-agent"
    )


_gz_cache: dict[str, tuple[tuple, bool]] = {}


def agent_gz_ok(arch: str) -> bool:
    """Сжатый бинарь распаковывается ровно в тот, что подписан в манифесте. Иначе его не
    отдаем: агент получит 404 и возьмет несжатый, а не будет раз за разом качать архив,
    который не пройдет проверку. Результат кэшируется до смены файлов."""
    dist = get_settings().agent_dist_dir
    names = ["manifest.json", f"kervax-agent-{arch}.gz"]
    fp = _release_fingerprint(dist, names)
    hit = _gz_cache.get(arch)
    if hit is not None and hit[0] == fp:
        return hit[1]
    ok = False
    try:
        with open(os.path.join(dist, "manifest.json"), encoding="utf-8") as f:
            art = (json.load(f).get("artifacts") or {}).get(arch) or {}
        size = int(art.get("size") or 0)
        digest = hashlib.sha256()
        got = 0
        with gzip.open(os.path.join(dist, f"kervax-agent-{arch}.gz"), "rb") as fz:
            while chunk := fz.read(1 << 20):
                got += len(chunk)
                if got > size:
                    break
                digest.update(chunk)
        ok = size > 0 and got == size and digest.hexdigest() == art.get("sha256")
    except (OSError, ValueError, EOFError, gzip.BadGzipFile):
        ok = False
    _gz_cache[arch] = (fp, ok)
    return ok


@agent_router.api_route("/download-gz/{arch}", methods=["GET", "HEAD"])
async def download_agent_gz(arch: str, x_forwarded_for: str = Header(default=""),
                            x_real_ip: str = Header(default="")) -> FileResponse:
    """Тот же бинарь, сжатый gzip (в 2.4 раза меньше). Агент 2.18+ берет его первым и сам
    распаковывает; подлинность проверяет по подписанному манифесту, как у несжатого. На
    канале за DPI (ru-be, ~0.2 МБ/мин) это 15 минут вместо 40. HEAD нужен агенту, чтобы
    узнать размер архива для докачки по кускам."""
    if arch not in ("amd64", "arm64"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "нет такой архитектуры")
    path = os.path.join(get_settings().agent_dist_dir, f"kervax-agent-{arch}.gz")
    if not os.path.exists(path) or not agent_gz_ok(arch):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "сжатого бинаря нет")
    install_watch.note(_client_ip(x_forwarded_for, x_real_ip), "agent")
    return FileResponse(path, media_type="application/octet-stream")


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
    request: Request,
    background: BackgroundTasks,
    authorization: str = Header(default=""),
    x_forwarded_for: str = Header(default=""),
    x_real_ip: str = Header(default=""),
) -> AgentConfigOut:
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Нет токена агента")
    # Строку сервера держим до конца приема: отчет переписывает last_report целиком, и два
    # одновременных запроса (повтор отчета после таймаута, going-down посреди отчета) раньше
    # затирали друг друга - пропадала отметка штатной перезагрузки (ложный "недоступен"), а
    # OOM считался дважды от одного и того же прошлого отчета
    server = await session.scalar(
        select(Server).where(Server.token_hash == hash_agent_token(token)).with_for_update()
    )
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен агента")

    now = datetime.now(timezone.utc)
    mem_pct = (
        round(body.mem_used / body.mem_total * 100, 1) if body.mem_total else None
    )
    disks_pct = [
        {"mount": d["mount"], "pct": round(d["used"] / d["total"] * 100, 1)}
        # inode с агента 2.15: по их истории прогноз видит, когда они кончатся
        | ({"ipct": round((d.get("inodes_used") or 0) / d["inodes"] * 100, 1)} if d.get("inodes") else {})
        for d in body.disks
        if d.get("total") and d.get("mount")
    ]
    disk_pct = max((d["pct"] for d in disks_pct), default=None)
    tot = body.mem_total or 0
    oom_delta, oom_pend = oom_mod.counter_step(server.last_report or {}, body)
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
        web_top, web_codes = web_breakdown(body.extras, now, body.clock_unix)
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
                oom_kill=float(oom_pend) if body.oom_kill is not None or body.oom_total is not None else None,
                mem_cache=round(body.mem_cached / tot * 100, 1) if tot else None,
                mem_free=round(body.mem_free / tot * 100, 1) if tot else None,
                swap_in=round(body.swap_in),
                swap_out=round(body.swap_out),
                mem_slab=round(body.mem_slab),
                mem_dirty=round(body.mem_dirty),
                mem_writeback=round(body.mem_writeback),
                disks=disks_pct or None,
                net_ifaces=[{k: v for k, v in i.items() if k not in _IFACE_TOTALS}
                            for i in body.net_ifaces if isinstance(i, dict)] or None,
                disk_devs=body.disk_devs or None,
                conntrack_count=round(body.conntrack_count),
                conntrack_max=round(body.conntrack_max),
                sock_used=round(body.sock_used),
                sock_tcp=round(body.sock_tcp),
                sock_tcp_tw=round(body.sock_tcp_tw),
                sock_udp=round(body.sock_udp),
                web_rpm=web_rate_total(body.extras, now, body.clock_unix),
                web_5xx=web_5xx_total(body.extras, now, body.clock_unix),
                web_top=web_top,
                web_codes=web_codes,
                gpu=gpu_mod.metric_gpu(body.extras, now, body.clock_unix),
                llm=gpu_mod.metric_llm(body.llm),
                ts=now,
            )
        )
    if body.hostname:
        server.hostname = body.hostname[:255]
    elif server.hostname and not safe_hostname(server.hostname):
        server.hostname = ""  # записано до проверки имени на приеме
    if body.os:
        server.os = body.os[:128]
    if body.local_ip:
        server.local_ip = body.local_ip[:64]
    ext = _client_ip(x_forwarded_for, x_real_ip)
    if body.proxy:
        # Агент ходит через прокси: панель видит адрес прокси, а не ноды. По нему считались бы
        # страна и проверки снаружи, и у всех нод за одним прокси он был бы один. Адрес ноды
        # тогда только тот, что задан руками ("IP сервера"); не задан - прежний не трогаем.
        ext = (server.agent_ip or "").strip()
    # через GRE до панели (app/gre.py) запрос приходит с адреса ноды в туннеле: в карточку идет
    # ее настоящий публичный адрес, а не 10.254.x.x
    ext = gre_mod.real_ip(server, ext, now)
    if ext:
        server.external_ip = ext
    server.agent_version = body.agent_version[:32]
    # детект перезагрузки: аптайм упал (с запасом 60с на дрожание) → отметка времени,
    # по которой collector пошлёт разовый алерт «сервер перезагружен». Ждали ли ее (обновления
    # просили перезагрузку) - видно только по прошлому отчету: причина живет в отчете дальше
    prev_rep = server.last_report or {}
    prev_up = prev_rep.get("uptime_seconds") or 0
    boot = prev_rep.get("boot") if isinstance(prev_rep.get("boot"), dict) else None
    rebooted = bool(prev_up and body.uptime_seconds and body.uptime_seconds < prev_up - 60)
    if rebooted:
        server.rebooted_at = now
        # агент предупредил о штатной перезагрузке (app/going_down.py) - она плановая: без алерта,
        # в ленте со временем без связи
        boot = {"at": now.isoformat(),
                "why": going_down.boot_why(prev_rep, os_support.reboot_why(prev_rep), server.last_seen, now)}
    # OOM-киллы копим КУМУЛЯТИВНО тут (в ingest'е видим каждый отчёт), а не в
    # collector'е по last_report — дельта живёт лишь один тик и терялась при поллинге.
    # Сколько их, считает oom_mod.counter_step по счетчику ядра (агент 2.42)
    if oom_delta > 0:
        server.oom_total = (server.oom_total or 0) + oom_delta
        victim = (getattr(body, "oom_victim", "") or "")[:200]
        if victim:
            server.oom_victim = victim
        # журнал OOM-событий: одна запись на отчёт с киллом (когда + кого)
        session.add(OomEvent(server_id=server.id, ts=now, victim=victim, count=oom_delta))
    await _store_web_errors(session, server, body, now)
    await autoban_mod.store(session, server, body.extras, now)
    report_dict = body.model_dump()
    # адрес репозитория бэкапа - только без логина и пароля, что бы ни прислал helper
    backup_target.clean_report(report_dict)
    # старый runner (restic в ExecStart юнита): пути бэкапа из самого юнита, а не из скрипта панели
    backup_unit.apply(report_dict)
    # отчет, собранный до перевода на https, не возвращает старый http-адрес
    backup_target.carry(prev_rep, report_dict, now.timestamp())
    # убийства, которые еще не попали в точку графика (точка пишется раз в минуту)
    report_dict["oom_pend"] = 0 if write_metric else oom_pend
    # имена контейнеров по id переносятся из отчета в отчет: OOM называет контейнер по cgroup с id,
    # а после OOM его часто пересоздают с новым (app/oom.py)
    report_dict[oom_mod.CT_SEEN_KEY] = oom_mod.remember_containers(prev_rep.get(oom_mod.CT_SEEN_KEY), report_dict,
                                                                   now.timestamp())
    await oom_mod.store(session, server, body.extras, now, report_dict)
    if boot:
        report_dict["boot"] = boot
    if gd := going_down.carry(prev_rep, now, rebooted):
        report_dict[going_down.KEY] = gd
    # Вернулся после долгого молчания (ВМ выключали, пропадала сеть): с этого момента прогрев,
    # его сайты и сервисы еще поднимаются (app/boot_warmup.py). Перезагрузку видно и по аптайму
    if boot_warmup.came_back(server.last_seen, now, max(server.offline_after_seconds or 120, 30)
                             + _OFFLINE_ALERT_EXTRA, panel_started_at()):
        report_dict[boot_warmup.BACK_KEY] = now.isoformat()
    elif back := boot_warmup.keep_back(prev_rep.get(boot_warmup.BACK_KEY), now):
        report_dict[boot_warmup.BACK_KEY] = back
    # журнал установок - разовый, в хронологию; в last_report ему жить незачем
    report_dict.pop("pkg_ev", None)
    # связи живут в своих таблицах (app/netlinks.py)
    report_dict.pop("links", None)
    # сдвиг часов: локальное время ноды (на момент отправки) минус время панели на приёме.
    # Работает даже если у ноды нет доступа к NTP — агент до панели по HTTPS достучался.
    # В разницу входит дорога отчета, при медленном DNS это секунды, поэтому берем минимум
    # за 5 минут (clock_skew_window), окно живет в отчете.
    if body.clock_unix:
        cw = clock_skew_window((server.last_report or {}).get("clock_win"),
                               int(now.timestamp()) - body.clock_unix, now.timestamp())
        report_dict["clock_win"] = cw
        report_dict["clock_skew_sec"] = cw["win"][0][1]
    server.last_report = report_dict
    server.last_seen = now
    if body.pkg_ev:
        await packages_mod.store_events(session, server, body.pkg_ev, now)
    # отпечаток списка пакетов не совпал с тем, что у панели есть - пусть пришлет список
    want_pkgs = bool(isinstance(body.pkgs, dict) and body.pkgs.get("h")
                     and str(body.pkgs["h"])[:32] != (server.pkg_hash or ""))
    if server.first_report_at is None:
        server.first_report_at = now
    await traffic_mod.ingest(session, server, body.net_ifaces, now)
    await usage_mod.ingest(session, server, body.net_users, body.disk_writers, now)
    link_tasks = await netlinks.handle(session, server, body.links, now)

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
    pending = await _store_site_probes(session, server.id, body.site_probes or [], now)
    # Ручные проверки — первыми: агент проходит задания по порядку, и «Проверить
    # сейчас» не должно ждать, пока он переберёт медленные плановые сайты.
    manual = await manual_probe.tasks_for_report(session, server, now)
    probes = manual + await _site_probe_tasks(session, server.id)
    await session.commit()
    background.add_task(send_alerts_soon, request.app.state.session_factory, pending, now)

    return AgentConfigOut(
        # пока ждём ответ на ручную проверку, агент отчитывается чаще — иначе его
        # ответ уехал бы только через обычные 15 секунд
        interval=(
            manual_probe.PENDING_REPORT_INTERVAL if manual
            else get_settings().server_report_interval
        ),
        checks=[], update=upd,
        docker_commands=cmds, kube_commands=kcmds, backup_commands=bcmds,
        site_probes=probes, want_pkgs=want_pkgs, links=link_tasks,
    )


@agent_router.post("/packages")
async def agent_packages(
    body: AgentPackagesIn, session: SessionDep, authorization: str = Header(default=""),
) -> dict:
    """Кусок списка пакетов (app/packages.py): агент шлет их по одному, каждым своим соединением."""
    token = authorization.removeprefix("Bearer ").strip()
    server = await session.scalar(select(Server).where(Server.token_hash == hash_agent_token(token))) if token else None
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен агента")
    if body.part >= body.parts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "номер куска больше их числа")
    done = await packages_mod.store_part(session, server, body.h, body.mgr, body.part, body.parts, body.items,
                                         datetime.now(timezone.utc))
    await session.commit()
    return {"ok": True, "done": done}


async def _store_site_probes(session, server_id: int, results: list, now) -> list:
    """Кладёт присланные агентом результаты локальных проверок.

    Только для мониторов, которым ЭТОТ сервер назначен проверяющим: иначе агент
    (или тот, кто добыл его токен) мог бы объявить любой чужой монитор упавшим.
    Отрицательные номера — ответы на ручную проверку (см. manual_probe); вернёт
    очередь алертов, которые они вызвали."""
    if not results:
        return []

    def _id(r) -> int:
        try:
            return int(r.get("id") or 0)
        except (TypeError, ValueError):
            return 0

    pending = await manual_probe.ingest(
        session, server_id, [r for r in results if _id(r) < 0], now
    )
    await _store_path_probes(
        session, server_id, [r for r in results if _id(r) >= manual_probe.PATH_TASK_BASE], now
    )
    ids = [i for r in results if 0 < (i := _id(r)) < manual_probe.PATH_TASK_BASE]
    if not ids:
        return pending
    mine = set(
        await session.scalars(
            select(Check.id).where(
                Check.id.in_(ids),
                Check.probe_server_id == server_id,
                Check.probe_local.is_(True),
            )
        )
    )
    for r in results:
        cid = _id(r)
        if cid not in mine:
            continue
        row = await session.get(AgentProbe, cid)
        if row is None:
            row = AgentProbe(check_id=cid, server_id=server_id, ts=now)
            session.add(row)
        elif row.manual_until is not None and now < _aware_dt(row.manual_until):
            # Свежая ручная проверка главнее: плановый ответ агент повторяет из
            # памяти, пока не проверит сайт заново, и он может быть старше нажатия.
            # Отметку времени при этом двигаем: агент на связи и про сайт отчитался,
            # а застывшая ts через пару минут читалась бы как «агент молчит».
            row.ts = now
            continue
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
        row.via = str(r.get("via") or "")[:128]
    return pending


async def _store_path_probes(session, server_id: int, results: list, now) -> None:
    """Planned agent results for the additional paths of local monitors.

    The task number carries the monitor and a hash of the path (manual_probe.path_task_ids).
    A number that no longer matches a path of the monitor is an answer about a renamed or
    removed path, and it is dropped. As with the main address, only the node that checks the
    monitor may report on it."""
    if not results:
        return
    by_check: dict[int, list] = {}
    for r in results:
        try:
            tid = int(r.get("id") or 0)
        except (TypeError, ValueError):
            continue
        by_check.setdefault(manual_probe.path_task_check_id(tid), []).append((tid, r))
    checks = await session.scalars(
        select(Check).where(
            Check.id.in_(list(by_check)),
            Check.probe_server_id == server_id,
            Check.probe_local.is_(True),
        )
    )
    for check in checks:
        paths = {tid: path for path, tid in manual_probe.path_task_ids(check).items()}
        for tid, r in by_check.get(check.id, []):
            path = paths.get(tid)
            if path is None:
                continue
            row = await session.get(AgentPathProbe, (check.id, path))
            if row is None:
                row = AgentPathProbe(check_id=check.id, path=path, server_id=server_id, ts=now)
                session.add(row)
            elif row.manual_until is not None and now < _aware_dt(row.manual_until):
                row.ts = now  # a fresh manual answer wins, see the same case in _store_site_probes
                continue
            row.server_id = server_id
            row.ts = now
            row.code = int(r.get("code") or 0)
            lat = r.get("latency_ms")
            row.latency_ms = int(lat) if isinstance(lat, (int, float)) else None
            row.error = str(r.get("error") or "")[:512]
            row.via = str(r.get("via") or "")[:128]


def _aware_dt(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _site_probe_tasks(session, server_id: int) -> list[dict]:
    """Плановые задания агенту: что проверять изнутри сервера (см. site_probe_task).

    Только мониторы с галочкой «локально»: снятая галочка оставляла привязку, и агент
    ещё долго проверял сайт, который панель давно проверяет сама."""
    rows = list(
        await session.scalars(
            select(Check).where(
                Check.enabled.is_(True),
                Check.probe_local.is_(True),
                Check.probe_server_id == server_id,
                Check.type == "http",
            )
        )
    )
    return [task for c in rows for task in manual_probe.planned_tasks(c)]


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
    # ручная проверка сайта — агенту 2.7+, он ответит через /agent/site-probe-result
    sprobes = await manual_probe.take_fast_tasks(session, server, datetime.now(timezone.utc))
    await session.commit()
    return {
        "docker_commands": cmds, "kube_commands": kcmds, "backup_commands": bcmds,
        "site_probes": sprobes,
    }


@agent_router.post("/site-probe-result")
async def agent_site_probe_result(
    body: AgentSiteProbesIn,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
    authorization: str = Header(default=""),
) -> dict:
    """Агент 2.7+ присылает ответ на ручную проверку сразу, не дожидаясь отчёта."""
    token = authorization.removeprefix("Bearer ").strip()
    server = await session.scalar(
        select(Server).where(Server.token_hash == hash_agent_token(token))
    )
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен агента")
    now = datetime.now(timezone.utc)
    pending = await _store_site_probes(session, server.id, body.site_probes, now)
    await session.commit()
    background.add_task(send_alerts_soon, request.app.state.session_factory, pending, now)
    return {"ok": True}


@agent_router.post("/going-down")
async def agent_going_down(
    body: AgentGoingDownIn,
    session: SessionDep,
    authorization: str = Header(default=""),
) -> dict:
    """Агент 2.47+ уходит штатно (SIGTERM от systemd): перезагрузка, выключение или остановка
    одного агента. Пока сервер грузится, "недоступен" не поднимается (app/going_down.py).
    Строка под замком, как в agent_report: отметка не теряется под отчетом, идущим в это время."""
    token = authorization.removeprefix("Bearer ").strip()
    server = await session.scalar(
        select(Server).where(Server.token_hash == hash_agent_token(token)).with_for_update()
    )
    if server is None or not server.enabled:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен агента")
    rep = dict(server.last_report or {})
    rep[going_down.KEY] = going_down.make(body.kind, datetime.now(timezone.utc))
    server.last_report = rep
    await session.commit()
    return {"ok": True}


@agent_router.post("/docker-result")
async def agent_docker_result(
    body: DockerResultIn,
    session: SessionDep,
    request: Request,
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
    if not body.ok:
        panel_errors.command_failed(request.app.state.session_factory, "docker", c, server_name=server.name or "")
    return {"ok": True}


@agent_router.post("/kube-result")
async def agent_kube_result(
    body: KubeResultIn,
    session: SessionDep,
    request: Request,
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
    if not body.ok:
        panel_errors.command_failed(request.app.state.session_factory, "kube", c, server_name=server.name or "")
    return {"ok": True}


@agent_router.post("/backup-result")
async def agent_backup_result(
    body: BackupResultIn,
    session: SessionDep,
    request: Request,
    background: BackgroundTasks,
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
    # отказ дампа без причины (helper до 0.38) - панель дописывает, что известно; payload с
    # движком еще не стерт
    out = dump_reason.fill_dump_reason(c.action, body.ok, body.output or "", c.payload,
                                       (server.last_report or {}).get("setup_versions"))
    c.result = out[-100_000:]
    c.status = "done" if body.ok else "error"
    c.done_at = datetime.now(timezone.utc)
    if c.action == "deprovision" and body.ok:
        server.backup_setup_err = None  # настройку сняли - упавшей больше нет
    if c.action == "to_https" and body.ok:
        # бэкап уже на https: адрес в отчете сразу, не дожидаясь следующего отчета агента
        # (app/backup_target.py, switched)
        rep = backup_target.switched(server.last_report or {}, (c.payload or {}).get("port"),
                                     c.done_at.timestamp())
        if rep is not None:
            server.last_report = rep
    if not body.ok:
        # параметры команды - в журнал до того, как payload сотрется (маска секретов внутри)
        panel_errors.command_failed(request.app.state.session_factory, "backup", c, server_name=server.name or "")
    if c.action != "disk_fix":  # у disk_fix секретов нет, а название действия нужно уведомлению
        c.payload = None  # секреты (repopass/hpass) в БД не задерживаем - стираем сразу
    await session.commit()
    if c.action == "disk_fix" and c.origin == "auto" and (c.payload or {}).get("name") != "analyze":
        # что сделала авто-очистка - в каналы алертов, иначе удаление от root прошло бы тихо
        # (разбор раздела по прогнозу ничего не удаляет - о нем писать нечего)
        background.add_task(send_autofix_note, request.app.state.session_factory, server.id, c.id)
    return {"ok": True}
