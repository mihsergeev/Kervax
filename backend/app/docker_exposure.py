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
import re
import shlex
import time

from app import tg_fmt
from app.compose_cmd import verdict

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


# Старый образ можно и не обновлять: прокси под софт, который на новой версии не заработает, или
# стенд, которому скоро конец. Тогда это решение человека "оставить как есть" (alert_mutes
# "proxyold:<контейнер>"): пункт уходит из "Требует действий" и группы ansible kervax_proxy_old,
# в карточке виден приглушенным с кнопкой "вернуть". Не заглушенный алерт - в "Заглушено" его нет.
OLD_MUTE = "proxyold:"


def old_live(rep: dict, now: float, mutes: list[str] | None) -> list[dict]:
    """Старые образы, по которым нет решения "оставить как есть": для ansible и нужен ли helper."""
    got = set(mutes or [])
    return [p for p in outdated(rep, now) if OLD_MUTE + p["name"] not in got]


def old_view(rep: dict, now: float, mutes: list[str] | None) -> list[dict]:
    """Для карточки: старые образы с решением (key, muted) и шагами обновления под ноду (fix)."""
    got = set(mutes or [])
    eds = edges(rep)
    out = []
    for p in outdated(rep, now):
        key = OLD_MUTE + p["name"]
        out.append({**p, "key": key, "muted": key in got, "fix": upgrade(p, rep, eds)})
    return out


# На что обновлять (сверено с Docker Hub 19.10.2026). caddy-docker-proxy - закрепленная версия, как
# в роли caddy-proxy: 2.13.1 - последний релиз (сборка 02.07.2026, внутри caddy 2.11.4). У остальных
# последняя ветка, патчи в ней приезжают с pull. traefik v2 остается на v2: в v3 другие метки и
# настройки, это переезд, а не обновление. Вариант образа (alpine) сохраняется.
_LATEST = {"cdp": "2.13.1", "caddy": "2.11", "nginx-proxy": "1.11", "docker-gen": "0.17"}
_TRAEFIK = {2: "v2.11", 3: "v3"}
_TRAEFIK3 = 1714348800  # 29.04.2024: вышел traefik v3.0, latest с тех пор - v3
_OFFICIAL = ([], ["library"])


def _split(image: str) -> tuple[str, list[str], str]:
    """'mirror.example.com/library/caddy:2.6-alpine@sha256:..' -> ('mirror.example.com/', ['library', 'caddy'],
    '2.6-alpine'): реестр (зеркало) сохраняем, меняем только тег."""
    name = (image or "").strip().split("@", 1)[0]
    tag = ""
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        tag = last.split(":", 1)[1]
        name = name[: len(name) - len(tag) - 1]
    parts = [x for x in name.split("/") if x]
    reg = ""
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        reg = parts.pop(0) + "/"
    return reg, parts, tag


