#!/usr/bin/env bash
# Kervax: relay for agents. A server whose address is blocked on the way to the panel (its IP is
# on a block list of the panel's provider) cannot send its agent's reports there directly. The
# agent (2.44+) can go through an HTTP proxy with a login, and this helper turns a server that
# does reach the panel into such a proxy: "Relay for agents" in the server settings in the panel.
#
# What it installs:
#   * kervax-relay.service - a small HTTP CONNECT proxy (python3, standard library only) under
#     its own unprivileged user. It lets a client through ONLY to the panel's host:port, taken
#     from this node's own agent config - never from the panel's answer, so even a forged answer
#     cannot turn the relay into an open proxy. Only from the addresses of the servers that chose
#     this relay, and only with the login and password the panel issued. Nothing about the
#     clients, the login or the password goes to the log.
#   * kervax-relay-sync.timer - every minute, as root, asks the panel what to hold (port, login,
#     password, client addresses) with this node's agent token, and reports back how it is going.
#     It opens the port for exactly those addresses in ufw or firewalld, when one of them is
#     active, and closes it again for addresses that are gone.
# The relay switched off in the panel (or the server deleted there) - the sync stops the proxy,
# removes its firewall rules and the helper itself, version marker included.
#
# Why python3 and not a proxy package: tinyproxy or squid come from different repositories on
# Debian, Ubuntu and RHEL (EPEL), in versions with different config syntax, and neither limits a
# CONNECT to one host:port by itself. python3 is on every one of them, and the whole proxy is
# right here in the repository: ansible copies it from the repository, nothing is downloaded.

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
# Only where the relay is switched on in the panel: asked with this node's agent token, which
# goes as the request body straight from the agent config (no quotes, braces or backslashes here:
# the condition travels through the playbook and the panel's helper index as is). Assigned before
# set -e: the command substitution in it runs here too, and must not end the script.
KERVAX_SETUP_WHEN="sed -n s/^token=//p /etc/kervax-agent.conf | curl -fsS -m 15 --data-binary @- $(sed -n s/^url=//p /etc/kervax-agent.conf 2>/dev/null)/api/agent/relay-wanted | grep -qx yes"
set -euo pipefail

HELPER_DIR=/lib65/kervax
PROXY="$HELPER_DIR/kervax-relay.py"
SYNC="$HELPER_DIR/kervax-relay-sync.py"
STATE_DIR=/var/lib/kervax
RELAY_USER=kervax-relay

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi
if [ ! -r /etc/kervax-agent.conf ]; then echo "No Kervax agent on this node (/etc/kervax-agent.conf)." >&2; exit 1; fi
PY=$(command -v python3 || true)
[ -z "$PY" ] && [ -x /usr/libexec/platform-python ] && PY=/usr/libexec/platform-python   # RHEL 8 minimal
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 7))'; then
  echo "python3 3.7 or newer is required for the relay (apt/dnf install python3)." >&2; exit 1
fi

install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" /etc/kervax
install -d -m 0700 "$STATE_DIR/relay"
id "$RELAY_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$RELAY_USER" 2>/dev/null || \
  adduser --system --no-create-home --shell /usr/sbin/nologin "$RELAY_USER" 2>/dev/null || true
id "$RELAY_USER" >/dev/null 2>&1 || { echo "Could not create the user $RELAY_USER." >&2; exit 1; }

# Written next to the old ones and swapped in with mv: a run in progress keeps its own copy.
cat > "$PROXY.new" <<'PROXY_EOF'
"""Kervax relay: HTTP CONNECT proxy to exactly one host:port (the panel).

Usage: kervax-relay.py <config.json> <stats.json>. The config is written by kervax-relay-sync:
{"port", "dest_host", "dest_port", "user", "password", "clients": [ip, ...]}. It is re-read when it
changes; a new port needs a restart (the sync does it). Every 30 s the counters go to stats.json,
which the sync reports to the panel. No client address, login or password is ever logged.
"""
import asyncio
import base64
import hmac
import ipaddress
import json
import os
import sys
import time

CONF, STATS = sys.argv[1], sys.argv[2]
HEAD_MAX = 8192
HEAD_TIMEOUT = 10
DIAL_TIMEOUT = 10
IDLE = 300
MAX_CONNS = 256

