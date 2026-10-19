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
    assert "KERVAX_SETUP_VERSION=0.2" in SRC and "KERVAX_SETUP_WHEN=" in SRC and "\r" not in SRC
    # 0.2 ставится и туда, где обычный caddy без меток docker: его образ тоже смотрит в интернет
    assert "grep -q -e caddy -e traefik" in SRC
    # ничего не применяет: только копия файла 0600 в своем каталоге и отчет
    assert "0o600" in SRC and "install -d -m 0700" in SRC and " up -d" not in SRC.split("HELPER_EOF")[1]
    # проверка docker compose config и запасной путь для старого compose без --format
    assert '"config", "--format", "json"' in SRC and '"unknown flag"' in SRC
    # python 3.6: без capture_output и text=
    assert "capture_output" not in SRC and "text=True" not in SRC
