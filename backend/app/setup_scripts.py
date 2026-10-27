"""Каталог раздаваемых helper'ов и разбор того, чего ноде не хватает.

Жило в api/servers.py, пока это нужно было только экрану серверов. Теперь тем же
знанием пользуется планировщик (суточная сводка «появилось новое, покрытия нет»),
а тащить в него FastAPI-модуль ради двух функций — плохая идея: отдельный модуль
без зависимостей от веб-слоя.
"""

from __future__ import annotations

import os
import re
import time

from app import gpu as gpu_mod
from app.config import get_settings


def setup_scripts() -> tuple[str, ...]:
    """Какие helper'ы панель раздаёт — читаем каталог, а НЕ держим список в коде.

    Захардкоженный кортеж означал, что новый helper, забытый в нём, панель молча
    перестаёт отслеживать: раздаётся, ставится установщиком, а «устарел» по нему не
    показывается никогда. Молчание тут хуже ложного срабатывания — источником правды
    остаются сами файлы (как и у install.sh, который давно читает этот же каталог)."""
    try:
        return tuple(sorted(
            f[:-3] for f in os.listdir(get_settings().agent_dist_dir)
            # установщик и его русская копия — не helper'ы: попади они в каталог,
            # агент попытался бы выполнить их как setup-скрипт
            if f.endswith(".sh") and f not in ("install.sh", "install-ru.sh")
        ))
    except OSError:
        return ()


def current_setup_versions() -> dict[str, str]:
    """Текущие версии setup-скриптов — читаем маркер KERVAX_SETUP_VERSION из раздаваемых
    файлов. Единственный источник правды: бампнул версию в скрипте → панель это увидела."""
    out: dict[str, str] = {}
    d = get_settings().agent_dist_dir
    for name in setup_scripts():
        try:
            with open(os.path.join(d, f"{name}.sh"), encoding="utf-8") as f:
                m = re.search(r"^KERVAX_SETUP_VERSION=([0-9.]+)", f.read(), re.MULTILINE)
                if m:
                    out[name] = m.group(1)
        except OSError:
            pass
    return out


