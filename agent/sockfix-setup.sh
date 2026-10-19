#!/usr/bin/env bash
# Kervax: a ready compose file that takes the docker socket away from an internet-facing proxy.
# caddy-docker-proxy reads container labels through /var/run/docker.sock, and that socket is root
# on the host: one hole in the proxy and the whole server is someone else's. The cure is a
# read-only socket proxy (wollomatic/socket-proxy) on an internal network that the proxy reads
# through instead. The panel used to show a generic snippet and leave the rest to a person: which
# file, what to replace and what to add. Now a root helper does the editing on the node itself.
#
# Every 10 minutes it finds running caddy-docker-proxy, traefik and nginx-proxy containers that
# hold the socket, and for caddy-docker-proxy started by docker compose v2 from one file it edits a
# copy of that very file: the socket leaves the volumes, DOCKER_HOST (and CADDY_INGRESS_NETWORKS
# when it is not set) joins the environment, the service joins the docker-api network, and the
# docker-api service and its internal network are added with the real gid of the socket. Text
# edits only, comments and the order of keys stay. docker compose config must accept the result
# and show the socket gone. The copy lands in /var/lib/kervax/sockfix/<project>.yml (root only:
# the compose file may hold secrets, they never leave the node); the report carries the diff with
# secret values masked, and the panel shows it with the commands to apply it and to roll back.
# Nothing is applied here: a person runs the command. A file it cannot edit safely (YAML anchors,
# keys written inline, several compose files, all of /var/run mounted, traefik) gets a reason.
# Writes /var/lib/kervax/report.d/sockfix.json; the agent hands report.d to the panel as is.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_WHEN="command -v docker >/dev/null 2>&1 && docker ps 2>/dev/null | grep -q -e caddy-docker-proxy -e traefik -e nginx-proxy -e docker-gen"

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-sockfix"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/sockfix.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"
install -d -m 0700 "$STATE_DIR/sockfix"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Internet-facing proxies holding the docker socket -> a fixed compose file in
# /var/lib/kervax/sockfix and /var/lib/kervax/report.d/sockfix.json. Changes nothing that runs.
set -u
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OUT=${KERVAX_SOCKFIX_OUT:-/var/lib/kervax/report.d/sockfix.json}
export KERVAX_SOCKFIX_DIR=${KERVAX_SOCKFIX_DIR:-/var/lib/kervax/sockfix}

exec 9>/run/kervax-sockfix.lock
flock -n 9 || exit 0
TMPD=$(mktemp -d /tmp/kervax-sockfix.XXXXXX) || exit 0
trap 'rm -rf "$TMPD"' EXIT
mkdir -p "${OUT%/*}"

put() { chmod 0644 "$1" && mv -f "$1" "$OUT"; }
if ! command -v docker >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
  why=nodocker; command -v docker >/dev/null 2>&1 && why=nopython
  printf '{"v":1,"ts":%s,"items":[],"err":"%s"}\n' "$(date +%s)" "$why" > "$TMPD/out.json"; put "$TMPD/out.json"; exit 0
fi
ids=$(timeout 10 docker ps -q 2>/dev/null) || ids=""
echo '[]' > "$TMPD/inspect.json"
# shellcheck disable=SC2086
[ -n "$ids" ] && timeout 30 docker inspect $ids > "$TMPD/inspect.json" 2>/dev/null
timeout 240 python3 - "$TMPD/inspect.json" > "$TMPD/out.json" <<'PY' || exit 0
import difflib
import json
import os
import re
import subprocess
import sys
import time

# internet-facing proxies that read container labels through the docker socket
EDGE = {"caddy-docker-proxy": "caddy", "traefik": "traefik", "nginx-proxy": "nginx-proxy", "docker-gen": "nginx-proxy"}
SOCKS = ("/var/run/docker.sock", "/run/docker.sock")
OUT_DIR = os.environ.get("KERVAX_SOCKFIX_DIR", "/var/lib/kervax/sockfix")
IMAGE = "wollomatic/socket-proxy:1.13.1"
ALLOW_GET = r"/(v[0-9]+\.[0-9]+/)?(_ping|version|info|events|containers/json|networks(/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?)"
SECRET = re.compile(r"(pass(word|wd)?|secret|token|api[_-]?key|auth|credential|private)", re.I)


class Bail(Exception):
    """why the file is not edited: a code for the panel and a detail"""
    def __init__(self, code, detail=""):
        Exception.__init__(self, code)
        self.code, self.detail = code, detail


def kind_of(image):
    base = (image or "").lower().split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]
    return EDGE.get(base)


