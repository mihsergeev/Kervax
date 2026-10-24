#!/usr/bin/env bash
# Kervax: background workers kept alive by supervisor or pm2. A queue worker, a listener or a
# daemon under supervisord or pm2 is invisible to systemd and to Docker: supervisor restarts it,
# gives up after a few failed starts (FATAL) and nobody notices until its work piles up. A root
# helper finds every supervisord and pm2 daemon on the node by its process (docker, Kubernetes
# pods and the host alike), asks it for its programs every minute and writes
# /var/lib/kervax/report.d/workers.json; the agent hands report.d to the panel as is.
#
# supervisord is asked over its own XML-RPC socket by curl of the host: the socket path and the
# credentials come from its config ([supervisorctl], else [unix_http_server] or [inet_http_server]),
# they never leave the node. pm2 is asked over its rpc.sock with the axon client shipped inside pm2 itself
# (getMonitorData, what "pm2 jlist" calls), by the daemon's own node. Not "pm2 jlist": the pm2
# command starts a new daemon when it does not find one, and a wrong PM2_HOME would do just that.
#
# For each program: its state, since when, its uptime, the exit code, and how many times it was
# restarted in the last 15 minutes and hour - counted here between runs by its start time. For
# a program that is down or was started within 15 minutes, the last line of its log (stderr,
# else stdout), with values after password=, token= and the like masked. Read only.
#
# 0.2: a backslash in a name or a line (App\Jobs\Mail, a regex in a query) made the report invalid
# JSON on Debian and Ubuntu, whose awk is mawk: there gsub(/\\/, "\\\\") puts back one backslash,
# not two, and the agent drops a block that is not valid JSON - silently, the whole of it. Backslashes
# are doubled by splitting the string now, the same in every awk.
#
# 0.3: a restart after a clean exit (code 0) of a run that lasted a minute or more is a planned
# one and is counted apart (p60): a queue listener that handles a batch and exits, a max-jobs or
# memory limit. Fifteen such listeners recycling every few minutes were reported as "goes down
# and up" every few minutes. A restart after any other exit, or after a run shorter than a
# minute, is still counted as a crash (r15, r60). Restart times kept by 0.2 counted both and are
# dropped on the first run.
#
# 0.4: the helper ran, as root with every capability, the python or node a process pointed at: a
# user's process with "supervisord" in its arguments got its own file run as root, and an image's
# interpreter ran as root of the host outside the container's confinement. supervisord is asked
# by the host's curl now, over its XML-RPC socket (a unix one through /proc/PID/root, an inet one
# inside its network namespace only), and nothing of the daemon's runs. pm2's node runs as the
# daemon's own user; root of a docker container by docker exec, confined like the container, root
# of another container as nobody; root on the host only when the node is root's and nobody else
# can change it or a directory above it. A host without curl 7.40 asks supervisord the old way,
# under the same rule.
set -euo pipefail

KERVAX_SETUP_VERSION=0.4  # MAJOR.MINOR; compared component-wise
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

