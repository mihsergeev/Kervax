#!/usr/bin/env bash
# Kervax: who of the VPN clients torrents. A VPN server (AmneziaWG, WireGuard, OpenVPN, on the host
# or in a container with its own network namespace) NATs its clients, so on the host every flow of
# every client looks like one address. Only the conntrack table of the namespace where the VPN
# interface lives still knows the inner address of the client (10.8.x.y). The agent runs as an
# unprivileged user and sees none of it.
#
# A root helper finds every network namespace with a VPN interface (awg*, wg*, tun*, tap*) once a
# minute, enters it (nsenter -n, only the network: nothing of the container's files is read or run)
# and reads its conntrack table over netlink, the way the conntrack tool does, without the tool (it
# is not installed on most nodes, and /proc/net/nf_conntrack is gone from current kernels). Per
# inner address of a client: flows, UDP flows, distinct remote addresses, distinct remote addresses
# on high ports outside the usual services, flows to the BitTorrent ports. Torrent looks like
# hundreds of distinct peers on random high ports, mostly UDP (DHT, uTP); a browser, a messenger or a
# video call has tens of addresses, almost all on 443. The last 15 minutes of the high-port peers are
# kept per client, so the panel can tell a torrent from a burst. Names and traffic of the clients are
# not read here: they come from the list of VPN clients of the panel, joined by the inner address.
#
# Cost: one netlink dump per namespace, ~0.07 s of CPU for 1500 flows on one vCPU; a table over
# FLOW_CAP flows is not read (reported as skipped). Python 3 (every Ubuntu has it). Read only.
# Writes /var/lib/kervax/report.d/vpnflow.json; the agent hands report.d to the panel as is.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1 # MAJOR.MINOR; compared component-wise
# A WireGuard, AmneziaWG, OpenVPN or TAP interface on the host, or a VPN container (its interface is in
# its own namespace). No $, braces or apostrophes: the condition goes through Jinja and YAML in the playbook
KERVAX_SETUP_WHEN="command -v python3 >/dev/null 2>&1 && command -v nsenter >/dev/null 2>&1 && (ls /sys/class/net 2>/dev/null | grep -q -e ^wg -e ^awg -e ^tun -e ^tap || (command -v docker >/dev/null 2>&1 && docker ps 2>/dev/null | grep -qi -e amnezia -e wireguard -e wg-easy -e openvpn))"

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-vpnflow"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/vpnflow.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
command -v nsenter >/dev/null 2>&1 || { echo "nsenter (util-linux) is required" >&2; exit 1; }

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv, like the other helpers.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env python3
# VPN clients by inner address -> /var/lib/kervax/report.d/vpnflow.json. Read only: /proc, the
# conntrack table of each VPN namespace over netlink, `ip -o addr` inside it, `docker ps` for names.
# Python 3.8 (Ubuntu 20.04): no newer syntax.
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time

OUT = os.environ.get("KERVAX_VPNFLOW_OUT", "/var/lib/kervax/report.d/vpnflow.json")
STATE = os.environ.get("KERVAX_VPNFLOW_STATE", "/var/lib/kervax/vpnflow.state")
FLOW_CAP = 300000   # flows in one table; more - not read (a dump that big is seconds of CPU)
CLIENT_CAP = 200    # clients of one namespace in the report, the busiest first
HIST_MIN = 15       # minutes of high-port peers kept per client
VPN_IF = re.compile(r"^(a?wg|tun|tap)[0-9a-z_-]*$")
# high ports of the usual services: not a sign of torrent (VPN and proxies, STUN/TURN of the
# calls, push of the phones, chat, streaming, alternative HTTP)
COMMON = {1194, 1935, 3478, 3479, 3480, 3481, 5222, 5223, 5228, 5349, 8080, 8443, 8880, 9339, 19302,
          19303, 19304, 19305, 19306, 19307, 19308, 19309, 51820}
BT_PORTS = set(range(6881, 6890)) | {6969}


def attrs(b):
    i = 0
    n = len(b)
    while i + 4 <= n:
        ln, tp = struct.unpack_from("=HH", b, i)
        if ln < 4 or i + ln > n:
            break
        yield tp & 0x7FFF, b[i + 4:i + ln]
        i += (ln + 3) & ~3


def tuple_of(v):
    src = dst = None
    proto = sport = dport = 0
    for a, x in attrs(v):
        if a == 1:  # CTA_TUPLE_IP
            for a2, y in attrs(x):
                if a2 == 1 and len(y) == 4:
                    src = socket.inet_ntop(socket.AF_INET, y)
                elif a2 == 2 and len(y) == 4:
                    dst = socket.inet_ntop(socket.AF_INET, y)
                elif a2 == 3 and len(y) == 16:
                    src = socket.inet_ntop(socket.AF_INET6, y)
                elif a2 == 4 and len(y) == 16:
                    dst = socket.inet_ntop(socket.AF_INET6, y)
        elif a == 2:  # CTA_TUPLE_PROTO
            for a2, y in attrs(x):
                if a2 == 1 and len(y) >= 1:
                    proto = y[0]
                elif a2 == 2 and len(y) == 2:
                    sport = struct.unpack(">H", y)[0]
                elif a2 == 3 and len(y) == 2:
                    dport = struct.unpack(">H", y)[0]
    return src, dst, proto, sport, dport


