"""Готовый compose без docker-сокета (helper sockfix-setup): правка файла на ноде, разница и команды
в карточке. Правщик - python внутри helper'а, тест берет его оттуда же, чтобы не разошлись."""
import re
import time
from pathlib import Path

import pytest

from app import docker_exposure, setup_scripts

SRC = (Path(__file__).resolve().parents[2] / "agent" / "sockfix-setup.sh").read_text(encoding="utf-8")


def _helper_py():
    body = SRC.split("<<'PY' || exit 0\n", 1)[1].split("\nPY\n", 1)[0]
    assert body.rstrip().endswith("main(sys.argv[1])")
    ns: dict = {}
    exec(compile(body.rstrip()[: -len("main(sys.argv[1])")], "sockfix", "exec"), ns)
    return ns


SF = _helper_py()

PLAIN = """services:
  caddy:
    image: lucaslorentz/caddy-docker-proxy:2.12.0 # pinned
    restart: always
    ports:
      - "80:80"
      - "443:443"
    environment:
      - CADDY_INGRESS_NETWORKS=caddy
    networks:
      - caddy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - ./data:/data
networks:
  caddy:
    external: true
"""


def fix(text, svc="caddy", nets=("caddy",), has=True):
    return SF["fix_caddy"](text, svc, list(nets), has, 989, "/var/run/docker.sock")


def test_plain_compose():
    out = fix(PLAIN)
    # комментарии и порядок ключей на месте, сокет ушел из volumes, ./data остался
    assert "image: lucaslorentz/caddy-docker-proxy:2.12.0 # pinned\n" in out
    assert "      - ./data:/data\n" in out and "- /var/run/docker.sock:/var/run/docker.sock\n" not in out
    assert "      - CADDY_INGRESS_NETWORKS=caddy\n      - DOCKER_HOST=tcp://docker-api:2375\n" in out
    assert "      - caddy\n      - docker-api\n" in out
    # прокси сокета с настоящим gid, пускает только caddy, сеть внутренняя
    assert '  docker-api:\n' in out and 'user: "65534:989"' in out and "- -allowfrom=caddy\n" in out
    assert "      - /var/run/docker.sock:/var/run/docker.sock:ro\n" in out
    assert out.endswith("networks:\n  caddy:\n    external: true\n  docker-api:\n    internal: true\n")
    # только чтение: inspect, exec, создание контейнеров не пропускаются
    get = re.search(r"-allowGET=(\S+)", out).group(1)
    assert "containers/json" in get and "exec" not in get and "create" not in get and "/json|" not in get.split("containers/json")[1][:1]


def test_map_env_inline_networks_long_volume_and_4_spaces():
    text = """version: "3.8"
services:
    proxy:
        image: lucaslorentz/caddy-docker-proxy:ci-alpine
        environment:
            CADDY_INGRESS_NETWORKS: web
            ACME_TOKEN: s3cr3t
        networks: [web]
        volumes:
            - type: bind
              source: /var/run/docker.sock
              target: /var/run/docker.sock
            - caddy_data:/data
volumes:
    caddy_data: {}
networks:
    web:
        external: true
"""
    out = fix(text, svc="proxy", nets=("web",))
    assert "            DOCKER_HOST: tcp://docker-api:2375\n" in out
    assert "        networks: [web, docker-api]\n" in out
    assert "type: bind" not in out and "            - caddy_data:/data\n" in out
    assert "    docker-api:\n        # read-only" in out and "- -allowfrom=proxy\n" in out
    assert out.endswith("    web:\n        external: true\n    docker-api:\n        internal: true\n")


def test_no_env_no_networks_and_only_the_socket():
    text = "services:\n  caddy:\n    image: lucaslorentz/caddy-docker-proxy:2.13.1\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock:ro\n"
    out = fix(text, nets=("site_default",), has=False)
    # volumes целиком ушел, ingress-сети взяты у работающего контейнера, сеть по умолчанию осталась
    caddy = out.split("  docker-api:\n")[0]
    assert "volumes:" not in caddy and "docker.sock" not in caddy
    assert "    environment:\n      - DOCKER_HOST=tcp://docker-api:2375\n      - CADDY_INGRESS_NETWORKS=site_default\n" in out
    assert "    networks:\n      - default\n      - docker-api\n" in out
    assert out.endswith("networks:\n  docker-api:\n    internal: true\n")


