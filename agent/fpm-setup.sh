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
# slow" and "execution timed out", per pool, in 10-minute buckets for a day.
# Writes /var/lib/kervax/report.d/fpm.json; the agent hands report.d to the panel as is. Read only.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
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
# /proc/PID/net of each master, the new bytes of its log.
set -u
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OUT=${KERVAX_FPM_OUT:-/var/lib/kervax/report.d/fpm.json}
STATE=${KERVAX_FPM_STATE:-/var/lib/kervax/fpm.state}
SAMPLES=3        # connection counts per run, a second apart
CAP=8388608      # bytes of one log read in a run; more - only the last CAP
BUDGET=67108864  # bytes of logs read in a run in all

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
DROOT=""
if command -v docker >/dev/null 2>&1; then
  timeout 10 docker ps --no-trunc --format '{{.ID}} {{.Names}}' > "$TMPD/dps" 2>/dev/null || : > "$TMPD/dps"
  DROOT=$(timeout 10 docker info -f '{{.DockerRootDir}}' 2>/dev/null) || DROOT=""
  DROOT=${DROOT:-/var/lib/docker}
else
  : > "$TMPD/dps"
fi
# a path inside the root of process $1; an absolute symlink there points into ITS root. The paths
# come from the process and its config, so a ".." or a character outside the plain set gives
# nothing: a container must not steer reads outside its root, nor into a shell command
in_root() {
  local p=$1 path=$2 t
  case "$path" in *..*|*[!A-Za-z0-9._/@+-]*|"") return 0 ;; esac
  if [ -L "/proc/$p/root$path" ]; then
    t=$(readlink "/proc/$p/root$path" 2>/dev/null)
    case "$t" in *..*|*[!A-Za-z0-9._/@+-]*|"") return 0 ;; /*) path=$t ;; *) path="${path%/*}/$t" ;; esac
  fi
  printf '/proc/%s/root%s' "$p" "$path"
}

# -- each master: where, config, pools. Rows: "P master where pool listen pm max slow term status"
# and "L master logfile"; children per pool from ps --
ps -e -o pid=,ppid=,args= 2>/dev/null | awk '{ a = $0; sub(/^ *[0-9]+ +[0-9]+ /, "", a) }
  a ~ /^php-fpm[0-9.]*: pool / { sub(/^php-fpm[0-9.]*: pool /, "", a); sub(/ .*/, "", a); print $2 "\t" a }' \
  | sort | uniq -c | awk '{ print $2 "\t" $3 "\t" $1 }' > "$TMPD/kids"
: > "$TMPD/pools"
: > "$TMPD/skip"
for P in $MASTERS; do
  [ -r "/proc/$P/cmdline" ] || continue
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
  cf=$(in_root "$P" "$conf")
  [ -n "$cf" ] && [ -f "$cf" ] || { printf '%s\tnoconf\n' "$W" >> "$TMPD/skip"; continue; }
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
    d=$(in_root "$P" "${inc%/*}")
    [ -n "$d" ] || continue
    for f in "$d"/${inc##*/}; do [ -f "$f" ] && { cat "$f"; echo; } >> "$TMPD/conf"; done
  done
  awk -v OFS='\t' -v m="$P" -v w="$W" -v pre="$pre" -v logp="$LOGP" '
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
        if (logp != "") print "L", m, logp, "host"
      } else if (elog !~ /^syslog/) {
        if (elog !~ /^\//) elog = pre "/var/" elog
        if (elog !~ /\.\./ && elog ~ /^[A-Za-z0-9._\/@+-]+$/) print "L", m, elog, "root"
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

# -- the logs: FPM lines in the new bytes, by offset; lines of the container log are json --
awk -F'\t' '$1 == "L" { print $3 "\t" $2 "\t" $4 }' "$TMPD/pools" | sort -u > "$TMPD/logs"
: > "$TMPD/lstat"
while IFS=$'\t' read -r f m how; do
  case "$how" in root) ff=$(in_root "$m" "$f") ;; *) ff=$f ;; esac
  st=$(stat -c '%i	%s' "$ff" 2>/dev/null) || continue
  printf '%s\t%s\t%s\t%s\n' "$m" "$f" "$ff" "$st" >> "$TMPD/lstat"
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
'

