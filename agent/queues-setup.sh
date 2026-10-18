#!/usr/bin/env bash
# Kervax: background job queues kept in Redis. RabbitMQ queues the agent reads by itself (the
# prometheus plugin needs no password); queues of Celery, Sidekiq, RQ, BullMQ, Laravel and Yii live
# in Redis as plain lists, and a Redis usually has a password and sits in a container or a pod.
# A root helper finds every redis-server and valkey-server on the node by its process (docker,
# Kubernetes pods and the host alike), enters its network and mount namespaces and asks it with
# its own redis-cli. Every 30 minutes it looks for queues (one SCAN of each database), every
# minute it only reads their depth, and writes /var/lib/kervax/report.d/queues.json. The agent
# hands report.d to the panel as is; the panel watches how a queue moves and alerts on one that
# grows and is not consumed, like it does for RabbitMQ.
#
# How a queue is recognized:
#   Celery    _kombu.binding.<exchange> sets name the queues; a queue is a list, priorities are
#             lists "<queue>\x06\x16<n>"
#   Sidekiq   [<namespace>]queues set, lists [<namespace>]queue:<q>; workers - [<namespace>]processes,
#             each lists its queues in the "info" field (GitLab: namespace resque:gitlab:)
#   RQ        rq:queues set of rq:queue:<q> lists, rq:workers:<q> workers, rq:wip:<q> in progress
#   BullMQ    <prefix>:<q>:meta with <prefix>:<q>:id; waiting in :wait, :prioritized, :paused,
#             in progress in :active. Bull 3: bull:<q>:id
#   Laravel   [<prefix>]queues:<q> list, :reserved in progress
#   Yii       <channel>.message_id; waiting in <channel>.waiting, in progress in <channel>.reserved
# Delayed and scheduled jobs are not counted: they wait by design.
#
# The password: Redis overwrites its own environment with the process title, so it is read from
# the container's OCI spec (config.json of containerd: REDIS_PASSWORD in the environment or
# --requirepass in the arguments, for docker and Kubernetes alike) or from requirepass in the config
# file. It goes to redis-cli on stdin (REDISCLI_AUTH), never on a command line, and does not leave
# the node. A Redis whose password the helper does not know is listed as such, without queues.
#
# Only queue names and numbers leave the node: no job contents, no other keys.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_WHEN="pgrep -x redis-server >/dev/null 2>&1 || pgrep -x valkey-server >/dev/null 2>&1"

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-queues"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/queues.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes,
# and rewriting it in place would make a run in progress continue from the same byte offset.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Background job queues in Redis -> /var/lib/kervax/report.d/queues.json. Read only: PING, INFO,
# SCAN, TYPE, EXISTS, SMEMBERS, HGET, LLEN, ZCARD, SCARD.
set -u
export LC_ALL=C
OUT=${KERVAX_QUEUES_OUT:-/var/lib/kervax/report.d/queues.json}
CACHE=${KERVAX_QUEUES_CACHE:-/var/lib/kervax/queues.cache}
TO=5            # seconds for one redis-cli call
SCAN_TO=60      # and for the SCAN of one database
FIND_EVERY=1800 # look for queues anew every 30 minutes
MAX_Q=60        # queues per broker in the report, the deepest first
MAX_DB=16
MAX_KEYS=1000000  # a database with more keys is not scanned for queues

exec 9>/run/kervax-queues.lock
flock -n 9 || exit 0
TMPD=$(mktemp -d /tmp/kervax-queues.XXXXXX) || exit 0
trap 'rm -rf "$TMPD"' EXIT
NOW=$(date +%s)

