"""Внешний прокси, который держит docker-сокет целиком: caddy-docker-proxy, traefik, nginx-proxy.

Такой прокси смотрит в интернет, а docker-сокет - это root на хосте: через него создается
привилегированный контейнер с корнем хоста внутри. Значит, одна дыра в прокси - и чужой
получает весь сервер. Сокет, смонтированный только на чтение, этого не меняет: API тот же.
Лечится прокси сокета только на чтение (wollomatic/socket-proxy во внутренней сети), через
который caddy или traefik читают метки контейнеров и события.

Данные - из отчета агента: у контейнера есть bind-монтирования (binds), образ и состояние.
Наши собственные прокси сокета (kervax-docker-proxy, docker-api у caddy) держат сокет по
делу и в интернет не смотрят - их не трогаем, как и сборщиков метрик и логов (cadvisor,
promtail): они не принимают запросы снаружи.

Что именно поправить, говорит helper sockfix-setup: на самой ноде он правит копию compose-файла,
из которого запущен caddy-docker-proxy, и проверяет ее docker compose config (extras "sockfix").
Панель показывает разницу и команды: применить, проверить, вернуть. Сама ничего не применяет: у
агента к Docker только чтение и перезапуск, и правка прокси панелью сделала бы взлом панели root
на всех серверах - ровно ту дыру, которую закрываем.
"""
from __future__ import annotations

import os
import shlex
import time

_SOCK = frozenset({"/var/run/docker.sock", "/run/docker.sock", "/var/run", "/run"})

# имя образа без реестра, пространства и тега -> что за прокси
_EDGE = {
    "caddy-docker-proxy": "caddy",
    "traefik": "traefik",
    "nginx-proxy": "nginx-proxy",
    "docker-gen": "nginx-proxy",
}


def image_kind(image: str) -> str | None:
    """Внешний ли это прокси с метками docker: lucaslorentz/caddy-docker-proxy:2.12.0 -> caddy,
    traefik:v3 -> traefik, nginxproxy/nginx-proxy -> nginx-proxy. Остальное - None."""
    name = (image or "").lower().split("@", 1)[0]
    base = name.rsplit("/", 1)[-1]
    base = base.split(":", 1)[0]
    return _EDGE.get(base)


FIX_MAX_AGE = 3600  # helper пишет раз в 10 минут; старше часа - встал, его файл мог устареть
_FIX_KEYS = ("ok", "why", "why_detail", "file", "dir", "project", "service", "fixed", "diff", "gid", "nets", "sock",
             "compose")


def fixes(rep: dict) -> dict[str, dict]:
    """Готовые исправления helper'а sockfix-setup по имени контейнера; у готового (ok) - команды
    "apply", "check", "rollback"."""
    b = ((rep or {}).get("extras") or {}).get("sockfix")
    if not isinstance(b, dict) or b.get("v") != 1:
        return {}
    ref = float((rep or {}).get("clock_unix") or 0) or time.time()
    if ref - float(b.get("ts") or 0) > FIX_MAX_AGE:
        return {}
    out: dict[str, dict] = {}
    for it in b.get("items") or []:
        if not isinstance(it, dict) or not it.get("container"):
            continue
        fx = {k: it[k] for k in _FIX_KEYS if it.get(k) not in (None, "", [])}
        fx["ok"] = bool(it.get("ok") and it.get("fixed") and it.get("file") and it.get("dir") and it.get("project"))
        if fx["ok"]:
            fx.update(commands(fx))
        out[str(it["container"])] = fx
    return out


