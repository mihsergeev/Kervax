#!/usr/bin/env bash
# Kervax: VPN clients. A root helper lists who uses the VPN and proxy services on the node every
# 2 minutes: WireGuard and AmneziaWG peers (wg/awg show all dump on the host and inside the Amnezia
# and wg-easy containers), 3x-ui clients from its database, Xray and sing-box users from their config
# (with traffic from the Xray stats API when it is on), OpenVPN clients from its status file, Headscale
# nodes and whether Tailscale on the node is an exit node or a subnet router. It writes
# /var/lib/kervax/report.d/vpn.json. The agent hands report.d to the panel as is.
#
# Per client only: a name (clientsTable of Amnezia, a comment in wg*.conf, wg-easy, the email of an
# Xray user, the common name of an OpenVPN certificate), the address inside the tunnel, the last
# handshake or connection, bytes both ways and the address the client came from. No keys, no
# configs: the private and preshared keys in the dump are dropped while it is parsed.
#
# Read only: docker exec runs only wg/awg show, cat of a config or a status file, headscale nodes
# list and xray api statsquery (without -reset). Nothing on the node is changed.
#
# Whether the node serves the VPN or only sits in it: a WireGuard interface whose config has a peer
# without Endpoint (that peer connects to us) or with many peers is a server, a tunnel to one or two
# fixed peers is not. Tailscale is a server only as an exit node or a subnet router.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
# A WireGuard, AmneziaWG or Tailscale interface, a VPN daemon on the host or a VPN or proxy
# container. No $, braces or apostrophes in the condition: it is double-quoted here and goes through
# Jinja and YAML in the playbook. The panel asks for the helper only on a narrower set (app/vpn.py)
KERVAX_SETUP_WHEN="ls /sys/class/net 2>/dev/null | grep -q -e ^wg -e ^awg -e ^tailscale || [ -d /etc/x-ui ] || pgrep -x openvpn >/dev/null 2>&1 || pgrep -x xray >/dev/null 2>&1 || pgrep -x sing-box >/dev/null 2>&1 || pgrep -x charon >/dev/null 2>&1 || (command -v docker >/dev/null 2>&1 && docker ps 2>/dev/null | grep -qi -e amnezia -e acontrol-awg -e xray -e v2ray -e v2fly -e sing-box -e singbox -e x-ui -e marzban -e marzneshin -e remnawave -e hiddify -e openvpn -e wireguard -e wg-easy -e wg-portal -e wgportal -e headscale -e netbirdio -e mtg -e mtproto -e telegrammessenger -e telemt -e shadowbox -e outline-ss -e shadowsocks -e ss-server -e strongswan -e ipsec -e libreswan -e ocserv -e softether -e pritunl -e hysteria -e trojan -e naiveproxy -e 3proxy -e squid -e dante -e sockd -e gost)"

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-vpn"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/vpn.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# VPN clients -> /var/lib/kervax/report.d/vpn.json. Read only.
set -u
export LC_ALL=C
OUT=/var/lib/kervax/report.d/vpn.json
command -v python3 >/dev/null 2>&1 || {
  t=$(mktemp "$OUT.XXXXXX") || exit 0
  printf '{"v":1,"ts":%s,"svc":[],"err":"nopython"}\n' "$(date +%s)" > "$t"
  chmod 0644 "$t" && mv -f "$t" "$OUT"
  exit 0
}
t=$(mktemp "$OUT.XXXXXX") || exit 0
if timeout 100 python3 - > "$t" <<'PY'
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

MAX = 500  # clients per service: the panel shows a table, not a dump of the base
NOW = int(time.time())

# the same images the panel knows (backend app/vpn.py CATALOG), first match wins
CATALOG = [
    ("awg", r"amnezia-?awg|acontrol-awg|amneziawg"),
    ("xray", r"amnezia-xray"),
    ("openvpn", r"amnezia-openvpn|openvpn"),
    ("3x-ui", r"3x-ui|(^|/)x-ui(:|$)"),
    ("xray", r"xray"),
    ("v2ray", r"v2ray|v2fly"),
    ("sing-box", r"sing-?box"),
    ("wg", r"wg-easy|wireguard|wg-portal|wgportal"),
    ("headscale", r"headscale"),
]
PANEL = re.compile(r"amnezia-control|acontrol-(backend|frontend)")
CTRL = re.compile(r"[\x00-\x1f\x7f]")