def ind(line):
    if "\t" in line[:len(line) - len(line.lstrip(" \t"))]:
        raise Bail("layout", "tabs")
    return len(line) - len(line.lstrip(" "))


def sig(line):
    s = line.strip()
    return bool(s) and not s.startswith("#")


def rest(line):
    """the value after "key:" without a comment, "" for a block"""
    return re.sub(r"\s+#.*$", "", line.split(":", 1)[1]).strip()


def block(lines, start, end, indent):
    """keys at this indent inside [start, end): {key: (line, end)}"""
    keys, cur = {}, None
    rx = re.compile(r"^ {%d}[\"']?([A-Za-z_][\w.-]*)[\"']?:(\s.*|)$" % indent)
    for i in range(start, end):
        if not sig(lines[i]):
            continue
        if ind(lines[i]) < indent:
            raise Bail("layout", "line %d" % (i + 1))
        m = rx.match(lines[i].rstrip("\r\n"))
        if m:
            if cur:
                keys[cur[0]] = (cur[1], i)
            cur = (m.group(1), i)
    if cur:
        keys[cur[0]] = (cur[1], end)
    return keys


def last_sig(lines, start, end):
    j = start
    for i in range(start, end):
        if sig(lines[i]):
            j = i
    return j


def first_sig(lines, start, end):
    return next((i for i in range(start, end) if sig(lines[i])), None)


def items(lines, start, end):
    """list items of a block key: [(first, end, text)] and the indent of their "-" """
    out, cur, ii = [], None, None
    for i in range(start + 1, end):
        if not sig(lines[i]):
            continue
        if lines[i].lstrip(" ").startswith("-") and (ii is None or ind(lines[i]) == ii):
            ii = ind(lines[i])
            if cur is not None:
                out.append(cur)
            cur = [i, i + 1]
        elif cur is not None:
            cur[1] = i + 1
    if cur is not None:
        out.append(cur)
    return [(a, b, "".join(lines[a:b])) for a, b in out], ii


