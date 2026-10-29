"""Сервер разработки.

Повод: на сервере разработки под приложения перезапускался 60 раз за сутки ("нет связи с базой"),
и панель будила алертом "под перезапускается". Там такое поведение приложений нормально: базу
перезапускают, код ломается и чинится, воркеры падают на недописанном. Будить надо только про
саму машину.

Пометка "разработка" ставится человеком в настройках сервера (Server.is_dev). Имя или группу с
-dev, -stage, -test панель только подсказывает (looks_dev), сама не ставит.

На сервере разработки:
- виды уровня приложения (APP) в Telegram не уходят: ни тревога, ни отбой. Отметки состояния
  пишутся, как будто сообщение дошло (как у подключения, app/onboard.py): после снятия пометки
  старое не придет, придет новое;
- в панели они видны как раньше, но серым с пометкой "разработка" (ServerOut.problems[].dev), и
  на главной в "Что сломано" и счетчики не идут;
- виды инфраструктуры (INFRA) - как у всех: недоступность, место, память и OOM, диски, бэкапы,
  безопасность, сертификаты, сроки кластера.

Каждый вид серверных алертов (settings_store.SERVER_ALERT_KINDS) и "Что сломано"
(mute_watch.PROBLEM_KINDS) отнесен ровно к одной группе: тест сверяет."""

import re

# Поведение приложений: на разработке это нормально
APP = frozenset({
    # контейнеры и поды приложений
    "docker_down", "docker_loop", "docker_restarts",
    "kube_pod", "kube_job", "kube_cron", "kube_restarts",
    # службы приложений
    "units", "unit_loop", "workers", "queue", "queue_stuck", "queue_failed", "cron_stuck",
    "log_errors", "fpm", "llm", "listen_gone",
    # базы на уровне приложения: коннекты, долгие транзакции, реплики приложения
    "db_conn", "db_health",
    # сайты на нем: 5xx и конфиг веб-сервера, который правят
    "web_5xx", "web_config",
    # процессы приложений: крутятся в пустом цикле, растут, упираются в свой лимит
    "cpu_spin", "mem_leak", "mem_limit",
})

# Сама машина и безопасность: будить как на проде
INFRA = frozenset({
    "offline", "reboot", "clock", "dns", "pressure",
    # ресурсы ноды
    "cpu", "mem", "oom", "temp", "throttle", "conntrack",
    # диски
    "disk", "inode", "disk_forecast", "disk_health", "disk_slow", "disktemp",
    # сеть
    "net_health", "link", "traffic",
    # клиенты VPN: торрент и много трафика (тариф и жалобы правообладателей идут на сервер)
    "vpn_torrent", "vpn_heavy",
    # безопасность
    "access_change", "access_risk", "ssh_risk", "exposed", "cf_realip", "port_new", "docker_sock",
    "docker_risk", "webfiles", "autoban", "bogus_requests",
    # мониторинг самой ноды: что на ней не покрыто проверками
    "uncovered",
    # сертификаты, сроки и версии
    "web_cert", "gitlab", "kube_expiry", "flux_down", "flux_stale",
    # кластер как инфраструктура
    "kube_node",
    # железо
    "gpu", "gpu_heat",
    # бэкапы
    "backup_missing", "backup_failed", "backup_stale", "backup_growth", "backup_restore", "backup_dump",
    "backup_dump_space", "backup_cron", "backup_custom", "backup_repo", "backup_unmonitored",
    "backup_prune", "backup_check", "backup_mirror", "backup_lock", "backup_rotation",
})

_DEV_NAME = re.compile(r"(?:^|[-_.])(?:dev|develop|stage|staging|test|testing)(?:$|[-_.\d])", re.I)


def active(s) -> bool:
    return bool(getattr(s, "is_dev", False))


def quiet(kind: str) -> bool:
    """Вид не будит на сервере разработки."""
    return kind in APP


def looks_dev(name: str) -> bool:
    """Имя похоже на сервер разработки: подсказка в настройках, панель сама не ставит."""
    return bool(_DEV_NAME.search(name or ""))


def mark(problems: list[dict]) -> list[dict]:
    """Строки "Что сломано" сервера разработки: виды приложений с пометкой dev (серым, не на главной)."""
    for p in problems:
        if quiet(str(p.get("kind") or "")):
            p["dev"] = True
    return problems


def split(msgs: list, states: list, now_apply: list) -> tuple[list, list]:
    """Сообщения и отметки одного сервера разработки: что отправить и что записать сразу.
    Виды приложений не отправляются, их отметки пишутся сразу, как будто сообщение дошло. Если
    что-то уходит, отметки ждут доставки вместе с ним: не дошло - на следующем тике все повторится,
    придержанное снова придержится."""
    keep = [m for m in msgs if not quiet(getattr(m, "kind", ""))]
    if keep:
        return keep, states
    now_apply.extend(states)
    return [], []
