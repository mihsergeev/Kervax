#!/usr/bin/env bash
# Kervax: errors in container logs. A service that starts failing usually says so in its own log
# long before anything else notices: exceptions, tracebacks, FATAL from a database, [error] from
# nginx. Nobody reads hundreds of container logs, and a log that always carries some errors makes
# grep useless. A root helper counts error lines in every container log on the node (Docker
# json-file logs and Kubernetes pod logs alike) every minute, reading only the bytes written since
# the previous run, so even a node with hundreds of chatty containers costs little. Counts go into
# 10-minute buckets for a day: the panel compares the last hour with the usual hour of the same
# log and alerts on a spike, not on a log that is always noisy.
# Writes /var/lib/kervax/report.d/logerr.json; the agent hands report.d to the panel as is.
#
# An error line: ERROR, FATAL, CRITICAL, PANIC, Exception, a Python traceback, [error] [crit]
# [alert] [emerg] of nginx and Apache, level=error and "level":"error" of structured logs, PHP Fatal
# and Parse errors, Uncaught. The last such line of each log is kept as an example, with values
# after password=, token= and the like masked. A log is read from where it ended the previous run;
# a new log starts from its end, a truncated or rotated one from its beginning. More than CAP new
# bytes in a minute: the last CAP are read and the count is scaled. Read only.
#
# 0.2: a stack frame is not an error line of its own: "3. DB::Exception::Exception<>(...) @ 0x..."
# of ClickHouse, "at org.foo.Bar(Bar.java:12)" of Java and Node, "#3 /app/x.php(12): ..." of PHP,
# "Caused by:" and "... 12 more". One exception with thirty frames counted as thirty errors, and a
# ClickHouse pod reached the "no usual hour yet" threshold with a few dozen of them.
#
# 0.3: nginx lines the client causes are not errors of the server: limit_req and limit_conn
# rejections, deny rules, a missing file (404), a directory without an index, a body over
# client_max_body_size, basic auth failures, a client TLS handshake that failed, "while sending to
# client" (the client left) and "Primary script unknown" (404 through PHP-FPM). nginx logs them at
# [error], and one bot hitting a rate limit made ingress-nginx look like it was failing. The report
# also carries the error count of each 10-minute bucket of the last hour ("hb", oldest first, the
# last one still filling): the panel alerts only on a spike that holds, not on a five-minute burst.
#
# 0.4: the report is valid UTF-8. The example line is cut by bytes and once ended in half a
# Cyrillic letter; the panel rejected every report of that node with 400 and it looked down.
set -euo pipefail

KERVAX_SETUP_VERSION=0.4  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: without Docker and pods it writes an empty report

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-logerr"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/logerr.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes,
# and rewriting it in place would make a run in progress continue from the same byte offset.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Error lines in container logs -> /var/lib/kervax/report.d/logerr.json. Read only: docker ps,
# docker info, the new bytes of each log file.
set -u
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OUT=${KERVAX_LOGERR_OUT:-/var/lib/kervax/report.d/logerr.json}
STATE=${KERVAX_LOGERR_STATE:-/var/lib/kervax/logerr.state}
PODS=${KERVAX_LOGERR_PODS:-/var/log/pods}
CAP=8388608       # bytes of one log read in a run; more is counted by proportion
BUDGET=67108864   # bytes read in a run in all; past it a log only moves its offset
MAX_SRC=150       # logs in the report: those with errors in a day, the most erring first

exec 9>/run/kervax-logerr.lock
flock -n 9 || exit 0
NOW=$(date +%s)
TMPD=$(mktemp -d /tmp/kervax-logerr.XXXXXX) || exit 0
trap 'rm -rf "$TMPD"' EXIT

# -- the logs: "path<TAB>where<TAB>container" --
: > "$TMPD/src"
if command -v docker >/dev/null 2>&1; then
  root=$(timeout 10 docker info -f '{{.DockerRootDir}}' 2>/dev/null) || root=""
  root=${root:-/var/lib/docker}
  # containers of Kubernetes under dockershim (k8s_*) are read from the pod logs below
  timeout 10 docker ps --no-trunc --format '{{.ID}}	{{.Names}}' 2>/dev/null \
    | awk -F'\t' -v r="$root" '$2 !~ /^k8s_/ { printf "%s/containers/%s/%s-json.log\tcontainer %s\t\n", r, $1, $1, $2 }' >> "$TMPD/src"