# JSON string body: control characters dropped, backslash and quote escaped
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# A key as an argument of a redis-cli command read from stdin: in double quotes, with \xHH for
# bytes outside printable ASCII (Celery priority lists carry \x06\x16), "|" and tab too: they
# separate commands in the cache
qk() {
  printf '%s' "$1" | awk 'BEGIN { for (i = 1; i < 256; i++) o[sprintf("%c", i)] = i }
    { if (NR > 1) s = s "\\x0a"; for (j = 1; j <= length($0); j++) { c = substr($0, j, 1); n = o[c]
        if (c == "\\") s = s "\\\\"; else if (c == "\"") s = s "\\\""; else if (n < 33 || n > 126 || c == "|") s = s sprintf("\\x%02x", n); else s = s c } }
    END { printf "\"%s\"", s }'
}

# -- Redis instances: by process, deduplicated by network namespace and port (socket) --
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
where_of() { # pid id spec -> "container X" | "pod ns/name" | "host"
  local p=$1 id=$2 spec=$3 ns name
  [ -n "$id" ] && [ -n "${DNAME[$id]:-}" ] && { echo "container ${DNAME[$id]}"; return; }
  if [ -n "$spec" ]; then
    name=$(grep -o '"io.kubernetes.cri.sandbox-name":"[^"]*"' "$spec" | head -1 | cut -d'"' -f4)
    ns=$(grep -o '"io.kubernetes.cri.sandbox-namespace":"[^"]*"' "$spec" | head -1 | cut -d'"' -f4)
    [ -n "$name" ] && { echo "pod ${ns:+$ns/}$name"; return; }
  fi
  [ -n "$id" ] && { echo "container ${id:0:12}"; return; }
  echo host
}
spec_env() { # spec var -> value
  grep -o "\"$2=[^\"]*\"" "$1" 2>/dev/null | head -1 | sed "s/^\"$2=//; s/\"\$//"
}
pw_of() { # pid spec -> the password or nothing
  local p=$1 spec=$2 pw="" f args
  if [ -n "$spec" ]; then
    for f in REDIS_PASSWORD VALKEY_PASSWORD REDIS_PASS; do pw=$(spec_env "$spec" $f); [ -n "$pw" ] && break; done
    if [ -z "$pw" ]; then
      f=$(spec_env "$spec" REDIS_PASSWORD_FILE)
      [ -n "$f" ] && pw=$(head -1 "/proc/$p/root$f" 2>/dev/null | tr -d '\r\n')
    fi
    args=$(grep -o '"args":\[[^]]*\]' "$spec" | head -1 | sed 's/^"args":\[//; s/\]$//; s/","/\n/g; s/^"//; s/"$//' | tr ' ' '\n')
    [ -z "$pw" ] && pw=$(printf '%s\n' "$args" | awk 'p { print; exit } $0 == "--requirepass" { p = 1; next } /^--requirepass=/ { sub(/^--requirepass=/, ""); print; exit }' | tr -d "\"'")
    if [ -z "$pw" ]; then
      f=$(printf '%s\n' "$args" | grep -m1 '\.conf$')
      [ -n "$f" ] && pw=$(awk '$1 == "requirepass" { print $2; exit }' "/proc/$p/root$f" 2>/dev/null | tr -d "\"'")
    fi
  fi
  if [ -z "$pw" ]; then
    for f in /usr/local/etc/redis/redis.conf /etc/redis/redis.conf /etc/redis.conf /etc/redis/6379.conf \
             /opt/bitnami/redis/etc/redis.conf /data/redis.conf /redis.conf /etc/valkey/valkey.conf \
             /var/opt/gitlab/redis/redis.conf; do
      pw=$(awk '$1 == "requirepass" { print $2; exit }' "/proc/$p/root$f" 2>/dev/null | tr -d "\"'")
      [ -n "$pw" ] && break
    done
  fi
  printf '%s' "$pw"
}