def _vt(s: str) -> tuple[int, ...]:
    """'v2.5.5' -> (2, 5, 5), '2.10-alpine' -> (2, 10), 'latest' -> ()"""
    m = re.match(r"v?(\d+(?:\.\d+)*)", s or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def target(image: str, version: str = "", built: int = 0) -> str:
    """Свежий образ того же прокси: тот же реестр и вариант, новый тег. "" - образ не из репозитория
    апстрима (своя сборка, например caddy с плагинами: тег не поменять, только пересобрать), traefik v1
    (на v2 - переезд) или тег новее того, что знает таблица выше (назад не советуем)."""
    reg, path, tag = _split(image)
    if not path:
        return ""
    ns, name = [x.lower() for x in path[:-1]], path[-1].lower()
    alp = "-alpine" if "alpine" in tag.lower() else ""
    if name == "caddy-docker-proxy" and ns == ["lucaslorentz"]:
        new = _LATEST["cdp"] + alp
    elif name == "caddy" and ns in _OFFICIAL:
        new = _LATEST["caddy"] + alp
    elif name == "traefik" and ns in _OFFICIAL:
        # ветку говорит метка версии, потом тег; у latest без метки - дата сборки
        major = (_vt(version) or _vt(tag) or ((3,) if built >= _TRAEFIK3 else (2,)))[0]
        new = _TRAEFIK.get(major, "")
    elif name in ("nginx-proxy", "docker-gen") and ns in (["nginxproxy"], ["jwilder"]):
        path = ["nginxproxy", name]  # jwilder/nginx-proxy переехал в nginxproxy
        new = _LATEST[name] + (alp if name == "nginx-proxy" else "")
    else:
        return ""
    cur, want = _vt(tag), _vt(new)
    if not new or (cur and cur[: len(want)] != want and cur > want):
        return ""
    return f"{reg}{'/'.join(path)}:{new}"


_EDGE_KEYS = ("ok", "why", "why_detail", "kind", "image", "project", "service", "dir", "files", "file", "line", "ref",
              "build")


def edges(rep: dict) -> dict[str, dict]:
    """Где compose-файл задает образ внешнего прокси (helper sockfix-setup 0.2), по имени контейнера:
    файл, строка и значение image как написано; cli - чем звать compose на этой ноде."""
    b = ((rep or {}).get("extras") or {}).get("sockfix")
    if not isinstance(b, dict) or b.get("v") != 1 or not isinstance(b.get("edges"), list):
        return {}
    ref = float((rep or {}).get("clock_unix") or 0) or time.time()
    if ref - float(b.get("ts") or 0) > FIX_MAX_AGE:
        return {}
    cli = "docker-compose" if b.get("compose1") and not b.get("compose") else "docker compose"
    out: dict[str, dict] = {}
    for it in b["edges"]:
        if isinstance(it, dict) and it.get("container"):
            ed = {k: it[k] for k in _EDGE_KEYS if it.get(k) not in (None, "", [])}
            ed["ok"] = bool(it.get("ok") and it.get("project") and it.get("service") and it.get("dir")
                            and it.get("files") and (it.get("line") or it.get("build")))
            out[str(it["container"])] = {**ed, "cli": cli}
    return out


# Коды ответов доменов прокси через него самого, до и после: curl --resolve на адрес, где он слушает
# 443 (или 80), мимо DNS и CDN. Домены - из web_services агента (у caddy-docker-proxy - из меток).
_WEB_KIND = {"caddy": "Caddy", "traefik": "Traefik", "nginx-proxy": "nginx"}
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
CODES_MAX = 40


def _codes(p: dict, rep: dict) -> str:
    """Цикл "домен код" по доменам прокси, "" - доменов не знаем."""
    web = _WEB_KIND.get(p["kind"])
    sites = sorted({str(s).lower() for w in rep.get("web_services") or []
                    if isinstance(w, dict) and w.get("kind") == web
                    for s in w.get("sites") or [] if _HOST.match(str(s).lower())})
    if not sites:
        return ""
    c = next((c for c in (rep.get("docker") or {}).get("containers") or []
              if isinstance(c, dict) and c.get("name") == p["name"]), {})
    ports = [x for x in c.get("ports") or [] if isinstance(x, dict) and (x.get("proto") or "tcp") == "tcp"]
    tls = next((x for x in ports if x.get("priv") == 443), None)
    pp = tls or next((x for x in ports if x.get("priv") == 80), None)
    ip, port, scheme = "127.0.0.1", 443, "https"  # сеть хоста или порты не опубликованы
    if pp:
        scheme = "https" if pp is tls else "http"
        port = int(pp.get("pub") or 0) or (443 if pp is tls else 80)
        ip = str(pp.get("ip") or "")
        if ip in ("", "0.0.0.0") or ":" in ip:
            ip = "127.0.0.1"
    url = f"{scheme}://$d/" if port in (80, 443) else f"{scheme}://$d:{port}/"
    return (f"for d in {' '.join(sites[:CODES_MAX])}; do echo \"$d $(curl -s -o /dev/null -m 10 -w '%{{http_code}}' "
            f"--resolve $d:{port}:{ip} {url})\"; done")


def _rel(f: str, d: str) -> str:
    return os.path.basename(f) if os.path.dirname(f) == d.rstrip("/") else f


_SED_META = re.compile(r"[.*^$|\\\[\]]")


def upgrade(p: dict, rep: dict, eds: dict[str, dict] | None = None) -> dict:
    """Как обновить старый образ прокси p на этой ноде: {"target", "why"?, "steps"}, шаги - [{"id",
    "cmd"?, "arg"?}], тексты во фронте по id. Команды - под ноду: каталог, compose-файлы, проект,
    сервис и строка image из отчета helper'а; после них пункт уходит сам. Панель ничего не применяет:
    у агента к Docker только чтение и перезапуск."""
    q = shlex.quote
    ed = (edges(rep) if eds is None else eds).get(p["name"]) or {}
    ref = str(ed.get("ref") or p["image"])
    tgt = target(ref, p.get("version") or "", int(p.get("built") or 0))
    out: dict = {"target": tgt, "steps": []}
    steps = out["steps"]
    loop = _codes(p, rep)
    saved = f"/tmp/kervax-codes-{re.sub(r'[^A-Za-z0-9_.-]', '_', p['name'])}.txt"
    if loop:
        steps.append({"id": "codes", "cmd": f"{loop} | tee {saved}"})
    proj, svc, d = str(ed.get("project") or ""), str(ed.get("service") or ""), str(ed.get("dir") or "")
    names = [_rel(str(f), d) for f in ed.get("files") or []]
    dc = f"{ed.get('cli') or 'docker compose'} -p {q(proj)} " + " ".join(f"-f {q(n)}" for n in names)
    cd = f"cd {q(d)}"
    pull_up = [f"{dc} pull {q(svc)}", f"{dc} up -d --no-deps {q(svc)}"]
    check = {"id": "check", "cmd": "\n".join(
        [cd, f"{dc} ps {q(svc)}", f"docker logs --since 5m {q(p['name'])} 2>&1 | tail -n 20"]
        + ([f"{loop} | diff {saved} - && echo OK"] if loop else []))}
    # где тег меняют в файле (sed или руками): проверка заодно скажет, на новом ли образе прокси, а не
    # только Up - sed, не нашедший строку, молчит, и up -d поднимал прежний образ
    on_new = {**check, "cmd": check["cmd"] + "\n" + verdict(
        f"docker inspect -f '{{{{.Config.Image}}}}' {q(p['name'])} | grep -qxF -- {q(tgt)}",
        "контейнер на новом образе", "НЕ ОБНОВЛЕН: контейнер на прежнем образе, смотрите вывод выше")} if tgt else check
    if not ed.get("ok"):
        # helper не прислал, где образ (не стоит, старый, не разобрал файл): что знаем - руками
        out["why"] = str(ed.get("why") or "nohelper")
        if out["why"] == "nocompose":
            steps.append({"id": "run", "cmd": f"docker pull {q(tgt)}" if tgt else None, "arg": tgt})
        elif proj and svc and d and names:
            steps += [{"id": "manual", "arg": tgt, "cmd": "\n".join([cd] + pull_up)}, on_new]
        else:
            labels = " ".join("{{index .Config.Labels \"com.docker.compose.%s\"}}" % k
                              for k in ("project.working_dir", "project.config_files", "project", "service"))
            steps += [{"id": "find", "cmd": f"docker inspect -f {q(labels)} {q(p['name'])}"},
                      {"id": "manual", "arg": tgt}]
        out["steps"] = [{k: v for k, v in s.items() if v not in (None, "")} for s in steps]
        return out
    f = _rel(str(ed.get("file") or ""), d)
    at = f"{f}:{ed.get('line')}"
    if ed.get("build"):
        # образ собирается здесь же: тег не поменять, только FROM в Dockerfile и пересборка
        steps += [{"id": "build", "arg": tgt, "cmd": "\n".join([cd, f"{dc} build --pull {q(svc)}", pull_up[1]])}, check]
    elif not tgt:
        steps += [{"id": "custom", "arg": at}]
    elif tgt.lower() == ref.lower():
        # тег тот же (traefik:v2.11 скачан год назад): прежний образ - под тегом kervax-old для отката
        old = tgt.rsplit(":", 1)[0] + ":kervax-old"
        steps += [{"id": "apply_same", "cmd": "\n".join([cd, f"docker tag {q(tgt)} {q(old)}"] + pull_up)}, check,
                  {"id": "rollback_same", "cmd": "\n".join([cd, f"docker tag {q(old)} {q(tgt)}", pull_up[1]])}]
    else:
        # sed меняет значение только в той строке, где helper нашел image: прочие сервисы с тем же
        # образом и закомментированные строки не трогает; точки и прочее в старом теге - буквально
        bak = f + ".bak-kervax-image"
        sed = "%ss|%s|%s|" % (ed.get("line"), _SED_META.sub(r"\\\g<0>", ref), tgt)
        line = f"sed -n {q(str(ed.get('line')) + 'p')} {q(f)}"
        apply = [cd, f"cp {q(f)} {q(bak)}", f"sed -i {q(sed)} {q(f)}", line,
                 verdict(f"{line} | grep -qF -- {q(tgt)}", "образ заменен",
                         "НЕ ЗАМЕНЕНО: строка image другая, поправьте ее руками")] + pull_up
        steps += [{"id": "apply", "arg": at, "cmd": "\n".join(apply)}, on_new, {"id": "rollback", "cmd": "\n".join([cd, f"cp {q(bak)} {q(f)}", pull_up[1]])}]
    return out


def text(items: list[dict]) -> str:
    """Строка алерта: "caddy-proxy-caddy-1 (caddy) держит docker-сокет: взлом прокси - root на хосте"."""
    if not items:
        return ""
    names = ", ".join(f"{i['name']} ({i['kind']})" for i in items[:3])
    more = f" и еще {len(items) - 3}" if len(items) > 3 else ""
    verb = "держит" if len(items) == 1 else "держат"
    return f"{names}{more} {verb} docker-сокет: взлом прокси, который смотрит в интернет, - root на хосте"


def tg_short(items: list[dict]) -> tuple[str, list[str]] | None:
    """Алерт docker_sock (вход - exposed()): "Внешний прокси держит docker-сокет", "caddy-proxy-caddy-1"."""
    if not items:
        return None
    what = "Внешний прокси держит docker-сокет" if len(items) == 1 else "Внешние прокси держат docker-сокет"
    return what, [tg_fmt.names(i["name"] for i in items)]