@pytest.mark.parametrize("change,code", [
    (lambda t: t.replace("restart: always", "restart: always\n    logging: &log {}"), "anchors"),
    (lambda t: t.replace("/var/run/docker.sock:/var/run/docker.sock", "/var/run:/var/run"), "rundir"),
    (lambda t: t.replace("environment:\n      - CADDY_INGRESS_NETWORKS=caddy", "environment: [CADDY_INGRESS_NETWORKS=caddy]"), "inline"),
    (lambda t: t.replace("- CADDY_INGRESS_NETWORKS=caddy", "- CADDY_INGRESS_NETWORKS=caddy\n      - DOCKER_HOST=unix:///x"), "dockerhost"),
    (lambda t: t.replace("  caddy:\n    image", "  web:\n    image"), "nosvc"),
    (lambda t: t.replace("restart: always", "restart: always\n    network_mode: host"), "networkmode"),
    (lambda t: t.replace("    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n", "    volumes:\n"), "elsewhere"),
    (lambda t: t + "  docker-api:\n    image: x\n", None),
    (lambda t: "caddy:\n  image: x\n", "v1"),
])
def test_what_it_does_not_edit(change, code):
    text = change(PLAIN)
    if code is None:
        # docker-api встал в конец файла после networks: это уже не services, сервис не дублируем
        text = PLAIN.replace("networks:\n  caddy:", "  docker-api:\n    image: x\nnetworks:\n  caddy:")
        code = "dupsvc"
    with pytest.raises(SF["Bail"]) as e:
        fix(text)
    assert e.value.code == code


def test_document_markers():
    # файл кончается маркером конца документа: сеть docker-api дописана до него, а не во второй документ
    out = fix(PLAIN + "...\n")
    assert out.endswith("networks:\n  caddy:\n    external: true\n  docker-api:\n    internal: true\n...\n")
    # маркер начала и директива перед ним остаются на месте, правка та же
    out = fix("%YAML 1.2\n---\n" + PLAIN + "...\n# end\n")
    assert out.startswith("%YAML 1.2\n---\nservices:\n") and out.endswith("    internal: true\n...\n# end\n")
    assert out[len("%YAML 1.2\n---\n"):-len("...\n# end\n")] == fix(PLAIN)
    # без networks наверху блок сети тоже встает перед маркером
    text = "---\nservices:\n  caddy:\n    image: x\n    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n...\n"
    assert fix(text).endswith("networks:\n  docker-api:\n    internal: true\n...\n")
    # пустой документ рядом не мешает
    assert fix("---\n# empty\n---\n" + PLAIN) == "---\n# empty\n---\n" + fix(PLAIN)


@pytest.mark.parametrize("text,code", [
    (PLAIN + "---\nservices:\n  web:\n    image: nginx\n", "multidoc"),
    (PLAIN + "...\nnetworks:\n  extra: {}\n", "multidoc"),
    ("--- services:\n", "layout"),
])
def test_several_documents_are_not_guessed(text, code):
    with pytest.raises(SF["Bail"]) as e:
        fix(text)
    assert e.value.code == code
    if code == "multidoc":
        with pytest.raises(SF["Bail"]):
            SF["image_at"](text, "caddy")


def test_image_line_after_start_marker():
    assert SF["image_at"]("---\n" + PLAIN + "...\n", "caddy")[:2] == (4, "lucaslorentz/caddy-docker-proxy:2.12.0")


