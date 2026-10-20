"""Старый образ внешнего прокси: на что обновлять, готовые команды под ноду (где compose-файл и строка
image - от helper'а sockfix-setup 0.2) и решение "оставить как есть" (proxyold:<контейнер>)."""
import time

import pytest

from app import docker_exposure as de
from app import setup_scripts

DAY = 86400
NOW = time.time()


def _rep(edge=None, image="lucaslorentz/caddy-docker-proxy:alpine", ports=None, sites=None, ts=None, **blk):
    c = {"name": "caddy-proxy-caddy-1", "image": image, "state": "running", "img_created": int(NOW - 480 * DAY),
         "ports": ports if ports is not None else [{"ip": "0.0.0.0", "pub": 443, "priv": 443, "proto": "tcp"},
                                                   {"ip": "0.0.0.0", "pub": 80, "priv": 80, "proto": "tcp"}]}
    rep = {"clock_unix": NOW, "docker": {"access": True, "containers": [c]},
           "web_services": [{"kind": "Caddy", "sites": sites if sites is not None else
                             ["shop.example.com", "api.example.com", "*.example.com"]}, {"kind": "nginx"}]}
    if edge is not None:
        rep["extras"] = {"sockfix": {"v": 1, "ts": ts if ts is not None else NOW - 60, "items": [],
                                     "compose": blk.get("compose", "2.35.1"), "compose1": blk.get("compose1", ""),
                                     "edges": [edge]}}
    return rep


EDGE = {"container": "caddy-proxy-caddy-1", "kind": "caddy", "image": "lucaslorentz/caddy-docker-proxy:alpine",
        "project": "caddy-proxy", "service": "caddy", "dir": "/app/caddy-proxy",
        "files": ["/app/caddy-proxy/docker-compose.yml"], "ok": True, "file": "/app/caddy-proxy/docker-compose.yml",
        "line": 5, "ref": "lucaslorentz/caddy-docker-proxy:alpine", "build": False}


def _fix(rep, mutes=None):
    [x] = de.old_view(rep, NOW, mutes or [])
    return x, {s["id"]: s for s in x["fix"]["steps"]}


@pytest.mark.parametrize("image,ver,built,want", [
    # закрепленная версия, как в роли caddy-proxy; вариант alpine сохраняется, ci-сборки - на релиз
    ("lucaslorentz/caddy-docker-proxy:alpine", "", 0, "lucaslorentz/caddy-docker-proxy:2.13.1-alpine"),
    ("lucaslorentz/caddy-docker-proxy:2.10.0", "", 0, "lucaslorentz/caddy-docker-proxy:2.13.1"),
    ("lucaslorentz/caddy-docker-proxy@sha256:abc", "", 0, "lucaslorentz/caddy-docker-proxy:2.13.1"),
    ("registry.example.com:5000/lucaslorentz/caddy-docker-proxy:ci-alpine", "", 0,
     "registry.example.com:5000/lucaslorentz/caddy-docker-proxy:2.13.1-alpine"),
    # зеркало Docker Hub остается зеркалом
    ("mirror.example.com/library/caddy:2.6.2-alpine", "", 0, "mirror.example.com/library/caddy:2.11-alpine"),
    ("caddy:2", "", 0, "caddy:2.11"),
    # traefik остается в своей ветке: v2 по метке версии, тегу или дате сборки (до v3.0 latest был v2)
    ("traefik:latest", "v2.5.5", 0, "traefik:v2.11"),
    ("traefik:latest", "", 1650000000, "traefik:v2.11"),
    ("traefik:latest", "", 1720000000, "traefik:v3"),
    ("traefik:v2.11.20", "", 0, "traefik:v2.11"),
    ("traefik:v1.7", "", 0, ""),
    ("jwilder/nginx-proxy:alpine", "", 0, "nginxproxy/nginx-proxy:1.11-alpine"),
    # своя сборка (caddy с плагинами) - тег не поменять; тег новее таблицы - назад не советуем
    ("ghcr.io/acme/caddy:latest", "", 0, ""),
    ("lucaslorentz/caddy-docker-proxy:2.14.0", "", 0, ""),
])
def test_target(image, ver, built, want):
    assert de.target(image, ver, built) == want


