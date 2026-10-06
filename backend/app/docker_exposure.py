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
