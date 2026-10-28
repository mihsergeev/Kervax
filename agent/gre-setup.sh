#!/usr/bin/env bash
# Kervax: GRE tunnel to the panel host. On some links (DPI at the node's provider) a TCP connection
# to the panel freezes after ~16 KB in either direction: compressed reports still pass, but an
# agent update crawls in 8 KB pieces for half an hour and a big command result never arrives. Bare
# GRE to the panel host is left alone by such DPI, and inside it goes the same TLS connection to the
# panel, only to an inner address. "GRE to the panel" in the server settings in the panel.
#
# What it installs:
#   * kervax-gre-sync.timer - every minute, as root, asks the panel what to hold (the panel host's
#     address, this node's /30 inside the tunnel) with this node's agent token, and reports back how
#     it is going. It brings up the tunnel, allows GRE from the panel host in ufw or firewalld, when
#     one of them is active, and checks that the panel answers through the tunnel. Only then it
#     writes connect=<the panel's inner address> into the agent config: the agent (2.56+) keeps TLS
#     and SNI on the panel's name and only dials the inner address, and goes direct again by itself
#     when the tunnel does not answer;
#   * kervax-gre.service - the tunnel itself (ip tunnel mode gre, mtu 1476), so it comes back after
#     a reboot before the panel is even asked.
# GRE does not pass NAT: a node without a public address on its interface says so to the panel and
# brings nothing up. Switched off in the panel (or the server deleted there) - the sync removes
# connect=, the tunnel, its firewall rules and the helper itself, version marker included.

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
# Only where GRE is switched on in the panel: asked with this node's agent token, which goes as the
# request body straight from the agent config (no quotes, braces or backslashes here: the condition
# travels through the playbook and the panel's helper index as is). Assigned before set -e: the
# command substitution in it runs here too, and must not end the script.
KERVAX_SETUP_WHEN="sed -n s/^token=//p /etc/kervax-agent.conf | curl -fsS -m 15 --data-binary @- $(sed -n s/^url=//p /etc/kervax-agent.conf 2>/dev/null)/api/agent/gre-wanted | grep -qx yes"
set -euo pipefail

HELPER_DIR=/lib65/kervax
SYNC="$HELPER_DIR/kervax-gre-sync.py"
STATE_DIR=/var/lib/kervax

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi
if [ ! -r /etc/kervax-agent.conf ]; then echo "No Kervax agent on this node (/etc/kervax-agent.conf)." >&2; exit 1; fi
PY=$(command -v python3 || true)
[ -z "$PY" ] && [ -x /usr/libexec/platform-python ] && PY=/usr/libexec/platform-python   # RHEL 8 minimal
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 7))'; then
  echo "python3 3.7 or newer is required for the GRE tunnel (apt/dnf install python3)." >&2; exit 1
fi
command -v ip >/dev/null 2>&1 || { echo "The ip command (iproute2) is required for the GRE tunnel." >&2; exit 1; }

install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" /etc/kervax
install -d -m 0700 "$STATE_DIR/gre"

# Written next to the old one and swapped in with mv: a run in progress keeps its own copy.
cat > "$SYNC.new" <<'SYNC_EOF'
"""Kervax GRE to the panel: sync (root, every minute) and the tunnel itself.

  kervax-gre-sync.py        - ask the panel what to hold, apply it, report back (the timer)
  kervax-gre-sync.py up     - bring the tunnel up from /etc/kervax/gre.json (kervax-gre.service)
  kervax-gre-sync.py down   - take it down

The panel answers {"enabled", "remote", "local_addr", "panel_addr", "prefix", "mtu"}: remote is the
panel host's address it expects GRE from (empty while the host has not set up its side), the two
addresses are this node's /30 inside the tunnel. Switched off or the server gone from the panel
(401) - everything is removed. Panel unreachable - everything stays as it is.
"""
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.parse

AGENT_CONF = "/etc/kervax-agent.conf"
CONF = "/etc/kervax/gre.json"
STATE = "/var/lib/kervax/gre/state.json"
VERSION_FILE = "/var/lib/kervax/versions/gre-setup.ver"
HELPER_DIR = "/lib65/kervax"
IFACE = "kervax-gre"
UNIT = "kervax-gre.service"
COMMENT = "kervax-gre"
TIMEOUT = 20


