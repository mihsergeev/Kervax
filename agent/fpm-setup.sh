#!/usr/bin/env bash
# Kervax: PHP-FPM pools. A pool has a hard limit of workers (pm.max_children, 5 in the stock docker
# image). When all of them are busy, new requests wait in the listen queue, nginx answers slowly
# and then 502/504, and nothing in the process list looks wrong: the same five php-fpm processes as
# always. FPM says it in its log ("server reached pm.max_children setting"), where nobody looks.
# A root helper finds every php-fpm master on the node by its process (docker containers,
# Kubernetes pods, the host), reads its config inside its root (pools, listen, pm,
# pm.max_children, slowlog and terminate timeouts), and three times a minute counts the
# connections of each pool from /proc/PID/net of its network namespace: established connections to
# the pool's port or socket are requests being served plus requests waiting, the queue of the
# listening TCP socket is the waiting ones. No status page is needed. The FPM log (the container
# log, or error_log on the host) is read by offset for "reached pm.max_children", "executing too
# slow", "execution timed out" and workers killed by a crash signal ("child N exited on signal 11
# (SIGSEGV)", also 4, 6, 7, 8), per pool, in 10-minute buckets for a day. The kernel journal is
# read by cursor for php processes killed by a segfault or a general protection fault: the kernel
# logs them for every container and pod of the node, also where the FPM log is not found. Workers
# crashing in a row is how an attempt on a memory bug of PHP looks (php2shell, October 2026).
# Writes /var/lib/kervax/report.d/fpm.json; the agent hands report.d to the panel as is. Read only.
# 0.3: the offset of a log is kept per container and path (two containers with the same
# /var/log/php-fpm.log inside used to share one offset and reread each other from the start), a
# line counts in the bucket of its own time and only if it is newer than the last one counted
# from that log, a log seen for the first time is read from its end, the time of the last
# "reached pm.max_children" goes to the report.
# 0.4: a process is accepted as a master only when /proc/PID/exe is a php-fpm-named binary and it
# is root on the host or belongs to a container. Container config and log paths are opened by host
# Python one component at a time with dir_fd + O_NOFOLLOW. A rename/symlink race therefore cannot
# turn a read by the root helper into a read from the host root. Without host Python container FPM
# is skipped fail-closed; host FPM and container stderr logs keep working.
set -euo pipefail

KERVAX_SETUP_VERSION=0.4 # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: without php-fpm it runs one ps and exits

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-fpm"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/fpm.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes,
# and rewriting it in place would make a run in progress continue from the same byte offset.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# PHP-FPM pools -> /var/lib/kervax/report.d/fpm.json. Read only: ps, the FPM config files,
# /proc/PID/net of each master, the new bytes of its log, the new lines of the kernel journal.
set -u
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OUT=${KERVAX_FPM_OUT:-/var/lib/kervax/report.d/fpm.json}
STATE=${KERVAX_FPM_STATE:-/var/lib/kervax/fpm.state}
SAMPLES=3        # connection counts per run, a second apart
CAP=8388608      # bytes of one log read in a run; more - only the last CAP
BUDGET=67108864  # bytes of logs read in a run in all
CONF_CAP=4194304 # bytes of one container's main config or one include expansion

exec 9>/run/kervax-fpm.lock
flock -n 9 || exit 0
NOW=$(date +%s)

MASTERS=$(ps -e -o pid=,args= 2>/dev/null | awk '{ a = $0; sub(/^ *[0-9]+ /, "", a) } a ~ /^php-fpm[0-9.]*: master process/ { print $1 }')
if [ -z "$MASTERS" ]; then
  mkdir -p "${OUT%/*}"
  printf '{"v":1,"ts":%s,"pools":[],"skipped":[]}\n' "$NOW" > "$OUT.tmp.$$" && chmod 0644 "$OUT.tmp.$$" && mv -f "$OUT.tmp.$$" "$OUT"
  rm -f "$STATE"
  exit 0
fi
TMPD=$(mktemp -d /tmp/kervax-fpm.XXXXXX) || exit 0
trap 'rm -rf "$TMPD"' EXIT
[ -f "$STATE" ] || : > "$STATE"

# A container controls its own mount tree and can rename a checked directory to an absolute
# symlink between a shell test and cat/stat.  Open every component relative to an already opened
# directory fd and with O_NOFOLLOW: the kernel, not a path pre-check, makes the decision.  These
# are host Python and host syscalls; no executable from the container is ever run.  On a minimal
# host without Python container configs are skipped fail-closed, while host FPM still works.
SAFE_PY=${KERVAX_FPM_PY:-$(command -v python3 2>/dev/null || true)}
[ -n "$SAFE_PY" ] || [ ! -x /usr/libexec/platform-python ] || SAFE_PY=/usr/libexec/platform-python
cat > "$TMPD/rootio.py" <<'PY'
import fnmatch
import os
import posixpath
import re
import stat
import sys

PLAIN = re.compile(r"^[A-Za-z0-9._@+-]+$")
PATTERN = re.compile(r"^[A-Za-z0-9._@+*?-]+$")
PROC = os.environ.get("KERVAX_FPM_PROC_ROOT", "/proc")
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
if not NOFOLLOW or not DIRECTORY or not hasattr(os, "supports_dir_fd"):
    raise SystemExit(1)


def split_path(path):
    if not path.startswith("/") or "\x00" in path:
        raise ValueError("absolute path required")
    out = [part for part in path.split("/") if part]
    if any(part in (".", "..") or not PLAIN.fullmatch(part) for part in out):
        raise ValueError("unsafe path")
    return out