# Where the instance listens, from its own sockets: "-h <ip> -p <port>" or "-s <path>". Not ss -p:
# that walks the descriptors of every process on the node, and with forty Redis containers one
# pass took twenty seconds
listen_of() { # pid
  local p=$1 ino
  ino=" $(ls -l "/proc/$p/fd" 2>/dev/null | sed -n 's/.*socket:\[\([0-9]*\)\].*/\1/p' | sort -u | tr '\n' ' ')"
  [ "$ino" = " " ] && return 0
  awk -v ino="$ino" 'FNR == 1 { next }
    $4 == "0A" && index(ino, " " $10 " ") { split($2, a, ":"); h = a[1]; port = 0
      for (i = 1; i <= length(a[2]); i++) port = port * 16 + index("0123456789ABCDEF", substr(a[2], i, 1)) - 1
      if (h ~ /^0+$/) ip = "127.0.0.1"
      else if (h == "00000000000000000000000001000000") ip = "::1"
      else if (length(h) == 8) { ip = ""; for (i = 7; i >= 1; i -= 2) { v = 0
          for (j = 0; j < 2; j++) v = v * 16 + index("0123456789ABCDEF", substr(h, i + j, 1)) - 1
          ip = ip (ip == "" ? "" : ".") v } }
      else next
      print "-h " ip " -p " port; exit }' "/proc/$p/net/tcp" "/proc/$p/net/tcp6" 2>/dev/null | head -1 | grep . && return 0
  awk -v ino="$ino" '$6 == "01" && $8 != "" && index(ino, " " $7 " ") { print "-s " $8; exit }' "/proc/$p/net/unix" 2>/dev/null
}

# redis-cli of the instance itself, inside its namespaces; P CLI TGT PW are set per instance
rcli() {
  printf '%s\n' "$PW" | timeout "${T:-$TO}" nsenter -t "$P" -n -m sh -c \
    'read -r REDISCLI_AUTH; [ -n "$REDISCLI_AUTH" ] && export REDISCLI_AUTH; c=$1; shift; exec "$c" "$@"' \
    sh "$CLI" $TGT "$@" 2>/dev/null
}
# commands from stdin, one reply per line
rbatch() {
  { printf '%s\n' "$PW"; cat; } | timeout "$TO" nsenter -t "$P" -n -m sh -c \
    'read -r REDISCLI_AUTH; [ -n "$REDISCLI_AUTH" ] && export REDISCLI_AUTH; c=$1; shift; exec "$c" "$@"' \
    sh "$CLI" $TGT -n "$1" 2>/dev/null
}

: > "$TMPD/inst"
declare -A SEEN
for P in $(pgrep -x redis-server; pgrep -x valkey-server); do
  exe=$(readlink "/proc/$P/exe" 2>/dev/null) || continue
  CLI=redis-cli
  for c in "${exe%/*}/redis-cli" "${exe%/*}/valkey-cli"; do [ -x "/proc/$P/root$c" ] && { CLI=$c; break; }; done
  TGT=$(listen_of "$P")
  case "$TGT" in
    -h*) key="$(readlink "/proc/$P/ns/net")|${TGT##* }" ;;
    -s*) key="$(readlink "/proc/$P/ns/mnt")|$TGT" ;;
    *) continue ;;
  esac
  [ -n "${SEEN[$key]:-}" ] && continue
  SEEN[$key]=1
  id=$(cid_of "$P"); spec=""
  [ -n "$id" ] && spec=$(spec_of "$id")
  w=$(where_of "$P" "$id" "$spec")
  # pid, where, target, cli, spec; the instance is "where|target"
  printf '%s\t%s\t%s\t%s\t%s\n' "$P" "$w" "$TGT" "$CLI" "$spec" >> "$TMPD/inst"
done