def setup_needed(name: str, rep: dict, mutes: list[str] | None = None, relay: bool = False) -> bool:
    """Нужен ли helper этой ноде; mutes - решения человека по ноде (alert_mutes сервера),
    relay - на сервере включен ретранслятор для агентов (app/relay.py).

    Незнакомое имя (helper добавили, а условие сюда не дописали) считаем нужным
    всюду: лучше лишний пункт в «Требует действий», который сразу видно и легко
    уточнить, чем невыкаченный helper, о котором панель молчит месяцами."""
    # relay-setup - только где в настройках сервера включен ретранслятор: helper держит прокси
    # и открывает порт, это решение человека, а не свойство ноды
    if name == "relay-setup":
        return relay
    if name == "backupserver-setup":
        bsrv = rep.get("backup_server") or {}
        return bool(bsrv.get("present") and bsrv.get("repos"))
    if name == "kube-setup":
        return bool((rep.get("kube") or {}).get("access"))
    # kubeexpiry-setup — везде, где кластер ЕСТЬ, а не только где панель в него пущена:
    # хелпер читает PKI и ходит в кластер локальным admin-kubectl ноды, панельный
    # ServiceAccount ему не нужен (и секретов ему не дают принципиально)
    if name == "kubeexpiry-setup":
        return bool((rep.get("kube") or {}).get("present"))
    # webserver-setup - где есть веб-сервер (иначе доменов все равно нет) или где helper уже
    # считает запросы: на host-a агент веб-сервер не распознал, а блок запросов от
    # helper 0.7 шел, и устаревший helper там никто не флагал
    if name == "webserver-setup":
        return bool(rep.get("web_services")) or bool((rep.get("extras") or {}).get("web-rate"))
    # autoban-setup - где webserver-setup нашел access-логи (банить можно только по ним) или helper
    # уже присылает отчет. Ставится ansible'ом в режиме watch: сам ничего не банит, пока не включат
    if name == "autoban-setup":
        ex = rep.get("extras") or {}
        logs = (ex.get("web-rate") or {}).get("logs") if isinstance(ex.get("web-rate"), dict) else None
        return bool(logs) or "autoban" in ex
    # webfiles-setup - где сайты на самом сервере: nginx или Apache (в том числе в контейнерах),
    # пулы PHP-FPM от fpm-setup или helper уже присылает файлы. Только прокси (Traefik, Envoy,
    # ingress) - не повод: кода сайтов на такой ноде нет. Блок fpm сам по себе тоже не повод:
    # fpm-setup стоит всюду и без PHP шлет пустой {"pools":[]} - по нему панель флагала helper
    # на серверах бэкапов, а условие в скрипте (pgrep nginx/php) на ноде ложно, и плейбук молча
    # ничего не ставил
    if name == "webfiles-setup":
        kinds = {str((w or {}).get("kind") or "") for w in rep.get("web_services") or [] if isinstance(w, dict)}
        ex = rep.get("extras") or {}
        fpm = ex.get("fpm") if isinstance(ex.get("fpm"), dict) else {}
        return (bool(kinds & {"nginx", "Apache"}) or bool(fpm.get("pools") or fpm.get("skipped"))
                or bool(ex.get("webfiles")))
    # dbstat-setup — только там, где СУБД реально есть (скан процессов агента);
    # на ноде без баз инвентарь пустой, флагать нечего
    if name == "dbstat-setup":
        return bool(rep.get("db_engines") or rep.get("db_stats"))
    # gpu-setup - где на шине есть карта NVIDIA (агент 2.36) или helper уже стоит
    if name == "gpu-setup":
        return gpu_mod.has_gpu(rep)
    # queues-setup - где есть Redis, PostgreSQL или MySQL (агент видит их и в подах): очереди задач
    # живут и в Redis, и в таблицах баз (Laravel, Yii, Symfony...); или helper уже присылает очереди
    if name == "queues-setup":
        eng = " ".join(str(e) for e in rep.get("db_engines") or []).lower()
        return (any(x in eng for x in ("redis", "valkey", "postgres", "mysql", "mariadb"))
                or any((d or {}).get("engine") in ("redis", "pg", "mysql")
                       for d in rep.get("db_stats") or [] if isinstance(d, dict))
                or bool((rep.get("extras") or {}).get("queues")))
    # dockerproxy-setup - там, где агенту уже дан доступ к Docker через наш прокси: helper его
    # укрепляет. Включать доступ там, где его не давали, - решение человека, не helper'а
    if name == "dockerproxy-setup":
        cs = (rep.get("docker") or {}).get("containers") or []
        return any(str(c.get("name") or "").startswith("kervax-docker-proxy") for c in cs)
    # sockfix-setup - где внешний прокси держит docker-сокет (helper готовит compose без него), где
    # его образ старше года (helper говорит, где в compose образ, панель пишет команды обновления;
    # решение "оставить как есть" не в счет) или helper уже присылает отчет: после исправления он
    # остается и следит, чтобы сокет не вернулся
    if name == "sockfix-setup":
        from app import docker_exposure
        ref = float(rep.get("clock_unix") or 0) or time.time()
        return (bool(docker_exposure.exposed(rep)) or bool(docker_exposure.old_live(rep, ref, mutes))
                or "sockfix" in (rep.get("extras") or {}))
    # gitlab-setup - где агент видит контейнер GitLab (образ gitlab-ce/ee, в том числе собранный
    # поверх него, или сервис compose с таким именем) или helper уже присылает инстансы. Пустой
    # блок не повод: helper ставится по условию, но мог остаться на ноде, где GitLab убрали
    if name == "gitlab-setup":
        return (any(_gitlab_ct(c) for c in (rep.get("docker") or {}).get("containers") or [] if isinstance(c, dict))
                or bool(((rep.get("extras") or {}).get("gitlab") or {}).get("instances")))
    # backup-setup — транспорт панели для дампов, нужен и без файлового бэкапа;
    # timesync-setup, agent-watchdog, diskusage-setup, diskhealth-setup и units-setup
    # ansible ставит всюду (диск забивается и ломается где угодно, а на VM здоровье дисков смотрит хотя бы
    # RAID и ошибки ядра); остальное - см. докстроку
    return True