def run(args, timeout=15):
    try:
        p = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout)
        return p.stdout.decode("utf-8", "replace") if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def clean(s, n=64):
    return CTRL.sub("", str(s or ""))[:n]


def ep_ip(ep):
    """203.0.113.4:5555 or [2001:db8::1]:5555 -> the address only"""
    ep = (ep or "").strip()
    if not ep or ep == "(none)":
        return ""
    if ep.startswith("["):
        return clean(ep[1:].split("]", 1)[0], 45)
    return clean(ep.rsplit(":", 1)[0] if ep.count(":") == 1 else ep, 45)


def tun_ip(allowed):
    """the first address of a peer that is a client: 10.8.1.2/32 -> 10.8.1.2; a default route means
    the peer is our upstream, not a client"""
    for a in (allowed or "").split(","):
        a = a.strip()
        if not a or a == "(none)" or a.endswith("/0"):
            continue
        return clean(a.split("/", 1)[0], 45)
    return ""


def parse_dump(text):
    """wg/awg show all dump -> {iface: {"port": n, "peers": [{pub, ep, ip, hs, rx, tx}]}}. The first
    line of an interface carries its private key and is read only for the listen port; the preshared
    key of a peer is never taken"""
    out = {}
    for line in (text or "").splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        ifc = f[0]
        if ifc not in out:
            port = f[3] if f[3].isdigit() else "0"
            out[ifc] = {"port": int(port), "peers": []}
            continue
        if len(f) < 8:
            continue
        out[ifc]["peers"].append({
            "pub": f[1], "ep": ep_ip(f[3]), "ip": tun_ip(f[4]), "allowed": f[4],
            "hs": int(f[5]) if f[5].isdigit() else 0,
            "rx": int(f[6]) if f[6].isdigit() else 0,
            "tx": int(f[7]) if f[7].isdigit() else 0,
        })
    return out


def conf_peers(text):
    """[Peer] sections of a wg-quick config: {public key: (name, has Endpoint)}. The name is a
    comment right above [Peer] or inside it: "# alice", "### Client alice", "# Name = alice"."""
    out, cur, note, last = {}, None, "", ""
    for raw in (text or "").splitlines():
        s = raw.strip()
        if s.startswith("#"):
            c = s.lstrip("#").strip()
            c = re.sub(r"^(client|name|friendly_name|peer)\s*[:=]?\s*", "", c, flags=re.I)
            if cur is not None and not cur["name"]:
                cur["name"] = c
            else:
                last = c
            continue
        if s.lower() == "[peer]":
            if cur is not None and cur.get("pub"):
                out[cur["pub"]] = (cur["name"], cur["ep"])
            cur, note, last = {"name": last, "ep": False, "pub": ""}, "", ""
            continue
        if s.startswith("["):
            if cur is not None and cur.get("pub"):
                out[cur["pub"]] = (cur["name"], cur["ep"])
            cur, last = None, ""
            continue
        if cur is not None and "=" in s:
            k, v = s.split("=", 1)
            k = k.strip().lower()
            if k == "publickey":
                cur["pub"] = v.strip()
            elif k == "endpoint":
                cur["ep"] = True
        elif not s:
            last = "" if cur is None else last
    if cur is not None and cur.get("pub"):
        out[cur["pub"]] = (cur["name"], cur["ep"])
    return out


def table_names(text):
    """clientsTable of Amnezia: [{clientId, userData: {clientName}}] -> {clientId: name}"""
    try:
        rows = json.loads(text or "[]")
    except ValueError:
        return {}
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and r.get("clientId"):
            out[str(r["clientId"])] = clean(((r.get("userData") or {}).get("clientName")) or "")
    return out