# -- together: pools, children, connections, log counts and history. State rows:
# O file inode offset | F key first-seen | S key saturated-minutes | B key reached slow timeout
# (per 10-minute bucket "b:n,b:n") | X key kind ts example --
awk -F'\t' -v OFS='\t' -v now="$NOW" -v cap="$CAP" -v budget="$BUDGET" -v ns="$TMPD/state" -v jout="$TMPD/rows" "$AWKFN"'
  BEGIN { nowm = int(now / 60); nowb = int(now / 600) }
  t == "state" {
    if ($1 == "O") { ino[$2] = $3; off[$2] = $4 }
    else if ($1 == "F") first[$2] = $3
    else if ($1 == "S") sat[$2] = $3
    else if ($1 == "B") { br[$2] = $3; bs[$2] = $4; bt[$2] = $5 }
    else if ($1 == "X") { xts[$2 SUBSEP $3] = $4; xtx[$2 SUBSEP $3] = $5 }
    next }
  t == "kids" { kid[$1 SUBSEP $2] = $3; next }
  t == "pools" && $1 == "P" {
    m = $2; k = $3 "|" $4; np++; pk[np] = k; pm_[k] = m; pw[k] = $3; pn[k] = $4; pl[k] = $5; ppm[k] = $6
    pmx[k] = $7 + 0; psl[k] = $8 + 0; ptt[k] = $9 + 0; pst[k] = $10 + 0; poolof[m SUBSEP $4] = k
    if (!(k in first)) first[k] = now
    # the listen key in /proc/net: a port number, or the socket path
    l = $5; if (l ~ /^\//) lk[k] = l; else { sub(/.*:/, "", l); lk[k] = l + 0 }
    next }
  t == "conn" {
    if ($4 == "e") ce[$1 SUBSEP $2 SUBSEP $3] = $5; else cq[$1 SUBSEP $2 SUBSEP $3] = $5
    smps[$1] = 1; next }
  t == "lstat" {
    m = $1; f = $2; ff = $3; size = $5 + 0; o = 0
    no[f] = $4; nf[f] = size
    if (!(f in off)) next
    if (ino[f] == $4 && size >= off[f] + 0) o = off[f] + 0
    n = size - o; if (n <= 0) next
    if (used >= budget) { nf[f] = o; next }  # read next time
    rd = n > cap ? cap : n; from = size - rd
    cmd = "tail -c +" sprintf("%.0f", from + 1) " \047" ff "\047 2>/dev/null | head -c " sprintf("%.0f", rd) \
      " | grep -aF -e \047reached pm.max_children\047 -e \047executing too slow\047 -e \047execution timed out\047"
    while ((cmd | getline l) > 0) {
      s = msg(l)
      if (!match(s, /\[pool [^]]+\]/)) continue
      pool = substr(s, RSTART + 6, RLENGTH - 7); k = poolof[m SUBSEP pool]; if (k == "") continue
      if (index(s, "server reached pm.max_children")) nr[k]++
      else if (index(s, "executing too slow") || index(s, "execution timed out")) {
        kind = index(s, "executing too slow") ? "slow" : "tout"
        if (kind == "slow") nsl[k]++; else nto[k]++
        x = s; if (match(x, /script \047/)) x = substr(x, RSTART); sub(/, (logging|terminating)$/, "", x)
        xts[k SUBSEP kind] = now; xtx[k SUBSEP kind] = mask(x)
      }
    }
    close(cmd); used += rd
    next }
  function merge(old, add,   a, n, i, p, b, c, res) {
    n = split(old, a, ","); for (i = 1; i <= n; i++) { p = index(a[i], ":"); if (!p) continue; b = substr(a[i], 1, p - 1) + 0
      if (b > nowb - 150) c[b] += substr(a[i], p + 1) }
    if (add > 0) c[nowb] += add
    res = ""; for (b = nowb - 149; b <= nowb; b++) if (c[b] > 0) res = res (res == "" ? "" : ",") b ":" c[b]
    return res }
  function sum(bk, from,   a, n, i, p, s) { s = 0; n = split(bk, a, ",")
    for (i = 1; i <= n; i++) { p = index(a[i], ":"); if (p && substr(a[i], 1, p - 1) + 0 > from) s += substr(a[i], p + 1) }
    return s }
  END {
    for (f in nf) printf "O\t%s\t%s\t%.0f\n", f, no[f], nf[f] > ns
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
      r = merge(br[k], nr[k]); sl = merge(bs[k], nsl[k]); to = merge(bt[k], nto[k])
      printf "F\t%s\t%d\n", k, first[k] > ns
      if (s != "") printf "S\t%s\t%s\n", k, s > ns
      if (r != "" || sl != "" || to != "") printf "B\t%s\t%s\t%s\t%s\n", k, r, sl, to > ns
      ex = ""
      for (kind in kk) delete kk[kind]
      kk["slow"] = 1; kk["tout"] = 1
      for (kind in kk) if ((k SUBSEP kind) in xts && xts[k SUBSEP kind] > now - 86400) {
        printf "X\t%s\t%s\t%d\t%s\n", k, kind, xts[k SUBSEP kind], xtx[k SUBSEP kind] > ns
        ex = ex sprintf(",\"%s_last\":\"%s\",\"%s_ts\":%d", kind, e(xtx[k SUBSEP kind]), kind, xts[k SUBSEP kind]) }
      span = now - first[k]; if (span > 86400) span = 86400
      rows = rows (rows == "" ? "" : ",") sprintf("{\"where\":\"%s\",\"pool\":\"%s\",\"listen\":\"%s\",\"pm\":\"%s\",\"max\":%d,\"children\":%d,\"conns\":%d,\"queue\":%d,\"sat\":%d,\"span\":%d,\"reached\":%d,\"reached_d\":%d,\"slow\":%d,\"slow_d\":%d,\"tout\":%d,\"tout_d\":%d,\"slowlog\":%d,\"terminate\":%d,\"status\":%d%s}",
        e(pw[k]), e(pn[k]), e(pl[k]), e(ppm[k]), pmx[k], ch, ce1, cq1, sm, span,
        sum(r, nowb - 6), sum(r, nowb - 144), sum(sl, nowb - 6), sum(sl, nowb - 144), sum(to, nowb - 6), sum(to, nowb - 144),
        psl[k], ptt[k], pst[k], ex)
    }
    printf "%s", rows > jout
  }' t=state "$STATE" t=kids "$TMPD/kids" t=pools "$TMPD/pools" t=conn "$TMPD/conn" t=lstat "$TMPD/lstat" || exit 0
mv -f "$TMPD/state" "$STATE" 2>/dev/null || : > "$STATE"

skip=$(sort -u "$TMPD/skip" | head -50 | awk -F'\t' '
  function e(s,   _jp, _jn, _ji) { gsub(/[[:cntrl:]]/, "", s); _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
  { printf "%s{\"where\":\"%s\",\"why\":\"%s\"}", (NR > 1 ? "," : ""), e($1), e($2) }')
mkdir -p "${OUT%/*}"
printf '{"v":1,"ts":%s,"pools":[%s],"skipped":[%s]}\n' "$NOW" "$(cat "$TMPD/rows" 2>/dev/null)" "$skip" > "$OUT.tmp.$$"
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
