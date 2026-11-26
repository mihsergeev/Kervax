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
С агента 2.57 у контейнеров с опасными caps еще права главного процесса (proc): кто стартует от root
только ради entrypoint и работает дальше без прав, тот не ключ, а заметка (start_caps).

Инструментам, которым это нужно по назначению (прокси сокета, cadvisor, сборщики логов, VPN
Amnezia, docker-in-docker), разрешено ровно то, что им нужно: promtail с docker-сокетом - норма,
promtail в privileged - нет. Внешние прокси с сокетом (traefik, caddy-docker-proxy) уже флагает
docker_exposure: их сокет здесь не повторяем, чтобы одна дыра не висела двумя пунктами.

Docker внутри privileged-контейнера (dind, агент 2.60): CI-агенту privileged нужен ради docker build и
шагов в контейнере, совет убрать его ломает CI. Агент без root видит это по процессам: dockerd в
cgroup контейнера, containerd-shim - контейнеры внутри сейчас, подкаталоги docker/buildkit в cgroup -
внутри уже что-то запускалось, CPU dockerd - pull и push образов. Живой docker внутри - не ключ, а
заметка (note): решение за человеком, кнопка "знаю, так надо" - тот же мут dockrisk:<имя>. Dockerd
есть, но без дела - совет как раньше, с проверкой образов внутри.

Контейнер, который так устроен намеренно, глушится точечно (alert_mutes "dockrisk:<имя>"), весь
пункт - ключом "docker_risk".
Предупреждение появляется после 10 минут свежих наблюдений одного экземпляра. До этого
текущий риск виден в карточке Docker в ожидании подтверждения.

Как исправить (fix): копия compose, правка строки в блоке сервиса, пересоздать сервис, откат. Папка,
файлы и сервис - из меток compose (агент 2.41, compose_cmd); контейнеры одного проекта с одними
ключами чинятся одним блоком шагов. Панель ничего не применяет: у агента к Docker только чтение.
"""
from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone

from app import compose_cmd, docker_exposure, docker_origin, report_progress, tg_fmt
from app.models import Server

MUTE_PREFIX = "dockrisk:"
MUTE_ALL = "docker_risk"
OBSERVATIONS = "docker_risk_observations"
CONFIRM_SECONDS = 10 * 60
MIN_REPORTS = 3

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
    if base == "docker" and "dind" in tag:
        out.add(PRIV)
    return frozenset(out)


# CPU dockerd с запуска, секунд, после которого docker внутри точно работал: pull, распаковка и push
# образов по сотне мегабайт съедают его за несколько сборок, пустой dockerd за месяц - единицы секунд
DIND_CPU = 60
# dockerd моложе этого без дела судить рано: контейнер могли только что перезапустить
DIND_YOUNG = 2 * 86400


def dind(c: dict) -> dict | None:
    """Docker внутри privileged-контейнера: {"state", "run", "up", "cpu", "ran"}, state - live
    (работает), idle (dockerd поднят, но без дела), none (docker внутри нет). None - не знаем:
    старый агент или процессы не видны (hidepid)."""
    d = c.get("dind")
    if not isinstance(d, dict):
        return None

    def num(k: str) -> int:
        v = d.get(k)
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0

    run, up, cpu, ran = num("run"), num("up"), num("cpu"), d.get("ran") is True
    if d.get("dockerd") is not True:
        state = "none"
    elif run or ran or cpu >= DIND_CPU or up < DIND_YOUNG:
        state = "live"
    else:
        state = "idle"
    return {"state": state, "run": run, "up": up, "cpu": cpu, "ran": ran}


def _new_agent(rep: dict) -> bool:
    try:
        v = tuple(int(x) for x in str(rep.get("agent_version") or "0").split(".")[:2])
    except ValueError:
        return False
    return v >= (2, 27)


def start_only(c: dict) -> bool:
    """Опасные caps выданы, но главный процесс работает без них (proc, агент 2.57): стартует от
    root ради entrypoint, тот делает chown и setpriv/gosu на uid приложения. Не root (ни один из
    real/effective/saved/fs), в CapEff/CapPrm/CapAmb опасных нет и no_new_privs - вернуть права
    через setuid-бинарь или file capabilities нельзя. Нет proc (старый агент) - судим по cap_add."""
    p = c.get("proc")
    if not isinstance(p, dict) or p.get("nnp") is not True or p.get("caps"):
        return False
    uid = p.get("uid")
    return isinstance(uid, int) and not isinstance(uid, bool) and uid > 0


def flags(c: dict, new_agent: bool) -> list[str]:
    """Ключи от хоста у контейнера, без учета назначения."""
    binds = [str(b) for b in c.get("binds") or [] if isinstance(b, str)]
    out = []
    if c.get("priv"):
        out.append(PRIV)
    if c.get("caps") and not c.get("priv") and not start_only(c):
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
    [{"name", "image", "flags", "caps", "text", "muted", "dind"?, "note"?}]; заглушенные тоже, с
    muted=True. dind - docker внутри у privileged (dind()). Живой docker внутри - privileged не
    ключ: других ключей нет - заметка (note=True, flags ["priv"]), есть - privileged из flags убран.
    fix - незаглушенным ключам еще "fix": блок шагов (fixes), один на контейнеры одного проекта."""
    return _view(srv.last_report or {}, srv.alert_mutes or [], fix)


