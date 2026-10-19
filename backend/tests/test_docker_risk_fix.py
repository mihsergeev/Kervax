"""Готовые шаги "как исправить" для контейнеров с ключами от хоста: копия compose, правка в блоке
сервиса, пересоздать, откат; пути и сервис - из меток compose агента."""
from app import compose_cmd, docker_risk
from tests.test_docker_risk import _c, _rep, _srv


def _cm(svc, d="/srv/ci", files=None, project=None):
    return {"project": project or d.rsplit("/", 1)[-1], "service": svc, "dir": d,
            "files": files or f"{d}/docker-compose.yml"}


def _blocks(rep, mutes=None):
    out = {}
    for x in docker_risk.view(_srv(rep, mutes), fix=True):
        if "fix" in x:
            out.setdefault(x["fix"]["key"], x["fix"])
    return list(out.values())


def _ids(b):
    return [s["id"] for s in b["steps"]]


def _cmd(b, sid):
    return next(s["cmd"] for s in b["steps"] if s["id"] == sid)


AGENT = "jetbrains/teamcity-agent:2023.11.1-linux-sudo"


def test_ci_agents_of_one_project_get_one_block():
    rep = _rep(*[_c(f"ci-agent-{i}-1", AGENT, priv=True, binds=[f"/srv/ci/agents/agent-{i}/conf"],
                    compose=_cm(f"agent-{i}")) for i in (1, 2, 3)])
    [b] = _blocks(rep)
    assert b["names"] == ["ci-agent-1-1", "ci-agent-2-1", "ci-agent-3-1"]
    assert _ids(b) == ["ci_check", "bak", "priv_off", "up", "undo", "alt_ci"]
    assert b["args"] == {"names": "ci-agent-1-1, ci-agent-2-1, ci-agent-3-1", "flags": "priv",
                         "dind_env": "DOCKER_IN_DOCKER=start", "file": "docker-compose.yml", "dir": "/srv/ci",
                         "svc": "agent-1, agent-2, agent-3"}
    assert _cmd(b, "ci_check").splitlines()[0] == "sudo docker exec ci-agent-1-1 docker images -q | wc -l"
    assert _cmd(b, "bak") == "cd /srv/ci\nsudo cp docker-compose.yml docker-compose.yml.bak-kervax-host"
    # sed только в блоке своего сервиса: от "  agent-1:" до первой строки privileged после него
    assert _cmd(b, "priv_off").splitlines() == [
        "cd /srv/ci",
        "sudo sed -i '/^ *agent-1:/,/privileged:/ s/privileged: *true/privileged: false/' docker-compose.yml",
        "sudo sed -i '/^ *agent-2:/,/privileged:/ s/privileged: *true/privileged: false/' docker-compose.yml",
        "sudo sed -i '/^ *agent-3:/,/privileged:/ s/privileged: *true/privileged: false/' docker-compose.yml",
        "diff docker-compose.yml.bak-kervax-host docker-compose.yml"]
    assert _cmd(b, "up").splitlines() == [
        "cd /srv/ci",
        "sudo docker compose config -q && sudo docker compose up -d agent-1 agent-2 agent-3",
        "sudo docker compose ps agent-1 agent-2 agent-3",
        "sudo docker compose logs --tail 20 agent-1 agent-2 agent-3"]
    undo = next(s for s in b["steps"] if s["id"] == "undo")
    assert undo["undo"] and undo["cmd"].splitlines()[1:] == [
        "sudo cp docker-compose.yml.bak-kervax-host docker-compose.yml",
        "sudo docker compose up -d agent-1 agent-2 agent-3"]
    # у всех трех один и тот же блок
    assert {x["fix"]["key"] for x in docker_risk.view(_srv(rep), fix=True)} == {"ci-agent-1-1"}


def test_muted_container_leaves_the_block():
    rep = _rep(*[_c(f"ci-agent-{i}-1", AGENT, priv=True, compose=_cm(f"agent-{i}")) for i in (1, 2)])
    v = docker_risk.view(_srv(rep, ["dockrisk:ci-agent-1-1"]), fix=True)
    assert "fix" not in v[0] and v[1]["fix"]["names"] == ["ci-agent-2-1"]
    assert "fix" not in docker_risk.view(_srv(rep))[0]  # без fix=True шаги не считаются


def test_privileged_replaced_by_what_the_image_needs():
    rep = _rep(_c("vpn-1", "linuxserver/wireguard:1.0", priv=True, compose=_cm("vpn", d="/srv/vpn")),
               _c("tun-1", "tailscale/tailscale:v1.80", priv=True, compose=_cm("tun", d="/srv/vpn")))
    vpn, tun = _blocks(rep)
    assert _ids(vpn) == ["bak", "priv_set", "up", "undo"]
    assert vpn["args"]["cap_add"] == "NET_ADMIN, SYS_MODULE" and "devices" not in vpn["args"]
    assert _cmd(vpn, "priv_set").splitlines()[1] == (
        r"sudo sed -i '/^ *vpn:/,/privileged:/ s#^\( *\)privileged: *true#\1cap_add: [NET_ADMIN, SYS_MODULE]#' "
        "docker-compose.yml")
    assert _cmd(tun, "priv_set").splitlines()[1] == (
        r"sudo sed -i '/^ *tun:/,/privileged:/ s#^\( *\)privileged: *true#\1cap_add: [NET_ADMIN]\n\1devices: "
        r"[/dev/net/tun]#' docker-compose.yml")