def _gitlab_ct(c: dict) -> bool:
    img = str(c.get("image") or "").lower()
    svc = str((c.get("compose") or {}).get("service") or "").lower()
    return "runner" not in img and ("gitlab-ce" in img or "gitlab-ee" in img or svc in ("gitlab", "gitlab-ce", "gitlab-ee"))


# Чего именно не хватает — словами, которые говорят, ЧТО делать. Ключ нужен, чтобы
# отличать «это уже было вчера» от «появилось сегодня»: сводка шлётся о новом.
_MISSING_LABEL = {
    "docker": "Docker без доступа (нужен read-only proxy)",
    "kube": "Kubernetes без доступа (нужен kube-setup)",
}
_HELPER_LABEL = {
    "backup-setup": "бэкап-транспорт",
    "backupserver-setup": "статистика бэкап-сервера",
    "kube-setup": "доступ в Kubernetes",
    "kubeexpiry-setup": "сроки Kubernetes и Flux",
    "webserver-setup": "домены веб-сервера",
    "timesync-setup": "синхронизация времени",
    "dbstat-setup": "инвентарь СУБД",
    "agent-watchdog": "вотчдог агента",
    "diskusage-setup": "разбор места на диске",
    "diskhealth-setup": "здоровье дисков",
    "units-setup": "упавшие юниты systemd",
    "dockerproxy-setup": "защита доступа к Docker",
    "changes-setup": "изменения на сервере",
    "gpu-setup": "GPU NVIDIA",
    "queues-setup": "очереди задач",
    "workers-setup": "воркеры supervisor и pm2",
    "cron-setup": "кроны на хосте и в контейнерах",
    "logerr-setup": "ошибки в логах контейнеров",
    "fpm-setup": "пулы PHP-FPM",
    "gitlab-setup": "версия, бэкап и миграции GitLab",
    "sockfix-setup": "compose внешнего прокси: сокет и образ",
    "relay-setup": "ретранслятор для агентов",
    "webfiles-setup": "признаки взлома в файлах сайтов",
    "autoban-setup": "автобан сканеров",
}


def gaps(rep: dict, cur: dict[str, str], mutes: list[str] | None = None,
         relay: bool = False) -> list[tuple[str, str]]:
    """Что на ноде появилось, а покрытия под это нет: (ключ, человеческая строка).

    Только ПЕРВАЯ установка и доступы. Устаревшая версия helper'а сюда НЕ попадает:
    там функция уже работает, и звать инженера ночью незачем — для этого есть
    «Требует действий».

    Гейт по версии агента тот же, что у helper_advice: агент до 1.46 не шлёт
    setup_versions, и отличить «helper'а нет» от «агент про него молчит» нельзя.
    """
    try:
        if float(rep.get("agent_version") or 0) < 1.46:
            return []
    except (TypeError, ValueError):
        return []
    out: list[tuple[str, str]] = []
    dk = rep.get("docker") or {}
    if dk.get("present") and not dk.get("access"):
        out.append(("docker", _MISSING_LABEL["docker"]))
    kube = rep.get("kube") or {}
    if kube.get("present") and not kube.get("access"):
        out.append(("kube", _MISSING_LABEL["kube"]))
    sv = rep.get("setup_versions") or {}
    for name in sorted(cur):
        if not setup_needed(name, rep, mutes, relay) or sv.get(name) is not None:
            continue
        if name == "kube-setup" and ("kube", _MISSING_LABEL["kube"]) in out:
            continue  # про кластер уже сказано строкой выше — не дублируем
        out.append((f"helper:{name}",
                    f"нет helper'а «{name}» ({_HELPER_LABEL.get(name, name)})"))
    return out