def wg_service(kind, src, dump, names, confs, pid=None):
    """one service per interface; names - {pub: name}, confs - {pub: (name, has Endpoint)}"""
    out = []
    for ifc, d in sorted(dump.items()):
        # a peer that takes the default route is our way out (a cascade, a client config), not a client
        peers = [p for p in d["peers"] if p["ip"] or not any(a.strip().endswith("/0") for a in p["allowed"].split(","))]
        # a peer without Endpoint in the config connects to us; no config: many peers is a server
        if confs:
            server = any(not confs.get(p["pub"], ("", True))[1] for p in peers)
        else:
            server = len(peers) >= 3 or src != "host"
        cl = []
        for p in sorted(peers, key=lambda x: -x["hs"])[:MAX]:
            name = names.get(p["pub"]) or confs.get(p["pub"], ("", True))[0]
            cl.append({"name": clean(name), "ip": p["ip"], "hs": p["hs"], "rx": p["rx"], "tx": p["tx"], "ep": p["ep"]})
        sv = {"k": kind, "src": src, "if": clean(ifc, 32), "port": d["port"], "proto": "udp",
              "role": "server" if server else "mesh", "n": len(peers), "clients": cl}
        if pid:
            sv["pid"] = pid
        out.append(sv)
    return out


def dexec(ct, *args, timeout=15):
    return run(["docker", "exec", ct] + list(args), timeout)


def host_path(ct, path):
    """a file of the container through its bind mount, or None"""
    best = None
    for m in ct.get("mounts") or []:
        d = (m.get("Destination") or "").rstrip("/")
        if d and (path == d or path.startswith(d + "/")) and m.get("Source"):
            if best is None or len(d) > len(best[0]):
                best = (d, m["Source"])
    return best[1] + path[len(best[0]):] if best else None


def cread(ct, path, timeout=10):
    hp = host_path(ct, path)
    if hp:
        try:
            with open(hp, encoding="utf-8", errors="replace") as f:
                return f.read(4 << 20)
        except OSError:
            pass
    return dexec(ct["name"], "cat", path, timeout=timeout)


def containers():
    ids = (run(["docker", "ps", "-q"], 10) or "").split()
    if not ids:
        return []
    try:
        data = json.loads(run(["docker", "inspect"] + ids, 30) or "[]")
    except ValueError:
        return []
    out = []
    for c in data:
        img = str((c.get("Config") or {}).get("Image") or "").lower()
        if PANEL.search(img):
            continue
        kind = next((k for k, rx in CATALOG if re.search(rx, img)), None)
        if not kind:
            continue
        cfg = c.get("Config") or {}
        out.append({"name": (c.get("Name") or "").lstrip("/"), "kind": kind, "image": img,
                    "mounts": c.get("Mounts") or [], "pid": (c.get("State") or {}).get("Pid") or 0,
                    "cmd": [str(x) for x in (cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or [])]})
    return out


def awg_ct(ct):
    dump = dexec(ct["name"], "sh", "-c", "for t in awg wg; do command -v $t >/dev/null 2>&1 && exec $t show all dump; done; exit 1")
    if dump is None:
        return [{"k": "awg", "src": ct["name"], "err": "awg show failed"}]
    names = table_names(cread(ct, "/opt/amnezia/awg/clientsTable"))
    return wg_service("awg", ct["name"], parse_dump(dump), names, {}, ct["pid"])


def wgeasy_ct(ct):
    dump = dexec(ct["name"], "wg", "show", "all", "dump")
    if dump is None:
        return [{"k": "wg", "src": ct["name"], "err": "wg show failed"}]
    names = {}
    try:
        doc = json.loads(cread(ct, "/etc/wireguard/wg0.json") or "{}")
        for c in (doc.get("clients") or {}).values():
            if isinstance(c, dict) and c.get("publicKey"):
                names[c["publicKey"]] = clean(c.get("name"))
    except (ValueError, AttributeError):
        pass
    if not names:
        db = host_path(ct, "/etc/wireguard/wg-easy.db")
        for r in sqlite_rows(db, "select public_key, name from clients_table"):
            names[str(r[0])] = clean(r[1])
    return wg_service("wg", ct["name"], parse_dump(dump), names, {}, ct["pid"])


def sqlite_rows(path, q):
    if not path or not os.path.isfile(path):
        return []
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        try:
            return c.execute(q).fetchall()
        finally:
            c.close()
    except sqlite3.Error:
        return []


