"""Контейнер с ключами от хоста: взлом приложения в нем - root на сервере.

Что считаем ключами:
- privileged: в контейнере все устройства хоста и все capabilities, выйти на хост - дело минуты;
- опасные capabilities (SYS_ADMIN, SYS_MODULE, SYS_PTRACE, DAC_READ_SEARCH и т.п.);
- docker-сокет (или /run целиком, где он лежит): через API создается контейнер с корнем хоста
  внутри, и монтирование только на чтение этого не меняет - API тот же;
- корень хоста, /etc, /root или /boot на запись: правятся crontab, sudoers, ключи SSH.
--pid=host сам по себе только показывает процессы, поэтому отдельно его не считаем.

Данные - из отчета агента: binds давно, priv/caps/ro с агента 2.27. У старого агента видно только
сокет: privileged он не шлет, а без списка ro любой bind выглядел бы монтированием на запись.

Инструментам, которым это нужно по назначению (прокси сокета, cadvisor, сборщики логов, VPN
Amnezia, docker-in-docker), разрешено ровно то, что им нужно: promtail с docker-сокетом - норма,
promtail в privileged - нет. Внешние прокси с сокетом (traefik, caddy-docker-proxy) уже флагает
docker_exposure: их сокет здесь не повторяем, чтобы одна дыра не висела двумя пунктами.

Контейнер, который так устроен намеренно, глушится точечно (alert_mutes "dockrisk:<имя>"), весь
пункт - ключом "docker_risk".
"""
from __future__ import annotations

from app import docker_exposure
from app.models import Server

MUTE_PREFIX = "dockrisk:"
MUTE_ALL = "docker_risk"

_SOCK_FILES = ("/docker.sock", "/containerd.sock", "/podman.sock")
_SOCK_DIRS = frozenset({"/run", "/var/run"})
_HOST_RW = frozenset({"/", "/etc", "/root", "/boot"})

PRIV, CAPS, SOCK, HOST = "priv", "caps", "sock", "host"

# Что положено по назначению. Ключ - имя образа без реестра и тега ("promtail") или с
# пространством ("grafana/agent"), когда одно имя слишком общее.
_TOOLS: dict[str, frozenset[str]] = {}


def _allow(flags: set[str], *names: str) -> None:
    for n in names:
        _TOOLS[n] = frozenset(flags)


# прокси сокета: держат сокет, чтобы другим его не давать
_allow({SOCK}, "socket-proxy", "docker-socket-proxy")
# метрики и логи контейнеров
_allow({PRIV, SOCK, CAPS}, "cadvisor")
_allow({CAPS}, "node-exporter")
_allow({SOCK, CAPS}, "netdata", "datadog/agent", "metricbeat")
_allow({SOCK}, "promtail", "alloy", "grafana/agent", "vector", "fluent-bit", "filebeat")
# управление контейнерами, обновления, CI-раннеры
_allow({SOCK}, "portainer", "portainer-ce", "portainer-ee", "portainer/agent", "watchtower", "dozzle",
       "autoheal", "docker-autoheal", "diun", "ouroboros", "gitlab-runner", "drone-runner-docker", "act_runner",
       "woodpecker-agent")
# внешние прокси с метками docker: сокет у них - отдельный пункт docker_exposure
_allow({SOCK}, "traefik", "caddy-docker-proxy", "nginx-proxy", "docker-gen", "acme-companion",
       "letsencrypt-nginx-proxy-companion")
# VPN: туннель и модули ядра
_allow({CAPS}, "wg-easy", "wireguard")
# кластеры и докер внутри докера
_allow({PRIV}, "kindest/node", "k3s", "rancher/k3s")


def _image_keys(image: str) -> tuple[str, str]:
    """('promtail', 'grafana/promtail') из 'docker.io/grafana/promtail:3.4@sha256:...'"""
    name = (image or "").lower().split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    parts = name.split("/")
    return parts[-1], "/".join(parts[-2:])


