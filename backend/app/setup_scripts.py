"""Каталог раздаваемых helper'ов и разбор того, чего ноде не хватает.

Жило в api/servers.py, пока это нужно было только экрану серверов. Теперь тем же
знанием пользуется планировщик (суточная сводка «появилось новое, покрытия нет»),
а тащить в него FastAPI-модуль ради двух функций — плохая идея: отдельный модуль
без зависимостей от веб-слоя.
"""

from __future__ import annotations

import os
import re

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


def setup_needed(name: str, rep: dict) -> bool:
    """Нужен ли helper этой ноде.

    Незнакомое имя (helper добавили, а условие сюда не дописали) считаем нужным
    всюду: лучше лишний пункт в «Требует действий», который сразу видно и легко
    уточнить, чем невыкаченный helper, о котором панель молчит месяцами."""
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
    # webserver-setup — только где реально есть веб-сервер (иначе доменов всё равно нет)
    if name == "webserver-setup":
        return bool(rep.get("web_services"))
    # dbstat-setup — только там, где СУБД реально есть (скан процессов агента);
    # на ноде без баз инвентарь пустой, флагать нечего
    if name == "dbstat-setup":
        return bool(rep.get("db_engines") or rep.get("db_stats"))
    # backup-setup — транспорт панели для дампов, нужен и без файлового бэкапа;
    # timesync-setup и agent-watchdog ansible ставит всюду; остальное — см. докстроку
    return True


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
}


def gaps(rep: dict, cur: dict[str, str]) -> list[tuple[str, str]]:
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
        if not setup_needed(name, rep) or sv.get(name) is not None:
            continue
        if name == "kube-setup" and ("kube", _MISSING_LABEL["kube"]) in out:
            continue  # про кластер уже сказано строкой выше — не дублируем
        out.append((f"helper:{name}",
                    f"нет helper'а «{name}» ({_HELPER_LABEL.get(name, name)})"))
    return out