# not usable as a tunnel end: private networks and CGNAT (NAT is behind them, and GRE does not
# pass NAT) and special ones; the same list as the panel's
NOT_PUBLIC = tuple(ipaddress.IPv4Network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/3"))


def public4(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.version == 4 and not any(a in n for n in NOT_PUBLIC)


def run(*args):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)


def agent_conf():
    out = {}
    with open(AGENT_CONF, encoding="utf-8") as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and k in ("url", "token", "proxy", "connect"):
                out[k] = v.strip()
    return out


def set_connect(addr):
    """connect= in the agent config: the address the agent dials for the panel. Written only when
    it changes, with the same owner and mode (the file holds the agent token)."""
    with open(AGENT_CONF, encoding="utf-8") as f:
        lines = f.read().splitlines()
    keep = [ln for ln in lines if ln.strip().partition("=")[0].strip() != "connect"]
    if addr:
        keep.append("connect=" + addr)
    if keep == lines:
        return False
    st = os.stat(AGENT_CONF)
    tmp = AGENT_CONF + ".kervax-gre"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + "\n")
    os.chmod(tmp, st.st_mode & 0o7777)
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    os.replace(tmp, AGENT_CONF)
    return True


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, mode):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def active(unit):
    return run("systemctl", "is-active", "--quiet", unit).returncode == 0


def firewall():
    if shutil.which("ufw") and "Status: active" in run("ufw", "status").stdout:
        return "ufw"
    if shutil.which("firewall-cmd") and run("firewall-cmd", "--state").stdout.strip() == "running":
        return "firewalld"
    return "none"


def rich(ip):
    return 'rule family="ipv4" source address="%s" protocol value="gre" accept' % ip


def fw_apply(kind, want, have):
    """want/have - sets of panel host addresses GRE is allowed from. Only rules this helper added
    are ever removed."""
    for ip in sorted(have - want):
        if kind == "ufw":
            run("ufw", "delete", "allow", "proto", "gre", "from", ip, "to", "any")
        elif kind == "firewalld":
            run("firewall-cmd", "--permanent", "--remove-rich-rule=" + rich(ip))
    errors = []
    for ip in sorted(want - have):
        if kind == "ufw":
            r = run("ufw", "allow", "proto", "gre", "from", ip, "to", "any", "comment", COMMENT)
        elif kind == "firewalld":
            r = run("firewall-cmd", "--permanent", "--add-rich-rule=" + rich(ip))
        else:
            continue
        if r.returncode != 0:
            errors.append("%s: %s" % (kind, r.stdout.strip()[-120:]))
    if kind == "firewalld" and want != have:
        run("firewall-cmd", "--reload")
    return errors


def route_src(dst):
    """The address this node sends from towards dst - the near end of the tunnel."""
    m = re.search(r"\bsrc (\S+)", run("ip", "-o", "route", "get", dst).stdout)
    return m.group(1) if m else ""


def iface_up():
    try:
        with open("/sys/class/net/%s/operstate" % IFACE) as f:
            return f.read().strip() in ("up", "unknown")
    except OSError:
        return False


def tunnel_up():
    c = read_json(CONF, {})
    if not c:
        print("kervax-gre: no tunnel config %s" % CONF, file=sys.stderr)
        return 1
    run("ip", "link", "del", IFACE)
    r = run("ip", "tunnel", "add", IFACE, "mode", "gre", "local", c["local"], "remote", c["remote"], "ttl", "64")
    if r.returncode != 0:
        print("kervax-gre: ip tunnel add: %s" % r.stdout.strip(), file=sys.stderr)
        return 1
    run("ip", "addr", "add", "%s/%d" % (c["addr"], c["prefix"]), "dev", IFACE)
    r = run("ip", "link", "set", IFACE, "mtu", str(c["mtu"]), "up")
    if r.returncode != 0:
        print("kervax-gre: ip link set: %s" % r.stdout.strip(), file=sys.stderr)
        return 1
    return 0