def test_ready_commands_for_this_node():
    x, st = _fix(_rep(EDGE))
    assert x["key"] == "proxyold:caddy-proxy-caddy-1" and x["muted"] is False
    assert x["fix"]["target"] == "lucaslorentz/caddy-docker-proxy:2.13.1-alpine" and "why" not in x["fix"]
    assert list(st) == ["codes", "apply", "check", "rollback"]
    loop = ("for d in api.example.com shop.example.com; do echo \"$d $(curl -s -o /dev/null -m 10 -w '%{http_code}' "
            "--resolve $d:443:127.0.0.1 https://$d/)\"; done")
    assert st["codes"]["cmd"] == loop + " | tee /tmp/kervax-codes-caddy-proxy-caddy-1.txt"
    assert st["apply"]["arg"] == "docker-compose.yml:5"
    assert st["apply"]["cmd"] == (
        "cd /app/caddy-proxy\n"
        "cp docker-compose.yml docker-compose.yml.bak-kervax-image\n"
        "sed -i '5s|lucaslorentz/caddy-docker-proxy:alpine|lucaslorentz/caddy-docker-proxy:2.13.1-alpine|' docker-compose.yml\n"
        # строка после правки и вывод, вышло ли: sed, не нашедший тег в этой строке, молчит
        "sed -n 5p docker-compose.yml\n"
        "sed -n 5p docker-compose.yml | grep -qF -- lucaslorentz/caddy-docker-proxy:2.13.1-alpine"
        ' && echo "образ заменен" || echo "НЕ ЗАМЕНЕНО: строка image другая, поправьте ее руками"\n'
        "docker compose -p caddy-proxy -f docker-compose.yml pull caddy\n"
        "docker compose -p caddy-proxy -f docker-compose.yml up -d --no-deps caddy")
    assert st["check"]["cmd"].split("\n")[-2] == loop + " | diff /tmp/kervax-codes-caddy-proxy-caddy-1.txt - && echo OK"
    # и последней строкой - на новом ли образе прокси, а не только Up
    assert st["check"]["cmd"].split("\n")[-1] == (
        "docker inspect -f '{{.Config.Image}}' caddy-proxy-caddy-1 | grep -qxF -- lucaslorentz/caddy-docker-proxy:2.13.1-alpine"
        ' && echo "контейнер на новом образе" || echo "НЕ ОБНОВЛЕН: контейнер на прежнем образе, смотрите вывод выше"')
    assert "docker compose -p caddy-proxy -f docker-compose.yml ps caddy" in st["check"]["cmd"]
    assert st["rollback"]["cmd"] == (
        "cd /app/caddy-proxy\ncp docker-compose.yml.bak-kervax-image docker-compose.yml\n"
        "docker compose -p caddy-proxy -f docker-compose.yml up -d --no-deps caddy")


def test_sed_takes_the_old_tag_literally_and_ports_from_the_container():
    ed = {**EDGE, "ref": "traefik:v2.5.5", "image": "traefik:v2.5.5", "kind": "traefik", "service": "traefik",
          "files": ["/srv/edge/compose.yml", "/srv/edge/compose.prod.yml"], "file": "/srv/edge/compose.prod.yml",
          "dir": "/srv/edge", "project": "edge"}
    rep = _rep(ed, image="traefik:v2.5.5", ports=[{"ip": "203.0.113.5", "pub": 8443, "priv": 443, "proto": "tcp"}])
    rep["web_services"] = [{"kind": "Traefik", "sites": ["app.example.com"]}]
    x, st = _fix(rep)
    assert "sed -i '5s|traefik:v2\\.5\\.5|traefik:v2.11|' compose.prod.yml" in st["apply"]["cmd"]
    assert "docker compose -p edge -f compose.yml -f compose.prod.yml pull traefik" in st["apply"]["cmd"]
    assert "--resolve $d:8443:203.0.113.5 https://$d:8443/" in st["codes"]["cmd"]