def fix_caddy(text, svc, nets, has_ingress, gid, sock):
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    top = block(lines, 0, len(lines), 0)
    if "services" not in top:
        raise Bail("v1")
    if "<<" in text or re.search(r"(^|\s)[&*][A-Za-z0-9_-]+", text):
        raise Bail("anchors")
    s0, s1 = top["services"]
    first = first_sig(lines, s0 + 1, s1)
    if rest(lines[s0]) or first is None:
        raise Bail("inline", "services")
    S = ind(lines[first])
    svcs = block(lines, s0 + 1, s1, S)
    if svc not in svcs:
        raise Bail("nosvc")
    if "docker-api" in svcs:
        raise Bail("dupsvc")
    a0, a1 = svcs[svc]
    bfirst = first_sig(lines, a0 + 1, a1)
    if rest(lines[a0]) or bfirst is None:
        raise Bail("inline", svc)
    K = ind(lines[bfirst])
    u = K - S
    keys = block(lines, a0 + 1, a1, K)
    for k in ("extends", "network_mode"):
        if k in keys:
            raise Bail(k.replace("_", ""))
    dels, ins = set(), {}

    def add(at, new):
        ins.setdefault(at, []).extend(x + nl for x in new)

    # 1. the socket leaves the volumes
    if "volumes" not in keys:
        raise Bail("elsewhere")
    v0, v1 = keys["volumes"]
    if rest(lines[v0]):
        raise Bail("inline", "volumes")
    its, _ii = items(lines, v0, v1)
    if any(re.search(r"(^|[\s\"'-])/(var/)?run/?[\"']?:", x[2]) for x in its):
        raise Bail("rundir")
    hit = [x for x in its if "docker.sock" in x[2]]
    if not hit:
        raise Bail("elsewhere")
    for a, b, _t in hit:
        dels.update(range(a, b))
    if len(hit) == len(its):
        dels.update(range(v0, v1))

    # 2. DOCKER_HOST and the ingress networks: without CADDY_INGRESS_NETWORKS caddy inspects its
    # own container to find them, and the socket proxy does not allow inspect
    env = ["DOCKER_HOST=tcp://docker-api:2375"]
    if not has_ingress:
        env.append("CADDY_INGRESS_NETWORKS=" + ",".join(nets))
    if "environment" in keys:
        e0, e1 = keys["environment"]
        if rest(lines[e0]):
            raise Bail("inline", "environment")
        if re.search(r"(^|[\s\"'-])DOCKER_HOST\b", "".join(lines[e0 + 1:e1])):
            raise Bail("dockerhost")
        eits, ei = items(lines, e0, e1)
        at = last_sig(lines, e0, e1) + 1
        if eits:
            add(at, [" " * ei + "- " + x for x in env])
        else:
            ef = first_sig(lines, e0 + 1, e1)
            mi = ind(lines[ef]) if ef is not None else K + u
            add(at, [" " * mi + x.replace("=", ": ", 1) for x in env])
    else:
        add(last_sig(lines, a0, a1) + 1, [" " * K + "environment:"] + [" " * (K + u) + "- " + x for x in env])

    # 3. the service joins the docker-api network
    if "networks" in keys:
        n0, n1 = keys["networks"]
        r = rest(lines[n0])
        if r:
            m = re.match(r"^\[(.*)\]$", r)
            if not m:
                raise Bail("inline", "networks")
            names = [x.strip() for x in m.group(1).split(",") if x.strip()]
            lines[n0] = lines[n0].replace(r, "[" + ", ".join(names + ["docker-api"]) + "]", 1)
        else:
            nits, ni = items(lines, n0, n1)
            at = last_sig(lines, n0, n1) + 1
            if nits:
                add(at, [" " * ni + "- docker-api"])
            else:
                add(at, [" " * ind(lines[first_sig(lines, n0 + 1, n1)]) + "docker-api: {}"])
    else:
        # it sat on the default network of the project: keep it there
        add(last_sig(lines, a0, a1) + 1, [" " * K + "networks:", " " * (K + u) + "- default", " " * (K + u) + "- docker-api"])

    # 4. the socket proxy, at the end of services
    def p(n, s):
        return " " * (S + n * u) + s
    add(last_sig(lines, s0, s1) + 1, [
        p(0, "docker-api:"),
        p(1, "# read-only docker API for %s: the proxy facing the internet no longer holds the socket" % svc),
        p(1, "image: " + IMAGE),
        p(1, "restart: unless-stopped"),
        p(1, 'user: "65534:%d"   # gid of %s' % (gid, sock)),
        p(1, "read_only: true"),
        p(1, "mem_limit: 64M"),
        p(1, "cap_drop: [ALL]"),
        p(1, 'security_opt: ["no-new-privileges:true"]'),
        p(1, "volumes:"),
        p(2, "- %s:/var/run/docker.sock:ro" % sock),
        p(1, "networks: [docker-api]"),
        p(1, "command:"),
        p(2, "- -listenip=0.0.0.0"),
        p(2, "- -allowfrom=" + svc),
        p(2, "- -watchdoginterval=600"),
        p(2, "- -stoponwatchdog"),
        p(2, "- -allowHEAD=/_ping"),
        p(2, "- -allowGET=" + ALLOW_GET),
    ])

    # 5. the internal network
    if "networks" in top:
        t0, t1 = top["networks"]
        if rest(lines[t0]):
            raise Bail("inline", "networks")
        tf = first_sig(lines, t0 + 1, t1)
        ti = ind(lines[tf]) if tf is not None else u
        add(last_sig(lines, t0, t1) + 1, [" " * ti + "docker-api:", " " * (ti + u) + "internal: true"])
    else:
        add(len(lines), ["networks:", " " * u + "docker-api:", " " * (2 * u) + "internal: true"])

    out = []
    for i in range(len(lines) + 1):
        out.extend(ins.get(i, []))
        if i < len(lines) and i not in dels:
            out.append(lines[i])
    return "".join(out)


def mask(line):
    """a diff line with a secret value: the key stays, the value goes"""
    m = re.match(r"^([+ -]?\s*-?\s*[\"']?([A-Za-z0-9_.-]+)[\"']?\s*[:=]\s*)(.+)$", line)
    if m and SECRET.search(m.group(2)):
        return m.group(1) + "***"
    return line


def run(cmd, timeout):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout)


def compose_version():
    try:
        r = run(["docker", "compose", "version", "--short"], 20)
        return r.stdout.strip().lstrip("v") if r.returncode == 0 else ""
    except Exception:
        return ""


def validate(path, wd, project, svc):
    """docker compose renders the fixed file: the socket is gone, DOCKER_HOST is set, docker-api is there"""
    cmd = ["docker", "compose", "-p", project, "--project-directory", wd, "-f", path, "config", "--format", "json"]
    r = run(cmd, 60)
    if r.returncode != 0 and "unknown flag" in r.stderr:
        # an old compose v2 without --format can only say whether the file is valid
        r = run(cmd[:-2] + ["-q"], 60)
        if r.returncode == 0:
            return
    if r.returncode != 0:
        raise Bail("validate", (r.stderr.strip().splitlines() or ["failed"])[-1][:200])
    cfg = json.loads(r.stdout)
    s = cfg["services"][svc]
    if any("docker.sock" in str(v.get("source", "")) for v in s.get("volumes") or []):
        raise Bail("validate", "the socket is still mounted")
    envs = s.get("environment") or {}
    if (envs.get("DOCKER_HOST") if isinstance(envs, dict) else None) != "tcp://docker-api:2375":
        raise Bail("validate", "DOCKER_HOST did not land")
    if "docker-api" not in cfg["services"] or "docker-api" not in (s.get("networks") or {}):
        raise Bail("validate", "docker-api did not land")