def _validate_with(monkeypatch, tmp_path, answers):
    """validate() с подставным docker compose: answers - по очереди (код, stdout, stderr)"""
    calls = []

    def run(cmd, timeout):
        calls.append(cmd)
        rc, out, err = answers[len(calls) - 1]
        return SF["subprocess"].CompletedProcess(cmd, rc, out, err)
    monkeypatch.setitem(SF, "run", run)
    SF["validate"](str(tmp_path / "f.yml"), str(tmp_path), "edge", "caddy")
    return calls


YAML_OK = """name: edge
services:
  caddy:
    environment:
      DOCKER_HOST: tcp://docker-api:2375
    image: lucaslorentz/caddy-docker-proxy:2.12.0
    networks:
      caddy: null
      docker-api: null
  docker-api:
    image: wollomatic/socket-proxy:1.13.1
    volumes:
      - type: bind
        source: /var/run/docker.sock
        target: /var/run/docker.sock
networks:
  docker-api:
    internal: true
"""


def test_validate_refuses_what_compose_refused(monkeypatch, tmp_path):
    err = "yaml: line 38: did not find expected <document start>\n"
    with pytest.raises(SF["Bail"]) as e:
        _validate_with(monkeypatch, tmp_path, [(1, "", err)])
    assert e.value.code == "validate" and "document start" in e.value.detail
    # старый compose без --format: ошибка разбора и там отказ
    with pytest.raises(SF["Bail"]) as e:
        _validate_with(monkeypatch, tmp_path, [(16, "", "unknown flag: --format\n"),
                                               (15, "", 'service "caddy" refers to undefined network docker-api: invalid compose project\n')])
    assert e.value.code == "validate" and "undefined network" in e.value.detail


def test_validate_env_as_list(monkeypatch, tmp_path):
    # compose 2.24 печатает environment списком, как в файле: это тоже DOCKER_HOST
    cfg = {"services": {"caddy": {"environment": ["CADDY_INGRESS_NETWORKS=caddy", "DOCKER_HOST=tcp://docker-api:2375"],
                                  "volumes": [{"source": "./data"}], "networks": {"caddy": None, "docker-api": None}},
                        "docker-api": {}}}
    _validate_with(monkeypatch, tmp_path, [(0, SF["json"].dumps(cfg), "")])
    cfg["services"]["caddy"]["environment"][1] = "DOCKER_HOST=tcp://127.0.0.1:2375"
    with pytest.raises(SF["Bail"]) as e:
        _validate_with(monkeypatch, tmp_path, [(0, SF["json"].dumps(cfg), "")])
    assert e.value.detail == "DOCKER_HOST did not land"


def test_validate_old_compose_reads_the_yaml(monkeypatch, tmp_path):
    flag = (16, "", "unknown flag: --format\n")
    calls = _validate_with(monkeypatch, tmp_path, [flag, (0, YAML_OK, "")])
    assert calls[1][-1] == "config" and "--format" not in calls[1]
    # принял, но сокет остался у caddy или DOCKER_HOST не встал - тоже отказ
    for bad, why in ((YAML_OK.replace("    image: lucaslorentz", "    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n    image: lucaslorentz"), "socket"),
                     (YAML_OK.replace("tcp://docker-api:2375", "tcp://127.0.0.1:2375"), "DOCKER_HOST"),
                     (YAML_OK.replace("      docker-api: null\n", ""), "docker-api"),
                     ("services: {}\n", "no service")):
        with pytest.raises(SF["Bail"]) as e:
            _validate_with(monkeypatch, tmp_path, [flag, (0, bad, "")])
        assert e.value.code == "validate" and why in e.value.detail