# -- finding queues of one instance: D (when, status) and Q lines into the new cache --
# Q: inst, system, db, namespace, queue, ready commands, in-progress commands, workers command
qline() { printf 'Q\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$INST" "$@" >> "$TMPD/cache"; }
find_db() { # db
  local db=$1 k ns m name base
  T=$SCAN_TO rcli -n "$db" --scan --count 1000 > "$TMPD/keys" || :
  [ -s "$TMPD/keys" ] || T=$SCAN_TO rcli -n "$db" --scan > "$TMPD/keys" || :
  [ -s "$TMPD/keys" ] || return 0
  # Celery: queue names from the bindings, priorities are separate lists
  grep -a '^_kombu\.binding\.' "$TMPD/keys" | head -50 | while IFS= read -r k; do
    rcli -n "$db" smembers "$k" | awk -F'\006\026' 'NF >= 3 && $3 != "" { print $3 }'
  done | sort -u | grep -av -E '\.pidbox$|^celeryev\.|\.reply\.celery\.pidbox$' | head -200 | while IFS= read -r name; do
    r="LLEN $(qk "$name")"
    while IFS= read -r m; do r="$r|LLEN $(qk "$m")"; done \
      < <(awk -v p="$name"$'\006\026' 'index($0, p) == 1' "$TMPD/keys" | head -10)
    qline celery "$db" "" "$name" "$r" "" ""
  done
  # Sidekiq: [namespace]queues sets
  grep -a -E '(^|:)queues$' "$TMPD/keys" | grep -avx 'rq:queues' | head -10 | while IFS= read -r k; do
    [ "$(rcli -n "$db" type "$k")" = set ] || continue
    ns=${k%queues}
    rcli -n "$db" smembers "$k" | head -200 | while IFS= read -r m; do
      qline sidekiq "$db" "$ns" "$m" "LLEN $(qk "${ns}queue:$m")" "" ""
    done
  done
  # RQ
  if grep -qax 'rq:queues' "$TMPD/keys"; then
    rcli -n "$db" smembers rq:queues | grep -a '^rq:queue:' | head -200 | while IFS= read -r k; do
      name=${k#rq:queue:}
      qline rq "$db" "" "$name" "LLEN $(qk "$k")" "ZCARD $(qk "rq:wip:$name")" "SCARD $(qk "rq:workers:$name")"
    done
  fi
  # BullMQ (<prefix>:<q>:meta + :id) and Bull 3 (bull:<q>:id)
  { grep -a -E '^.+:[^:]+:meta$' "$TMPD/keys" | sed 's/:meta$//'
    grep -a -E '^bull:[^:]+:id$' "$TMPD/keys" | sed 's/:id$//'; } | sort -u | head -200 | while IFS= read -r base; do
    [ "$(rcli -n "$db" exists "$base:id")" = 1 ] || continue
    name=${base##*:}; ns=${base%"$name"}
    qline bullmq "$db" "$ns" "$name" "LLEN $(qk "$base:wait")|ZCARD $(qk "$base:prioritized")|LLEN $(qk "$base:paused")" \
      "LLEN $(qk "$base:active")" ""
  done
  # Laravel: [prefix]queues:<q> list and its :reserved; a string under the same pattern is not it
  grep -a -E 'queues:[^:]+(:reserved|:delayed|:notify)?$' "$TMPD/keys" | sed -E 's/:(reserved|delayed|notify)$//' \
    | sort -u | head -200 | while IFS= read -r base; do
    t=$(rcli -n "$db" type "$base")
    [ "$t" = list ] || { [ "$t" = none ] && [ "$(rcli -n "$db" type "$base:reserved")" = zset ]; } || continue
    name=${base##*queues:}; ns=${base%"$name"}
    qline laravel "$db" "$ns" "$name" "LLEN $(qk "$base")" "ZCARD $(qk "$base:reserved")" ""
  done
  # Yii2 queue: <channel>.message_id
  grep -a -E '\.message_id$' "$TMPD/keys" | sed 's/\.message_id$//' | head -100 | while IFS= read -r base; do
    [ "$(rcli -n "$db" type "$base.message_id")" = string ] || continue
    qline yii "$db" "" "$base" "LLEN $(qk "$base.waiting")" "ZCARD $(qk "$base.reserved")" ""
  done
}

: > "$TMPD/cache"
: > "$TMPD/skip"
while IFS=$'\t' read -r P W TGT CLI SPEC; do
  INST="$W|$TGT"
  last=$(awk -F'\t' -v i="$INST" '$1 == "D" && $2 == i { print $3 "\t" $4; exit }' "$CACHE" 2>/dev/null)
  when=${last%%$'\t'*}; status=${last#*$'\t'}
  if [ -n "$last" ] && [ $((NOW - ${when:-0})) -lt $FIND_EVERY ]; then
    awk -F'\t' -v i="$INST" '($1 == "D" || $1 == "Q") && $2 == i' "$CACHE" >> "$TMPD/cache"
    [ "$status" = auth ] && printf '%s\n' "$W" >> "$TMPD/skip"
    continue
  fi
  PW=$(pw_of "$P" "$SPEC")
  pong=$(rcli ping | head -1 | tr -d '\r')
  case "$pong" in
    PONG) ;;
    *NOAUTH*|*WRONGPASS*|*invalid*password*|*AUTH*)
      printf 'D\t%s\t%s\tauth\n' "$INST" "$NOW" >> "$TMPD/cache"; printf '%s\n' "$W" >> "$TMPD/skip"; PW=""; continue ;;
    *) printf 'D\t%s\t%s\tdown\n' "$INST" "$NOW" >> "$TMPD/cache"; PW=""; continue ;;
  esac
  printf 'D\t%s\t%s\tok\n' "$INST" "$NOW" >> "$TMPD/cache"
  for db in $(rcli info keyspace | tr -d '\r' | sed -n 's/^db\([0-9]*\):keys=\([0-9]*\).*/\1 \2/p' \
               | awk -v m=$MAX_KEYS '$2 > 0 && $2 <= m { print $1 }' | head -$MAX_DB); do
    find_db "$db"
  done
  PW=""
done < "$TMPD/inst"
mv -f "$TMPD/cache" "$CACHE.new" && mv -f "$CACHE.new" "$CACHE"

# -- depth of the known queues: one redis-cli per database, commands on stdin --
: > "$TMPD/rows"
while IFS=$'\t' read -r P W TGT CLI SPEC; do
  INST="$W|$TGT"
  grep -qF "Q"$'\t'"$INST"$'\t' "$CACHE" || continue
  PW=$(pw_of "$P" "$SPEC")
  for db in $(awk -F'\t' -v i="$INST" '$1 == "Q" && $2 == i { print $4 }' "$CACHE" | sort -un); do
    awk -F'\t' -v i="$INST" -v d="$db" '$1 == "Q" && $2 == i && $4 == d' "$CACHE" > "$TMPD/q"
    awk -F'\t' -v map="$TMPD/map" '{
        n = split($7, r, "|"); for (k = 1; k <= n; k++) if (r[k] != "") { print r[k]; print NR "\tR" > map }
        n = split($8, u, "|"); for (k = 1; k <= n; k++) if (u[k] != "") { print u[k]; print NR "\tU" > map }
        if ($9 != "") { print $9; print NR "\tC" > map }
      }' "$TMPD/q" > "$TMPD/cmds"
    rbatch "$db" < "$TMPD/cmds" | tr -d '\r' > "$TMPD/res"
    # a cut-short answer (timeout) would read as empty queues: skip the database this time
    [ "$(wc -l < "$TMPD/res")" = "$(wc -l < "$TMPD/map")" ] || continue
    # Sidekiq workers: each process names its queues in "info"
    : > "$TMPD/sk"
    for ns in $(awk -F'\t' '$3 == "sidekiq" { print ($5 == "" ? "-" : $5) }' "$TMPD/q" | sort -u); do
      [ "$ns" = - ] && ns=""
      rcli -n "$db" smembers "${ns}processes" | head -100 | while IFS= read -r m; do
        rcli -n "$db" hget "$ns$m" info | grep -o '"queues":\[[^]]*\]' | head -1 \
          | sed 's/^"queues":\[//; s/\]$//; s/"//g' | tr ',' '\n' | sed "s#^#$ns\t#"
      done
    done | sort | uniq -c | awk '{ c = $1; sub(/^ *[0-9]+ /, ""); print $0 "\t" c }' > "$TMPD/sk"
    paste "$TMPD/map" "$TMPD/res" | awk -F'\t' -v qf="$TMPD/q" -v sk="$TMPD/sk" -v w="$W" '
      BEGIN { while ((getline l < qf) > 0) line[++n] = l
              while ((getline l < sk) > 0) { split(l, s, "\t"); work[s[1] "\t" s[2]] = s[3] } }
      { v = ($3 ~ /^[0-9]+$/) ? $3 + 0 : 0
        if ($2 == "R") r[$1] += v; else if ($2 == "U") u[$1] += v; else { c[$1] = v; hc[$1] = 1 } }
      END { for (i = 1; i <= n; i++) { split(line[i], f, "\t"); cs = (i in hc) ? c[i] : ""
              if (f[3] == "sidekiq") cs = ((f[5] "\t" f[6]) in work) ? work[f[5] "\t" f[6]] : 0
              printf "%s\t%s\t%s\t%s\t%s\t%d\t%d\t%s\n", w, f[3], f[4], f[5], f[6], r[i], u[i], cs } }' >> "$TMPD/rows"
  done
  PW=""
done < "$TMPD/inst"

# -- the report: brokers by instance, system, database and namespace, the deepest queues first --
json=$(sort -t$'\t' -k1,1 -k2,2 -k3,3n -k4,4 -k6,6nr "$TMPD/rows" | awk -F'\t' -v max=$MAX_Q '
  function e(s) { gsub(/[[:cntrl:]]/, "", s); gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); return s }
  function flush() { if (g == "") return
    b = b (nb++ ? "," : "") sprintf("{\"system\":\"%s\",\"where\":\"%s\",\"db\":%d,\"ns\":\"%s\",\"total\":%d,\"queues\":[%s]}", gs, e(gw), gd, e(gn), cnt, qs) }
  { k = $1 "\t" $2 "\t" $3 "\t" $4
    if (k != g) { flush(); g = k; gw = $1; gs = $2; gd = $3; gn = $4; cnt = 0; shown = 0; qs = "" }
    cnt++
    # every queue with messages (up to max), empty ones only to fill a short list: GitLab keeps
    # two hundred queue names of old versions, all empty
    if (($6 + $7 > 0 && shown < max) || shown < 20) qs = qs (shown++ ? "," : "") sprintf("{\"name\":\"%s\",\"ready\":%d%s%s}", e($5), $6,
      $7 > 0 ? ",\"unacked\":" $7 : "", $8 != "" ? ",\"consumers\":" $8 : "") }
  END { flush(); printf "%s", b }')
skip=$(sort -u "$TMPD/skip" | head -50 | while IFS= read -r w; do printf '{"where":"%s","why":"auth"},' "$(esc "$w")"; done | sed 's/,$//')
mkdir -p "${OUT%/*}"
printf '{"v":1,"ts":%s,"brokers":[%s],"skipped":[%s]}\n' "$NOW" "$json" "$skip" > "$OUT.tmp.$$"
chmod 0644 "$OUT.tmp.$$"
mv -f "$OUT.tmp.$$" "$OUT"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-queues.service <<EOF
[Unit]
Description=Kervax: depth of the background job queues in Redis (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
TimeoutStartSec=5min
EOF
cat > /etc/systemd/system/kervax-queues.timer <<'EOF'
[Unit]
Description=Kervax: read the job queues in Redis every minute
[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-queues.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-queues.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/queues-setup.ver"
chmod 0644 "$STATE_DIR/versions/queues-setup.ver"
echo "✓ queues-setup: job queues in Redis (Celery, Sidekiq, RQ, BullMQ, Laravel, Yii) -> $OUT (every minute)."