def allowed(name: str, image: str) -> frozenset[str]:
    """Что положено контейнеру по назначению."""
    base, ns = _image_keys(image)
    tag = (image or "").lower().split("@", 1)[0].rsplit("/", 1)[-1].partition(":")[2]
    out = set(_TOOLS.get(ns) or _TOOLS.get(base) or ())
    nm = (name or "").lower()
    if base.startswith("coolify") or ns.startswith("coollabsio/"):
        out.add(SOCK)  # Coolify и его sentinel управляют контейнерами
    if nm.startswith("amnezia-"):
        out |= {PRIV, CAPS}  # контейнеры Amnezia VPN запускаются privileged
    if nm == "kervax-docker-proxy":
        out.add(SOCK)  # наш прокси сокета, образ бывает виден как sha256:...
    if base == "docker" and "dind" in tag:
        out.add(PRIV)
    return frozenset(out)


def _new_agent(rep: dict) -> bool:
    try:
        v = tuple(int(x) for x in str(rep.get("agent_version") or "0").split(".")[:2])
    except ValueError:
        return False
    return v >= (2, 27)


def flags(c: dict, new_agent: bool) -> list[str]:
    """Ключи от хоста у контейнера, без учета назначения."""
    binds = [str(b) for b in c.get("binds") or [] if isinstance(b, str)]
    out = []
    if c.get("priv"):
        out.append(PRIV)
    if c.get("caps") and not c.get("priv"):
        out.append(CAPS)
    if any(b.endswith(_SOCK_FILES) or b in _SOCK_DIRS for b in binds):
        out.append(SOCK)
    if new_agent:
        ro = set(str(x) for x in c.get("ro") or [])
        if any(b in _HOST_RW and b not in ro for b in binds):
            out.append(HOST)
    return out


def _caps_text(c: dict) -> str:
    return ", ".join(str(x) for x in c.get("caps") or [])


def describe(c: dict, fl: list[str]) -> str:
    """'privileged', 'docker-сокет', 'cap SYS_ADMIN', 'корень хоста на запись' через запятую"""
    words = {PRIV: "privileged", SOCK: "docker-сокет", HOST: "корень хоста на запись"}
    return ", ".join(f"cap {_caps_text(c)}" if f == CAPS else words[f] for f in fl)


def view(srv: Server) -> list[dict]:
    """Работающие контейнеры с ключами сверх положенного:
    [{"name", "image", "flags", "caps", "text", "muted"}]; заглушенные тоже, с muted=True."""
    rep = srv.last_report or {}
    dk = rep.get("docker") or {}
    if not dk.get("access"):
        return []
    new = _new_agent(rep)
    edge = {e["name"] for e in docker_exposure.exposed(rep)}
    mutes = set(srv.alert_mutes or [])
    out = []
    for c in dk.get("containers") or []:
        if not isinstance(c, dict) or c.get("state") != "running":
            continue
        name = str(c.get("name") or "")
        ok = allowed(name, str(c.get("image") or ""))
        fl = [f for f in flags(c, new) if f not in ok and not (f == SOCK and name in edge)]
        if fl:
            out.append({"name": name, "image": str(c.get("image") or ""), "flags": fl,
                        "caps": [str(x) for x in c.get("caps") or []] if CAPS in fl else [],
                        "text": describe(c, fl), "muted": MUTE_PREFIX + name in mutes})
    return out


def advice(srv: Server) -> list[dict]:
    """Для "Требует действий": незаглушенные, пока не заглушен весь пункт."""
    if MUTE_ALL in set(srv.alert_mutes or []):
        return []
    return [x for x in view(srv) if not x["muted"]]


def text(items: list[dict]) -> str:
    """Строка алерта и пункта: "app (privileged), web (docker-сокет): взлом ... - root на сервере"."""
    if not items:
        return ""
    names = ", ".join(f"{i['name']} ({i['text']})" for i in items[:3])
    more = f" и еще {len(items) - 3}" if len(items) > 3 else ""
    return f"у контейнеров ключи от хоста: {names}{more} - взлом приложения в них даст root на сервере"