def test_refused_file_does_not_stay(monkeypatch, tmp_path):
    src = tmp_path / "docker-compose.yml"
    src.write_text(PLAIN, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "edge.yml").write_text("old\n", encoding="utf-8")
    monkeypatch.setitem(SF, "OUT_DIR", str(out))
    sock = tmp_path / "docker.sock"
    sock.write_text("", encoding="utf-8")
    monkeypatch.setitem(SF, "SOCKS", (str(sock),))
    c = _cont("edge-caddy-1", "lucaslorentz/caddy-docker-proxy:2.12.0", [str(src)], str(tmp_path))
    c["Mounts"] = [{"Source": str(sock)}]
    made = set()

    def refuse(path, wd, project, svc):
        assert path.endswith(".new.yml") and open(path, encoding="utf-8").read().endswith("internal: true\n")
        raise SF["Bail"]("validate", "did not find expected <document start>")
    monkeypatch.setitem(SF, "validate", refuse)
    it = SF["one"](c, "2.33.1", made)
    assert not it["ok"] and it["why"] == "validate" and "fixed" not in it and not made
    # старый файл проекта не подменен; что не в made, main() убирает из каталога
    assert (out / "edge.yml").read_text(encoding="utf-8") == "old\n"
    monkeypatch.setitem(SF, "validate", lambda *a: None)
    it = SF["one"](c, "2.33.1", made)
    assert it["ok"] and it["fixed"] == str(out / "edge.yml") and made == {"edge.yml"}
    assert sorted(x.name for x in out.iterdir()) == ["edge.yml"]


def test_diff_masks_secrets():
    m = SF["mask"]
    assert m("            ACME_TOKEN: s3cr3t") == "            ACME_TOKEN: ***"
    assert m("+      - CF_API_KEY=abc") == "+      - CF_API_KEY=***"
    assert m("     image: x") == "     image: x"


def _rep(items, ts=None, name="caddy-proxy-caddy-1"):
    now = time.time()
    return {"clock_unix": now,
            "docker": {"containers": [{"name": name, "image": "lucaslorentz/caddy-docker-proxy:2.12.0", "state": "running",
                                       "binds": ["/var/run/docker.sock", "/srv/caddy/data"]}]},
            "extras": {"sockfix": {"v": 1, "ts": ts if ts is not None else now, "items": items}}}


READY = {"container": "caddy-proxy-caddy-1", "kind": "caddy", "ok": True, "project": "caddy-proxy", "service": "caddy",
         "dir": "/srv/caddy", "file": "/srv/caddy/docker-compose.yml", "fixed": "/var/lib/kervax/sockfix/caddy-proxy.yml",
         "diff": "--- a\n+++ b\n-      - /var/run/docker.sock:/var/run/docker.sock", "gid": 989, "nets": ["caddy"]}


def test_card_gets_the_ready_fix_and_commands():
    [x] = docker_exposure.view(_rep([READY]))
    fx = x["fix"]
    assert fx["ok"] and fx["diff"].startswith("--- a")
    assert fx["apply"] == ("cd /srv/caddy\ncp docker-compose.yml docker-compose.yml.bak-kervax\n"
                           "cp /var/lib/kervax/sockfix/caddy-proxy.yml docker-compose.yml\n"
                           "docker compose -p caddy-proxy -f docker-compose.yml up -d")
    assert fx["rollback"].endswith("cp docker-compose.yml.bak-kervax docker-compose.yml\n"
                                   "docker compose -p caddy-proxy -f docker-compose.yml up -d --remove-orphans")
    assert "logs --since 10m docker-api | grep -c blocked" in fx["check"]
    # файл вне каталога проекта - полным путем, имена с пробелами в кавычках
    odd = {**READY, "dir": "/srv/my caddy", "file": "/etc/caddy/compose.yml"}
    assert "cp /etc/caddy/compose.yml /etc/caddy/compose.yml.bak-kervax" in docker_exposure.view(_rep([odd]))[0]["fix"]["apply"]
    assert docker_exposure.view(_rep([odd]))[0]["fix"]["apply"].startswith("cd '/srv/my caddy'\n")
    # алерт и "Что сломано" видят только имена: exposed без fix
    assert "fix" not in docker_exposure.exposed(_rep([READY]))[0]


