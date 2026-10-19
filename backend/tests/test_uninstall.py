"""install.sh --uninstall снимает все, что ставят helper'ы.

Раньше uninstall снимал юниты по своему списку, а список отставал от helper'ов: после снятия
агента на ноде оставались таймеры changes, cron, disk-*, fpm, logerr, units, web-rate, workers,
продолжали работать и стучаться в панель, плюс /etc/cron.d/kervax-backup и /var/lib/kervax.
Теперь юниты снимаются по маске kervax-*, а тест сверяет каждый юнит, cron и каталог, которые
пишут *-setup.sh, с тем, что uninstall умеет снять: новый helper с юнитом вне маски не пройдет.
"""
import re
from fnmatch import fnmatch
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2] / "agent"
INSTALLERS = ("install.sh", "install-ru.sh")
# cron, который uninstall оставляет намеренно: ночная чистка репозиториев на сервере бэкапов
KEPT_CRON = ("kervax-prune-*",)


def helpers():
    out = sorted(AGENT.glob("*-setup.sh")) + [AGENT / "agent-watchdog.sh"]
    assert len(out) > 15
    return [(p.name, p.read_text(encoding="utf-8")) for p in out]


def uninstall_block(name):
    src = (AGENT / name).read_text(encoding="utf-8")
    start = src.index('if [ "$UNINSTALL" = 1 ]; then')
    return src[start:src.index("\n  exit 0\nfi\n", start)]


def code(block):
    return "\n".join(line for line in block.splitlines() if not line.lstrip().startswith(("#", "echo")))


def helper_units():
    units = set()
    for name, src in helpers():
        for u in re.findall(r"/etc/systemd/system/([A-Za-z0-9@_.$-]+\.(?:service|timer|path|socket))\b", src):
            units.add((name, u))
    return units


def test_helper_units_are_found():
    # из постановки: что оставалось на ноде после старого uninstall
    names = {u for _, u in helper_units()}
    for u in ("changes", "cron", "disk-health", "disk-usage", "du-req", "fpm", "logerr", "sockfix",
              "units", "units-req", "web-rate", "workers", "backup-req", "bsrv-req", "tsync-req",
              "db-stats", "dumps", "web-sites"):
        assert f"kervax-{u}.service" in names, u
        assert f"kervax-{u}.timer" in names or f"kervax-{u}.path" in names, u


def test_every_helper_unit_falls_under_the_uninstall_mask():
    for name in INSTALLERS:
        block = code(uninstall_block(name))
        # список юнитов - из systemd и из каталога, по маске, а не перечислением
        assert "systemctl list-unit-files --no-legend --plain 'kervax-*'" in block, name
        assert "systemctl list-units --all --no-legend --plain 'kervax-*'" in block, name
        assert "ls /etc/systemd/system" in block, name
        assert "/^kervax-[A-Za-z0-9@_.-]*\\.(timer|path|socket|service)$/" in block, name
        assert "kervax-backup-req kervax-bsrv-req" not in block, name  # старого списка больше нет
    rx = re.compile(r"^kervax-[A-Za-z0-9@_.-]*\.(timer|path|socket|service)$")
    for helper, unit in helper_units():
        assert rx.match(unit.replace("$UNIT", "kervax-agent")), (helper, unit)


def test_timers_and_paths_stop_before_services():
    for name in INSTALLERS:
        block = code(uninstall_block(name))
        assert "for kind in timer path socket service; do" in block, name
        assert 'case "$u" in *."$kind") systemctl disable --now "$u"' in block, name
        # файлы и drop-in'ы, ссылки включения, потом перечитать и забыть упавшие
        rm = block.index("rm -rf /etc/systemd/system/kervax-*")
        assert block.index("for kind in timer path socket service") < rm, name
        assert "find /etc/systemd/system -name 'kervax-*' \\( -type l -o -type f \\) -delete" in block, name
        assert rm < block.index("systemctl daemon-reload") < block.index("systemctl reset-failed 'kervax-*'"), name