def test_socket_goes_through_read_only_proxy():
    rep = _rep(_c("web-a", "shop/web:1", binds=["/var/run/docker.sock", "/srv/web"], compose=_cm("web", d="/srv/web")))
    [b] = _blocks(rep)
    assert _ids(b) == ["bak", "sock_rm", "sock_add", "up", "undo", "alt_sock"]
    assert _cmd(b, "sock_rm").splitlines()[1] == (
        r"sudo sed -i '/^ *web:/,/docker\.sock/ {/docker\.sock/d}' docker-compose.yml")
    add = _cmd(b, "sock_add")
    assert "      - -allowfrom=web" in add.splitlines() and "image: wollomatic/socket-proxy:1.13.1" in add
    assert "internal: true" in add and "/var/run/docker.sock:/var/run/docker.sock:ro" in add
    assert _cmd(b, "up").splitlines()[1] == "sudo docker compose config -q && sudo docker compose up -d docker-api web"
    # откат убирает и прокси: его нет в прежнем файле
    assert _cmd(b, "undo").splitlines()[-1] == "sudo docker compose up -d --remove-orphans web"


def test_run_dir_socket_caps_and_host_root():
    rep = _rep(_c("tool-1", "shop/tool:1", binds=["/run", "/", "/srv/x"], ro=[], caps=["SYS_ADMIN", "SYS_PTRACE"],
                  compose=_cm("tool", d="/srv/tool", files="/srv/tool/stack.yml,/srv/tool/stack.prod.yml",
                              project="tools")))
    [b] = _blocks(rep)
    assert _ids(b) == ["bak", "sock_rm", "sock_add", "caps_edit", "host_edit", "up", "undo", "alt_sock"]
    assert b["args"]["caps"] == "SYS_ADMIN, SYS_PTRACE" and b["args"]["paths"] == "/"
    assert _cmd(b, "bak").splitlines()[1:] == ["sudo cp stack.yml stack.yml.bak-kervax-host",
                                               "sudo cp stack.prod.yml stack.prod.yml.bak-kervax-host"]
    assert _cmd(b, "sock_rm").splitlines()[1] == (
        r"sudo sed -i '/^ *tool:/,/\/run:/ {/\/run:/d}' stack.yml stack.prod.yml")
    assert _cmd(b, "caps_edit").splitlines()[1] == "grep -nE 'SYS_ADMIN|SYS_PTRACE' stack.yml stack.prod.yml"
    assert _cmd(b, "host_edit").splitlines()[1] == "grep -nE -- '- *\"?(/):' stack.yml stack.prod.yml"
    # свои имена файлов и проект не по папке - compose не найдет их сам
    assert "sudo docker compose -p tools -f stack.yml -f stack.prod.yml up -d docker-api tool" in _cmd(b, "up")


def test_old_agent_and_docker_run():
    rep = _rep(_c("app-1", "shop/app:1", priv=True), _c("app-2", "shop/app:1", priv=True),
               _c("job", "shop/job:1", caps=["SYS_MODULE"], compose={}))
    old, run = _blocks(rep)
    # агент без меток compose: одна команда покажет, где файлы обоих, правка словами
    assert old["names"] == ["app-1", "app-2"] and _ids(old) == ["find", "hand_priv", "hand_up"]
    assert _cmd(old, "find").endswith(" app-1 app-2") and "project.working_dir" in _cmd(old, "find")
    assert _ids(run) == ["run"] and run["args"]["caps"] == "SYS_MODULE"


def test_compose_where():
    assert compose_cmd.where({}) is None
    assert compose_cmd.where({"compose": {}}) == {}
    w = compose_cmd.where({"compose": _cm("web", d="/srv/web/", files="/srv/web/compose.yml,/srv/web/compose.override.yml",
                                          project="web")})
    assert w == {"dir": "/srv/web", "files": ["compose.yml", "compose.override.yml"], "service": "web",
                 "project": "web", "flags": ""}
    w = compose_cmd.where({"compose": _cm("web", d="/srv/My App", files="/opt/other.yml", project="shop")})
    assert w["flags"] == "-p shop -f /opt/other.yml"
    assert compose_cmd.Cmd(w).cd == "cd '/srv/My App'"
    # точка в имени сервиса в адресе sed - буквально
    assert compose_cmd.Cmd(w).sed_in("web.v2", "x:", "d").startswith(r"sudo sed -i '/^ *web\.v2:/,/x:/ d'")