# -- who runs what inside a daemon's namespaces --
# A container's files are its image's, and a host process' command line is whatever its user typed:
# nothing found through a process runs as root unless the process is root's on the host itself and
# the file is root's too (it and every directory above it, none writable by anybody else). Anything
# else runs as the process' own user, root of a container as nobody, and a container is entered
# with its PID namespace as well.
on_host() { [ "$(stat -L -c %d:%i "/proc/$1/root" 2>/dev/null)" = "$(stat -L -c %d:%i /)" ]; }
roots() { # path -> 0 if root's and nobody else can change it
  local f m
  f=$(readlink -f "$1" 2>/dev/null) && [ -e "$f" ] || return 1
  while :; do
    m=$(stat -c '%u %a' "$f" 2>/dev/null) || return 1
    [ "${m%% *}" = 0 ] && [ $((0${m#* } & 022)) = 0 ] || return 1
    [ "$f" = / ] && return 0
    f=${f%/*}; f=${f:-/}
  done
}
ids_of() { awk '/^Uid:/ { u = $2 } /^Gid:/ { g = $2 } END { if (u != "") print u, g }' "/proc/$1/status" 2>/dev/null; }
ns_as() { # pid binary (as seen inside) -> nsenter options
  local u g
  read -r u g < <(ids_of "$1") && [ -n "$u" ] || return 1
  if on_host "$1"; then
    [ "$u" = 0 ] && ! roots "$2" && { u=65534; g=65534; }
    printf -- '-m -n -S %s -G %s' "$u" "$g"
  else
    [ "$u" = 0 ] && { u=65534; g=65534; }
    printf -- '-m -n -p -S %s -G %s' "$u" "$g"
  fi
}

# supervisord is asked by the host's own curl over its XML-RPC socket: a unix socket through
# /proc/PID/root, an inet one inside its network namespace only. Nothing of the daemon's runs.
# The credentials go to curl on stdin, not on its command line.
curl -q --help all 2>/dev/null | grep -q -- --unix-socket && CURL_UNIX=1 || CURL_UNIX=0
sup_ini() { # pid conf -> "url user password" (tab separated) to try, and "norpc" if it has no socket at all
  local p=$1 conf=$2 here f pat pats=()
  here=${conf%/*}; here=${here:-/}
  # [include] files: a socket may be set in an included file
  read -r -a pats < <(awk '/^[ \t]*\[/ { s = $0; next } s ~ /^[ \t]*\[include\]/ && /^[ \t]*files[ \t]*[=:]/ { sub(/^[^=:]*[=:]/, ""); sub(/[ \t]+;.*$/, ""); print; exit }' \
                      "/proc/$p/root$conf" 2>/dev/null)
  {
    head -c 262144 "/proc/$p/root$conf" 2>/dev/null; echo
    for pat in "${pats[@]:0:5}"; do
      case "$pat" in /*) ;; *) pat="$here/$pat" ;; esac
      for f in "/proc/$p/root"$pat; do [ -f "$f" ] && { head -c 262144 "$f" 2>/dev/null; echo; }; done
    done
  } > "$TMPD/conf"
  tr '\0' '\n' < "/proc/$p/environ" > "$TMPD/env" 2>/dev/null
  awk -v here="$here" -v envf="$TMPD/env" '
    BEGIN { while ((getline l < envf) > 0) { i = index(l, "="); if (i) env["ENV_" substr(l, 1, i - 1)] = substr(l, i + 1) } }
    function val(s,   k) { sub(/^[ \t]+/, "", s); sub(/[ \t]+;.*$/, "", s); sub(/[ \t\r]+$/, "", s); gsub(/%\(here\)s/, here, s)
      while (match(s, /%\(ENV_[A-Za-z0-9_]+\)s/)) { k = substr(s, RSTART + 2, RLENGTH - 4); s = substr(s, 1, RSTART - 1) env[k] substr(s, RSTART + RLENGTH) }
      return s }
    /^[ \t]*[;#]/ { next }
    /^[ \t]*\[/ { sec = $0; gsub(/^[ \t]*\[|\][ \t\r]*$/, "", sec); has[sec] = 1; next }
    { i = index($0, "="); j = index($0, ":"); if (!i || (j && j < i)) i = j; if (!i) next
      k = tolower(substr($0, 1, i - 1)); gsub(/[ \t]/, "", k); v[sec "|" k] = val(substr($0, i + 1)) }
    END {
      if (v["supervisorctl|serverurl"] != "") print v["supervisorctl|serverurl"] "\t" v["supervisorctl|username"] "\t" v["supervisorctl|password"]
      if (v["unix_http_server|file"] != "") print "unix://" v["unix_http_server|file"] "\t" v["unix_http_server|username"] "\t" v["unix_http_server|password"]
      if (v["inet_http_server|port"] != "") { p = v["inet_http_server|port"]; sub(/.*:/, "", p)
        print "http://127.0.0.1:" p "\t" v["inet_http_server|username"] "\t" v["inet_http_server|password"] }
      # without [unix_http_server] and [inet_http_server] supervisord has no control socket at all
      # (docker-mailserver): nobody, supervisorctl included, can ask it
      if (!has["unix_http_server"] && !has["inet_http_server"] && !has["include"]) print "norpc" }' "$TMPD/conf"
}
sup_call() { # pid url user password body -> the reply in $TMPD/x; 0 - answered, 1 - refused (auth), 2 - no answer
  local p=$1 url=$2 user=$3 pw=$4 body=$5 auth="" code sock
  cq() { printf '%s' "$1" | sed 's/[\\"]/\\&/g'; }
  [ -n "$user" ] && auth="user = \"$(cq "$user"):$(cq "$pw")\""
  : > "$TMPD/x"
  case "$url" in
    unix://*)
      sock="/proc/$p/root${url#unix://}"
      [ "$CURL_UNIX" = 1 ] && [ -S "$sock" ] || return 2
      code=$(printf '%s\n' "$auth" | curl -q -s -m 5 -K - --noproxy '*' -o "$TMPD/x" -w '%{http_code}' --unix-socket "$sock" \
             -H 'Content-Type: text/xml' --data-binary "$body" http://localhost/RPC2 2>/dev/null) ;;
    http://*)
      code=$(printf '%s\n' "$auth" | nsenter -t "$p" -n -- curl -q -s -m 5 -K - --noproxy '*' -o "$TMPD/x" -w '%{http_code}' \
             -H 'Content-Type: text/xml' --data-binary "$body" "${url%/}/RPC2" 2>/dev/null) ;;
    *) return 2 ;;
  esac
  [ "$code" = 401 ] && return 1
  [ "$code" = 200 ] || return 2
}
xq() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }
# XML-RPC replies, read tag by tag; values unescaped, a log line with secrets hidden
SUP_AWK='
function un(s) { gsub(/&lt;/, "<", s); gsub(/&gt;/, ">", s); gsub(/&quot;/, "\"", s); gsub(/&apos;/, "\047", s); gsub(/&amp;/, "\\&", s); return s }
function cl(s) { gsub(/[\t\r\n]+/, " ", s); return s }
function mask(s,   out, st, ln, m) { s = substr(s, 1, 600); out = ""
  while (match(tolower(s), /(pass(word|wd)?|secret|token|api[_-]?key|authorization)["\047]?[ ]*[=:][ ]*[^ ]+/)) {
    st = RSTART; ln = RLENGTH; m = substr(s, st, ln); match(m, /[=:][ ]*/)
    out = out substr(s, 1, st - 1) substr(m, 1, RSTART + RLENGTH - 1) "***"; s = substr(s, st + ln) }
  s = out s; gsub(/[[:cntrl:]]/, " ", s); return substr(s, 1, 300) }
BEGIN { RS = "<" }
{ i = index($0, ">"); t = substr($0, 1, i - 1); x = substr($0, i + 1) }
t == "fault" { fault = 1 }
'
# getAllProcessInfo -> "P name state start now pid exit desc" and OK
SUP_ROWS=$SUP_AWK'
t == "struct" { split("", m) }
t == "name" { k = x }
t == "value" { vv = x }
t ~ /^(string|int|i4|i8|boolean|double)$/ { vv = x }
t == "/member" { m[k] = un(vv) }
t == "/struct" && ("statename" in m) { n++
  print "P\t" cl(m["group"] == m["name"] ? m["name"] : m["group"] ":" m["name"]) "\t" cl(m["statename"]) "\t" cl(m["start"]) "\t" cl(m["now"]) \
    "\t" cl(m["pid"]) "\t" cl(m["exitstatus"]) "\t" cl(substr(m["spawnerr"], 1, 200)) }
END { if (!fault) print "OK" }'
# tailProcess*Log -> its last line that is not empty
SUP_TAIL=$SUP_AWK'
t == "string" && !got { s = x; got = 1 }
END { if (fault || !got) exit; s = un(s); gsub(sprintf("%c", 27) "\\[[0-9;?]*[A-Za-z]", "", s)
  n = split(s, L, "\n"); for (j = n; j >= 1; j--) { y = L[j]; gsub(/^[ \t\r]+|[ \t\r]+$/, "", y); if (y != "") break }
  if (y != "") print mask(y) }'
SUP_ALL='<?xml version="1.0"?><methodCall><methodName>supervisor.getAllProcessInfo</methodName><params></params></methodCall>'
sup_ask() { # pid conf -> rows like SUP_PY prints, from the host
  local p=$1 conf=$2 url user pw r why=down norpc="" ok="" tag full st start now pid ex desc line m t0=$SECONDS nlog=0
  while IFS=$'\t' read -r url user pw; do
    [ "$url" = norpc ] && { norpc=1; continue; }
    sup_call "$p" "$url" "$user" "$pw" "$SUP_ALL"; r=$?
    [ $r = 1 ] && why=auth
    [ $r = 0 ] && awk "$SUP_ROWS" "$TMPD/x" > "$TMPD/sup" 2>/dev/null && grep -qx OK "$TMPD/sup" && { ok=1; break; }
  done < <(sup_ini "$p" "$conf")
  if [ -z "$ok" ]; then
    [ -n "$norpc" ] && [ "$why" = down ] && why=norpc
    printf 'E\t%s\n' "$why"; return
  fi
  while IFS=$'\t' read -r tag full st start now pid ex desc; do
    [ "$tag" = P ] || continue
    line=""
    case "$st" in FATAL|BACKOFF|EXITED|UNKNOWN) m=1 ;; *) m=$(( ${now:-0} - ${start:-0} < 900 )) ;; esac
    # the last line of its log, the time of one daemon and 30 programs at most
    if [ "$m" = 1 ] && [ $nlog -lt 30 ] && [ $((SECONDS - t0)) -lt "$TO" ]; then
      nlog=$((nlog + 1))
      for t in tailProcessStderrLog tailProcessStdoutLog; do
        sup_call "$p" "$url" "$user" "$pw" "<?xml version=\"1.0\"?><methodCall><methodName>supervisor.$t</methodName><params><param><value><string>$(xq "$full")</string></value></param><param><value><int>0</int></value></param><param><value><int>4000</int></value></param></params></methodCall>" || continue
        line=$(awk "$SUP_TAIL" "$TMPD/x" 2>/dev/null)
        [ -n "$line" ] && break
      done
    fi
    printf 'P\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$full" "$st" "$start" "$now" "$pid" "$ex" "$desc" "$line"
  done < "$TMPD/sup"
  echo OK
}

# supervisord: its python, its config; prints "P name state start now pid exit desc log" or "E why".
# Only where the curl of the host cannot reach a unix socket (curl before 7.40), run as ns_as says
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
    : > "$TMPD/out"
    if [ "$CURL_UNIX" = 1 ] || sup_ini "$P" "$conf" | grep -q '^http://'; then
      sup_ask "$P" "$conf" > "$TMPD/out" 2>/dev/null
    else
      # the interpreter from the command line: a supervisor installed in a virtualenv imports only
      # there, and /proc/PID/exe points past the venv to the system python
      py=$(tr '\0' '\n' < "/proc/$P/cmdline" 2>/dev/null | head -1)
      case "${py##*/}" in python*) ;; *) py=$exe ;; esac
      # shellcheck disable=SC2086
      o=$(ns_as "$P" "$py") && timeout -k 5 "$TO" nsenter -t "$P" $o -- "$py" -E -s -c "$SUP_PY" "$conf" \
        < /dev/null > "$TMPD/out" 2>/dev/null
    fi
  else
    home=$(pm2_home "$P")
    ID="$W|pm2|$home"
    id=$(cid_of "$P")
    : > "$TMPD/out"
    # a docker container's own node runs there by docker exec, confined like the container itself
    # (as its root too: pm2 keeps rpc.sock to its own user); elsewhere as ns_as says
    if [ -n "$id" ] && [ -n "${DNAME[$id]:-}" ] && ! on_host "$P"; then
      # shellcheck disable=SC2046
      timeout -k 5 "$TO" docker exec -u "$(ids_of "$P" | tr ' ' :)" "$id" "$exe" -e "$PM2_JS" "$home" \
        $(pm2_dirs "$P" "$home" | sort -u) < /dev/null > "$TMPD/out" 2>/dev/null
    elif o=$(ns_as "$P" "$exe"); then
      # shellcheck disable=SC2046,SC2086
      timeout -k 5 "$TO" nsenter -t "$P" $o -- "$exe" -e "$PM2_JS" "$home" \
        $(pm2_dirs "$P" "$home" | sort -u) < /dev/null > "$TMPD/out" 2>/dev/null
    fi
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
# cache: V 3 | S id|name start state since crash-restart-times(comma) planned-restart-times(comma)
awk -F'\t' -v OFS='\t' -v now="$NOW" -v cf="$CACHE" -v nc="$TMPD/cache" '
  function hour(list,   m, x, i, r) { r = ""; m = split(list, x, ",")
    for (i = 1; i <= m; i++) if (x[i] != "" && now - x[i] <= 3600) r = r (r == "" ? "" : ",") x[i]
    return r }
  BEGIN { while ((getline l < cf) > 0) { split(l, c, "\t"); if (c[1] == "V") cv = c[2] + 0
            else if (c[1] == "S") { cs[c[2]] = c[3]; cst[c[2]] = c[4]; csi[c[2]] = c[5]; crt[c[2]] = c[6]; cpt[c[2]] = c[7] } }
          print "V", 3 > nc }
  { k = $1 "|" $4; n15 = 0; n60 = 0
    # times kept before 0.3 counted planned restarts as crashes too: dropped
    rt = cv >= 3 ? hour(crt[k]) : ""; pt = cv >= 3 ? hour(cpt[k]) : ""
    if ((k in cs) && cs[k] != $6 && $6 > 0) {
      # exited with code 0 after a minute or more of work: a planned recycle, not a crash
      if ($9 == "0" && $6 - cs[k] >= 60) pt = pt (pt == "" ? "" : ",") now
      else rt = rt (rt == "" ? "" : ",") now }
    m = split(rt, t, ","); for (i = 1; i <= m; i++) { n60++; if (now - t[i] <= 900) n15++ }
    p60 = split(pt, t, ",")
    since = ((k in cst) && cst[k] == $5) ? csi[k] : now
    print "S", k, $6, $5, since, rt, pt > nc
    print $0, since, n15, n60, p60 }' "$TMPD/rows" > "$TMPD/rows2"