def _view(rep: dict, mutes: list[str], fix: bool = False) -> list[dict]:
    dk = rep.get("docker")
    if not isinstance(dk, dict):
        return []
    if not dk.get("access"):
        return []
    new = _new_agent(rep)
    edge = {e["name"] for e in docker_exposure.exposed(rep)}
    mutes = set(mutes)
    out = []
    cs = _containers(rep)
    for name, c in cs.items():
        if c.get("state") != "running":
            continue
        ok = allowed(name, str(c.get("image") or ""))
        if docker_origin.verified_socket(rep, c):
            ok = ok | {SOCK}
        fl = [f for f in flags(c, new) if f not in ok and not (f == SOCK and name in edge)]
        dd = dind(c) if PRIV in fl else None
        if dd:
            dd["ci"] = is_ci(str(c.get("image") or ""))  # в заметке: "CI собирает образы"
        note = bool(dd and dd["state"] == "live")
        if note and fl != [PRIV]:
            fl, note = [f for f in fl if f != PRIV], False
        if fl:
            # svc - сервис compose (агент 2.41): короче имени контейнера, по нему алерт в чате
            cp = c.get("compose") if isinstance(c.get("compose"), dict) else {}
            out.append({"name": name, "image": str(c.get("image") or ""), "flags": fl,
                        "caps": [str(x) for x in c.get("caps") or []] if CAPS in fl else [],
                        "text": describe(c, fl), "muted": MUTE_PREFIX + name in mutes,
                        "svc": str(cp.get("service") or "")})
            if dd:
                out[-1]["dind"] = dd
            if note:
                out[-1]["note"] = True
    if fix:
        _attach_fixes(out, cs)
    return out


def _attach_fixes(items: list[dict], containers: dict[str, dict]) -> None:
    live = {x["name"]: x for x in items if not x["muted"] and not x.get("note")}
    for block in fixes(list(live.values()), containers):
        for name in block["names"]:
            live[name]["fix"] = block


def _inventory(rep: dict) -> list[dict] | None:
    dk = rep.get("docker")
    if not isinstance(dk, dict) or not dk.get("access") or dk.get("containers_complete") is False:
        return None
    # Go omits an empty list, but still emits containers_complete=true. Older
    # agents did not mark failed listings and retain their empty-list contract.
    containers = dk.get("containers", [])
    if not isinstance(containers, list) or not all(isinstance(c, dict) and c.get("name") and c.get("state")
            and all(c.get(key) is None or (isinstance(c[key], list) and all(isinstance(x, str) for x in c[key]))
                    for key in ("binds", "ro", "caps")) for c in containers):
        return None
    return containers


def _containers(rep: dict) -> dict[str, dict]:
    dk = rep.get("docker")
    raw = dk.get("containers") if isinstance(dk, dict) else None
    return {str(c.get("name") or ""): c for c in raw if isinstance(c, dict)} if isinstance(raw, list) else {}