def one(c, cv, made):
    cfg = c.get("Config") or {}
    lab = cfg.get("Labels") or {}
    env = {}
    for e in cfg.get("Env") or []:
        k, _, v = e.partition("=")
        if k in ("CADDY_INGRESS_NETWORKS", "DOCKER_HOST"):
            env[k] = v
    sock = next((m.get("Source") for m in c.get("Mounts") or [] if m.get("Source") in SOCKS), SOCKS[0])
    it = {
        "container": (c.get("Name") or "").lstrip("/"),
        "kind": kind_of(cfg.get("Image")),
        "project": lab.get("com.docker.compose.project", ""),
        "service": lab.get("com.docker.compose.service", ""),
        "dir": lab.get("com.docker.compose.project.working_dir", ""),
        "files": [f for f in lab.get("com.docker.compose.project.config_files", "").split(",") if f],
        "nets": sorted(((c.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
        "sock": sock, "compose": cv, "ok": False,
    }
    try:
        it["gid"] = os.stat(sock).st_gid
    except OSError:
        it["gid"] = None
    try:
        if it["kind"] != "caddy":
            raise Bail("kind")
        if not it["project"] or not it["files"]:
            raise Bail("nocompose")
        if len(it["files"]) != 1:
            raise Bail("multi")
        if not cv or not cv.split(".")[0].isdigit() or int(cv.split(".")[0]) < 2:
            raise Bail("composev1")
        if it["gid"] is None:
            raise Bail("nosock")
        if env.get("DOCKER_HOST"):
            raise Bail("dockerhost")
        f = it["files"][0]
        if not os.path.isabs(f):
            f = os.path.join(it["dir"], f)
        it["file"] = f
        with open(f, encoding="utf-8") as fh:
            src = fh.read()
        new = fix_caddy(src, it["service"], [n for n in it["nets"] if n != "docker-api"],
                        "CADDY_INGRESS_NETWORKS" in env, it["gid"], sock)
        dst = os.path.join(OUT_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", it["project"]) + ".yml")
        fd = os.open(dst + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new)
        os.replace(dst + ".tmp", dst)
        made.add(os.path.basename(dst))
        validate(dst, it["dir"], it["project"], it["service"])
        it["fixed"] = dst
        diff = difflib.unified_diff(src.splitlines(), new.splitlines(), os.path.basename(f),
                                    os.path.basename(f) + " (fixed)", lineterm="", n=2)
        it["diff"] = "\n".join(mask(x) for x in diff)[:12000]
        it["ok"] = True
    except Bail as e:
        it["why"] = e.code
        if e.detail:
            it["why_detail"] = e.detail
    except Exception as e:  # anything unexpected is a reason, not a crash
        it["why"], it["why_detail"] = "unexpected", (str(e)[:200] or type(e).__name__)
    return it


def main(path):
    with open(path, encoding="utf-8") as fh:
        conts = json.load(fh) or []
    pick = [c for c in conts if kind_of((c.get("Config") or {}).get("Image"))
            and any(m.get("Source") in SOCKS for m in c.get("Mounts") or [])]
    cv = compose_version() if pick else ""
    os.makedirs(OUT_DIR, mode=0o700, exist_ok=True)
    made = set()
    res = [one(c, cv, made) for c in pick]
    # a proxy that no longer holds the socket leaves no stale file with its compose behind
    for f in os.listdir(OUT_DIR):
        if f not in made:
            os.remove(os.path.join(OUT_DIR, f))
    print(json.dumps({"v": 1, "ts": int(time.time()), "items": res}))


main(sys.argv[1])
PY
[ -s "$TMPD/out.json" ] && put "$TMPD/out.json"
exit 0
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-sockfix.service <<EOF
[Unit]
Description=Kervax: a compose file without the docker socket for internet-facing proxies
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=5min
EOF
cat > /etc/systemd/system/kervax-sockfix.timer <<'EOF'
[Unit]
Description=Kervax: prepare a compose file without the docker socket every 10 minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-sockfix.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-sockfix.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/sockfix-setup.ver"
chmod 0644 "$STATE_DIR/versions/sockfix-setup.ver"
echo "✓ sockfix-setup: a compose file without the docker socket -> $STATE_DIR/sockfix (every 10 minutes)."
