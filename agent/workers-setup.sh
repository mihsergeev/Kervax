#!/usr/bin/env bash
# Kervax: background workers kept alive by supervisor or pm2. A queue worker, a listener or a
# daemon under supervisord or pm2 is invisible to systemd and to Docker: supervisor restarts it,
# gives up after a few failed starts (FATAL) and nobody notices until its work piles up. A root
# helper finds every supervisord and pm2 daemon on the node by its process (docker, Kubernetes
# pods and the host alike), asks it for its programs every minute and writes
# /var/lib/kervax/report.d/workers.json; the agent hands report.d to the panel as is.
#
# supervisord is asked over its own XML-RPC socket with its own python and supervisor module,
# inside its mount and network namespaces: the socket path and the credentials come from its
# config ([supervisorctl], else [unix_http_server] or [inet_http_server]), they never leave the
# namespace. pm2 is asked over its rpc.sock with the axon client shipped inside pm2 itself
# (getMonitorData, what "pm2 jlist" calls), by the daemon's own node. Not "pm2 jlist": the pm2
# command starts a new daemon when it does not find one, and a wrong PM2_HOME would do just that.
#
# For each program: its state, since when, its uptime, the exit code, and how many times it was
# restarted in the last 15 minutes and hour - counted here between runs by its start time. For
# a program that is down or was started within 15 minutes, the last line of its log (stderr,
# else stdout), with values after password=, token= and the like masked. Read only.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: without supervisord or pm2 it runs one ps and exits

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-workers"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/workers.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes,
# and rewriting it in place would make a run in progress continue from the same byte offset.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Workers under supervisord and pm2 -> /var/lib/kervax/report.d/workers.json. Read only:
# supervisor.getAllProcessInfo and tail*Log, pm2 getMonitorData and the tail of its log files.
set -u
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OUT=${KERVAX_WORKERS_OUT:-/var/lib/kervax/report.d/workers.json}
CACHE=${KERVAX_WORKERS_CACHE:-/var/lib/kervax/workers.cache}
TO=15         # seconds for one daemon
MAX_PROGS=200 # programs per daemon in the report

exec 9>/run/kervax-workers.lock
flock -n 9 || exit 0
NOW=$(date +%s)