def ct_dump():
    """Flows of this namespace: (src, dst, proto, dport) of the original direction."""
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 12)  # NETLINK_NETFILTER
    s.settimeout(20)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
    except OSError:
        pass
    # nlmsghdr + nfgenmsg: CTNETLINK (1) << 8 | IPCTNL_MSG_CT_GET (1), REQUEST | DUMP, all families
    s.send(struct.pack("=IHHII", 20, (1 << 8) | 1, 0x1 | 0x300, 1, 0) + struct.pack("=BBH", 0, 0, 0))
    while True:
        data = s.recv(1 << 20)
        if not data:
            return
        rows, done = parse(data)
        for r in rows:
            yield r
        if done:
            return


def parse(data):
    """One recv of the dump: ([(src, dst, proto, dport)], whether the dump is over)"""
    rows = []
    i = 0
    while i + 16 <= len(data):
        ln, tp = struct.unpack_from("=IH", data, i)
        if ln < 16 or i + ln > len(data):
            return rows, True
        if tp == 3:  # NLMSG_DONE
            return rows, True
        if tp == 2:  # NLMSG_ERROR
            raise OSError("netlink error %d" % -struct.unpack_from("=i", data, i + 16)[0])
        for a, v in attrs(data[i + 20:i + ln]):
            if a == 1:  # CTA_TUPLE_ORIG
                src, dst, proto, _sp, dport = tuple_of(v)
                if src and dst:
                    rows.append((src, dst, proto, dport))
                break
        i += (ln + 3) & ~3
    return rows, False


def tally(flows, nets, own):
    """Per inner address of a client: [flows, udp, distinct remotes, distinct remotes on high ports
    outside COMMON, flows to the BitTorrent ports]; flows over FLOW_CAP are not counted"""
    cl = {}
    match = {}
    total = 0
    for src, dst, proto, dport in flows:
        total += 1
        if total > FLOW_CAP:
            break
        hit = match.get(src)
        if hit is None:
            try:
                a = ipaddress.ip_address(src)
                hit = src not in own and any(a in n for n in nets)
            except ValueError:
                hit = False
            match[src] = hit
        if not hit:
            continue
        c = cl.get(src)
        if c is None:
            c = cl[src] = [0, 0, set(), set(), 0]
        c[0] += 1
        if proto == 17:
            c[1] += 1
        c[2].add(dst)
        if dport >= 1024 and dport not in COMMON:
            c[3].add(dst)
        if dport in BT_PORTS:
            c[4] += 1
    return min(total, FLOW_CAP), {ip: [c[0], c[1], len(c[2]), len(c[3]), c[4]] for ip, c in cl.items()}


def vpn_nets(out):
    """`ip -o addr show` -> (client networks of the VPN interfaces, own addresses, "awg0 10.8.3.0/24")"""
    nets = []
    own = set()
    ifs = []
    for line in out.splitlines():
        f = line.split()
        if len(f) < 4 or f[2] not in ("inet", "inet6"):
            continue
        name = f[1].split("@")[0]
        if not VPN_IF.match(name):
            continue
        try:
            n = ipaddress.ip_interface(f[3])
        except ValueError:
            continue
        if n.ip.is_link_local or n.network.prefixlen == n.max_prefixlen:
            continue
        nets.append(n.network)
        own.add(str(n.ip))
        ifs.append("%s %s" % (name, n.network))
    return nets, own, ifs