def tunnel_down():
    run("ip", "link", "del", IFACE)
    return 0


def reach(addr, port, timeout=4):
    try:
        socket.create_connection((addr, port), timeout).close()
        return True
    except OSError:
        return False


def request(url, token, body, via=None):
    """POST to the panel: (status, body). via - dial this address instead of the panel's name;
    TLS and SNI stay on the name, like curl --resolve."""
    u = urllib.parse.urlsplit(url)
    host = u.hostname
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path.rstrip("/") or "") + "/api/agent/gre"
    sock = socket.create_connection((via or host, port), TIMEOUT)
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(host, port, timeout=TIMEOUT, context=ctx)
        conn.sock = ctx.wrap_socket(sock, server_hostname=host)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
        conn.sock = sock
    try:
        conn.request("POST", path, body=body, headers={
            "Authorization": "Bearer " + token, "Content-Type": "application/json"})
        r = conn.getresponse()
        return r.status, r.read(65536)
    finally:
        conn.close()


def teardown():
    """GRE switched off: connect=, the tunnel, its rules, its files and this helper go away."""
    try:
        set_connect("")
    except OSError:
        pass
    state = read_json(STATE, {})
    have = set(state.get("rules") or [])
    if have:
        fw_apply(state.get("firewall") or firewall(), set(), have)
    for unit in (UNIT, "kervax-gre-sync.timer"):
        run("systemctl", "disable", "--now", unit)
    tunnel_down()
    for path in ("/etc/systemd/system/" + UNIT, "/etc/systemd/system/kervax-gre-sync.service",
                 "/etc/systemd/system/kervax-gre-sync.timer", CONF, VERSION_FILE):
        try:
            os.remove(path)
        except OSError:
            pass
    shutil.rmtree(os.path.dirname(STATE), ignore_errors=True)
    run("systemctl", "daemon-reload")
    try:
        os.remove(os.path.join(HELPER_DIR, "kervax-gre-sync.py"))   # this script itself
    except OSError:
        pass
    print("kervax-gre: switched off in the panel, removed")


def valid_answer(ans):
    try:
        prefix, mtu = int(ans.get("prefix")), int(ans.get("mtu"))
        mine = ipaddress.IPv4Address(str(ans.get("local_addr")))
        panel = ipaddress.IPv4Address(str(ans.get("panel_addr")))
        remote = str(ans.get("remote") or "")
        if remote and not public4(remote):
            return None
    except (TypeError, ValueError):
        return None
    net = ipaddress.IPv4Network("%s/%d" % (mine, prefix), strict=False) if 24 <= prefix <= 30 else None
    if net is None or panel not in net or panel == mine or not 1280 <= mtu <= 1500:
        return None
    return {"remote": remote, "addr": str(mine), "peer": str(panel), "prefix": prefix, "mtu": mtu}