def protos_of(inbounds, key="protocol"):
    out = set()
    for ib in inbounds:
        p = str(ib.get(key) or "").lower()
        if p and p not in ("dokodemo-door", "freedom", "blackhole", "direct", "tun", "mixed", "dns"):
            out.add(p)
    return sorted(out)


def xui(src, db, pid=None):
    ibs = sqlite_rows(db, "select id, protocol, port, enable, settings from inbounds")
    if not ibs:
        return [{"k": "3x-ui", "src": src, "err": "no database"}]
    traffic = {}
    for r in sqlite_rows(db, "select email, up, down, last_online from client_traffics"):
        traffic[str(r[0])] = r
    if not traffic:
        for r in sqlite_rows(db, "select email, up, down, 0 from client_traffics"):
            traffic[str(r[0])] = r
    ips = {}
    for r in sqlite_rows(db, "select client_email, ips from inbound_client_ips"):
        try:
            v = json.loads(r[1] or "[]")
            ips[str(r[0])] = ep_ip(str(v[0]).split(" ", 1)[0]) if v else ""
        except (ValueError, IndexError, TypeError):
            pass
    cl, n, protos, ports = [], 0, set(), []
    for _id, proto, port, enable, settings in ibs:
        if not enable:
            continue
        protos.add(str(proto or "").lower())
        if port:
            ports.append(int(port))
        try:
            st = json.loads(settings or "{}")
        except ValueError:
            continue
        for c in st.get("clients") or []:
            if not isinstance(c, dict) or c.get("enable") is False:
                continue
            n += 1
            em = str(c.get("email") or "")
            t = traffic.get(em)
            lo = int(t[3] or 0) if t else 0
            lo = lo // 1000 if lo > 10 ** 11 else lo  # newer 3x-ui keeps milliseconds
            cl.append({"name": clean(em), "ip": "", "hs": lo, "rx": int(t[1] or 0) if t else 0,
                       "tx": int(t[2] or 0) if t else 0, "ep": ips.get(em, "")})
    cl.sort(key=lambda x: (-x["hs"], -(x["rx"] + x["tx"])))
    sv = {"k": "3x-ui", "src": src, "role": "server", "protos": sorted(p for p in protos if p),
          "n": n, "clients": cl[:MAX]}
    if ports:
        sv["port"], sv["proto"] = ports[0], "tcp"
    if pid:
        sv["pid"] = pid
    return [sv]


def xray_conf_paths(ct):
    out = []
    cmd = ct["cmd"]
    for i, a in enumerate(cmd):
        if a in ("-c", "-config", "--config", "-C") and i + 1 < len(cmd):
            out.append(cmd[i + 1])
        elif a.startswith("-config=") or a.startswith("--config="):
            out.append(a.split("=", 1)[1])
    return out + ["/opt/amnezia/xray/server.json", "/etc/xray/config.json", "/usr/local/etc/xray/config.json",
                  "/etc/v2ray/config.json", "/usr/local/etc/v2ray/config.json", "/etc/sing-box/config.json"]