conf = {}
conf_mtime = 0.0
stats = {"listening": False, "error": "", "conns": 0, "denied": 0, "clients": 0}
slots = None  # asyncio.Semaphore, made inside the running loop (python 3.7-3.9 bind it at creation)


def load():
    global conf, conf_mtime
    try:
        m = os.stat(CONF).st_mtime
        if m == conf_mtime:
            return
        with open(CONF, encoding="utf-8") as f:
            c = json.load(f)
        c["clients"] = {ipaddress.ip_address(x).compressed for x in c.get("clients") or []}
        c["auth"] = ("%s:%s" % (c["user"], c["password"])).encode()
        conf, conf_mtime = c, m
        stats["clients"] = len(c["clients"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        stats["error"] = "config: %s" % type(e).__name__


def write_stats():
    tmp = STATS + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(stats, at=int(time.time())), f)
        os.replace(tmp, STATS)
    except OSError:
        pass


def peer_ip(writer):
    try:
        ip = ipaddress.ip_address(writer.get_extra_info("peername")[0])
    except (TypeError, ValueError, IndexError):
        return ""
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.compressed


def authorized(headers):
    for line in headers:
        name, _, value = line.partition(":")
        if name.strip().lower() != "proxy-authorization":
            continue
        kind, _, token = value.strip().partition(" ")
        if kind.lower() != "basic":
            return False
        try:
            got = base64.b64decode(token.strip(), validate=True)
        except ValueError:
            return False
        return hmac.compare_digest(got, conf["auth"])
    return False


def target_ok(target):
    host, sep, port = target.rpartition(":")
    if not sep:
        return False
    host = host.strip("[]").lower()
    return host == str(conf["dest_host"]).lower() and port == str(conf["dest_port"])


async def answer(writer, code, text, extra=""):
    writer.write(("HTTP/1.1 %d %s\r\n%sContent-Length: 0\r\nConnection: close\r\n\r\n" % (code, text, extra)).encode())
    try:
        await writer.drain()
    except OSError:
        pass


async def pipe(reader, writer):
    try:
        while True:
            data = await asyncio.wait_for(reader.read(65536), IDLE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def handle(reader, writer):
    load()
    if not conf or peer_ip(writer) not in conf["clients"]:
        stats["denied"] += 1
        writer.close()
        return
    if slots.locked():
        await answer(writer, 503, "Busy")
        writer.close()
        return
    async with slots:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split()
        if len(parts) != 3 or parts[0] != "CONNECT":
            await answer(writer, 405, "Method Not Allowed")
            writer.close()
            return
        if not authorized(lines[1:]):
            stats["denied"] += 1
            await answer(writer, 407, "Proxy Authentication Required", 'Proxy-Authenticate: Basic realm="kervax"\r\n')
            writer.close()
            return
        if not target_ok(parts[1]):
            stats["denied"] += 1
            await answer(writer, 403, "Forbidden")
            writer.close()
            return
        try:
            up_r, up_w = await asyncio.wait_for(
                asyncio.open_connection(conf["dest_host"], int(conf["dest_port"])), DIAL_TIMEOUT)
        except (OSError, asyncio.TimeoutError):
            await answer(writer, 502, "Bad Gateway")
            writer.close()
            return
        stats["conns"] += 1
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))


async def ticker():
    while True:
        load()
        write_stats()
        await asyncio.sleep(30)


async def main():
    global slots
    slots = asyncio.Semaphore(MAX_CONNS)
    load()
    if not conf:
        stats["error"] = stats["error"] or "no config"
        write_stats()
        sys.exit(1)
    try:
        # host=None: every address of the node, IPv4 and IPv6
        server = await asyncio.start_server(handle, host=None, port=int(conf["port"]), limit=HEAD_MAX)
    except OSError as e:
        stats["error"] = "port %s: %s" % (conf["port"], e.strerror or type(e).__name__)
        write_stats()
        print("kervax-relay: cannot listen on port %s: %s" % (conf["port"], e.strerror), file=sys.stderr)
        sys.exit(1)
    stats["listening"], stats["error"] = True, ""
    print("kervax-relay: listening on port %s, clients %d" % (conf["port"], len(conf["clients"])), flush=True)
    async with server:
        await asyncio.gather(server.serve_forever(), ticker())


if __name__ == "__main__":
    asyncio.run(main())
PROXY_EOF