def test_reason_and_stale_report():
    [x] = docker_exposure.view(_rep([{"container": "caddy-proxy-caddy-1", "ok": False, "why": "anchors", "gid": 989}]))
    assert x["fix"] == {"ok": False, "why": "anchors", "gid": 989}
    # helper встал: его файл мог устареть, команды не показываем
    assert "fix" not in docker_exposure.view(_rep([READY], ts=time.time() - 7200))[0]
    # прокси уже без сокета - пункта нет, даже если старый отчет helper'а говорит иное
    rep = _rep([READY])
    rep["docker"]["containers"][0]["binds"] = ["/srv/caddy/data"]
    assert docker_exposure.view(rep) == []


def test_helper_needed_where_a_proxy_holds_the_socket():
    rep = _rep([])
    assert setup_scripts.setup_needed("sockfix-setup", rep)
    clean = {"docker": {"containers": []}}
    assert not setup_scripts.setup_needed("sockfix-setup", clean)
    assert setup_scripts.setup_needed("sockfix-setup", {**clean, "extras": {"sockfix": {"v": 1}}})


def test_image_line_of_the_service():
    """0.2: где compose задает образ прокси - строка и значение как написано, для команд обновления"""
    at = SF["image_at"]
    assert at(PLAIN, "caddy") == (3, "lucaslorentz/caddy-docker-proxy:2.12.0", False)
    # закомментированная строка выше и кавычки не мешают, сервиса нет в файле - None
    text = 'services:\n  caddy:\n#    image: lucaslorentz/caddy-docker-proxy:ci-alpine\n    image: "caddy:2.6-alpine"\n    build: .\n'
    assert at(text, "caddy") == (4, "caddy:2.6-alpine", True)
    assert at(text, "web") is None
    assert at("services:\n  caddy:\n    build: ./caddy\n", "caddy") == (0, "", True)
    for bad, code in [("    image: caddy:${CADDY_TAG}\n", "var"), ("    <<: *base\n", "anchors"),
                      ("    extends: base\n", "extends")]:
        with pytest.raises(SF["Bail"]) as e:
            at("services:\n  caddy:\n" + bad, "caddy")
        assert e.value.code == code


def _cont(name, image, files, d, svc="caddy"):
    return {"Name": "/" + name, "Config": {"Image": image, "Labels": {
        "com.docker.compose.project": "edge", "com.docker.compose.service": svc,
        "com.docker.compose.project.working_dir": d, "com.docker.compose.project.config_files": ",".join(files)}}}


def test_edge_item_and_override_file(tmp_path):
    base = tmp_path / "docker-compose.yml"
    base.write_text(PLAIN, encoding="utf-8")
    it = SF["edge"](_cont("edge-caddy-1", "lucaslorentz/caddy-docker-proxy:2.12.0", [str(base)], str(tmp_path)))
    assert it["ok"] and it["file"] == str(base) and it["line"] == 3 and it["build"] is False
    assert it["ref"] == "lucaslorentz/caddy-docker-proxy:2.12.0" and it["kind"] == "caddy"
    # образ задан во втором файле - правим его: в compose побеждает последний
    over = tmp_path / "override.yml"
    over.write_text("services:\n  caddy:\n    image: caddy:2.7\n", encoding="utf-8")
    it = SF["edge"](_cont("edge-caddy-1", "caddy:2.7", [str(base), str(over)], str(tmp_path)))
    assert it["ok"] and it["file"] == str(over) and it["line"] == 3 and it["ref"] == "caddy:2.7"
    # запущен руками, docker run - не из compose
    it = SF["edge"]({"Name": "/caddy", "Config": {"Image": "caddy:2", "Labels": {}}})
    assert not it["ok"] and it["why"] == "nocompose"
    # обычный caddy тоже внешний прокси, socket-proxy - нет
    assert SF["img_kind"]("caddy:2.6") == "caddy" and SF["img_kind"]("wollomatic/socket-proxy:1") is None