def xray_ct(ct):
    conf, path = None, ""
    for p in xray_conf_paths(ct):
        if p.endswith("/") or not p.startswith("/"):
            continue
        text = cread(ct, p)
        if not text:
            continue
        try:
            conf, path = json.loads(text), p
            break
        except ValueError:
            continue
    if not isinstance(conf, dict):
        return [{"k": ct["kind"], "src": ct["name"], "role": "server", "err": "config not found"}]
    names = table_names(cread(ct, "/opt/amnezia/xray/clientsTable")) if path.startswith("/opt/amnezia/") else {}
    ibs = [ib for ib in conf.get("inbounds") or [] if isinstance(ib, dict)]
    singbox = any("type" in ib and "protocol" not in ib for ib in ibs)
    protos = protos_of(ibs, "type" if singbox else "protocol")
    users, ports = [], []
    for ib in ibs:
        if str(ib.get("tag") or "") == "api" or str(ib.get("protocol") or "") == "dokodemo-door":
            continue
        if ib.get("port") or ib.get("listen_port"):
            try:
                ports.append(int(ib.get("port") or ib.get("listen_port")))
            except (TypeError, ValueError):
                pass
        if singbox:
            us = ib.get("users") or []
        else:
            us = (ib.get("settings") or {}).get("clients") or []
        for u in us:
            if not isinstance(u, dict):
                continue
            uid = str(u.get("id") or u.get("uuid") or u.get("password") or "")
            em = str(u.get("email") or u.get("name") or "")
            # Amnezia sets email = UUID; its names live in clientsTable by the UUID
            users.append((em or uid, names.get(uid) or names.get(em) or em))
    stats = xray_stats(ct, conf) if not singbox else {}
    cl = []
    for key, name in users:
        st = stats.get(key) or {}
        cl.append({"name": clean(name or key[:8]), "ip": "", "hs": 0, "rx": st.get("up", 0), "tx": st.get("down", 0), "ep": ""})
    cl.sort(key=lambda x: -(x["rx"] + x["tx"]))
    # only a local SOCKS or HTTP inbound and no users: the entry of a cascade or a proxy for this node,
    # it goes out through someone else and serves nobody
    serves = bool(users) or bool(set(protos) - {"socks", "http"})
    sv = {"k": "sing-box" if singbox and ct["kind"] == "xray" else ct["kind"], "src": ct["name"],
          "role": "server" if serves else "mesh",
          "protos": protos, "n": len(users), "clients": cl[:MAX], "pid": ct["pid"]}
    if ports:
        sv["port"], sv["proto"] = ports[0], "tcp"
    return [sv]


def xray_stats(ct, conf):
    """per-user traffic from the stats API of Xray, if the config has it (api inbound on localhost)"""
    if "stats" not in conf or not isinstance(conf.get("api"), dict):
        return {}
    port = 0
    for ib in conf.get("inbounds") or []:
        if isinstance(ib, dict) and (ib.get("tag") == conf["api"].get("tag") or ib.get("tag") == "api"):
            try:
                port = int(ib.get("port") or 0)
            except (TypeError, ValueError):
                port = 0
    if not port:
        return {}
    text = dexec(ct["name"], "xray", "api", "statsquery", "--server=127.0.0.1:%d" % port, "-pattern", "user>>>")
    try:
        doc = json.loads(text or "{}")
    except ValueError:
        return {}
    out = {}
    for s in doc.get("stat") or []:
        parts = str(s.get("name") or "").split(">>>")
        if len(parts) == 4 and parts[0] == "user" and parts[2] == "traffic":
            d = out.setdefault(parts[1], {"up": 0, "down": 0})
            d["up" if parts[3] == "uplink" else "down"] = int(s.get("value") or 0)
    return out


def ovpn_since(s):
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    try:
        return int(time.mktime(time.strptime(s, "%a %b %d %H:%M:%S %Y")))
    except ValueError:
        return 0


def parse_ovpn_status(text):
    """status file of OpenVPN, versions 1, 2 and 3 -> [{cn, ep, ip, rx, tx, since}]"""
    rows, virt = {}, {}
    sect = ""
    for line in (text or "").splitlines():
        f = re.split(r"[,\t]", line.strip())
        if not f or not f[0]:
            continue
        if f[0] in ("OpenVPN CLIENT LIST", "ROUTING TABLE", "GLOBAL STATS"):
            sect = f[0]
            continue
        if f[0] == "CLIENT_LIST" and len(f) >= 8:
            rows[f[1] + "|" + f[2]] = {"cn": f[1], "ep": ep_ip(f[2]), "ip": f[3], "rx": int(f[5] or 0) if f[5].isdigit() else 0,
                                       "tx": int(f[6] or 0) if f[6].isdigit() else 0,
                                       "since": ovpn_since(f[8] if len(f) > 8 else f[7])}
        elif sect == "OpenVPN CLIENT LIST" and len(f) >= 5 and f[0] != "Common Name" and f[0] != "Updated":
            rows[f[0] + "|" + f[1]] = {"cn": f[0], "ep": ep_ip(f[1]), "ip": "", "rx": int(f[2]) if f[2].isdigit() else 0,
                                       "tx": int(f[3]) if f[3].isdigit() else 0, "since": ovpn_since(f[4])}
        elif sect == "ROUTING TABLE" and len(f) >= 3 and f[0] != "Virtual Address":
            virt[f[1] + "|" + f[2]] = f[0]
    for k, ip in virt.items():
        # tap mode routes by MAC: only addresses
        if k in rows and not rows[k]["ip"] and (re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) or "::" in ip):
            rows[k]["ip"] = ip
    return list(rows.values())