def test_same_tag_build_custom_and_old_compose():
    # тег тот же, образ скачан давно: прежний - под тегом kervax-old, откат - вернуть тег
    ed = {**EDGE, "ref": "caddy:2.11", "image": "caddy:2.11"}
    x, st = _fix(_rep(ed, image="caddy:2.11"))
    assert list(st) == ["codes", "apply_same", "check", "rollback_same"]
    assert "docker tag caddy:2.11 caddy:kervax-old" in st["apply_same"]["cmd"]
    assert "docker tag caddy:kervax-old caddy:2.11" in st["rollback_same"]["cmd"]
    # образ собирается из Dockerfile: только пересборка
    x, st = _fix(_rep({**EDGE, "build": True}))
    assert "docker compose -p caddy-proxy -f docker-compose.yml build --pull caddy" in st["build"]["cmd"]
    # своя сборка: ни sed, ни команд
    ed = {**EDGE, "ref": "ghcr.io/acme/caddy:latest", "image": "ghcr.io/acme/caddy:latest"}
    x, st = _fix(_rep(ed, image="ghcr.io/acme/caddy:latest"))
    assert x["fix"]["target"] == "" and st["custom"] == {"id": "custom", "arg": "docker-compose.yml:5"}
    assert "apply" not in st and "rollback" not in st
    # на ноде только docker-compose v1
    x, st = _fix(_rep(EDGE, compose="", compose1="1.29.2"))
    assert "docker-compose -p caddy-proxy -f docker-compose.yml pull caddy" in st["apply"]["cmd"]


def test_without_ready_data():
    # helper'а нет (или старый, или отчет устарел): где compose - командой, остальное руками
    for rep in (_rep(), _rep(EDGE, ts=NOW - 2 * 3600)):
        x, st = _fix(rep)
        assert x["fix"]["why"] == "nohelper" and list(st) == ["codes", "find", "manual"]
        assert st["find"]["cmd"].startswith("docker inspect -f '{{index .Config.Labels \"com.docker.compose.project.working_dir\"}}")
        assert st["find"]["cmd"].endswith(" caddy-proxy-caddy-1") and st["manual"]["arg"].endswith(":2.13.1-alpine")
    # helper не разобрал файл (образ из переменной), но проект знает: pull и up даем
    x, st = _fix(_rep({**EDGE, "ok": False, "why": "var", "line": None, "ref": None, "file": None}))
    assert x["fix"]["why"] == "var" and "up -d --no-deps caddy" in st["manual"]["cmd"] and "check" in st
    # docker run без compose
    x, st = _fix(_rep({"container": "caddy-proxy-caddy-1", "ok": False, "why": "nocompose"}))
    assert st["run"]["cmd"] == "docker pull lucaslorentz/caddy-docker-proxy:2.13.1-alpine"
    # доменов не знаем - без шага кодов
    x, st = _fix(_rep(EDGE, sites=[]))
    assert "codes" not in st and "diff" not in st["check"]["cmd"]


def test_decision_keep_as_is():
    rep = _rep(EDGE)
    mutes = ["proxyold:caddy-proxy-caddy-1"]
    x, _st = _fix(rep, mutes)
    assert x["muted"] is True
    assert de.old_live(rep, NOW, mutes) == [] and len(de.old_live(rep, NOW, [])) == 1
    # helper нужен для команд обновления, пока решения нет
    bare = {k: v for k, v in rep.items() if k != "extras"}
    assert setup_scripts.setup_needed("sockfix-setup", bare)
    assert not setup_scripts.setup_needed("sockfix-setup", bare, mutes)


async def test_decision_leaves_the_ansible_group(client, auth_headers):
    r = await client.post("/api/servers", json={"name": "edge-a"}, headers=auth_headers)
    token, sid = r.json()["token"], r.json()["server"]["id"]
    rep = {"hostname": "edge-a", "os": "Ubuntu", "agent_version": "2.39", "cpu_percent": 5, "mem_used": 1,
           "mem_total": 2, **{k: v for k, v in _rep(EDGE).items() if k != "clock_unix"}}
    r = await client.post("/api/agent/report", json=rep, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    r = await client.post("/api/settings/ansible", headers=auth_headers)
    ah = {"Authorization": f"Bearer {r.json()['token']}"}

    async def state():
        srv = [x for x in (await client.get("/api/servers", headers=auth_headers)).json() if x["id"] == sid][0]
        row = {x["name"]: x for x in (await client.get("/api/ansible/servers", headers=ah)).json()["servers"]}["edge-a"]
        return srv["proxy_outdated"][0], row["issues"]

    p, issues = await state()
    assert p["muted"] is False and p["fix"]["steps"][1]["id"] == "apply" and "proxy_old" in issues
    r = await client.patch(f"/api/servers/{sid}", json={"alert_mutes": ["proxyold:caddy-proxy-caddy-1"]},
                           headers=auth_headers)
    assert r.status_code == 200, r.text
    p, issues = await state()
    assert p["muted"] is True and "proxy_old" not in issues
