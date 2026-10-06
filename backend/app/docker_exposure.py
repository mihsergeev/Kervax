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
"""

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


def text(items: list[dict]) -> str:
    """Строка алерта: "caddy-proxy-caddy-1 (caddy) держит docker-сокет: взлом прокси - root на хосте"."""
    if not items:
        return ""
    names = ", ".join(f"{i['name']} ({i['kind']})" for i in items[:3])
    more = f" и еще {len(items) - 3}" if len(items) > 3 else ""
    verb = "держит" if len(items) == 1 else "держат"
    return f"{names}{more} {verb} docker-сокет: взлом прокси, который смотрит в интернет, - root на хосте"