def test_helper_script():
    assert "KERVAX_SETUP_VERSION=0.3  # MAJOR.MINOR" in SRC and "KERVAX_SETUP_WHEN=" in SRC and "\r" not in SRC
    # 0.2 ставится и туда, где обычный caddy без меток docker: его образ тоже смотрит в интернет
    assert "grep -q -e caddy -e traefik" in SRC
    # ничего не применяет: только копия файла 0600 в своем каталоге и отчет
    assert "0o600" in SRC and "install -d -m 0700" in SRC and " up -d" not in SRC.split("HELPER_EOF")[1]
    # проверка docker compose config и запасной путь для старого compose без --format
    assert '"config", "--format", "json"' in SRC and '"unknown flag"' in SRC
    # python 3.6: без capture_output и text=
    assert "capture_output" not in SRC and "text=True" not in SRC


def _bare(**kw):
    """Отчет без блока helper'а: прокси с сокетом, версии helper'ов - как пришлет агент"""
    return {"clock_unix": time.time(), "agent_version": "2.53", **kw,
            "docker": {"present": True, "access": True, "containers": [
                {"name": "edge-caddy-1", "image": "lucaslorentz/caddy-docker-proxy:2.12.0", "state": "running",
                 "binds": ["/var/run/docker.sock", "/srv/caddy/data"]}]}}


def test_card_says_whether_the_helper_is_there():
    """Готового файла нет: карточка различает "helper'а нет" (команда установки), "установщик еще
    идет" и "helper стоит, но еще не собрал". Раньше на всех трех было одно "соберет helper"."""
    rep = _bare(setup_versions={"timesync-setup": "0.3"})
    [x] = docker_exposure.view(rep)
    assert x["helper"] == "missing" and "fix" not in x
    assert docker_exposure.view(rep, fresh=True)[0]["helper"] == "installing"
    rep["setup_versions"]["sockfix-setup"] = "0.3"
    assert "helper" not in docker_exposure.view(rep)[0]
    # агент не шлет версий helper'ов (до 1.46): стоит ли helper, неизвестно - без пометки
    assert "helper" not in docker_exposure.view(_bare())[0]
    # отчет helper'а есть - значит стоит, даже если версии не пришли
    assert "helper" not in docker_exposure.view(_rep([READY]))[0]
    # алерт и "Что сломано" пометки не видят
    assert "helper" not in docker_exposure.exposed(rep)[0]


def test_node_with_the_socket_and_no_helper_goes_to_kervax_outdated():
    """Прокси держит сокет, sockfix-setup на ноде нет: после окна свежей ноды она в kervax_outdated,
    плейбук -l kervax_outdated ставит helper по его условию. В окне (установщик еще ставит helper'ы
    по списку) не зовем, но проблема в kervax_docker_sock видна сразу."""
    from datetime import datetime, timedelta, timezone

    from app.api.ansible import node_of
    from app.models import Server

    now = datetime.now(timezone.utc)
    s = Server(id=5, name="node-a", token_hash="x", enabled=True, last_seen=now, offline_after_seconds=180,
               last_report=_bare(setup_versions={"timesync-setup": "0.3"}), first_report_at=now - timedelta(minutes=4))
    cur = {"timesync-setup": "0.3", "sockfix-setup": "0.3"}
    n = node_of(s, cur, now)
    assert n is not None and n.issues == ["docker_sock"] and n.outdated == []
    s.first_report_at = now - timedelta(minutes=40)
    n = node_of(s, cur, now)
    assert [a.name for a in n.outdated] == ["sockfix-setup"] and n.outdated[0].installed is None and n.rollout


def test_when_covers_every_proxy_the_panel_wants_the_helper_for():
    """Панель зовет sockfix-setup по образу прокси (docker_exposure), плейбук и установщик ставят его по
    KERVAX_SETUP_WHEN на ноде. Прокси, которого условие не узнает, держал бы ноду в kervax_outdated
    вечно: панель просит, плейбук пропускает. В выводе docker ps образ виден целиком - ищем его имя."""
    when = re.search(r'^KERVAX_SETUP_WHEN="([^"]+)"', SRC, re.MULTILINE).group(1)
    pats = re.findall(r"-e (\S+)", when)
    for base in [*docker_exposure._EDGE, "caddy"]:
        assert docker_exposure.proxy_kind(f"example/{base}:1"), base
        assert any(p in base for p in pats), base