cat > "$SYNC.new" <<'SYNC_EOF'
"""Kervax relay sync (root, every minute): asks the panel what the relay holds, applies it.

Reads url and token from /etc/kervax-agent.conf, posts the relay's status to <url>/api/agent/relay
and gets back {"enabled", "port", "user", "password", "clients"}. The destination is the agent's own
panel URL, never something from the answer. Switched off or the server gone from the panel (401) -
removes the relay completely. Panel unreachable - leaves everything as it is.
"""
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

AGENT_CONF = "/etc/kervax-agent.conf"
CONF = "/etc/kervax/relay.json"
STATE = "/var/lib/kervax/relay/state.json"
STATS = "/run/kervax-relay/stats.json"
VERSION_FILE = "/var/lib/kervax/versions/relay-setup.ver"
HELPER_DIR = "/lib65/kervax"
USER = "kervax-relay"
COMMENT = "kervax-relay"
SAFE = re.compile(r"^[A-Za-z0-9_.~-]{1,128}$")


def run(*args, check=False):
    return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=check, timeout=60)


def agent_conf():
    out = {}
    with open(AGENT_CONF, encoding="utf-8") as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and k in ("url", "token"):
                out[k] = v.strip()
    return out


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, mode, group=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, sort_keys=True)
    os.chmod(tmp, mode)
    if group:
        shutil.chown(tmp, "root", group)
    os.replace(tmp, path)


def active(unit):
    return run("systemctl", "is-active", "--quiet", unit).returncode == 0


def firewall():
    if shutil.which("ufw") and "Status: active" in run("ufw", "status").stdout:
        return "ufw"
    if shutil.which("firewall-cmd") and run("firewall-cmd", "--state").stdout.strip() == "running":
        return "firewalld"
    return "none"


def rich(ip, port):
    fam = "ipv6" if ":" in ip else "ipv4"
    return 'rule family="%s" source address="%s" port port="%d" protocol="tcp" accept' % (fam, ip, port)


def fw_apply(kind, want, have):
    """want/have - sets of (ip, port). Only rules this helper added are ever removed."""
    for ip, port in sorted(have - want):
        if kind == "ufw":
            run("ufw", "delete", "allow", "proto", "tcp", "from", ip, "to", "any", "port", str(port))
        elif kind == "firewalld":
            run("firewall-cmd", "--permanent", "--remove-rich-rule=" + rich(ip, port))
    errors = []
    for ip, port in sorted(want - have):
        if kind == "ufw":
            r = run("ufw", "allow", "proto", "tcp", "from", ip, "to", "any", "port", str(port), "comment", COMMENT)
        elif kind == "firewalld":
            r = run("firewall-cmd", "--permanent", "--add-rich-rule=" + rich(ip, port))
        else:
            continue
        if r.returncode != 0:
            errors.append("%s: %s" % (kind, r.stdout.strip()[-120:]))
    if kind == "firewalld" and want != have:
        run("firewall-cmd", "--reload")
    return errors


def teardown():
    """Relay switched off: proxy, its rules, its files and this helper go away."""
    state = read_json(STATE, {})
    have = {(r[0], int(r[1])) for r in state.get("rules") or []}
    if have:
        fw_apply(state.get("firewall") or firewall(), set(), have)
    for unit in ("kervax-relay.service", "kervax-relay-sync.timer"):
        run("systemctl", "disable", "--now", unit)
    for path in ("/etc/systemd/system/kervax-relay.service", "/etc/systemd/system/kervax-relay-sync.service",
                 "/etc/systemd/system/kervax-relay-sync.timer", CONF, VERSION_FILE,
                 os.path.join(HELPER_DIR, "kervax-relay.py")):
        try:
            os.remove(path)
        except OSError:
            pass
    shutil.rmtree(os.path.dirname(STATE), ignore_errors=True)
    run("systemctl", "daemon-reload")
    run("userdel", USER)
    try:
        os.remove(os.path.join(HELPER_DIR, "kervax-relay-sync.py"))   # this script itself
    except OSError:
        pass
    print("kervax-relay: switched off in the panel, removed")


