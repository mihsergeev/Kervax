"""Границы root-части helper'ов агента (аудит 24.10): спулы, docker-прокси, kube-setup, install.sh."""
import re
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent"


def _src(name: str) -> str:
    s = (AGENT / name).read_text(encoding="utf-8")
    assert "\r" not in s
    return s


def test_spool_answers_are_root_only():
    # каталог ответов root (агент только читает), ответ через mktemp, без chmod по имени после mv
    for name, ver in (("timesync-setup.sh", "0.3"), ("diskusage-setup.sh", "0.7"), ("units-setup.sh", "0.6")):
        s = _src(name)
        assert f"KERVAX_SETUP_VERSION={ver}  # " in s
        assert '-m 0750 "$RES_DIR"' in s and '-m 0770 "$RES_DIR"' not in s
        assert 'mktemp "$RES_DIR/.res.XXXXXX"' in s and 'mv -fT "$t"' in s
        assert 'chmod 0644 "$RES_DIR/$id.res"' not in s and ".res.tmp" not in s
        # симлинк или не файл вместо запроса выбрасывается непрочитанным, читаются первые 4 КБ
        assert '[ -L "$req" ] || [ ! -f "$req" ]' in s and 'head -c 4096 "$req"' in s
        assert "-mmin +10 -delete" in s


def test_timesync_panel_url_only_from_agent_config():
    s = _src("timesync-setup.sh")
    assert 'grep -lxF "url=$panel" /etc/kervax-agent.conf /etc/kervax-agent-*.conf' in s
    assert "MAX_STEP=21600" in s and 'date -s "@$when"' in s


def test_units_whole_journal_needs_fix_conf_and_is_masked():
    s = _src("units-setup.sh")
    assert "journal_all=yes" in s and "/etc/kervax/fix.conf" in s
    assert "(bearer|basic)" in s and "***@" in s
    assert "#journal_all=yes" in _src("diskusage-setup.sh")


def test_docker_proxy_on_a_unix_socket():
    pin = "wollomatic/socket-proxy:1.13.2@sha256:"
    for name in ("dockerproxy-setup.sh", "install.sh", "install-ru.sh"):
        s = _src(name)
        assert pin in s and "--network none" in s and "-proxysocketendpoint /run/kervax/docker.sock" in s
        assert "-listenip" not in s.split("setup_docker_proxy()")[-1] and "tcp://127.0.0.1:2375\\n" not in s
        assert "version|_ping|containers/json" in s and "|info|" not in s
    dp = _src("dockerproxy-setup.sh")
    assert "KERVAX_SETUP_VERSION=0.3  # " in dp and 'install -d -o root -g kervax -m 2770 "$SOCK_DIR"' in dp
    assert '--group-add "$KGID"' in dp and "switch_agents" in dp
    fe = (AGENT.parent / "frontend" / "src" / "DockerPage.tsx").read_text(encoding="utf-8")
    assert pin in fe and "tcp://127.0.0.1:2375" not in fe


def test_kube_patch_only_under_admission_policy():
    s = _src("kube-setup.sh")
    assert "KERVAX_SETUP_VERSION=0.15  # " in s and "kind: ValidatingAdmissionPolicy" in s
    assert 'WORKLOAD_VERBS="get, list, watch"' in s and 'WORKLOAD_VERBS="get, list, watch, patch"' in s
    assert "object.spec.template.spec == oldObject.spec.template.spec" in s
    assert re.search(r"resources: \[replicasets\]\n\s+verbs: \[get, list, watch\]", s)
    assert '-H "Authorization: Bearer $TOKEN"' not in s and "curl -sS -K -" in s


def test_install_ca_file_and_token_env():
    for name in ("install.sh", "install-ru.sh"):
        s = _src(name)
        assert "/tmp/kervax-ca-check.err" not in s and 'CA_ERR=$(mktemp)' in s
        assert '[ -L "$CA_FILE" ]' in s and 'cp "$CA_TMP" "$CA_DST"' in s and "SUDO_UID" in s
        assert "unset KERVAX_TOKEN" in s


def test_kubeexpiry_and_dbstat_secrets():
    ke = _src("kubeexpiry-setup.sh")
    assert '"-H", "@-"' in ke and '"PRIVATE-TOKEN: " + token,' not in ke and "kubeexpiry-hosts.json" in ke
    assert "/tmp/kv-kube-rows" not in ke
    db = _src("dbstat-setup.sh")
    assert "/tmp/kv-dbs" not in db and "'''?'''" in db
