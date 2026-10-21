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

Как исправить (fix): копия compose, правка строки в блоке сервиса, пересоздать сервис, откат. Папка,
файлы и сервис - из меток compose (агент 2.41, compose_cmd); контейнеры одного проекта с одними
ключами чинятся одним блоком шагов. Панель ничего не применяет: у агента к Docker только чтение.
"""
from __future__ import annotations

import re
import shlex

from app import compose_cmd, docker_exposure, tg_fmt
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


def view(srv: Server, fix: bool = False) -> list[dict]:
    """Работающие контейнеры с ключами сверх положенного:
    [{"name", "image", "flags", "caps", "text", "muted"}]; заглушенные тоже, с muted=True. fix -
    незаглушенным еще "fix": блок шагов (fixes), один на контейнеры одного проекта."""
    rep = srv.last_report or {}
    dk = rep.get("docker") or {}
    if not dk.get("access"):
        return []
    new = _new_agent(rep)
    edge = {e["name"] for e in docker_exposure.exposed(rep)}
    mutes = set(srv.alert_mutes or [])
    out = []
    cs: dict[str, dict] = {}
    for c in dk.get("containers") or []:
        if not isinstance(c, dict) or c.get("state") != "running":
            continue
        name = str(c.get("name") or "")
        ok = allowed(name, str(c.get("image") or ""))
        fl = [f for f in flags(c, new) if f not in ok and not (f == SOCK and name in edge)]
        if fl:
            # svc - сервис compose (агент 2.41): короче имени контейнера, по нему алерт в чате
            cp = c.get("compose") if isinstance(c.get("compose"), dict) else {}
            out.append({"name": name, "image": str(c.get("image") or ""), "flags": fl,
                        "caps": [str(x) for x in c.get("caps") or []] if CAPS in fl else [],
                        "text": describe(c, fl), "muted": MUTE_PREFIX + name in mutes,
                        "svc": str(cp.get("service") or "")})
            cs[name] = c
    if fix:
        live = [x for x in out if not x["muted"]]
        for blk in fixes(live, cs):
            for x in live:
                if x["name"] in blk["names"]:
                    x["fix"] = blk
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


_NUM_TAIL = re.compile(r"^(.*?)[-_](\d+)$")


def _series(labels: list[str]) -> list[str]:
    """Имена, которые отличаются только номером в конце, - одной записью:
    teamcity-agent-1, teamcity-agent-2 -> "teamcity-agent 1, 2". Порядок - по первому из группы."""
    groups: dict[str, list[str]] = {}
    for lb in labels:
        m = _NUM_TAIL.match(lb)
        groups.setdefault(m.group(1) if m else lb, []).append(lb)
    out = []
    for base, lbs in groups.items():
        if len(lbs) > 1:
            nums = sorted({_NUM_TAIL.match(x).group(2) for x in lbs}, key=int)
            out.append(f"{base} {', '.join(nums)}")
        else:
            out.append(lbs[0])
    return out


def tg_short(items: list[dict]) -> tuple[str, list[str]] | None:
    """Алерт docker_risk (вход - advice()): "Контейнеры с root на хосте", "teamcity-agent 1, 2, 3
    (privileged)". Имя - сервис compose, если он есть и не повторяется, иначе имя контейнера;
    контейнеры с одними ключами - одной записью."""
    if not items:
        return None
    svcs = [i.get("svc") or "" for i in items]
    label = {id(i): (s if s and svcs.count(s) == 1 else i["name"]) for i, s in zip(items, svcs)}
    by: dict[str, list[str]] = {}
    for i in items:
        by.setdefault(i.get("text") or "", []).append(label[id(i)])
    groups = list(by.items())
    fact = ", ".join(tg_fmt.names(_series(lbs)) + (f" ({t})" if t else "") for t, lbs in groups[:2])
    rest = sum(len(lbs) for _t, lbs in groups[2:])
    if rest:
        fact += f" и еще {rest}"
    what = "Контейнер с root на хосте" if len(items) == 1 else "Контейнеры с root на хосте"
    return what, [fact]


# CI-агенты: privileged у них ради docker внутри сборок (DinD), а сборкам он нужен не всегда
_CI = frozenset({"teamcity-agent", "jenkins", "inbound-agent", "jenkins/agent", "buildkite/agent", "github-runner",
                 "actions-runner", "gitlab-runner", "woodpecker-agent", "act_runner", "drone-runner-docker"})
# что дать вместо privileged по назначению образа: (cap_add, devices)
_NEEDS: dict[str, tuple[str, str]] = {
    "wg-easy": ("NET_ADMIN, SYS_MODULE", ""), "wireguard": ("NET_ADMIN, SYS_MODULE", ""),
    "tailscale": ("NET_ADMIN", "/dev/net/tun"), "gluetun": ("NET_ADMIN", "/dev/net/tun"),
    "openvpn": ("NET_ADMIN", "/dev/net/tun"), "kylemanna/openvpn": ("NET_ADMIN", "/dev/net/tun"),
}
BAK = ".bak-kervax-host"
SOCK_PROXY_IMAGE = "wollomatic/socket-proxy:1.13.1"


def is_ci(image: str) -> bool:
    base, ns = _image_keys(image)
    return base in _CI or ns in _CI


def needs(image: str) -> tuple[str, str]:
    """(cap_add, devices) вместо privileged; ("", "") - по образу не понять."""
    base, ns = _image_keys(image)
    return _NEEDS.get(ns) or _NEEDS.get(base) or ("", "")


def _re(s: str) -> str:
    """Строка буквально в адресе sed /.../."""
    return re.sub(r"([.*^$\\\[\]/])", r"\\\1", s)


def _sock_bind(c: dict) -> str:
    return next((b for b in (str(x) for x in c.get("binds") or []) if b.endswith(_SOCK_FILES) or b in _SOCK_DIRS), "")


def _host_binds(c: dict) -> list[str]:
    ro = set(str(x) for x in c.get("ro") or [])
    return [b for b in (str(x) for x in c.get("binds") or []) if b in _HOST_RW and b not in ro]


def sock_snippet(svcs: list[str]) -> str:
    """Что дописать в compose руками: прокси сокета только на чтение во внутренней сети и
    DOCKER_HOST у сервисов. Пускает чтение контейнеров, образов, сетей и событий; запуск, остановку
    и exec - нет. Комментарии по-английски: текст копируют в файл как есть."""
    return "\n".join([
        f"# to {', '.join(svcs)}: environment and networks",
        "    environment:",
        "      - DOCKER_HOST=tcp://docker-api:2375",
        "    networks: [docker-api]   # plus the networks it has now",
        "# to services:",
        "  docker-api:",
        f"    image: {SOCK_PROXY_IMAGE}",
        "    restart: unless-stopped",
        '    user: "65534:GID"   # GID: stat -c %g /var/run/docker.sock',
        "    read_only: true",
        "    mem_limit: 64M",
        "    cap_drop: [ALL]",
        '    security_opt: ["no-new-privileges:true"]',
        "    volumes:",
        "      - /var/run/docker.sock:/var/run/docker.sock:ro",
        "    networks: [docker-api]",
        "    command:",
        "      - -listenip=0.0.0.0",
        f"      - -allowfrom={','.join(svcs)}",
        "      - -watchdoginterval=600",
        "      - -stoponwatchdog",
        "      - -allowHEAD=/_ping",
        r"      - -allowGET=/(v[0-9]+\.[0-9]+/)?(_ping|version|info|events|containers/json"
        r"|containers/[a-zA-Z0-9][a-zA-Z0-9_.-]*/json|images/json|networks(/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?)",
        "# to networks (top level):",
        "  docker-api:",
        "    internal: true",
    ])


def _compose_steps(fl: list[str], c0: dict, image: str, names: list[str], svcs: list[str], cm: compose_cmd.Cmd) -> list[dict]:
    """Копия compose, правка в блоке каждого сервиса, пересоздать, откат. Правка и перезапуск сами
    говорят, вышло ли: sed, не нашедший строку, молчит, а up -d тогда пересоздает прежнее."""
    q = shlex.quote
    files = " ".join(cm.files)
    hand = "НЕ ИЗМЕНЕНО: нужной строки в блоке сервиса нет, поправьте руками"
    steps: list[dict] = [{"id": "bak", "cmd": "\n".join([cm.cd] + cm.backup(BAK))}]
    if PRIV in fl:
        cap_add, devices = needs(image)
        if cap_add or devices:
            # privileged: true -> cap_add и devices с тем же отступом, по строке на ключ
            rep = r"\n\1".join(([f"cap_add: [{cap_add}]"] if cap_add else []) + ([f"devices: [{devices}]"] if devices else []))
            sid, expr = "priv_set", r"s#^\( *\)privileged: *true#\1" + rep + "#"
        else:
            sid, expr = "priv_off", "s/privileged: *true/privileged: false/"
        steps.append({"id": sid, "cmd": "\n".join([cm.cd] + cm.changed(
            [cm.sed_in(s, "privileged:", expr) for s in svcs] + cm.diff(BAK), hand))})
    sock = _sock_bind(c0) if SOCK in fl else ""
    if SOCK in fl:
        pat = _re(sock.rsplit("/", 1)[-1] if sock.endswith(_SOCK_FILES) else sock + ":")
        steps.append({"id": "sock_rm", "cmd": "\n".join([cm.cd] + cm.changed(
            [cm.sed_in(s, pat, "{/%s/d}" % pat) for s in svcs] + cm.diff(BAK), hand))})
        steps.append({"id": "sock_add", "cmd": sock_snippet(svcs)})
    if CAPS in fl:
        caps = [str(x) for x in c0.get("caps") or []]
        steps.append({"id": "caps_edit", "cmd": "\n".join([cm.cd, f"grep -nE {q('|'.join(caps))} {files}"])})
    if HOST in fl:
        paths = _host_binds(c0)
        pat = '- *"?(' + "|".join(re.escape(p) for p in paths) + "):"
        steps.append({"id": "host_edit", "cmd": "\n".join([cm.cd, f"grep -nE -- {q(pat)} {files}"])})
    ups = " ".join(q(s) for s in (["docker-api"] if SOCK in fl else []) + svcs)
    only = " ".join(q(s) for s in svcs)
    # не просто Up: у пересозданного контейнера ключа больше нет (по docker inspect)
    after = []
    for n in names:
        if PRIV in fl:
            after.append(compose_cmd.verdict(f"sudo docker inspect -f '{{{{.HostConfig.Privileged}}}}' {q(n)} | grep -qx false",
                                             f"{n}: privileged снят", f"{n}: НЕ СНЯТ: контейнер все еще privileged"))
        if sock:
            after.append(compose_cmd.verdict(
                f"! sudo docker inspect -f '{{{{range .Mounts}}}}{{{{println .Source}}}}{{{{end}}}}' {q(n)} | grep -qxF -- {q(sock)}",
                f"{n}: docker-сокета у контейнера нет", f"{n}: НЕ УБРАН: docker-сокет все еще у контейнера"))
    steps.append({"id": "up", "cmd": "\n".join([
        cm.cd, f"{cm.dc} config -q && {cm.dc} up -d {ups}", f"{cm.dc} ps {ups}", f"{cm.dc} logs --tail 20 {only}"] + after)})
    orphans = " --remove-orphans" if SOCK in fl else ""
    steps.append({"id": "undo", "undo": True, "cmd": "\n".join([cm.cd] + cm.restore(BAK) + [f"{cm.dc} up -d{orphans} {only}"])})
    return steps


def _block(items: list[dict], cs: dict[str, dict], w: dict | None) -> dict:
    """Шаги для контейнеров одного проекта с одними ключами: {"key", "names", "args", "steps"}.
    Тексты во фронте по id шага, args - подстановки (names, flags, file, dir, svc, caps, paths, cap_add,
    devices, dind_env). w - откуда запущены (compose_cmd.where): {} - docker run, None - старый агент."""
    q = shlex.quote
    names = [x["name"] for x in items]
    fl, image = items[0]["flags"], items[0]["image"]
    c0 = cs[names[0]]
    ci = PRIV in fl and is_ci(image)
    cap_add, devices = needs(image) if PRIV in fl else ("", "")
    args = {k: v for k, v in {
        "names": ", ".join(names), "flags": ",".join(fl), "cap_add": cap_add, "devices": devices,
        "caps": ", ".join(str(x) for x in c0.get("caps") or []) if CAPS in fl else "",
        "paths": ", ".join(_host_binds(c0)) if HOST in fl else "",
        # агент TeamCity поднимает docker внутри по DOCKER_IN_DOCKER=start: без privileged он не нужен
        "dind_env": "DOCKER_IN_DOCKER=start" if ci and _image_keys(image)[0] == "teamcity-agent" else ""}.items() if v}
    steps: list[dict] = []
    if ci:
        steps.append({"id": "ci_check", "cmd": "\n".join(f"sudo docker exec {q(n)} docker images -q | wc -l" for n in names)})
    if w is None:
        # агент не присылает метки compose: где файл, покажет команда, что поменять - словами
        steps += [{"id": "find", "cmd": compose_cmd.find_cmd(names)}] + [{"id": "hand_" + f} for f in fl]
        steps.append({"id": "hand_up"})
    elif not w:
        steps.append({"id": "run"})
    else:
        svcs = [str(cs[n]["compose"]["service"]) for n in names]
        args.update({"file": ", ".join(w["files"]), "dir": w["dir"], "svc": ", ".join(svcs)})
        steps += _compose_steps(fl, c0, image, names, svcs, compose_cmd.Cmd(w))
    if ci:
        steps.append({"id": "alt_ci", "alt": True})
    elif SOCK in fl:
        steps.append({"id": "alt_sock", "alt": True})
    return {"key": names[0], "names": names, "args": args, "steps": steps}


def fixes(items: list[dict], cs: dict[str, dict]) -> list[dict]:
    """Блоки шагов: контейнеры одного compose-проекта с одинаковыми ключами и образом - одним блоком
    (три CI-агента одного проекта - одни шаги на всех). У старого агента - тоже одним, по ключам."""
    groups: dict[tuple, list[dict]] = {}
    where: dict[tuple, dict | None] = {}
    for x in items:
        c = cs[x["name"]]
        w = compose_cmd.where(c)
        if w:
            src: tuple = (w["dir"], tuple(w["files"]), w["flags"])
        else:
            src = ("old",) if w is None else ("run", x["name"])
        k = src + (tuple(x["flags"]), tuple(x["caps"]), _image_keys(x["image"])[1],
                   _sock_bind(c) if SOCK in x["flags"] else "", tuple(_host_binds(c)) if HOST in x["flags"] else ())
        groups.setdefault(k, []).append(x)
        where[k] = w
    return [_block(v, cs, where[k]) for k, v in groups.items()]