def ovpn_service(src, status, names, n=None, pid=None):
    cl = []
    for r in parse_ovpn_status(status)[:MAX]:
        cl.append({"name": clean(names.get(r["cn"]) or r["cn"]), "ip": clean(r["ip"], 45), "hs": r["since"],
                   "rx": r["rx"], "tx": r["tx"], "ep": r["ep"], "on": True})
    sv = {"k": "openvpn", "src": src, "role": "server", "clients": cl}
    if n is not None:
        sv["n"] = n
    if pid:
        sv["pid"] = pid
    return sv


def status_paths(confs_text, base_dirs):
    out = []
    for line in (confs_text or "").splitlines():
        f = line.split()
        if len(f) >= 2 and f[0] == "status":
            p = f[1]
            out += [p] if p.startswith("/") else [d.rstrip("/") + "/" + p for d in base_dirs]
    return out


def openvpn_ct(ct):
    confs = dexec(ct["name"], "sh", "-c", "cat /etc/openvpn/*.conf /etc/openvpn/server/*.conf /opt/amnezia/openvpn/*.conf 2>/dev/null")
    paths = status_paths(confs, ["/", "/etc/openvpn", "/opt/amnezia/openvpn"]) + [
        "/tmp/openvpn-status.log", "/openvpn-status.log", "/etc/openvpn/openvpn-status.log"]
    status = None
    for p in paths:
        status = cread(ct, p)
        if status:
            break
    if not status:
        return [{"k": "openvpn", "src": ct["name"], "role": "server", "err": "status file not found"}]
    table = cread(ct, "/opt/amnezia/openvpn/clientsTable")
    names = table_names(table)
    n = len(names) if table else None
    if n is None:
        issued = dexec(ct["name"], "ls", "/etc/openvpn/pki/issued")
        if issued is not None:
            # kylemanna: one certificate per client plus the server's own
            n = max(0, len([x for x in issued.split() if x.endswith(".crt")]) - 1)
    return [ovpn_service(ct["name"], status, names, n, ct["pid"])]


def headscale_ct(ct):
    text = dexec(ct["name"], "headscale", "nodes", "list", "-o", "json", timeout=20)
    try:
        nodes = json.loads(text or "null")
    except ValueError:
        nodes = None
    if not isinstance(nodes, list):
        return [{"k": "headscale", "src": ct["name"], "role": "server", "err": "headscale nodes list failed"}]
    cl = []
    for nd in nodes[:MAX]:
        if not isinstance(nd, dict):
            continue
        ls = nd.get("last_seen") or {}
        seen = int(ls.get("seconds") or 0) if isinstance(ls, dict) else 0
        ips = [a for a in nd.get("ip_addresses") or [] if isinstance(a, str)]
        ip4 = next((a for a in ips if "." in a), ips[0] if ips else "")
        cl.append({"name": clean(nd.get("given_name") or nd.get("name")), "ip": clean(ip4, 45), "hs": seen,
                   "rx": 0, "tx": 0, "ep": "", "on": bool(nd.get("online"))})
    return [{"k": "headscale", "src": ct["name"], "role": "server", "n": len(nodes), "clients": cl, "pid": ct["pid"]}]


def host_wg():
    out = []
    for tool, kind, conf_dirs in (("wg", "wg", ["/etc/wireguard"]), ("awg", "awg", ["/etc/amnezia/amneziawg", "/etc/amnezia/awg"])):
        if not run(["sh", "-c", "command -v %s" % tool], 5):
            continue
        dump = parse_dump(run([tool, "show", "all", "dump"], 10))
        # awg also shows plain wg interfaces and the other way round: each by its own name
        dump = {k: v for k, v in dump.items() if (k.startswith("awg") or k.startswith("amn")) == (kind == "awg")}
        for ifc, d in dump.items():
            confs = {}
            for cd in conf_dirs:
                try:
                    with open(os.path.join(cd, ifc + ".conf"), encoding="utf-8", errors="replace") as f:
                        confs = conf_peers(f.read(1 << 20))
                    break
                except OSError:
                    pass
            out += wg_service(kind, "host", {ifc: d}, {}, confs)
    return out