def commands(fx: dict) -> dict[str, str]:
    """Применить готовый файл, проверить, вернуть как было. Файл проекта - по имени, если лежит в его
    каталоге, иначе полным путем; проект - явно (-p), как его запустили."""
    d, f, proj = str(fx["dir"]), str(fx["file"]), str(fx["project"])
    name = os.path.basename(f) if os.path.dirname(f) == d.rstrip("/") else f
    q = shlex.quote
    up = f"docker compose -p {q(proj)} -f {q(name)} up -d"
    return {
        "apply": "\n".join([f"cd {q(d)}", f"cp {q(name)} {q(name + '.bak-kervax')}", f"cp {q(str(fx['fixed']))} {q(name)}", up]),
        "check": "\n".join([f"docker compose -p {q(proj)} ps", f"docker compose -p {q(proj)} logs --since 10m docker-api | grep -c blocked"]),
        "rollback": "\n".join([f"cd {q(d)}", f"cp {q(name + '.bak-kervax')} {q(name)}", up + " --remove-orphans"]),
    }


def view(rep: dict) -> list[dict]:
    """Для карточки: прокси с сокетом и готовое исправление ("fix"), если helper его прислал"""
    fx = fixes(rep)
    return [{**x, "fix": fx[x["name"]]} if x["name"] in fx else x for x in exposed(rep)]


def exposed(rep: dict) -> list[dict]:
    """Работающие внешние прокси ноды с docker-сокетом: [{"name", "image", "kind"}]."""
    out: list[dict] = []
    for c in ((rep or {}).get("docker") or {}).get("containers") or []:
        if not isinstance(c, dict) or c.get("state") != "running":
            continue
        if not _SOCK.intersection(c.get("binds") or []):
            continue
        kind = image_kind(str(c.get("image") or ""))
        if kind:
            out.append({"name": str(c.get("name") or ""), "image": str(c.get("image") or ""), "kind": kind})
    return out


# Сборка образа внешнего прокси старше этого - пора обновлять: он смотрит в интернет, а за год в
# caddy и traefik закрывают не одну дыру. traefik:latest на одной ноде оказался сборкой 2021 года
# (v2.5.5), еще до исправления HTTP/2 rapid reset, а caddy-docker-proxy:ci-alpine - caddy 2.4.5.
OUTDATED_DAYS = 365
_DAY = 86400


def proxy_kind(image: str) -> str | None:
    """Внешний прокси для проверки возраста образа: те же, что image_kind, плюс обычный caddy."""
    kind = image_kind(image)
    if kind:
        return kind
    base = (image or "").lower().split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
    return "caddy" if base == "caddy" else None


def outdated(rep: dict, now: float) -> list[dict]:
    """Работающие внешние прокси со старым образом:
    [{"name", "image", "kind", "version", "built", "age_days"}], built - сборка образа, unix-секунды.

    Дату сборки присылает агент 2.23 (img_created). Нет ее - не флагаем: по тегу возраст не понять
    (latest бывает и вчерашним, и пятилетним), а на ноде может стоять агент постарше или прокси
    агента, который не пускает к списку образов."""
    out: list[dict] = []
    for c in ((rep or {}).get("docker") or {}).get("containers") or []:
        if not isinstance(c, dict) or c.get("state") != "running":
            continue
        kind = proxy_kind(str(c.get("image") or ""))
        try:
            built = int(c.get("img_created") or 0)
        except (TypeError, ValueError):
            built = 0
        if not kind or built <= 0:
            continue
        age = int((now - built) // _DAY)
        if age < OUTDATED_DAYS:
            continue
        out.append({
            "name": str(c.get("name") or ""), "image": str(c.get("image") or ""), "kind": kind,
            "version": str(c.get("img_ver") or "")[:64], "built": built, "age_days": age,
        })
    return out


def text(items: list[dict]) -> str:
    """Строка алерта: "caddy-proxy-caddy-1 (caddy) держит docker-сокет: взлом прокси - root на хосте"."""
    if not items:
        return ""
    names = ", ".join(f"{i['name']} ({i['kind']})" for i in items[:3])
    more = f" и еще {len(items) - 3}" if len(items) > 3 else ""
    verb = "держит" if len(items) == 1 else "держат"
    return f"{names}{more} {verb} docker-сокет: взлом прокси, который смотрит в интернет, - root на хосте"