mv -f "$TMPD/cache" "$CACHE" 2>/dev/null || : > "$CACHE"

# -- the report: a daemon per id, its programs in the order it gave them --
json=$(awk -F'\t' '
  function e(s,   _jp, _jn, _ji) { gsub(/[[:cntrl:]]/, "", s); _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
  function n(v) { return (v ~ /^-?[0-9]+$/) ? v + 0 : "null" }
  function flush() { if (g == "") return
    b = b (nb++ ? "," : "") sprintf("{\"kind\":\"%s\",\"where\":\"%s\",\"progs\":[%s]}", gk, e(gw), ps) }
  { if ($1 != g) { flush(); g = $1; gw = $2; gk = $3; ps = ""; np = 0 }
    up = ($5 == "RUNNING" || $5 == "online") && $6 > 0 ? $7 - $6 : 0
    ps = ps (np++ ? "," : "") sprintf("{\"name\":\"%s\",\"state\":\"%s\",\"since\":%d,\"up\":%d,\"pid\":%s,\"exit\":%s,\"r15\":%d,\"r60\":%d,\"p60\":%d%s%s}",
      e($4), e($5), $12, up, n($8), n($9), $13, $14, $15, $10 != "" ? ",\"desc\":\"" e($10) "\"" : "", $11 != "" ? ",\"log\":\"" e($11) "\"" : "") }
  END { flush(); printf "%s", b }' "$TMPD/rows2")
skip=$(sort -u "$TMPD/skip" | head -50 | awk -F'\t' '
  function e(s,   _jp, _jn, _ji) { gsub(/[[:cntrl:]]/, "", s); _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
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