def host_openvpn():
    confs = ""
    for p in glob.glob("/etc/openvpn/*.conf") + glob.glob("/etc/openvpn/server/*.conf"):
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                confs += f.read(1 << 20) + "\n"
        except OSError:
            pass
    paths = status_paths(confs, ["/etc/openvpn", "/etc/openvpn/server"]) + sorted(
        glob.glob("/run/openvpn-server/status-*.log") + glob.glob("/run/openvpn/*.status")
        + glob.glob("/var/log/openvpn/*status*.log"))
    out, seen = [], set()
    for p in paths:
        rp = os.path.realpath(p)
        if rp in seen or not os.path.isfile(rp):
            continue
        seen.add(rp)
        try:
            with open(rp, encoding="utf-8", errors="replace") as f:
                out.append(ovpn_service("host", f.read(4 << 20), {}))
        except OSError:
            pass
    return out


def host_xui():
    db = "/etc/x-ui/x-ui.db"
    return xui("host", db) if os.path.isfile(db) else []


def tailscale():
    if not run(["sh", "-c", "command -v tailscale"], 5):
        return []
    try:
        st = json.loads(run(["tailscale", "status", "--json"], 15) or "{}")
    except ValueError:
        return []
    me = st.get("Self") or {}
    if not me:
        return []
    allowed = [a for a in me.get("AllowedIPs") or [] if isinstance(a, str)]
    prim = [a for a in me.get("PrimaryRoutes") or [] if isinstance(a, str)]
    exit_node = any(a.endswith("/0") for a in allowed + prim)
    routes = sorted(set(a for a in prim + allowed if not a.endswith("/32") and not a.endswith("/128")))
    sv = {"k": "tailscale", "src": "host", "if": "tailscale0", "role": "server" if exit_node or routes else "mesh",
          "routes": routes[:8]}
    if exit_node:
        sv["exit"] = True
    return [sv]


def main():
    svc = []
    for ct in containers():
        try:
            if ct["kind"] == "awg":
                svc += awg_ct(ct)
            elif ct["kind"] == "wg":
                svc += wgeasy_ct(ct)
            elif ct["kind"] == "3x-ui":
                svc += xui(ct["name"], host_path(ct, "/etc/x-ui/x-ui.db"), ct["pid"])
            elif ct["kind"] in ("xray", "v2ray", "sing-box"):
                svc += xray_ct(ct)
            elif ct["kind"] == "openvpn":
                svc += openvpn_ct(ct)
            elif ct["kind"] == "headscale":
                svc += headscale_ct(ct)
        except Exception as e:  # one broken container must not hide the others
            svc.append({"k": ct["kind"], "src": ct["name"], "err": clean(type(e).__name__, 40)})
    for fn in (host_wg, host_openvpn, host_xui, tailscale):
        try:
            svc += fn()
        except Exception as e:
            svc.append({"k": fn.__name__, "src": "host", "err": clean(type(e).__name__, 40)})
    json.dump({"v": 1, "ts": NOW, "svc": svc}, sys.stdout, separators=(",", ":"))


main()
PY
then
  chmod 0644 "$t" && mv -f "$t" "$OUT"
else
  rm -f "$t"
fi
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"

cat > /etc/systemd/system/kervax-vpn.service <<EOF
[Unit]
Description=Kervax: VPN clients (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
TimeoutStartSec=3min
EOF
cat > /etc/systemd/system/kervax-vpn.timer <<'EOF'
[Unit]
Description=Kervax: list VPN clients every 2 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
AccuracySec=10s
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-vpn.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-vpn.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/vpn-setup.ver"
chmod 0644 "$STATE_DIR/versions/vpn-setup.ver"
echo "✓ vpn-setup: VPN services and their clients -> $OUT (every 2 minutes)."