def test_helper_drop_ins_go_with_the_unit_files():
    drop_ins = set()
    for _, src in helpers():
        drop_ins |= set(re.findall(r"/etc/systemd/system/([A-Za-z0-9@_$.-]+\.service\.d)", src))
    assert "kervax-agent.service.d" in drop_ins
    for d in drop_ins:
        d = d.replace("$UNIT", "kervax-agent")
        assert fnmatch(d, "kervax-*"), d  # уходит с rm -rf /etc/systemd/system/kervax-*


def test_helper_cron_is_removed_or_kept_on_purpose():
    crons = set()
    for _, src in helpers():
        crons |= set(re.findall(r"/etc/cron\.d/(kervax-[a-z0-9-]+)", src))
        crons |= {m for m in re.findall(r'CRON=/etc/cron\.d/([a-z0-9-]+)', src)}
    assert {"kervax-backup", "kervax-backupserver", "kervax-prune-"} <= crons
    for c in crons:
        assert fnmatch(c, "kervax-*"), c
    for name in INSTALLERS:
        block = code(uninstall_block(name))
        assert 'for f in /etc/cron.d/kervax-*; do' in block, name
        assert '/etc/cron.d/kervax-prune-*) KEEP_PRUNE="$KEEP_PRUNE $f" ;;' in block, name
        assert '*) rm -f "$f" ;;' in block, name
        # без prune наш restic уходит, с ним - остается для скриптов чистки
        assert "! -name restic -exec rm -rf {} +" in block and "rm -rf /lib65/kervax\n" in block, name
        # и пользователю сказано, что осталось и как снять
        assert "for f in $KEEP_PRUNE; do echo" in uninstall_block(name), name


def test_helper_state_and_spools_are_removed():
    dirs = set()
    for _, src in helpers():
        dirs |= set(re.findall(r"(/var/lib/kervax|/lib65/kervax|/etc/kervax|/run/kervax-|/etc/sudoers\.d/kervax-)", src))
    assert {"/var/lib/kervax", "/lib65/kervax", "/run/kervax-", "/etc/sudoers.d/kervax-"} <= dirs
    for name in INSTALLERS:
        block = code(uninstall_block(name))
        assert "rm -rf /opt/kervax-agent /opt/kervax/bin /etc/kervax /var/lib/kervax /run/kervax-*" in block, name
        assert "rm -f /etc/sudoers.d/kervax-*" in block, name


def test_only_our_socket_proxy_is_removed():
    names = set()
    for _, src in helpers():
        names |= set(re.findall(r"\b(kervax-docker-proxy(?:-old)?)\b", src))
    assert names == {"kervax-docker-proxy", "kervax-docker-proxy-old"}
    for name in INSTALLERS:
        block = code(uninstall_block(name))
        assert "docker rm -f kervax-docker-proxy kervax-docker-proxy-old" in block, name
        # чужие socket-proxy (traefik, caddy-docker-proxy, tecnativa) не ищутся и не снимаются
        docker = [line for line in block.splitlines() if "docker " in line]
        assert len(docker) == 1 and not re.search(r"docker (ps|container ls|stop)|--filter|ancestor", block), name


def test_instance_uninstall_leaves_helpers_alone():
    for name in INSTALLERS:
        block = code(uninstall_block(name))
        mask = block.index("KUNITS=$({")
        assert block.rindex('if [ -z "$INSTANCE" ]; then', 0, mask) > block.index('UNITS="kervax-agent@$INSTANCE"'), name
        assert "[ -z \"$INSTANCE\" ] && { systemctl reset-failed 'kervax-*'" in block, name


def test_installers_have_the_same_uninstall_code():
    en, ru = (code(uninstall_block(n)) for n in INSTALLERS)
    # сравниваем без комментариев и текста сообщений: они на своем языке
    strip = lambda s: [re.sub(r'echo "[^"]*"', "echo", re.sub(r"\s+#.*$", "", line)) for line in s.splitlines()]  # noqa: E731
    assert strip(en) == strip(ru)
    for n in INSTALLERS:
        assert "\r" not in (AGENT / n).read_text(encoding="utf-8"), n