def root_fd(pid):
    if not pid.isdigit():
        raise ValueError("bad pid")
    procfd = os.open(PROC, os.O_RDONLY | DIRECTORY | CLOEXEC)
    try:
        pidfd = os.open(pid, os.O_RDONLY | DIRECTORY | NOFOLLOW | CLOEXEC, dir_fd=procfd)
    finally:
        os.close(procfd)
    try:
        # proc owns this magic link. Opening it relative to the pinned PID directory cannot jump
        # to a process that later reused the numeric PID; a dead process makes this open fail.
        return os.open("root", os.O_RDONLY | DIRECTORY | CLOEXEC, dir_fd=pidfd)
    finally:
        os.close(pidfd)


def dir_fd(pid, path):
    fd = root_fd(pid)
    try:
        for part in split_path(path):
            nxt = os.open(part, os.O_RDONLY | DIRECTORY | NOFOLLOW | CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def file_fd(pid, path):
    parts = split_path(path)
    if not parts:
        raise ValueError("file required")
    dfd = dir_fd(pid, "/" + "/".join(parts[:-1]))
    try:
        fd = os.open(parts[-1], os.O_RDONLY | NOFOLLOW | CLOEXEC, dir_fd=dfd)
    finally:
        os.close(dfd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("regular file required")
    return fd


def zone_fd(pid, path):
    """Resolve /etc/localtime and final zoneinfo symlinks without leaving zoneinfo."""
    if path != "/etc/localtime" and not path.startswith("/usr/share/zoneinfo/"):
        raise ValueError("timezone path outside zoneinfo")
    for _ in range(8):
        parts = split_path(path)
        if not parts:
            raise ValueError("timezone file required")
        dfd = dir_fd(pid, "/" + "/".join(parts[:-1]))
        try:
            st = os.stat(parts[-1], dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISREG(st.st_mode):
                fd = os.open(parts[-1], os.O_RDONLY | NOFOLLOW | CLOEXEC, dir_fd=dfd)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    os.close(fd)
                    raise ValueError("regular timezone file required")
                return fd
            if not stat.S_ISLNK(st.st_mode):
                raise ValueError("timezone is not a file")
            target = os.readlink(parts[-1], dir_fd=dfd)
        finally:
            os.close(dfd)
        path = posixpath.normpath(target if target.startswith("/")
                                  else posixpath.join(posixpath.dirname(path), target))
        if not path.startswith("/usr/share/zoneinfo/"):
            raise ValueError("timezone symlink outside zoneinfo")
    raise ValueError("too many timezone symlinks")


def copy_fd(fd, out, limit=None):
    left = limit
    total = 0
    while left is None or left > 0:
        chunk = os.read(fd, 131072 if left is None else min(131072, left))
        if not chunk:
            break
        os.write(out, chunk)
        total += len(chunk)
        if left is not None:
            left -= len(chunk)
    return total


def cat(pid, path, cap):
    fd = file_fd(pid, path)
    try:
        size = os.fstat(fd).st_size
        if size > cap:
            raise ValueError("config too large")
        copy_fd(fd, 1, size)
    finally:
        os.close(fd)


def glob_cat(pid, path, pattern, cap):
    if not PATTERN.fullmatch(pattern) or "/" in pattern:
        raise ValueError("unsafe pattern")
    dfd = dir_fd(pid, path)
    opened = []
    try:
        for name in sorted(n for n in os.listdir(dfd) if fnmatch.fnmatchcase(n, pattern)):
            try:
                fd = os.open(name, os.O_RDONLY | NOFOLLOW | CLOEXEC, dir_fd=dfd)
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    os.close(fd)
                    continue
                opened.append((fd, st.st_size))
            except OSError:
                continue
        if len(opened) > 256 or sum(size + 1 for _, size in opened) > cap:
            raise ValueError("include expansion too large")
        for fd, size in opened:
            copy_fd(fd, 1, size)
            os.write(1, b"\n")
    finally:
        for fd, _ in opened:
            os.close(fd)
        os.close(dfd)


def snap(pid, path, target, cap):
    fd = file_fd(pid, path)
    try:
        st = os.fstat(fd)
        start = max(0, st.st_size - cap)
        os.lseek(fd, start, os.SEEK_SET)
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | CLOEXEC, 0o600)
        try:
            copy_fd(fd, out, st.st_size - start)
        finally:
            os.close(out)
        print(f"{st.st_ino}\t{st.st_size}\t{start}")
    finally:
        os.close(fd)


def zone(pid, path, target, cap):
    fd = zone_fd(pid, path)
    try:
        size = os.fstat(fd).st_size
        if size > cap:
            raise ValueError("timezone file too large")
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | CLOEXEC, 0o600)
        try:
            copy_fd(fd, out, size)
        finally:
            os.close(out)
    finally:
        os.close(fd)


try:
    mode = sys.argv[1]
    if mode == "cat":
        cat(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    elif mode == "glob":
        glob_cat(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    elif mode == "snap":
        snap(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    elif mode == "zone":
        zone(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    else:
        raise ValueError("bad mode")
except (OSError, ValueError, IndexError):
    raise SystemExit(1)
PY
safe_cat() { [ -n "$SAFE_PY" ] && "$SAFE_PY" -I "$TMPD/rootio.py" cat "$1" "$2" "$CONF_CAP"; }
safe_glob() { [ -n "$SAFE_PY" ] && "$SAFE_PY" -I "$TMPD/rootio.py" glob "$1" "$2" "$3" "$CONF_CAP"; }
safe_snap() { [ -n "$SAFE_PY" ] && "$SAFE_PY" -I "$TMPD/rootio.py" snap "$1" "$2" "$3" "$4"; }
safe_zone() { [ -n "$SAFE_PY" ] && "$SAFE_PY" -I "$TMPD/rootio.py" zone "$1" "$2" "$3" 1048576; }

cid_of() { # pid -> container id from its cgroup
  sed 's#.*/##' "/proc/$1/cgroup" 2>/dev/null | sed 's/^docker-//; s/^cri-containerd-//; s/^crio-//; s/\.scope$//' \
    | grep -m1 -E '^[0-9a-f]{64}$'
}
fpm_master() { # pid -> true only for a real host/container FPM master
  local p=$1 exe uid id
  exe=$(readlink "/proc/$p/exe" 2>/dev/null) || return 1
  exe=${exe% (deleted)}
  [[ ${exe##*/} =~ ^php-fpm([0-9]+([.][0-9]+)*)?$ ]] || return 1
  uid=$(awk '$1 == "Uid:" { print $2; exit }' "/proc/$p/status" 2>/dev/null)
  [ "$uid" = 0 ] && return 0
  id=$(cid_of "$p")
  [ -n "$id" ]
}
spec_of() { # container id -> its OCI config.json
  ls -d /run/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /run/k0s/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /run/k3s/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /var/snap/microk8s/common/run/containerd/io.containerd.runtime.v2.task/*/"$1"/config.json \
        /run/docker/containerd/daemon/io.containerd.runtime.v2.task/*/"$1"/config.json 2>/dev/null | head -1
}
DROOT=""
if command -v docker >/dev/null 2>&1; then
  timeout 10 docker ps --no-trunc --format '{{.ID}} {{.Names}}' > "$TMPD/dps" 2>/dev/null || : > "$TMPD/dps"
  DROOT=$(timeout 10 docker info -f '{{.DockerRootDir}}' 2>/dev/null) || DROOT=""
  DROOT=${DROOT:-/var/lib/docker}
else
  : > "$TMPD/dps"
fi
# A path inside the root of process $1. Every component must be a plain non-symlink path: following
# an absolute symlink through /proc/PID/root would resolve it in the host root. The paths come from
# the process and its config, so ".." or a character outside the plain set gives nothing too.
in_root() {
  local p=$1 path=$2 cur part
  cur="/proc/$p/root"
  case "$path" in /*) ;; *) return 0 ;; esac
  case "$path" in *..*|*[!A-Za-z0-9._/@+-]*|"") return 0 ;; esac
  path=${path#/}
  while [ -n "$path" ]; do
    part=${path%%/*}
    if [ "$part" = "$path" ]; then path=""; else path=${path#*/}; fi
    case "$part" in ''|.) continue ;; ..) return 0 ;; esac
    cur="$cur/$part"
    [ -L "$cur" ] && return 0
  done
  printf '%s' "$cur"
}
# seconds east of UTC of the local time a master writes in its own log file: FPM stamps lines with
# localtime, which is TZ of the process or /etc/localtime of its root (none - UTC)
tzoff_of() {
  local p=$1 z f o t id zf
  z=$(tr '\0' '\n' < "/proc/$p/environ" 2>/dev/null | sed -n 's/^TZ=//p' | head -1); z=${z#:}
  case "$z" in *..*|*[!A-Za-z0-9._/@+,-]*) z="" ;; esac
  id=$(cid_of "$p")
  if [ -n "$id" ]; then
    # /etc/localtime may be a regular file or a symlink into zoneinfo. rootio resolves the final
    # link with readlinkat semantics and copies the regular zone file through O_NOFOLLOW fds.
    zf="$TMPD/tz.$p"
    if [ -z "$z" ]; then
      if safe_zone "$p" /etc/localtime "$zf"; then z=":$zf"; else z=UTC0; fi
    elif [[ "$z" = /* ]]; then
      if safe_zone "$p" "$z" "$zf"; then z=":$zf"; else z=UTC0; fi
    elif safe_zone "$p" "/usr/share/zoneinfo/$z" "$zf"; then z=":$zf"
    # No such zone file: keep a POSIX TZ string such as EST5 or MSK-3.
    fi
  elif [ -n "$z" ]; then
    case "$z" in /*) f=$(in_root "$p" "$z") ;; *) f=$(in_root "$p" "/usr/share/zoneinfo/$z"); [ -f "$f" ] || f="/usr/share/zoneinfo/$z" ;; esac
    [ -n "$f" ] && [ -f "$f" ] && z=":$f"  # else a POSIX string as it is: MSK-3, UTC0
  else
    # /etc/localtime normally is an absolute or ../usr/share/zoneinfo symlink. Read its target as
    # data and resolve that path again under the process root; never follow the proc-root symlink.
    if [ -L "/proc/$p/root/etc/localtime" ]; then
      t=$(readlink "/proc/$p/root/etc/localtime" 2>/dev/null)
      case "$t" in /usr/share/zoneinfo/*) f=$(in_root "$p" "$t") ;;
        ../usr/share/zoneinfo/*) f=$(in_root "$p" "/${t#../}") ;; *) f="" ;; esac
    else f=$(in_root "$p" /etc/localtime); fi
    if [ -n "$f" ] && [ -f "$f" ]; then z=":$f"; else z=UTC0; fi
  fi
  o=$(TZ="$z" date +%z 2>/dev/null)
  case "$o" in [+-][0-9][0-9][0-9][0-9]) ;; *) o=+0000 ;; esac
  echo $(( (10#${o:1:2} * 3600 + 10#${o:3:2} * 60) * ${o:0:1}1 ))
}

# -- each master: where, config, pools. Rows: "P master where pool listen pm max slow term status"
# and "L master logfile how where"; children per pool from ps --
ps -e -o pid=,ppid=,args= 2>/dev/null | awk '{ a = $0; sub(/^ *[0-9]+ +[0-9]+ /, "", a) }
  a ~ /^php-fpm[0-9.]*: pool / { sub(/^php-fpm[0-9.]*: pool /, "", a); sub(/ .*/, "", a); print $2 "\t" a }' \
  | sort | uniq -c | awk '{ print $2 "\t" $3 "\t" $1 }' > "$TMPD/kids"
: > "$TMPD/pools"
: > "$TMPD/skip"
for P in $MASTERS; do
  [ -r "/proc/$P/cmdline" ] || continue
  fpm_master "$P" || continue
  id=$(cid_of "$P")
  W=host
  if [ -n "$id" ]; then
    n=$(awk -v i="$id" '$1 == i { print $2; exit }' "$TMPD/dps")
    if [ -n "$n" ]; then W="container $n"
    else
      spec=$(spec_of "$id"); pod=""; ns=""
      [ -n "$spec" ] && pod=$(grep -o '"io.kubernetes.cri.sandbox-name":"[^"]*"' "$spec" | head -1 | cut -d'"' -f4) \
        && ns=$(grep -o '"io.kubernetes.cri.sandbox-namespace":"[^"]*"' "$spec" | head -1 | cut -d'"' -f4)
      if [ -n "$pod" ]; then W="pod ${ns:+$ns/}$pod"; else W="container ${id:0:12}"; fi
    fi
  fi
  # stderr of a container: the json-file log of Docker, or the newest file of a pod container
  # under /var/log/pods
  LOGP=""
  if [ -n "$id" ] && [ "${W#pod }" != "$W" ]; then
    cn=$(grep -o '"io.kubernetes.cri.container-name":"[^"]*"' "$spec" | head -1 | cut -d'"' -f4)
    [ -n "$cn" ] && LOGP=$(ls -t /var/log/pods/"${ns}_${pod}"_*/"$cn"/*.log 2>/dev/null | head -1)
    case "$LOGP" in *[!A-Za-z0-9._/@+-]*) LOGP="" ;; esac
  elif [ -n "$id" ] && [ -n "$DROOT" ]; then
    LOGP="$DROOT/containers/$id/$id-json.log"
  fi
  conf=$(tr '\0' ' ' < "/proc/$P/cmdline" | sed -n 's/.*master process (\([^)]*\)).*/\1/p')
  [ -n "$conf" ] || { printf '%s\tnoconf\n' "$W" >> "$TMPD/skip"; continue; }
  if [ -n "$id" ]; then
    [ -n "$SAFE_PY" ] || { printf '%s\tnopython\n' "$W" >> "$TMPD/skip"; continue; }
    cf="$TMPD/main.$P"
    safe_cat "$P" "$conf" > "$cf" || { printf '%s\tunsafeconf\n' "$W" >> "$TMPD/skip"; continue; }
  else
    cf=$(in_root "$P" "$conf")
    [ -n "$cf" ] && [ -f "$cf" ] || { printf '%s\tnoconf\n' "$W" >> "$TMPD/skip"; continue; }
  fi
  # one stream in the order FPM reads it: the main file cut at its include lines, each piece
  # followed by the files of that include; a relative include is relative to the prefix
  # (/usr/local for /usr/local/etc/php-fpm.conf)
  pre=${conf%/*}; pre=${pre%/*}
  rm -f "$TMPD"/c.* "$TMPD"/i.*
  n=$(awk -v d="$TMPD" 'BEGIN { n = 0; f = d "/c.0"; printf "" > f }
    /^[ \t]*include[ \t]*=/ { v = $0; gsub(/\r/, "", v); sub(/^[^=]*=[ \t]*/, "", v); sub(/[ \t]*;.*$/, "", v); gsub(/^"|"$/, "", v)
      print v > (d "/i." n); close(d "/i." n); close(f); n++; f = d "/c." n; printf "" > f; next }
    { print > f }
    END { close(f); print n + 0 }' "$cf" 2>/dev/null) || n=0
  : > "$TMPD/conf"
  for ((j = 0; j <= ${n:-0} && j < 20; j++)); do
    cat "$TMPD/c.$j" >> "$TMPD/conf" 2>/dev/null
    [ -f "$TMPD/i.$j" ] || continue
    inc=$(head -1 "$TMPD/i.$j")
    case "$inc" in *..*|*[!A-Za-z0-9._/@+*?-]*|"") continue ;; /*) ;; *) inc="$pre/$inc" ;; esac
    if [ -n "$id" ]; then
      safe_glob "$P" "${inc%/*}" "${inc##*/}" >> "$TMPD/conf" || continue
    else
      d=$(in_root "$P" "${inc%/*}")
      [ -n "$d" ] || continue
      for f in "$d"/${inc##*/}; do [ -f "$f" ] && [ ! -L "$f" ] && { cat "$f"; echo; } >> "$TMPD/conf"; done
    fi
  done
  awk -v OFS='\t' -v m="$P" -v w="$W" -v pre="$pre" -v logp="$LOGP" -v inside="${id:+1}" '
    function v(s) { sub(/^[^=]*=[ \t]*/, "", s); sub(/[ \t]*;.*$/, "", s); gsub(/^"|"$/, "", s); return s }
    function secs(s) { if (s ~ /m$/) return s * 60; if (s ~ /h$/) return s * 3600; return s + 0 }
    { gsub(/\r/, "") }  # configs written on Windows
    /^[ \t]*[;#]/ || /^[ \t]*$/ { next }
    /^[ \t]*\[/ { sec = $0; sub(/^[ \t]*\[/, "", sec); sub(/\].*$/, "", sec); gsub(/[[:cntrl:]|]/, "", sec); if (sec != "global" && !(sec in seen)) { seen[sec] = 1; np++; order[np] = sec } next }
    { k = $0; sub(/^[ \t]*/, "", k); sub(/[ \t]*=.*/, "", k); val = v($0) }
    sec == "global" && k == "error_log" { elog = val; next }
    sec != "global" && sec != "" {
      gsub(/\$pool/, sec, val)
      if (k == "listen") lst[sec] = val; else if (k == "pm") pm[sec] = val
      else if (k == "pm.max_children") mx[sec] = val + 0
      else if (k == "request_slowlog_timeout") sl[sec] = secs(val)
      else if (k == "request_terminate_timeout") tt[sec] = secs(val)
      else if (k == "pm.status_path") stp[sec] = 1 }
    END {
      for (i = 1; i <= np; i++) { s = order[i]
        print "P", m, w, s, lst[s], (pm[s] == "" ? "dynamic" : pm[s]), mx[s] + 0, sl[s] + 0, tt[s] + 0, stp[s] + 0 }
      # where FPM writes: stderr in a container is the container log, syslog is not read; the
      # path goes into a command later, so only the plain characters
      if (elog == "" || elog ~ /^\/proc\/self\/fd\/[12]$|^\/dev\/std(err|out)$/) {
        if (logp != "") print "L", m, logp, "host", w
      } else if (elog !~ /^syslog/) {
        if (elog !~ /^\//) elog = pre "/var/" elog
        if (elog !~ /\.\./ && elog ~ /^[A-Za-z0-9._\/@+-]+$/) print "L", m, elog, (inside ? "safe" : "root"), w
      }
    }' "$TMPD/conf" 2>/dev/null >> "$TMPD/pools"
done

# -- connections: SAMPLES times, one awk over /proc/PID/net of all masters each time --
args=()
for P in $MASTERS; do
  for f in tcp tcp6 unix; do [ -r "/proc/$P/net/$f" ] && args+=("m=$P" "k=$f" "/proc/$P/net/$f"); done
done
: > "$TMPD/conn"
[ ${#args[@]} -gt 0 ] && for i in $(seq 1 $SAMPLES); do
  [ "$i" -gt 1 ] && sleep 1
  awk -v OFS='\t' -v smp="$i" '
    function hex(s,   i, n) { n = 0; s = toupper(s); for (i = 1; i <= length(s); i++) n = n * 16 + index("0123456789ABCDEF", substr(s, i, 1)) - 1; return n }
    FNR == 1 { next }
    k == "tcp" || k == "tcp6" { split($2, a, ":"); port = hex(a[2])
      if ($4 == "0A") { split($5, q, ":"); lq[m SUBSEP port] += hex(q[2]) }
      else if ($4 == "01") es[m SUBSEP port]++
      next }
    k == "unix" && NF >= 8 && $6 == "03" { es[m SUBSEP $8]++ }
    END { for (x in es) { split(x, b, SUBSEP); print smp, b[1], b[2], "e", es[x] }
          for (x in lq) { split(x, b, SUBSEP); print smp, b[1], b[2], "q", lq[x] } }' "${args[@]}" >> "$TMPD/conn"
done

# -- the logs: FPM lines in the new bytes, by offset; lines of the container log are json. The
# offset is kept by "where|path": the same /var/log/php7.4-fpm.log of two containers is two files.
# Two masters of one container writing one file - one read. Rows: key file inode size tz where --
awk -F'\t' '$1 == "L" { print $5 "\t" $3 "\t" $2 "\t" $4 }' "$TMPD/pools" | sort -u > "$TMPD/logs"
: > "$TMPD/lstat"
declare -A LSEEN=()
ln=0
while IFS=$'\t' read -r w f m how; do
  [ -z "${LSEEN["$w|$f"]:-}" ] || continue
  tzo=0; base=0
  case "$how" in
    safe)
      ln=$((ln + 1)); ff="$TMPD/log.$ln"
      meta=$(safe_snap "$m" "$f" "$ff" "$CAP") || continue
      IFS=$'\t' read -r lino lsize base <<< "$meta"
      st="$lino"$'\t'"$lsize"; tzo=$(tzoff_of "$m") ;;
    root) ff=$(in_root "$m" "$f"); tzo=$(tzoff_of "$m"); [ -n "$ff" ] || continue
      st=$(stat -c '%i	%s' "$ff" 2>/dev/null) || continue ;;
    *) ff=$f; st=$(stat -c '%i	%s' "$ff" 2>/dev/null) || continue ;;
  esac
  LSEEN["$w|$f"]=1
  printf '%s|%s\t%s\t%s\t%s\t%s\t%s\n' "$w" "$f" "$ff" "$st" "$tzo" "$w" "$base" >> "$TMPD/lstat"
done < "$TMPD/logs"

AWKFN='
function e(s,   _jp, _jn, _ji) { gsub(/[[:cntrl:]]/, "", s); _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
function mask(s,   out, st, ln, m) { s = substr(s, 1, 600); out = ""
  while (match(tolower(s), /(pass(word|wd)?|secret|token|api[_-]?key|authorization)["\047]?[ ]*[=:][ ]*[^ &"\047]+/)) {
    st = RSTART; ln = RLENGTH; m = substr(s, st, ln); match(m, /[=:][ ]*/)
    out = out substr(s, 1, st - 1) substr(m, 1, RSTART + RLENGTH - 1) "***"; s = substr(s, st + ln) }
  s = out s; gsub(/[[:cntrl:]]/, " ", s); gsub(/^ +| +$/, "", s); return substr(s, 1, 300) }
function msg(l,   q, s) {
  if (substr(l, 1, 8) != "{\"log\":\"") return l
  s = substr(l, 9); q = index(s, "\",\"stream\":"); if (q) s = substr(s, 1, q - 1)
  gsub(/\\n/, " ", s); gsub(/\\t/, " ", s); gsub(/\\"/, "\"", s); gsub(/\\u0026/, "\\&", s)
  return s }
# days since 1970-01-01 of a civil date (no mktime in mawk)
function days(y, mo, d,   era, yoe, doy) { y -= (mo <= 2); era = int(y / 400); yoe = y - era * 400
  doy = int((153 * (mo + (mo > 2 ? -3 : 9)) + 2) / 5) + d - 1
  return era * 146097 + yoe * 365 + int(yoe / 4) - int(yoe / 100) + doy - 719468 }
# "2026-10-30T10:15:02.123456789Z" or "...+03:00" -> unix time
function rfc(x,   t, r, z) { t = days(substr(x, 1, 4) + 0, substr(x, 6, 2) + 0, substr(x, 9, 2) + 0) * 86400 \
    + substr(x, 12, 2) * 3600 + substr(x, 15, 2) * 60 + substr(x, 18, 2)
  r = substr(x, 20); sub(/^\.[0-9]+/, "", r)
  if (r ~ /^[+-][0-9][0-9]:[0-9][0-9]/) { z = substr(r, 2, 2) * 3600 + substr(r, 5, 2) * 60; t -= (substr(r, 1, 1) == "-" ? -z : z) }
  return t }
# the time of a log line, unix: "time" of Docker json-file, the time of a CRI pod log line, or the
# "[30-Oct-2026 10:15:02]" of FPM in the local time of the container (tzo east of UTC); -1 - none
function ltime(l, s, tzo,   D, x, a) {
  D = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]"
  if (match(l, "\"time\":\"" D)) return rfc(substr(l, RSTART + 8))
  if (match(l, "^" D)) return rfc(l)
  if (match(s, /^\[[0-9][0-9]?-[A-Z][a-z][a-z]-[0-9][0-9][0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]/)) {
    x = substr(s, 2, RLENGTH - 1); split(x, a, /[- :]/)
    return days(a[3] + 0, (index("JanFebMarAprMayJunJulAugSepOctNovDec", a[2]) + 2) / 3, a[1] + 0) * 86400 \
      + a[4] * 3600 + a[5] * 60 + a[6] - tzo }
  return -1 }
'

# -- the kernel journal since the last run: php processes killed by a fault (the kernel prints
# "php-fpm[PID]: segfault at" and "traps: php-fpm[PID] general protection fault" for any container
# or pod). Rows: N count | E ts example | C cursor; no cursor - since the last run, or the last
# 5 minutes on the first one --
: > "$TMPD/kern"
if command -v journalctl >/dev/null 2>&1; then
  kc=$(awk -F'\t' '$1 == "KC" { print $2; exit }' "$STATE")
  kt=$(awk -F'\t' '$1 == "KT" { print $2 + 0; exit }' "$STATE")
  case "$kc" in *[!A-Za-z0-9=\;]*) kc="" ;; esac
  if [ -n "$kc" ]; then set -- "--after-cursor=$kc"
  elif [ "${kt:-0}" -gt $((NOW - 3600)) ]; then set -- "--since=@$kt"
  else set -- --since=-5min; fi
  timeout 30 journalctl -k -q --no-pager -o short-unix --show-cursor "$@" > "$TMPD/kj" 2>/dev/null
  js=$?
  awk -v OFS='\t' '
    /^-- cursor: / { c = substr($0, 12); next }
    { s = $0; ts = $1 + 0; sub(/^[0-9.]+ +[^ ]+ +kernel: +/, "", s); sub(/^traps: +/, "", s) }
    s ~ /^(php|lsphp)[A-Za-z0-9._-]*\[[0-9]+\]:? +(segfault at|general protection|trap )/ { n++; last = substr(s, 1, 300); lts = int(ts) }
    END { print "N", n + 0; if (n) print "E", lts, last; if (c != "") print "C", c }' "$TMPD/kj" > "$TMPD/kern" 2>/dev/null || : > "$TMPD/kern"
  # nothing new prints no cursor: keep the old one; a cursor the journal no longer has is an error
  # and is dropped, the next run reads since this one
  grep -q '^C' "$TMPD/kern" || { [ "$js" = 0 ] && [ -n "$kc" ] && printf 'C\t%s\n' "$kc" >> "$TMPD/kern"; }
fi

# -- together: pools, children, connections, log counts and history. State rows:
# V 2 | O where|file inode offset last-line-time | F key first-seen | S key saturated-minutes |
# B key reached slow timeout crash (per 10-minute bucket "b:n,b:n") | X key kind ts example |
# KB kernel crashes per bucket | KX ts example | KC journal cursor | KT time of the run.
# A state of 0.2 (no V): offsets were per path only, and the counts of a log shared by two
# containers were the same old lines counted again every minute - both go, the history starts over --
if ! grep -q '^V	2$' "$STATE"; then
  awk -F'\t' '$1 != "O" && $1 != "B"' "$STATE" > "$TMPD/st0" 2>/dev/null && cat "$TMPD/st0" > "$STATE"
fi
awk -F'\t' -v OFS='\t' -v now="$NOW" -v cap="$CAP" -v budget="$BUDGET" -v ns="$TMPD/state" -v jout="$TMPD/rows" -v kout="$TMPD/kern.json" "$AWKFN"'
  BEGIN { nowm = int(now / 60); nowb = int(now / 600) }
  t == "state" {
    if ($1 == "O") { ino[$2] = $3; off[$2] = $4; lt[$2] = $5 + 0 }
    else if ($1 == "F") first[$2] = $3
    else if ($1 == "S") sat[$2] = $3
    else if ($1 == "B") { br[$2] = $3; bs[$2] = $4; bt[$2] = $5; bc[$2] = $6 }
    else if ($1 == "X") { xts[$2 SUBSEP $3] = $4; xtx[$2 SUBSEP $3] = $5 }
    else if ($1 == "KB") kb = $2
    else if ($1 == "KX") { kxt = $2 + 0; kxx = $3 }
    next }
  t == "kern" {
    kread = 1
    if ($1 == "N") kn = $2 + 0; else if ($1 == "E") { kxt = $2 + 0; kxx = $3 } else if ($1 == "C") kcur = $2
    next }
  t == "kids" { kid[$1 SUBSEP $2] = $3; next }
  t == "pools" && $1 == "P" {
    m = $2; k = $3 "|" $4; np++; pk[np] = k; pm_[k] = m; pw[k] = $3; pn[k] = $4; pl[k] = $5; ppm[k] = $6
    pmx[k] = $7 + 0; psl[k] = $8 + 0; ptt[k] = $9 + 0; pst[k] = $10 + 0
    if (!(k in first)) first[k] = now
    # the listen key in /proc/net: a port number, or the socket path
    l = $5; if (l ~ /^\//) lk[k] = l; else { sub(/.*:/, "", l); lk[k] = l + 0 }
    next }
  t == "conn" {
    if ($4 == "e") ce[$1 SUBSEP $2 SUBSEP $3] = $5; else cq[$1 SUBSEP $2 SUBSEP $3] = $5
    smps[$1] = 1; next }
  t == "lstat" {
    f = $1; ff = $2; size = $4 + 0; tzo = $5 + 0; w = $6; snapbase = $7 + 0; o = 0
    no[f] = $3; nf[f] = size
    # a log seen for the first time: from its end, and no line older than now counts from it later
    if (!(f in off)) { nl[f] = now; next }
    lw = lt[f]; nl[f] = lw
    # else rotated, truncated or recreated: from the start, but only the lines after the last counted
    if (ino[f] == $3 && size >= off[f] + 0) o = off[f] + 0
    n = size - o; if (n <= 0) next
    if (used >= budget) { nf[f] = o; next }  # read next time
    rd = n > cap ? cap : n; from = size - rd; localfrom = from - snapbase; if (localfrom < 0) localfrom = 0
    cmd = "tail -c +" sprintf("%.0f", localfrom + 1) " \047" ff "\047 2>/dev/null | head -c " sprintf("%.0f", rd) \
      " | grep -aF -e \047reached pm.max_children\047 -e \047executing too slow\047 -e \047execution timed out\047 -e \047exited on signal\047"
    while ((cmd | getline l) > 0) {
      s = msg(l)
      if (!match(s, /\[pool [^]]+\]/)) continue
      pool = substr(s, RSTART + 6, RLENGTH - 7); k = w "|" pool; if (!(k in pm_)) continue
      # a line counts in the 10-minute bucket of its own time; older than the last counted from
      # this log or than a day - already counted or out of every window
      tl = ltime(l, s, tzo); if (tl < 0) tl = now
      if (tl < lw || tl <= now - 86400) continue
      if (tl > now) tl = now
      if (tl > nl[f]) nl[f] = tl
      kind = ""
      if (index(s, "server reached pm.max_children")) {
        kind = "reached"; x = s; if (match(x, /server reached/)) x = substr(x, RSTART)
      } else if (index(s, "executing too slow") || index(s, "execution timed out")) {
        kind = index(s, "executing too slow") ? "slow" : "tout"
        x = s; if (match(x, /script \047/)) x = substr(x, RSTART); sub(/, (logging|terminating)$/, "", x)
      } else if (match(s, /exited on signal (4|6|7|8|11) /)) {
        # a crash, not a kill: 9 and 15 are the terminate timeout, OOM and reloads
        kind = "crash"; x = s; if (match(x, /child [0-9]+ exited/)) x = substr(x, RSTART)
      }
      if (kind == "") continue
      cnt[kind SUBSEP k SUBSEP int(tl / 600)]++
      if (!((k SUBSEP kind) in xts) || tl >= xts[k SUBSEP kind]) { xts[k SUBSEP kind] = tl; xtx[k SUBSEP kind] = mask(x) }
    }
    close(cmd); used += rd
    next }
  # old buckets, add in the current one and the lines of this run counted under ck (kind SUBSEP key)
  function merge(old, add, ck,   a, n, i, p, b, c, res) {
    n = split(old, a, ","); for (i = 1; i <= n; i++) { p = index(a[i], ":"); if (!p) continue; b = substr(a[i], 1, p - 1) + 0
      if (b > nowb - 150) c[b] += substr(a[i], p + 1) }
    if (add > 0) c[nowb] += add
    res = ""; for (b = nowb - 149; b <= nowb; b++) {
      if (ck != "" && ((ck SUBSEP b) in cnt)) c[b] += cnt[ck SUBSEP b]
      if (c[b] > 0) res = res (res == "" ? "" : ",") b ":" c[b] }
    return res }
  function sum(bk, from,   a, n, i, p, s) { s = 0; n = split(bk, a, ",")
    for (i = 1; i <= n; i++) { p = index(a[i], ":"); if (p && substr(a[i], 1, p - 1) + 0 > from) s += substr(a[i], p + 1) }
    return s }
  END {
    printf "V\t2\n" > ns
    for (f in nf) printf "O\t%s\t%s\t%.0f\t%d\n", f, no[f], nf[f], nl[f] > ns
    rows = ""
    for (i = 1; i <= np; i++) {
      k = pk[i]; m = pm_[k]; ch = kid[m SUBSEP pn[k]] + 0
      # the busiest of the samples: connections (served + waiting) and the waiting ones - the
      # listen queue of a TCP socket, or what is over the workers (a unix socket shows no queue)
      ce1 = 0; cq1 = 0; satn = 0; lim = pmx[k] > 0 ? pmx[k] : ch
      for (smp in smps) {
        c = ce[smp SUBSEP m SUBSEP lk[k]] + 0; q = cq[smp SUBSEP m SUBSEP lk[k]] + 0
        if (lim > 0 && c - lim > q) q = c - lim
        if (c > ce1) ce1 = c; if (q > cq1) cq1 = q
        if (q > 0) satn++
      }
      # saturated minutes of the last hour
      s = ""; had = 0; n = split(sat[k], a, ",")
      for (j = 1; j <= n; j++) if (a[j] + 0 > nowm - 60) { s = s (s == "" ? "" : ",") a[j]; if (a[j] + 0 == nowm) had = 1 }
      if (satn > 0 && !had) s = s (s == "" ? "" : ",") nowm
      sm = (s == "" ? 0 : split(s, a, ","))
      r = merge(br[k], 0, "reached" SUBSEP k); sl = merge(bs[k], 0, "slow" SUBSEP k)
      to = merge(bt[k], 0, "tout" SUBSEP k); cr = merge(bc[k], 0, "crash" SUBSEP k)
      printf "F\t%s\t%d\n", k, first[k] > ns
      if (s != "") printf "S\t%s\t%s\n", k, s > ns
      if (r != "" || sl != "" || to != "" || cr != "") printf "B\t%s\t%s\t%s\t%s\t%s\n", k, r, sl, to, cr > ns
      ex = ""
      for (kind in kk) delete kk[kind]
      kk["reached"] = 1; kk["slow"] = 1; kk["tout"] = 1; kk["crash"] = 1
      for (kind in kk) if ((k SUBSEP kind) in xts && xts[k SUBSEP kind] > now - 86400) {
        printf "X\t%s\t%s\t%d\t%s\n", k, kind, xts[k SUBSEP kind], xtx[k SUBSEP kind] > ns
        ex = ex sprintf(",\"%s_last\":\"%s\",\"%s_ts\":%d", kind, e(xtx[k SUBSEP kind]), kind, xts[k SUBSEP kind]) }
      span = now - first[k]; if (span > 86400) span = 86400
      rows = rows (rows == "" ? "" : ",") sprintf("{\"where\":\"%s\",\"pool\":\"%s\",\"listen\":\"%s\",\"pm\":\"%s\",\"max\":%d,\"children\":%d,\"conns\":%d,\"queue\":%d,\"sat\":%d,\"span\":%d,\"reached\":%d,\"reached_d\":%d,\"slow\":%d,\"slow_d\":%d,\"tout\":%d,\"tout_d\":%d,\"crash\":%d,\"crash_d\":%d,\"slowlog\":%d,\"terminate\":%d,\"status\":%d%s}",
        e(pw[k]), e(pn[k]), e(pl[k]), e(ppm[k]), pmx[k], ch, ce1, cq1, sm, span,
        sum(r, nowb - 6), sum(r, nowb - 144), sum(sl, nowb - 6), sum(sl, nowb - 144), sum(to, nowb - 6), sum(to, nowb - 144),
        sum(cr, nowb - 6), sum(cr, nowb - 144), psl[k], ptt[k], pst[k], ex)
    }
    printf "%s", rows > jout
    if (kread) {
      kbs = merge(kb, kn, ""); if (kxt <= now - 86400) kxx = ""
      if (kbs != "") printf "KB\t%s\n", kbs > ns
      if (kxx != "") printf "KX\t%d\t%s\n", kxt, kxx > ns
      if (kcur != "") printf "KC\t%s\n", kcur > ns
      printf "KT\t%d\n", now > ns
      printf ",\"kern\":{\"n\":%d,\"n_d\":%d,\"last\":\"%s\",\"ts\":%d}", sum(kbs, nowb - 6), sum(kbs, nowb - 144),
        e(kxx), (kxx == "" ? 0 : kxt) > kout
    }
  }' t=state "$STATE" t=kids "$TMPD/kids" t=pools "$TMPD/pools" t=conn "$TMPD/conn" t=lstat "$TMPD/lstat" t=kern "$TMPD/kern" || exit 0
mv -f "$TMPD/state" "$STATE" 2>/dev/null || : > "$STATE"

skip=$(sort -u "$TMPD/skip" | head -50 | awk -F'\t' '
  function e(s,   _jp, _jn, _ji) { gsub(/[[:cntrl:]]/, "", s); _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
  { printf "%s{\"where\":\"%s\",\"why\":\"%s\"}", (NR > 1 ? "," : ""), e($1), e($2) }')
mkdir -p "${OUT%/*}"
printf '{"v":1,"ts":%s,"pools":[%s],"skipped":[%s]%s}\n' "$NOW" "$(cat "$TMPD/rows" 2>/dev/null)" "$skip" "$(cat "$TMPD/kern.json" 2>/dev/null)" > "$OUT.tmp.$$"
chmod 0644 "$OUT.tmp.$$"
mv -f "$OUT.tmp.$$" "$OUT"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-fpm.service <<EOF
[Unit]
Description=Kervax: PHP-FPM pools (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
TimeoutStartSec=5min
EOF
cat > /etc/systemd/system/kervax-fpm.timer <<'EOF'
[Unit]
Description=Kervax: look at the PHP-FPM pools every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-fpm.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-fpm.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/fpm-setup.ver"
chmod 0644 "$STATE_DIR/versions/fpm-setup.ver"
echo "✓ fpm-setup: PHP-FPM pools -> $OUT (every minute)."