def main():
    ac = agent_conf()
    url = urllib.parse.urlsplit(ac.get("url", ""))
    if url.scheme != "https" or not url.hostname or not ac.get("token"):
        print("kervax-relay: the agent config has no https panel url or token", file=sys.stderr)
        return 1
    dest_host, dest_port = url.hostname, url.port or 443
    state = read_json(STATE, {})
    stats = read_json(STATS, {})
    status = {
        "version": open(VERSION_FILE).read().strip() if os.path.exists(VERSION_FILE) else "",
        "listening": active("kervax-relay.service") and bool(stats.get("listening")),
        "firewall": state.get("firewall") or firewall(),
        "error": stats.get("error") or state.get("error") or "",
        "conns": stats.get("conns", 0), "denied": stats.get("denied", 0), "clients": stats.get("clients", 0),
    }
    if os.path.exists(CONF) and not active("kervax-relay.service") and not status["error"]:
        status["error"] = "kervax-relay.service is not running"
    req = urllib.request.Request(
        ac["url"].rstrip("/") + "/api/agent/relay", data=json.dumps(status).encode(), method="POST",
        headers={"Authorization": "Bearer " + ac["token"], "Content-Type": "application/json"})
    # no proxy from the environment: the relay is the node that reaches the panel directly
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as r:
            ans = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):   # the server is gone from the panel: nothing to relay for
            teardown()
            return 0
        print("kervax-relay: panel answered %d" % e.code, file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print("kervax-relay: panel unreachable: %s" % type(e).__name__, file=sys.stderr)
        return 1
    if not ans.get("enabled"):
        teardown()
        return 0

    port, user, password = ans.get("port"), str(ans.get("user") or ""), str(ans.get("password") or "")
    clients = []
    for ip in ans.get("clients") or []:
        try:
            clients.append(ipaddress.ip_address(str(ip)).compressed)
        except ValueError:
            pass
    if not isinstance(port, int) or not 1024 <= port <= 65535 or not SAFE.match(user) or not SAFE.match(password):
        state["error"] = "the panel sent a relay config this helper does not accept"
        write_json(STATE, state, 0o600)
        return 1
    clients = sorted(set(clients))
    want_conf = {"port": port, "dest_host": dest_host, "dest_port": dest_port, "user": user,
                 "password": password, "clients": clients}
    old = read_json(CONF, {})
    if old != want_conf:
        write_json(CONF, want_conf, 0o640, USER)
    if old.get("port") != port or not active("kervax-relay.service"):
        run("systemctl", "enable", "kervax-relay.service")
        run("systemctl", "restart", "kervax-relay.service")

    kind = firewall()
    have = {(r[0], int(r[1])) for r in state.get("rules") or []}
    if state.get("firewall") and state.get("firewall") != kind:
        fw_apply(state["firewall"], set(), have)   # the firewall changed: drop what we added to the old one
        have = set()
    want = {(ip, port) for ip in clients} if kind != "none" else set()
    errors = fw_apply(kind, want, have)
    state = {"firewall": kind, "rules": sorted([list(x) for x in want]), "error": "; ".join(errors)[:300]}
    write_json(STATE, state, 0o600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
SYNC_EOF
chmod 0644 "$PROXY.new" && mv -f "$PROXY.new" "$PROXY"
chmod 0700 "$SYNC.new" && mv -f "$SYNC.new" "$SYNC"
chown root:root "$PROXY" "$SYNC"

cat > /etc/systemd/system/kervax-relay.service <<EOF
[Unit]
Description=Kervax: relay for agents (HTTP CONNECT to the panel only)
After=network-online.target
Wants=network-online.target
[Service]
User=$RELAY_USER
ExecStart=$PY $PROXY /etc/kervax/relay.json /run/kervax-relay/stats.json
RuntimeDirectory=kervax-relay
Restart=always
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
CapabilityBoundingSet=
MemoryMax=256M
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/kervax-relay-sync.service <<EOF
[Unit]
Description=Kervax: relay for agents - what to hold, from the panel
After=network-online.target
[Service]
Type=oneshot
ExecStart=$PY $SYNC
TimeoutStartSec=3min
EOF
cat > /etc/systemd/system/kervax-relay-sync.timer <<'EOF'
[Unit]
Description=Kervax: relay for agents - ask the panel every minute
[Timer]
OnBootSec=30s
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-relay-sync.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-relay-sync.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/relay-setup.ver"
chmod 0644 "$STATE_DIR/versions/relay-setup.ver"
echo "✓ relay-setup: relay for agents - the port, login and client list come from the panel within a minute."