def signature(item: dict) -> str:
    return f"{item['name']}:{','.join(item['flags'])}"


def _identity(name: str, container: dict) -> str:
    cid = container.get("id")
    return f"id:{cid}" if isinstance(cid, str) and cid else f"name:{name}:{container.get('image') or ''}"


def _instance(item: dict, container: dict) -> str:
    return f"{_identity(item['name'], container)}:{','.join(item['flags'])}"


_UP_TIME = re.compile(r"^Up (\d{1,5}|(?:About )?an?) (second|minute|hour|day|week|month|year)s?\b")
_UP_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400,
             "week": 604800, "month": 2592000, "year": 31536000}


def _legacy_old_enough(container: dict) -> bool:
    # До агента 2.46 ID в отчете не было. Docker Status позволяет отличить
    # запущенный давно контейнер от новой копии с тем же именем и образом.
    status = str(container.get("status") or "")
    if container.get("id") or not status.startswith("Up "):
        return True
    m = _UP_TIME.match(status)
    if not m:
        return False
    n, unit = m.groups()
    return (int(n) if n.isdigit() else 1) * _UP_UNITS[unit] >= CONFIRM_SECONDS


def _legacy_active(srv: Server) -> set[str]:
    state = srv.alert_state or {}
    return set(state.get(f"{MUTE_ALL}_sig") or state.get(f"{MUTE_ALL}_told") or []) if state.get(MUTE_ALL) else set()


def _progress_input(rep: dict) -> dict:
    # Uptime also distinguishes retries when a legacy agent has no wall clock.
    return rep if rep.get("clock_unix") else {**rep, "clock_unix": rep.get("uptime_seconds") or 0}


def observe(srv: Server, rep: dict, now: datetime, *, interval: int = 15) -> None:
    """Вызывается на приеме каждого отчета под блокировкой строки сервера."""
    prev = srv.last_report or {}
    old = prev.get(OBSERVATIONS) or {}
    complete = _inventory(rep) is not None
    if not complete and not old and _inventory(prev) is None:
        return

    prior = old.get("progress")
    if not old and srv.last_seen is not None:
        seen = srv.last_seen
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        prior, _, _ = report_progress.step(None, _progress_input(prev), seen)
    members = old.get("members") or {}
    if not old and _inventory(prev) is not None and (active := _legacy_active(srv)):
        previous_containers = _containers(prev)
        members = {_instance(item, previous_containers[item["name"]]): {"ready": True}
                   for item in _view(prev, []) if signature(item) in active and not item.get("note")}
    max_gap = max(report_progress.MAX_SAME_SECONDS, interval + 60)
    progress, fresh, epoch = report_progress.step(prior, _progress_input(rep), now, max_gap=max_gap)
    # step() допускает повтор снимка для чтения, но повтор не подтверждает длительность.
    if not fresh or (progress and prior and progress.get("at") == prior.get("at")):
        rep[OBSERVATIONS] = {**old, "valid": False, "progress": progress, "members": members}
        return

    stamp = now.timestamp()
    old_at = old.get("at")
    continuous = (not epoch and bool(progress) == bool(prior)
                  and isinstance(old_at, (int, float)) and 0 < stamp - old_at <= max_gap)
    if progress and prior and not 0 < progress["clock"] - prior["clock"] <= max_gap:
        continuous = False
    updated = {}
    unknown = set()
    active = False
    if complete:
        containers = _containers(rep)
        # With the modern inventory marker, inspect always emits compose={} on
        # success, even for docker run. A failed inspect of a formerly unsafe
        # running container cannot prove it lost host access.
        if rep["docker"].get("containers_complete") is True:
            uninspected = {_identity(name, c) for name, c in containers.items()
                           if c["state"] == "running" and not isinstance(c.get("compose"), dict)}
            unknown = {key for key, item in members.items()
                       if item.get("ready") and key.rpartition(":")[0] in uninspected}
        mutes = set(srv.alert_mutes or [])
        for item in _view(rep, []):
            if item.get("note"):
                continue
            c = containers[item["name"]]
            key = _instance(item, c)
            last = members.get(key) or {}
            first = last.get("first", stamp) if continuous else stamp
            count = min(MIN_REPORTS, last.get("n", 0) + 1) if continuous else 1
            ready = _legacy_old_enough(c) and (
                last.get("ready") or (count >= MIN_REPORTS and stamp - first >= CONFIRM_SECONDS))
            updated[key] = {"ready": True} if ready else {"first": first, "n": count}
            if ready and MUTE_ALL not in mutes and MUTE_PREFIX + item["name"] not in mutes:
                active = True
            unknown.discard(key)
        updated.update((key, members[key]) for key in unknown)
    else:
        # Нет снимка: объявленный риск не восстановился, но выдержку нового не засчитываем.
        updated = {key: item for key, item in members.items() if item.get("ready")}

    clean = None
    if complete and not unknown and not active:
        clean = (old.get("clean_since") or stamp) if continuous else stamp
    rep[OBSERVATIONS] = {"valid": complete, "progress": progress, "at": stamp, "members": updated,
                         "unknown": bool(unknown), "clean_since": clean}