def sync():
    ac = agent_conf()
    url = ac.get("url", "")
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("https", "http") or not u.hostname or not ac.get("token"):
        print("kervax-gre: the agent config has no panel url or token", file=sys.stderr)
        return 1
    port = u.port or (443 if u.scheme == "https" else 80)
    state = read_json(STATE, {})
    cur = read_json(CONF, {})
    status = {
        "version": open(VERSION_FILE).read().strip() if os.path.exists(VERSION_FILE) else "",
        "up": bool(state.get("up")), "nat": bool(state.get("nat")), "local_ip": state.get("local_ip") or "",
        "firewall": state.get("firewall") or firewall(), "error": state.get("error") or "",
    }
    body = json.dumps(status).encode()
    # through the tunnel first when it works (that is what it is for), then direct: a small
    # request passes even where big ones are cut
    vias = [cur["peer"]] if cur.get("peer") and state.get("up") else []
    got = None
    for via in vias + [None]:
        try:
            got = request(url, ac["token"], body, via)
            break
        except (OSError, http.client.HTTPException) as e:
            got = e
    if not isinstance(got, tuple):
        print("kervax-gre: panel unreachable: %s" % type(got).__name__, file=sys.stderr)
        return 1
    code, data = got
    if code in (401, 403):   # the server is gone from the panel: nothing to tunnel for
        teardown()
        return 0
    if code != 200:
        print("kervax-gre: panel answered %d" % code, file=sys.stderr)
        return 1
    try:
        ans = json.loads(data)
    except ValueError:
        print("kervax-gre: panel answer is not JSON", file=sys.stderr)
        return 1
    if not ans.get("enabled"):
        teardown()
        return 0
    want = valid_answer(ans)
    if want is None:
        state["error"] = "the panel sent a tunnel config this helper does not accept"
        write_json(STATE, state, 0o600)
        return 1

    kind = firewall()
    have = set(state.get("rules") or [])
    if state.get("firewall") and state.get("firewall") != kind:
        fw_apply(state["firewall"], set(), have)   # the firewall changed: drop what we added to the old one
        have = set()
    remote = want["remote"]
    # the near end: the address the node sends from towards the panel host (or towards the panel's
    # name while the host address is not known yet - only to tell the panel the node's address)
    dst = remote
    if not dst:
        try:
            dst = socket.gethostbyname(u.hostname)
        except OSError:
            dst = ""
    local = route_src(dst) if dst else ""
    nat = bool(local) and not public4(local)
    new = {"local_ip": local, "nat": nat, "firewall": kind, "up": False, "error": "", "rules": []}
    rules = set()
    if not local:
        new["error"] = "no route to the panel host"
    elif nat:
        new["error"] = ("the node has no public address on its interface (%s is behind NAT): "
                        "GRE does not pass NAT" % local)
    elif not remote:
        new["error"] = "waiting for the panel host to set up its side of the tunnel"
    else:
        rules = {remote} if kind != "none" else set()
        want["local"] = local
        if cur != want:
            write_json(CONF, want, 0o600)
            run("systemctl", "enable", UNIT)
            r = run("systemctl", "restart", UNIT)
        elif not iface_up() or not active(UNIT):
            r = run("systemctl", "restart", UNIT)
        else:
            r = None
        if r is not None and r.returncode != 0:
            new["error"] = "%s did not start: see journalctl -u %s" % (UNIT, UNIT)
        new["up"] = reach(want["peer"], port)
        if not new["up"] and not new["error"]:
            new["error"] = "the panel does not answer through the tunnel (%s:%d)" % (want["peer"], port)
    errors = fw_apply(kind, rules, have)
    if errors:
        new["error"] = "; ".join([new["error"]] + errors if new["error"] else errors)[:300]
    new["rules"] = sorted(rules)
    if not new["up"] and (nat or not remote or not local) and cur:
        # nothing to hold any more: the tunnel goes down, the agent goes direct
        run("systemctl", "disable", "--now", UNIT)
        tunnel_down()
        try:
            os.remove(CONF)
        except OSError:
            pass
    # the agent dials the inner address only while the panel answers there
    set_connect(want["peer"] if new["up"] and not ac.get("proxy") else "")
    write_json(STATE, new, 0o600)
    return 0


def main(argv):
    if len(argv) > 1 and argv[1] == "up":
        return tunnel_up()
    if len(argv) > 1 and argv[1] == "down":
        return tunnel_down()
    return sync()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
SYNC_EOF
chmod 0700 "$SYNC.new" && mv -f "$SYNC.new" "$SYNC"
chown root:root "$SYNC"

cat > /etc/systemd/system/kervax-gre.service <<EOF
[Unit]
Description=Kervax: GRE tunnel to the panel host
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$PY $SYNC up
ExecStop=$PY $SYNC down
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/kervax-gre-sync.service <<EOF
[Unit]
Description=Kervax: GRE tunnel to the panel - what to hold, from the panel
After=network-online.target
[Service]
Type=oneshot
ExecStart=$PY $SYNC
TimeoutStartSec=3min
EOF
cat > /etc/systemd/system/kervax-gre-sync.timer <<'EOF'
[Unit]
Description=Kervax: GRE tunnel to the panel - ask the panel every minute
[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-gre-sync.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-gre-sync.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/gre-setup.ver"
chmod 0644 "$STATE_DIR/versions/gre-setup.ver"
echo "✓ gre-setup: GRE tunnel to the panel - the addresses come from the panel within a minute."