# -- daemons: one ps; supervisord (also "python -m supervisor.supervisord"), the pm2 God daemon
# and pm2-runtime, which runs the God daemon inside itself (docker images) --
DAEMONS=$(ps -e -o pid=,args= 2>/dev/null | awk '
  { a = $0; sub(/^ *[0-9]+ /, "", a) }
  a ~ /(^|[ \/])supervisord( |$)|supervisor\.supervisord/ && a !~ /supervisorctl/ { print $1 "\tsupervisor"; next }
  a ~ /^PM2 v[0-9.]+: God Daemon/ || a ~ /(^|[ \/])pm2-runtime( |$)/ { print $1 "\tpm2" }')
if [ -z "$DAEMONS" ]; then
  # nothing to watch: an empty report, so the panel knows the helper works
  mkdir -p "${OUT%/*}"
  printf '{"v":1,"ts":%s,"daemons":[],"skipped":[]}\n' "$NOW" > "$OUT.tmp.$$" && chmod 0644 "$OUT.tmp.$$" && mv -f "$OUT.tmp.$$" "$OUT"
  rm -f "$CACHE"
  exit 0
fi
TMPD=$(mktemp -d /tmp/kervax-workers.XXXXXX) || exit 0
trap 'rm -rf "$TMPD"' EXIT

declare -A DNAME
if command -v docker >/dev/null 2>&1; then
  while read -r id name; do [ -n "$id" ] && DNAME[$id]=$name; done \
    < <(timeout 10 docker ps --no-trunc --format '{{.ID}} {{.Names}}' 2>/dev/null)
fi
cid_of() { # pid -> container id from its cgroup
  sed 's#.*/##' "/proc/$1/cgroup" 2>/dev/null | sed 's/^docker-//; s/^cri-containerd-//; s/^crio-//; s/\.scope$//' \
    | grep -m1 -E '^[0-9a-f]{64}$'
}
spec_of() { # container id -> its OCI config.json
  ls -d /run/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /run/k0s/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /run/k3s/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /var/snap/microk8s/common/run/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /run/docker/containerd/daemon/io.containerd.runtime.v2.task/*/"$1"/config.json 2>/dev/null | head -1
}
where_of() { # pid -> "container X" | "pod ns/name" | "host"
  local p=$1 id spec ns name
  id=$(cid_of "$p")
  [ -n "$id" ] && [ -n "${DNAME[$id]:-}" ] && { echo "container ${DNAME[$id]}"; return; }
  [ -n "$id" ] && spec=$(spec_of "$id") || spec=""
  if [ -n "$spec" ]; then
    name=$(grep -o '"io.kubernetes.cri.sandbox-name":"[^"]*"' "$spec" | head -1 | cut -d'"' -f4)
    ns=$(grep -o '"io.kubernetes.cri.sandbox-namespace":"[^"]*"' "$spec" | head -1 | cut -d'"' -f4)
    [ -n "$name" ] && { echo "pod ${ns:+$ns/}$name"; return; }
  fi
  [ -n "$id" ] && { echo "container ${id:0:12}"; return; }
  echo host
}
in_root() { [ -e "/proc/$1/root$2" ]; }  # pid path

# supervisord: its python, its config; prints "P name state start now pid exit desc log" or "E why"
SUP_PY='
import sys, re, os
try:
    import xmlrpc.client as xc
except ImportError:
    import xmlrpclib as xc
try:
    from configparser import RawConfigParser
except ImportError:
    from ConfigParser import RawConfigParser
from supervisor.xmlrpc import SupervisorTransport
conf = sys.argv[1]
here = os.path.dirname(os.path.abspath(conf))
cands = []
try:
    from supervisor.options import ClientOptions
    o = ClientOptions()
    o.realize(args=["-c", conf], doc="")
    cands.append((o.serverurl, o.username, o.password))
except BaseException:
    pass
norpc = False
try:
    c = RawConfigParser()
    c.read(conf)
    # without [unix_http_server] and [inet_http_server] supervisord has no control socket at all
    # (docker-mailserver): nobody, supervisorctl included, can ask it
    norpc = not (c.has_section("unix_http_server") or c.has_section("inet_http_server") or c.has_section("include"))
    def g(sec, k):
        return c.get(sec, k).replace("%(here)s", here) if c.has_option(sec, k) else None
    if g("unix_http_server", "file"):
        cands.append(("unix://" + g("unix_http_server", "file"), g("unix_http_server", "username"), g("unix_http_server", "password")))
    if g("inet_http_server", "port"):
        cands.append(("http://127.0.0.1:" + g("inet_http_server", "port").split(":")[-1], g("inet_http_server", "username"), g("inet_http_server", "password")))
except BaseException:
    pass
info, srv, why = None, None, "down"
for url, user, pw in cands:
    if not url:
        continue
    try:
        srv = xc.ServerProxy("http://127.0.0.1", transport=SupervisorTransport(user, pw, url))
        info = srv.supervisor.getAllProcessInfo()
        break
    except xc.ProtocolError as e:
        why = "auth" if e.errcode == 401 else why
    except BaseException:
        pass
if info is None:
    print("E\t" + ("norpc" if norpc and why == "down" else why))
    sys.exit(0)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SECRET = re.compile(r"(?i)(pass(word|wd)?|secret|token|api[_-]?key|authorization)([\"\x27]?\s*[=:]\s*)\S+")
def clean(v):
    return re.sub(r"[\t\r\n]+", " ", str(v if v is not None else ""))
for p in info:
    full = p["name"] if p["group"] == p["name"] else p["group"] + ":" + p["name"]
    st = p["statename"]
    line = ""
    if st in ("FATAL", "BACKOFF", "EXITED", "UNKNOWN") or p["now"] - p["start"] < 900:
        for fn in (srv.supervisor.tailProcessStderrLog, srv.supervisor.tailProcessStdoutLog):
            try:
                data = fn(full, 0, 4000)[0]
            except BaseException:
                continue
            ls = [x.strip() for x in ANSI.sub("", data).splitlines() if x.strip()]
            if ls:
                line = SECRET.sub(r"\1\3***", ls[-1])[:300]
                break
    print("\t".join(clean(x) for x in ("P", full, st, p["start"], p["now"], p["pid"], p["exitstatus"],
                                        (p.get("spawnerr") or "")[:200], line)))
print("OK")
'
sup_conf() { # pid -> its config file as seen inside its root
  local p=$1 c cwd
  c=$(tr '\0' '\n' < "/proc/$p/cmdline" 2>/dev/null | awk 'n { print; exit } $0 == "-c" || $0 == "--configuration" { n = 1; next }
       /^--configuration=/ { sub(/^--configuration=/, ""); print; exit } /^-c./ { print substr($0, 3); exit }')
  if [ -n "$c" ]; then
    case "$c" in /*) ;; *) cwd=$(readlink "/proc/$p/cwd" 2>/dev/null); c="${cwd%/}/$c" ;; esac
    printf '%s' "$c"; return
  fi
  for c in /etc/supervisor/supervisord.conf /etc/supervisord.conf /usr/local/etc/supervisord.conf /etc/supervisor.conf; do
    in_root "$p" "$c" && { printf '%s' "$c"; return; }
  done
}

# pm2: the axon client inside pm2 itself, by the daemon's node; prints the same rows or "E why"
PM2_JS='
const fs = require("fs"), path = require("path");
const home = process.argv[1], dirs = process.argv.slice(2);
let axon, rpc;
// pm2 7 keeps its axon client inside the package (modules/), pm2 5 among its dependencies
for (const d of dirs) {
  for (const [a, r] of [[d + "/modules/pm2-axon", d + "/modules/pm2-axon-rpc"], ["pm2-axon", "pm2-axon-rpc"]]) {
    try {
      axon = require(a[0] === "/" ? a : require.resolve(a, { paths: [d] }));
      rpc = require(r[0] === "/" ? r : require.resolve(r, { paths: [d] }));
      break;
    } catch (e) { axon = rpc = undefined }
  }
  if (axon) break;
}
if (!axon) { console.log("E\tnolib"); process.exit(0) }
const clean = (v) => String(v === undefined || v === null ? "" : v).replace(/[\t\r\n]+/g, " ");
const SECRET = /(pass(word|wd)?|secret|token|api[_-]?key|authorization)(["\x27]?\s*[=:]\s*)\S+/gi;
const tail = (f) => {
  try {
    const st = fs.statSync(f), n = Math.min(st.size, 4000), b = Buffer.alloc(n), fd = fs.openSync(f, "r");
    fs.readSync(fd, b, 0, n, st.size - n); fs.closeSync(fd);
    const ls = b.toString().replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "").split("\n").map((x) => x.trim()).filter(Boolean);
    return ls.length ? ls[ls.length - 1].replace(SECRET, "$1$3***").slice(0, 300) : "";
  } catch (e) { return "" }
};
const down = () => { console.log("E\tdown"); process.exit(0) };
setTimeout(down, 8000);
process.on("uncaughtException", down);
const req = axon.socket("req"), cl = new rpc.Client(req);
req.on("error", () => {});
req.connect(path.join(home, "rpc.sock"));
cl.call("getMonitorData", {}, (err, list) => {
  if (err || !Array.isArray(list)) { console.log("E\tdown"); process.exit(0) }
  const now = Math.floor(Date.now() / 1000), many = {};
  for (const p of list) { const n = (p.pm2_env || {}).name || p.name; many[n] = (many[n] || 0) + 1 }
  for (const p of list) {
    const e = p.pm2_env || {}, name = e.name || p.name, start = Math.floor((e.pm_uptime || 0) / 1000);
    let line = "";
    if (e.status !== "online" || now - start < 900) line = tail(e.pm_err_log_path || "") || tail(e.pm_out_log_path || "");
    console.log(["P", many[name] > 1 ? name + ":" + p.pm_id : name, e.status, start, now, p.pid || 0,
      e.exit_code === undefined ? "" : e.exit_code, "", line].map(clean).join("\t"));
  }
  console.log("OK");
  process.exit(0);
});
'
pm2_home() { # pid -> PM2_HOME
  local p=$1 h
  h=$(tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | sed -n 's/^PM2 v[0-9.]*: God Daemon (\(.*\)).*/\1/p')
  [ -n "$h" ] && { printf '%s' "$h"; return; }
  h=$(tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null | sed -n 's/^PM2_HOME=//p' | head -1)
  [ -n "$h" ] && { printf '%s' "$h"; return; }
  h=$(tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null | sed -n 's/^HOME=//p' | head -1)
  printf '%s/.pm2' "${h:-/root}"
}
pm2_dirs() { # pid home -> directories where the pm2 package may be, as seen inside its root
  local p=$1 home=$2 exe d a
  exe=$(readlink "/proc/$p/exe" 2>/dev/null)
  for a in $(tr '\0' '\n' < "/proc/$p/cmdline" 2>/dev/null | grep pm2 | grep '^/'); do
    d=$(readlink -f "/proc/$p/root$a" 2>/dev/null) && d=${d#/proc/$p/root} && printf '%s\n' "${d%/pm2/*}/pm2"
  done
  for d in "${exe%/bin/*}/lib/node_modules/pm2" /usr/lib/node_modules/pm2 /usr/local/lib/node_modules/pm2 \
           "${home%/.pm2}/.npm-global/lib/node_modules/pm2" /app/node_modules/pm2 /opt/app/node_modules/pm2 \
           /usr/src/app/node_modules/pm2; do
    in_root "$p" "$d" && printf '%s\n' "$d"
  done
  for d in "/proc/$p/root${home%/.pm2}"/.nvm/versions/node/*/lib/node_modules/pm2; do
    [ -d "$d" ] && printf '%s\n' "${d#/proc/$p/root}"
  done
}

: > "$TMPD/rows"
: > "$TMPD/skip"
while IFS=$'\t' read -r P KIND; do
  [ -r "/proc/$P/cmdline" ] || continue
  W=$(where_of "$P")
  exe=$(readlink "/proc/$P/exe" 2>/dev/null) || continue
  if [ "$KIND" = supervisor ]; then
    conf=$(sup_conf "$P")
    [ -n "$conf" ] || { printf '%s\t%s\tnoconf\n' "$W" "$KIND" >> "$TMPD/skip"; continue; }
    ID="$W|supervisor|$conf"
    # the interpreter from the command line: a supervisor installed in a virtualenv imports only
    # there, and /proc/PID/exe points past the venv to the system python
    py=$(tr '\0' '\n' < "/proc/$P/cmdline" 2>/dev/null | head -1)
    case "${py##*/}" in python*) ;; *) py=$exe ;; esac
    timeout "$TO" nsenter -t "$P" -m -n -- "$py" -c "$SUP_PY" "$conf" < /dev/null > "$TMPD/out" 2>/dev/null
  else
    home=$(pm2_home "$P")
    ID="$W|pm2|$home"
    # shellcheck disable=SC2046
    timeout "$TO" nsenter -t "$P" -m -n -- "$exe" -e "$PM2_JS" "$home" $(pm2_dirs "$P" "$home" | sort -u) \
      < /dev/null > "$TMPD/out" 2>/dev/null
  fi
  e=$(awk -F'\t' '$1 == "E" { print $2; exit }' "$TMPD/out")
  if [ -n "$e" ] || ! grep -qx OK "$TMPD/out"; then
    printf '%s\t%s\t%s\n' "$W" "$KIND" "${e:-down}" >> "$TMPD/skip"
    continue
  fi
  awk -F'\t' -v OFS='\t' -v id="$ID" -v w="$W" -v k="$KIND" '$1 == "P" { $1 = id "\t" w "\t" k; print }' "$TMPD/out" \
    | head -$MAX_PROGS >> "$TMPD/rows"
done <<< "$DAEMONS"

# -- restarts and since when: the previous run's start time and state of each program (cache) --
# rows: id where kind name state start now pid exit desc log
# cache: S id|name start state since restart-times(comma)
awk -F'\t' -v OFS='\t' -v now="$NOW" -v cf="$CACHE" -v nc="$TMPD/cache" '
  BEGIN { while ((getline l < cf) > 0) { split(l, c, "\t"); if (c[1] == "S") { cs[c[2]] = c[3]; cst[c[2]] = c[4]; csi[c[2]] = c[5]; crt[c[2]] = c[6] } } }
  { k = $1 "|" $4; rt = ""; n15 = 0; n60 = 0
    m = split(crt[k], t, ","); for (i = 1; i <= m; i++) if (t[i] != "" && now - t[i] <= 3600) rt = rt (rt == "" ? "" : ",") t[i]
    if ((k in cs) && cs[k] != $6 && $6 > 0) rt = rt (rt == "" ? "" : ",") now
    m = split(rt, t, ","); for (i = 1; i <= m; i++) { n60++; if (now - t[i] <= 900) n15++ }
    since = ((k in cst) && cst[k] == $5) ? csi[k] : now
    print "S", k, $6, $5, since, rt > nc
    print $0, since, n15, n60 }' "$TMPD/rows" > "$TMPD/rows2"
mv -f "$TMPD/cache" "$CACHE" 2>/dev/null || : > "$CACHE"

# -- the report: a daemon per id, its programs in the order it gave them --
json=$(awk -F'\t' '
  function e(s) { gsub(/[[:cntrl:]]/, "", s); gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); return s }
  function n(v) { return (v ~ /^-?[0-9]+$/) ? v + 0 : "null" }
  function flush() { if (g == "") return
    b = b (nb++ ? "," : "") sprintf("{\"kind\":\"%s\",\"where\":\"%s\",\"progs\":[%s]}", gk, e(gw), ps) }
  { if ($1 != g) { flush(); g = $1; gw = $2; gk = $3; ps = ""; np = 0 }
    up = ($5 == "RUNNING" || $5 == "online") && $6 > 0 ? $7 - $6 : 0
    ps = ps (np++ ? "," : "") sprintf("{\"name\":\"%s\",\"state\":\"%s\",\"since\":%d,\"up\":%d,\"pid\":%s,\"exit\":%s,\"r15\":%d,\"r60\":%d%s%s}",
      e($4), e($5), $12, up, n($8), n($9), $13, $14, $10 != "" ? ",\"desc\":\"" e($10) "\"" : "", $11 != "" ? ",\"log\":\"" e($11) "\"" : "") }
  END { flush(); printf "%s", b }' "$TMPD/rows2")
skip=$(sort -u "$TMPD/skip" | head -50 | awk -F'\t' '
  function e(s) { gsub(/[[:cntrl:]]/, "", s); gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); return s }
  { printf "%s{\"where\":\"%s\",\"kind\":\"%s\",\"why\":\"%s\"}", (NR > 1 ? "," : ""), e($1), $2, e($3) }')
mkdir -p "${OUT%/*}"
printf '{"v":1,"ts":%s,"daemons":[%s],"skipped":[%s]}\n' "$NOW" "$json" "$skip" > "$OUT.tmp.$$"
chmod 0644 "$OUT.tmp.$$"
mv -f "$OUT.tmp.$$" "$OUT"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-workers.service <<EOF
[Unit]
Description=Kervax: workers under supervisor and pm2 (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
TimeoutStartSec=5min
EOF
cat > /etc/systemd/system/kervax-workers.timer <<'EOF'
[Unit]
Description=Kervax: read the workers under supervisor and pm2 every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-workers.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-workers.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/workers-setup.ver"
chmod 0644 "$STATE_DIR/versions/workers-setup.ver"
echo "✓ workers-setup: workers under supervisor and pm2 -> $OUT (every minute)."