def observed(rep: dict) -> bool:
    return bool((rep.get(OBSERVATIONS) or {}).get("valid"))


def complete(rep: dict) -> bool:
    return observed(rep) and not rep[OBSERVATIONS].get("unknown")


def recovered(rep: dict, seconds: float) -> bool:
    obs = rep.get(OBSERVATIONS) or {}
    since = obs.get("clean_since")
    return complete(rep) and since is not None and obs["at"] - since >= max(0, seconds)


def split(srv: Server, fix: bool = False) -> tuple[list[dict], list[dict]]:
    """Подтвержденные риски и заметки отдельно от текущих фактов на выдержке."""
    rep = srv.last_report or {}
    obs = rep.get(OBSERVATIONS) or {}
    members = obs.get("members") or {}
    old_sig = _legacy_active(srv) if not obs else set()
    containers = _containers(rep)
    confirmed, pending = [], []
    for item in view(srv):
        key = _instance(item, containers[item["name"]])
        if item.get("note") or signature(item) in old_sig or (members.get(key) or {}).get("ready"):
            confirmed.append(item)
        else:
            pending.append(item)
    if fix:
        _attach_fixes(confirmed, containers)
    return confirmed, pending


def start_caps(srv: Server) -> list[dict]:
    """Заметка вместо ключей: работающие контейнеры, у которых опасные caps только на старт
    (start_only), сверх положенного по назначению: [{"name", "image", "caps", "uid"}]."""
    rep = srv.last_report or {}
    dk = rep.get("docker") or {}
    if not dk.get("access"):
        return []
    out = []
    for c in dk.get("containers") or []:
        if not isinstance(c, dict) or c.get("state") != "running" or c.get("priv") or not c.get("caps"):
            continue
        name = str(c.get("name") or "")
        if CAPS in allowed(name, str(c.get("image") or "")) or not start_only(c):
            continue
        out.append({"name": name, "image": str(c.get("image") or ""),
                    "caps": [str(x) for x in c.get("caps") or []], "uid": c["proc"]["uid"]})
    return out


def advice(srv: Server, confirmed: list[dict] | None = None) -> list[dict]:
    """Для "Требует действий": незаглушенные, пока не заглушен весь пункт. Заметки (note) - нет."""
    if MUTE_ALL in set(srv.alert_mutes or []):
        return []
    return [x for x in (split(srv)[0] if confirmed is None else confirmed) if not x["muted"] and not x.get("note")]


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