def ns_main():
    """Runs inside the namespace (nsenter -n): its VPN interfaces and their clients, JSON on stdout."""
    try:
        out = subprocess.run(["ip", "-o", "addr", "show"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=10, universal_newlines=True).stdout
    except Exception:
        out = ""
    nets, own, ifs = vpn_nets(out)
    if not nets:
        print(json.dumps({"skip": "noaddr"}))
        return
    try:
        with open("/proc/sys/net/netfilter/nf_conntrack_count") as f:
            count = int(f.read().strip() or 0)
    except (OSError, ValueError):
        count = -1
    if count < 0:
        print(json.dumps({"ifaces": ifs, "skip": "noconntrack"}))
        return
    if count > FLOW_CAP:
        print(json.dumps({"ifaces": ifs, "count": count, "skip": "toomany"}))
        return
    try:
        total, cl = tally(ct_dump(), nets, own)
    except OSError as e:
        print(json.dumps({"ifaces": ifs, "count": count, "skip": "dump: %s" % e}))
        return
    print(json.dumps({"ifaces": ifs, "count": total, "clients": cl}))


def names():
    """container id -> name, from docker ps"""
    m = {}
    try:
        out = subprocess.run(["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=15, universal_newlines=True).stdout
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 2:
                m[f[0]] = f[1]
    except Exception:
        pass
    return m


CID = re.compile(r"([0-9a-f]{64})")


def where_of(pid, host_ino, ino, dn):
    if ino == host_ino:
        return "host"
    try:
        with open("/proc/%d/cgroup" % pid) as f:
            cg = f.read()
    except OSError:
        cg = ""
    ids = CID.findall(cg)
    for i in ids:
        if i in dn:
            return "container " + dn[i]
    if ids:
        return "container " + ids[-1][:12]
    return "netns %d" % ino


def _recent(h, nowm):
    return [x for x in h or [] if isinstance(x, list) and len(x) == 2 and isinstance(x[0], int)
            and nowm - HIST_MIN < x[0] < nowm]


def rows_of(where, clients, hist, nowm, nh):
    """Clients of one namespace for the report, the busiest first; the high-port peers of this minute
    are added to their history (nh: the new state)"""
    rows = []
    for ip, c in clients.items():
        k = where + "|" + ip
        h = _recent(hist.get(k), nowm) + [[nowm, c[3]]]
        nh[k] = h
        rows.append({"ip": ip, "flows": c[0], "udp": c[1], "peers": c[2], "hp": c[3], "bt": c[4],
                     "h": [x[1] for x in h]})
    rows.sort(key=lambda x: (-x["hp"], -x["flows"]))
    return rows[:CLIENT_CAP]


def keep(hist, nowm, nh):
    """A client with no flows this minute keeps its history: a torrent paused for a minute is the same torrent"""
    for k, h in hist.items():
        if k not in nh:
            h = _recent(h, nowm + 1)
            if h:
                nh[k] = h


def main():
    now = int(time.time())
    host_ino = os.stat("/proc/1/ns/net").st_ino
    seen = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            ino = os.stat("/proc/%s/ns/net" % d).st_ino
        except OSError:
            continue
        if ino not in seen:
            seen[ino] = int(d)
    cand = []
    for ino, pid in seen.items():
        try:
            with open("/proc/%d/net/dev" % pid) as f:
                ifn = [ln.split(":", 1)[0].strip() for ln in f.read().splitlines()[2:] if ":" in ln]
        except OSError:
            continue
        if any(VPN_IF.match(x) for x in ifn):
            cand.append((ino, pid))
    dn = names() if cand else {}
    try:
        with open(STATE) as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = {}
    except (OSError, ValueError):
        st = {}
    nowm = now // 60
    hist = st.get("h") if isinstance(st.get("h"), dict) else {}
    nh = {}
    nets = []
    skipped = []
    me = os.path.abspath(sys.argv[0])
    for ino, pid in sorted(cand, key=lambda x: x[1]):
        where = where_of(pid, host_ino, ino, dn)
        try:
            r = subprocess.run(["nsenter", "-t", str(pid), "-n", sys.executable, me, "--ns"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=60, universal_newlines=True)
            res = json.loads(r.stdout or "{}")
        except Exception:
            skipped.append({"where": where, "why": "nsenter"})
            continue
        if res.get("skip"):
            if res["skip"] != "noaddr":
                skipped.append({"where": where, "why": res["skip"], "ifaces": res.get("ifaces") or [],
                                "count": res.get("count") or 0})
            continue
        nets.append({"where": where, "ifaces": res.get("ifaces") or [], "flows": res.get("count") or 0,
                     "clients": rows_of(where, res.get("clients") or {}, hist, nowm, nh)})
    keep(hist, nowm, nh)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    tmp = "%s.tmp.%d" % (OUT, os.getpid())
    with open(tmp, "w") as f:
        json.dump({"v": 1, "ts": now, "nets": nets, "skipped": skipped}, f, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)
    try:
        stmp = STATE + ".tmp"
        with open(stmp, "w") as f:
            json.dump({"h": nh}, f, separators=(",", ":"))
        os.replace(stmp, STATE)
    except OSError:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--ns":
        ns_main()
    else:
        main()
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-vpnflow.service <<EOF
[Unit]
Description=Kervax: VPN clients by inner address (read only)
[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 $HELPER
Nice=10
TimeoutStartSec=3min
EOF
cat > /etc/systemd/system/kervax-vpnflow.timer <<'EOF'
[Unit]
Description=Kervax: look at the VPN clients every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-vpnflow.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-vpnflow.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/vpnflow-setup.ver"
chmod 0644 "$STATE_DIR/versions/vpnflow-setup.ver"
echo "✓ vpnflow-setup: VPN clients by inner address -> $OUT (every minute)."