fi
if [ -d "$PODS" ]; then
  # /var/log/pods/<ns>_<pod>_<uid>/<container>/<restart>.log: the newest file of a container
  for d in "$PODS"/*/*/; do
    best=""
    for f in "$d"*.log; do
      [ -f "$f" ] || continue
      if [ -z "$best" ] || [ "$f" -nt "$best" ]; then best=$f; fi
    done
    [ -n "$best" ] || continue
    p=${d#"$PODS"/}; p=${p%%/*}; ns=${p%%_*}; rest=${p#*_}; pod=${rest%_*}
    c=${d%/}; c=${c##*/}
    printf '%s\tpod %s/%s\t%s\n' "$best" "$ns" "$pod" "$c" >> "$TMPD/src"
  done
fi

# inode and size of each, one stat
cut -f1 "$TMPD/src" | tr '\n' '\0' | xargs -0 -r stat -c '%i	%s	%n' 2>/dev/null > "$TMPD/stat"
[ -f "$STATE" ] || : > "$STATE"

AWKFN='
function e(s,   _jp, _jn, _ji) { gsub(/[[:cntrl:]]/, "", s); _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
function mask(s,   out, st, ln, m) { s = substr(s, 1, 600); out = ""
  while (match(tolower(s), /(pass(word|wd)?|secret|token|api[_-]?key|authorization)["\047]?[ ]*[=:][ ]*[^ ]+/)) {
    st = RSTART; ln = RLENGTH; m = substr(s, st, ln); match(m, /[=:][ ]*/)
    out = out substr(s, 1, st - 1) substr(m, 1, RSTART + RLENGTH - 1) "***"; s = substr(s, st + ln) }
  s = out s; gsub(/[[:cntrl:]]/, " ", s); gsub(/^ +| +$/, "", s); return substr(s, 1, 300) }
# the message of a log line: the "log" field of Docker json-file (unescaped), or the text after
# "time stream tag" of a CRI pod log
function msg(l,   q, s) {
  if (substr(l, 1, 8) == "{\"log\":\"") {
    s = substr(l, 9); q = index(s, "\",\"stream\":"); if (q) s = substr(s, 1, q - 1)
    gsub(/\\n/, " ", s); gsub(/\\t/, " ", s); gsub(/\\r/, "", s); gsub(/\\u003c/, "<", s); gsub(/\\u003e/, ">", s)
    gsub(/\\u0026/, "\\&", s); gsub(/\\"/, "\"", s)
    return s }
  if (match(l, /^[0-9T:.+Z-]+ (stdout|stderr) [FP] /)) return substr(l, RLENGTH + 1)
  return l }
'

awk -F'\t' -v OFS='\t' -v now="$NOW" -v cap="$CAP" -v budget="$BUDGET" -v ns="$TMPD/state" -v jout="$TMPD/rows" "$AWKFN"'
  BEGIN {
    RE = "ERROR|FATAL|CRITICAL|PANIC|Traceback \\(most recent|Exception|\\[(error|crit|alert|emerg)\\]|level=(error|fatal|panic|crit)|level[\\\\]?\"?: ?[\\\\]?\"(error|fatal|panic|crit)|PHP (Fatal|Parse) error|Uncaught "
    # a frame of a stack trace, the error itself is the line before it
    FR = "^[ \t]*([0-9]+\\. .* @ 0x[0-9a-fA-F]+|at .*[(:]|#[0-9]+ |Caused by: |\\.\\.\\. [0-9]+ (more|common frames))"
    # an nginx [error] the client caused: rate limit, deny, 404, no index, big body, basic auth, client
    # TLS, the client left, a php script that does not exist
    NG = "\\[(error|crit)\\] [0-9]+#[0-9]+: (\\*[0-9]+ )?(limiting (requests|connections)|access forbidden by rule|open\\(\\) \".*\" failed \\(2: No such file or directory\\)|\".*\" is not found \\(2: No such file or directory\\)|directory index of \".*\" is forbidden|client intended to send too large|no user/password was provided for basic authentication|user \".*\" was not found in|user \".*\": password mismatch|SSL_do_handshake\\(\\) failed .* while SSL handshaking, client|.* while sending to client|.*Primary script unknown)"
    nowb = int(now / 600); used = 0
  }
  t == "state" {
    if ($1 == "O") { ino[$2] = $3; off[$2] = $4 }           # O path inode offset
    else if ($1 == "B") { eb[$2] = $3; lb[$2] = $4 }        # B key err-buckets line-buckets ("b:n,b:n")
    else if ($1 == "F") first[$2] = $3                      # F key first-seen
    else if ($1 == "L") { lts[$2] = $3; lsm[$2] = $4 }      # L key ts example
    next }
  t == "src" { sw[$1] = $2; sc[$1] = $3; next }
  t == "stat" {
    f = $3; if (!(f in sw)) next
    k = sw[f] "|" sc[f]; seen[k] = 1; kw[k] = sw[f]; kc[k] = sc[f]
    if (!(k in first)) first[k] = now
    size = $2 + 0; o = 0
    if (!(f in off)) { no[f] = $1; nf[f] = size; next }     # a new log: from its end
    if (ino[f] == $1 && size >= off[f] + 0) o = off[f] + 0  # else rotated or truncated: from the start
    n = size - o; no[f] = $1; nf[f] = size
    if (n <= 0) next
    if (used >= budget) { skipped++; next }
    rd = n > cap ? cap : n; from = size - rd
    # sizes past 2 GiB: mawk turns a big integer into 7.1983e+09 in a string and %d cuts it at 2^31
    cmd = "tail -c +" sprintf("%.0f", from + 1) " \047" f "\047 2>/dev/null | head -c " sprintf("%.0f", rd)
    errs = 0; lines = 0; last = ""
    while ((cmd | getline l) > 0) { lines++; if (l ~ RE) { m = msg(l); if (m !~ FR && m !~ NG) { errs++; last = l } } }
    close(cmd); used += rd
    if (rd < n) { errs = int(errs * n / rd + 0.5); lines = int(lines * n / rd + 0.5) }
    ne[k] += errs; nl[k] += lines
    if (last != "") { lts[k] = now; lsm[k] = mask(msg(last)) }
    next }
  function merge(old, add,   a, n, i, p, b, c, res) {
    n = split(old, a, ","); for (i = 1; i <= n; i++) { p = index(a[i], ":"); if (!p) continue; b = substr(a[i], 1, p - 1) + 0
      if (b > nowb - 150) c[b] += substr(a[i], p + 1) }
    if (add > 0) c[nowb] += add
    res = ""; for (b = nowb - 149; b <= nowb; b++) if (c[b] > 0) res = res (res == "" ? "" : ",") b ":" c[b]
    return res }
  function sum(bs, from,   a, n, i, p, s) { s = 0; n = split(bs, a, ",")
    for (i = 1; i <= n; i++) { p = index(a[i], ":"); if (p && substr(a[i], 1, p - 1) + 0 > from) s += substr(a[i], p + 1) }
    return s }
  # the six buckets of the last hour, oldest first: "[0,0,12,340,410,95]"
  function hbs(bs,   a, n, i, p, b, c, res) { n = split(bs, a, ",")
    for (i = 1; i <= n; i++) { p = index(a[i], ":"); if (p) c[substr(a[i], 1, p - 1) + 0] = substr(a[i], p + 1) + 0 }
    res = ""; for (b = nowb - 5; b <= nowb; b++) res = res (res == "" ? "" : ",") sprintf("%.0f", c[b] + 0)
    return "[" res "]" }
  END {
    for (f in nf) printf "O\t%s\t%s\t%.0f\n", f, no[f], nf[f] > ns
    for (k in seen) {
      e1 = merge(eb[k], ne[k]); l1 = merge(lb[k], nl[k])
      printf "F\t%s\t%d\n", k, first[k] > ns
      if (e1 != "" || l1 != "") printf "B\t%s\t%s\t%s\n", k, e1, l1 > ns
      if ((k in lts) && lts[k] > now - 86400) printf "L\t%s\t%d\t%s\n", k, lts[k], lsm[k] > ns
      d1 = sum(e1, nowb - 144); if (d1 <= 0) continue
      h1 = sum(e1, nowb - 6); hl = sum(l1, nowb - 6)
      span = now - first[k]; if (span > 86400) span = 86400
      printf "%09d\t%09d\t{\"where\":\"%s\",\"container\":\"%s\",\"h1\":%d,\"d1\":%d,\"lines1\":%d,\"span\":%d,\"hb\":%s,\"last_ts\":%d,\"last\":\"%s\"}\n",
        999999999 - h1, 999999999 - d1, e(kw[k]), e(kc[k]), h1, d1, hl, span, hbs(e1), lts[k] + 0, e(lsm[k]) > jout
    }
    printf "%d\t%.0f\n", skipped + 0, used
  }' t=state "$STATE" t=src "$TMPD/src" t=stat "$TMPD/stat" > "$TMPD/head" || exit 0
mv -f "$TMPD/state" "$STATE" 2>/dev/null || : > "$STATE"

read -r SKIP USED < "$TMPD/head"
NSRC=$(wc -l < "$TMPD/stat")
ROWS=$( { [ -f "$TMPD/rows" ] && sort "$TMPD/rows"; } | head -$MAX_SRC | cut -f3 | awk '{ printf "%s%s", (NR > 1 ? "," : ""), $0 }')
mkdir -p "${OUT%/*}"
printf '{"v":1,"ts":%s,"logs":%s,"read":%s,"skipped":%s,"sources":[%s]}\n' "$NOW" "${NSRC:-0}" "${USED:-0}" "${SKIP:-0}" "$ROWS" > "$OUT.tmp.$$"
# substr counts bytes (LC_ALL=C, mawk): an example line cut at 300 could end in half a letter,
# and that one broken byte made the panel reject the whole report of the node. iconv -c drops
# what is not UTF-8; without iconv the file goes as it is.
if command -v iconv >/dev/null 2>&1; then
  iconv -f UTF-8 -t UTF-8 -c "$OUT.tmp.$$" > "$OUT.tmp.$$.u" 2>/dev/null || :
  if [ -s "$OUT.tmp.$$.u" ]; then mv -f "$OUT.tmp.$$.u" "$OUT.tmp.$$"; else rm -f "$OUT.tmp.$$.u"; fi
fi
chmod 0644 "$OUT.tmp.$$"
mv -f "$OUT.tmp.$$" "$OUT"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-logerr.service <<EOF
[Unit]
Description=Kervax: error lines in container logs (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=5min
EOF
cat > /etc/systemd/system/kervax-logerr.timer <<'EOF'
[Unit]
Description=Kervax: count the errors in container logs every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-logerr.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-logerr.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/logerr-setup.ver"
chmod 0644 "$STATE_DIR/versions/logerr-setup.ver"
echo "✓ logerr-setup: error lines in container logs -> $OUT (every minute)."