def _block(items: list[dict], cs: dict[str, dict], w: dict | None, occupied_proxy: bool) -> dict:
    """Шаги для контейнеров одного проекта с одними ключами: {"key", "names", "args", "steps"}.
    Тексты во фронте по id шага, args - подстановки (names, flags, file, dir, svc, caps, paths, cap_add,
    devices, dind_env, idle_days, idle_cpu). w - откуда запущены (compose_cmd.where): {} - docker run, None - старый агент."""
    q = shlex.quote
    names = [x["name"] for x in items]
    fl, image = items[0]["flags"], items[0]["image"]
    c0 = cs[names[0]]
    ci = PRIV in fl and is_ci(image)
    # docker внутри по агенту: нет его - проверять образы нечего, без дела - сказать, сколько
    dd = items[0].get("dind") if PRIV in fl else None
    st = dd["state"] if dd else ""
    cap_add, devices = needs(image) if PRIV in fl else ("", "")
    args = {k: v for k, v in {
        "names": ", ".join(names), "flags": ",".join(fl), "cap_add": cap_add, "devices": devices,
        "caps": ", ".join(str(x) for x in c0.get("caps") or []) if CAPS in fl else "",
        "paths": ", ".join(_host_binds(c0)) if HOST in fl else "",
        # агент TeamCity поднимает docker внутри по DOCKER_IN_DOCKER=start: без privileged он не нужен
        "dind_env": "DOCKER_IN_DOCKER=start" if ci and _image_keys(image)[0] == "teamcity-agent" else "",
        "idle_days": str(dd["up"] // 86400) if st == "idle" else "",
        "idle_cpu": str(dd["cpu"]) if st == "idle" else ""}.items() if v}
    steps: list[dict] = []
    if ci and st != "none":
        steps.append({"id": "ci_idle" if st == "idle" else "ci_check", "cmd": "\n".join(f"sudo docker exec {q(n)} docker images -q | wc -l" for n in names)})
    if w is None:
        # агент не присылает метки compose: где файл, покажет команда, что поменять - словами
        steps += [{"id": "find", "cmd": compose_cmd.find_cmd(names)}] + [{"id": "hand_" + f} for f in fl]
        steps.append({"id": "hand_up"})
    elif not w:
        steps.append({"id": "run"})
    else:
        svcs = [str(cs[n]["compose"]["service"]) for n in names]
        args.update({"file": ", ".join(w["files"]), "dir": w["dir"], "svc": ", ".join(svcs)})
        if SOCK in fl and occupied_proxy:
            steps.append({"id": "sock_existing"})
            return {"key": names[0], "names": names, "args": args, "steps": steps}
        steps += _compose_steps(fl, c0, image, names, svcs, compose_cmd.Cmd(w))
    if ci and st != "none":
        steps.append({"id": "alt_ci", "alt": True})
    elif SOCK in fl:
        steps.append({"id": "alt_sock", "alt": True})
    return {"key": names[0], "names": names, "args": args, "steps": steps}


def fixes(items: list[dict], cs: dict[str, dict]) -> list[dict]:
    """Блоки шагов: контейнеры одного compose-проекта с одинаковыми ключами и образом - одним блоком
    (три CI-агента одного проекта - одни шаги на всех). У старого агента - тоже одним, по ключам."""
    groups: dict[tuple, list[dict]] = {}
    where: dict[tuple, dict | None] = {}
    # Имя docker-api зарезервировано шаблоном. Учитываем и остановленный сервис, и другой
    # compose-файл того же проекта. Это проверка коллизии, а не исключение из алерта.
    proxy_projects = {
        w["project"] or compose_cmd.project_name(w["dir"])
        for c in cs.values() if (w := compose_cmd.where(c)) and w["service"] == "docker-api"
    }
    for x in items:
        c = cs[x["name"]]
        w = compose_cmd.where(c)
        if w:
            src: tuple = (w["dir"], tuple(w["files"]), w["flags"])
        else:
            src = ("old",) if w is None else ("run", x["name"])
        k = src + (tuple(x["flags"]), tuple(x["caps"]), _image_keys(x["image"])[1], (x.get("dind") or {}).get("state", ""),
                   _sock_bind(c) if SOCK in x["flags"] else "", tuple(_host_binds(c)) if HOST in x["flags"] else ())
        groups.setdefault(k, []).append(x)
        where[k] = w
    blocks = []
    for key, group in groups.items():
        w = where[key]
        project = (w["project"] or compose_cmd.project_name(w["dir"])) if w else None
        blocks.append(_block(group, cs, w, project in proxy_projects))
    return blocks
