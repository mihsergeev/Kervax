#!/usr/bin/env bash
# Kervax: background job queues kept in Redis and in database tables. RabbitMQ queues the agent
# reads by itself (the prometheus plugin needs no password); queues of Celery, Sidekiq, RQ, BullMQ,
# Laravel and Yii live in Redis as plain lists, and a Redis usually has a password and sits in a
# container or a pod.
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
# 0.2: under systemd PATH has no /bin, and an alpine Redis image keeps its shell only there:
# "nsenter: failed to execute sh", and such a Redis looked down for half an hour. The shell is
# called by its full path now, PATH is set explicitly, and a Redis that did not answer is tried
# again in 5 minutes.
#
# 0.3: queues kept as rows of a database table, in PostgreSQL and MySQL/MariaDB: Yii2 queue (db
# driver), Laravel (database driver), Symfony Messenger (doctrine transport), Delayed Job, Solid
# Queue, GoodJob, Que, Oban, River, pg-boss, Hangfire. Every 30 minutes the helper finds every
# postgres and mysqld process (docker, pods, host), a way in and the queue tables - by the table
# name and its columns - and checks each table answers; every minute one query per database
# counts the ready and the taken jobs of each queue. The way in: PostgreSQL over its socket as the
# server's own user (peer or trust, as the image sets it up), else over TCP with POSTGRES_PASSWORD
# from the container settings; MySQL with debian.cnf or /root/.my.cnf, else MYSQL_ROOT_PASSWORD
# (MARIADB_*, the app user's MYSQL_PASSWORD) from the container settings, else root on the socket.
# The client is the instance's own psql or mysql, run inside its namespaces. The session is read
# only, a statement stops after 5 seconds and a lock is waited for 2; a table with more than a
# million rows (finished jobs kept) is not counted every minute and is named in the panel.
#
# 0.4: how long the oldest ready job waits, and failed jobs. The oldest: by the enqueue time of the
# job at the head of the list in Sidekiq (enqueued_at), RQ (enqueued_at of the job hash), BullMQ
# (timestamp + delay) and Laravel under Horizon (pushedAt), by the minimum time a ready row became
# due in a table. Failed jobs every 5 minutes: Sidekiq's dead set, RQ's failed registry, BullMQ's
# failed set, Laravel's failed_jobs table, Symfony's failure transport, Delayed Job's failed_at,
# GoodJob, Solid Queue, Que, Oban, River and pg-boss - how many failed in the last hour and day,
# which job class failed most and the first line of the last error (values after password=,
# token= and the like hidden). Laravel and Symfony keep their times in the app's own time zone, so
# their failures are counted by row id: the id an hour and a day ago is kept in a history file.
# A backslash in a queue or class name (App\Jobs\Mail) made the report invalid JSON under mawk, the
# awk of Debian and Ubuntu (its gsub puts back one backslash, not two), and the agent dropped the
# whole block; mysql printed backslashes doubled in its batch output and now runs with --raw.
#
# Only queue names, numbers, job class names and the first line of the last error leave the node:
# no job arguments, no other keys or rows.
set -euo pipefail

KERVAX_SETUP_VERSION=0.4  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_WHEN="pgrep -x redis-server >/dev/null 2>&1 || pgrep -x valkey-server >/dev/null 2>&1 || pgrep -x postgres >/dev/null 2>&1 || pgrep -x postmaster >/dev/null 2>&1 || pgrep -x mysqld >/dev/null 2>&1 || pgrep -x mariadbd >/dev/null 2>&1"

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
# Background job queues in Redis and in database tables -> /var/lib/kervax/report.d/queues.json.
# Read only: in Redis PING, INFO, SCAN, TYPE, EXISTS, SMEMBERS, HGET, HMGET, LLEN, LINDEX, ZCARD,
# ZCOUNT, ZREVRANGE, ZREVRANGEBYSCORE, SCARD, ECHO; in a database the catalog and selects of the
# queue tables in a read-only session.
set -u
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
OUT=${KERVAX_QUEUES_OUT:-/var/lib/kervax/report.d/queues.json}
CACHE=${KERVAX_QUEUES_CACHE:-/var/lib/kervax/queues.cache}
TO=5            # seconds for one redis-cli call
SCAN_TO=60      # and for the SCAN of one database
FIND_EVERY=1800 # look for queues anew every 30 minutes
RETRY_DOWN=300  # and try a Redis that did not answer again in 5 minutes
MAX_Q=60        # queues per broker in the report, the deepest first
MAX_DB=16
MAX_KEYS=1000000  # a database with more keys is not scanned for queues
FAIL_EVERY=270    # failed jobs every 5 minutes (a run starts a few seconds late)
HIST=${KERVAX_QUEUES_HIST:-/var/lib/kervax/queues-hist}    # counters an hour and a day ago: "H key ts value"
FCACHE=${KERVAX_QUEUES_FCACHE:-/var/lib/kervax/queues-fail.cache}  # failed jobs of the last pass

exec 9>/run/kervax-queues.lock
flock -n 9 || exit 0
TMPD=$(mktemp -d /tmp/kervax-queues.XXXXXX) || exit 0
trap 'rm -rf "$TMPD"' EXIT
NOW=$(date +%s)
TAB=$'\t'
US=$'\037'  # joins the parts of a broker key in the failed jobs rows

# failed jobs are looked at every 5 minutes; in between the last pass is reported again
: > "$TMPD/frows"
: > "$TMPD/hnew"
FAILRUN=1
if [ -s "$FCACHE" ]; then
  fts=$(head -1 "$FCACHE" | cut -f2)
  if [ $((NOW - ${fts:-0})) -lt $FAIL_EVERY ] 2>/dev/null; then FAILRUN=0; tail -n +2 "$FCACHE" > "$TMPD/frows"; fi
fi
# a counter an hour (a day) ago: "value ts" of the last sample at least that old, else the oldest one
hist_at() { # key seconds
  awk -F'\t' -v k="$1" -v t=$((NOW - $2)) '$1 == "H" && $2 == k { if ($3 <= t) { v = $4; ts = $3 } else if (o == "") { o = $4; ots = $3 } }
    END { if (v != "") print v, ts; else if (o != "") print o, ots }' "$HIST" 2>/dev/null
}
# awk functions for job bodies: a JSON string or number field, an ISO time in UTC as epoch seconds,
# an error line with secrets hidden (values after password=, token= and the like), at most 300 characters
AWKFN='
function unq(r) { gsub(/\\\\/, "\001", r); gsub(/\\"/, "\"", r); gsub(/\\\//, "/", r); gsub(/\\[nrt]/, " ", r); gsub(/\001/, "\\\\", r); return r }
function js(s, k,   r) { if (!match(s, "\"" k "\": ?\"([^\"\\\\]|\\\\.)*\"")) return ""; r = substr(s, RSTART, RLENGTH); sub(/^"[^"]*": ?"/, "", r); return unq(substr(r, 1, length(r) - 1)) }
function sjs(s, k,   r) { if (!match(s, k "[\\\\]*\":[\\\\]*\"")) return ""; r = substr(s, RSTART + RLENGTH); sub(/\\*".*$/, "", r); return unq(r) }
function jn(s, k,   r) { if (!match(s, "\"" k "\": ?\"?[0-9][0-9.]*")) return ""; r = substr(s, RSTART, RLENGTH); sub(/^[^:]*: ?"?/, "", r); return r + 0 }
function ep(s,   y, m, d) {
  if (!match(s, /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]/)) return 0
  y = substr(s, 1, 4) + 0; m = substr(s, 6, 2) + 0; d = substr(s, 9, 2) + 0
  if (m <= 2) { y--; m += 12 }
  return (365 * y + int(y / 4) - int(y / 100) + int(y / 400) + int((153 * (m - 3) + 2) / 5) + d - 719469) * 86400 \
    + substr(s, 12, 2) * 3600 + substr(s, 15, 2) * 60 + substr(s, 18, 2) }
function mask(s,   out, st, ln, m) { s = substr(s, 1, 600); out = ""
  while (match(tolower(s), /(pass(word|wd)?|secret|token|api[_-]?key|authorization)["\047]?[ ]*[=:][ ]*[^ ]+/)) {
    st = RSTART; ln = RLENGTH; m = substr(s, st, ln); match(m, /[=:][ ]*/)
    out = out substr(s, 1, st - 1) substr(m, 1, RSTART + RLENGTH - 1) "***"; s = substr(s, st + ln) }
  s = out s; gsub(/[[:cntrl:]]/, " ", s); gsub(/^ +| +$/, "", s); return substr(s, 1, 300) }
'

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
  printf '%s\n' "$PW" | timeout "${T:-$TO}" nsenter -t "$P" -n -m /bin/sh -c \
    'read -r REDISCLI_AUTH; [ -n "$REDISCLI_AUTH" ] && export REDISCLI_AUTH; c=$1; shift; exec "$c" "$@"' \
    sh "$CLI" $TGT "$@" 2>/dev/null
}
# commands from stdin, one reply per line
rbatch() {
  { printf '%s\n' "$PW"; cat; } | timeout "$TO" nsenter -t "$P" -n -m /bin/sh -c \
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
# Q: inst, system, db, namespace, queue, ready commands, in-progress commands, workers command, the
# list whose head is the oldest job, the set of failed jobs, the prefix of a job hash (keys quoted by qk)
qline() { printf 'Q\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$INST" "$@" >> "$TMPD/cache"; }
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
    qline celery "$db" "" "$name" "$r" "" "" "" "" ""
  done
  # Sidekiq: [namespace]queues sets
  grep -a -E '(^|:)queues$' "$TMPD/keys" | grep -avx 'rq:queues' | head -10 | while IFS= read -r k; do
    [ "$(rcli -n "$db" type "$k")" = set ] || continue
    ns=${k%queues}
    rcli -n "$db" smembers "$k" | head -200 | while IFS= read -r m; do
      qline sidekiq "$db" "$ns" "$m" "LLEN $(qk "${ns}queue:$m")" "" "" "$(qk "${ns}queue:$m")" "$(qk "${ns}dead")" ""
    done
  done
  # RQ
  if grep -qax 'rq:queues' "$TMPD/keys"; then
    rcli -n "$db" smembers rq:queues | grep -a '^rq:queue:' | head -200 | while IFS= read -r k; do
      name=${k#rq:queue:}
      qline rq "$db" "" "$name" "LLEN $(qk "$k")" "ZCARD $(qk "rq:wip:$name")" "SCARD $(qk "rq:workers:$name")" \
        "$(qk "$k")" "$(qk "rq:registry:failed:$name")" "$(qk "rq:job:")"
    done
  fi
  # BullMQ (<prefix>:<q>:meta + :id) and Bull 3 (bull:<q>:id)
  { grep -a -E '^.+:[^:]+:meta$' "$TMPD/keys" | sed 's/:meta$//'
    grep -a -E '^bull:[^:]+:id$' "$TMPD/keys" | sed 's/:id$//'; } | sort -u | head -200 | while IFS= read -r base; do
    [ "$(rcli -n "$db" exists "$base:id")" = 1 ] || continue
    name=${base##*:}; ns=${base%"$name"}
    qline bullmq "$db" "$ns" "$name" "LLEN $(qk "$base:wait")|ZCARD $(qk "$base:prioritized")|LLEN $(qk "$base:paused")" \
      "LLEN $(qk "$base:active")" "" "$(qk "$base:wait")" "$(qk "$base:failed")" "$(qk "$base:")"
  done
  # Laravel: [prefix]queues:<q> list and its :reserved; a string under the same pattern is not it
  grep -a -E 'queues:[^:]+(:reserved|:delayed|:notify)?$' "$TMPD/keys" | sed -E 's/:(reserved|delayed|notify)$//' \
    | sort -u | head -200 | while IFS= read -r base; do
    t=$(rcli -n "$db" type "$base")
    [ "$t" = list ] || { [ "$t" = none ] && [ "$(rcli -n "$db" type "$base:reserved")" = zset ]; } || continue
    name=${base##*queues:}; ns=${base%"$name"}
    qline laravel "$db" "$ns" "$name" "LLEN $(qk "$base")" "ZCARD $(qk "$base:reserved")" "" "$(qk "$base")" "" ""
  done
  # Yii2 queue: <channel>.message_id
  grep -a -E '\.message_id$' "$TMPD/keys" | sed 's/\.message_id$//' | head -100 | while IFS= read -r base; do
    [ "$(rcli -n "$db" type "$base.message_id")" = string ] || continue
    qline yii "$db" "" "$base" "LLEN $(qk "$base.waiting")" "ZCARD $(qk "$base.reserved")" "" "" "" ""
  done
}

: > "$TMPD/cache"
: > "$TMPD/skip"
while IFS=$'\t' read -r P W TGT CLI SPEC; do
  INST="$W|$TGT"
  last=$(awk -F'\t' -v i="$INST" '$1 == "D" && $2 == i { print $3 "\t" $4; exit }' "$CACHE" 2>/dev/null)
  when=${last%%$'\t'*}; status=${last#*$'\t'}
  every=$FIND_EVERY; [ "$status" = down ] && every=$RETRY_DOWN
  # a cache line of 0.3 has no fields for the oldest job and failed jobs: such queues are looked for anew
  if [ -n "$last" ] && [ $((NOW - ${when:-0})) -lt $every ] \
     && ! awk -F'\t' -v i="$INST" '$1 == "Q" && $2 == i && NF < 12 { f = 1 } END { exit !f }' "$CACHE"; then
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
    # the oldest job of each queue and, every 5 minutes, failed jobs: commands between ECHO markers,
    # a reply takes one line, several (job bodies) or none. Phase two asks the job hashes by the ids
    # the first one found (RQ and BullMQ keep ids in their lists)
    awk -F'\t' -v now="$NOW" -v fr="$FAILRUN" '
      function mk(n, t) { print "ECHO @@" n ":" t "@@" }
      $10 != "" { mk(NR, "o"); print "LINDEX " $10 (($3 == "sidekiq" || $3 == "bullmq") ? " -1" : " 0") }
      fr == 1 && $11 != "" {
        if ($3 == "sidekiq") { if (!($11 in seen)) { seen[$11] = 1
            mk(NR, "f1"); print "ZCOUNT " $11 " " (now - 3600) " +inf"
            mk(NR, "f24"); print "ZCOUNT " $11 " " (now - 86400) " +inf"
            mk(NR, "fl"); print "ZREVRANGEBYSCORE " $11 " +inf " (now - 3600) " LIMIT 0 50" } }
        else if ($3 == "bullmq") {
            mk(NR, "f1"); print "ZCOUNT " $11 " " (now - 3600) "000 +inf"
            mk(NR, "f24"); print "ZCOUNT " $11 " " (now - 86400) "000 +inf"
            mk(NR, "fl"); print "ZREVRANGE " $11 " 0 0" }
        else if ($3 == "rq") { mk(NR, "fc"); print "ZCARD " $11 } }
      END { mk(0, "end") }' "$TMPD/q" > "$TMPD/xcmd"
    : > "$TMPD/xa"
    if [ "$(wc -l < "$TMPD/xcmd")" -gt 1 ]; then
      rbatch "$db" < "$TMPD/xcmd" | tr -d '\r' > "$TMPD/xres"
      if [ "$(tail -1 "$TMPD/xres")" = "@@0:end@@" ]; then
        awk -F'\t' -v qf="$TMPD/q" "$AWKFN"'
          BEGIN { while ((getline l < qf) > 0) { split(l, f, "\t"); sy[++m] = f[3]; qn[m] = f[6] } }
          /^@@[0-9]+:[a-z0-9]+@@$/ { split(substr($0, 3, length($0) - 4), a, ":"); n = a[1]; t = a[2]; k = 0; next }
          { k++ }
          t == "o" && k == 1 {
            if (sy[n] == "sidekiq") { v = jn($0, "enqueued_at"); if (v > 100000000000) v = v / 1000 }  # Sidekiq 8 keeps milliseconds
            else if (sy[n] == "laravel") { v = jn($0, "pushedAt"); if (v == "") v = jn($0, "createdAt") }
            else { if ($0 ~ /^[A-Za-z0-9:_.-]+$/) print n "\toid\t" $0; next }
            if (v + 0 > 0) printf "%s\to\t%d\n", n, int(v); next }
          (t == "f1" || t == "f24" || t == "fc") && k == 1 { if ($0 ~ /^[0-9]+$/) print n "\t" t "\t" $0; next }
          t == "fl" && sy[n] == "bullmq" && k == 1 { if ($0 ~ /^[A-Za-z0-9:_.-]+$/) print n "\tfid\t" $0; next }
          t == "fl" && sy[n] == "sidekiq" {
            c = js($0, "wrapped"); if (c == "") c = js($0, "class"); q = js($0, "queue")
            if (c == "" && q == "") next
            print n "\tfj\t" q "\t" c
            if (!(n in fe)) { fe[n] = 1; e = js($0, "error_class"); m2 = js($0, "error_message")
              print n "\tfe\t" q "\t" c "\t" e (e != "" && m2 != "" ? ": " : "") m2 } }' "$TMPD/xres" > "$TMPD/xa"
        awk -F'\t' -v qf="$TMPD/q" '
          BEGIN { while ((getline l < qf) > 0) { split(l, f, "\t"); sy[++m] = f[3]; hp[m] = substr(f[12], 1, length(f[12]) - 1) } }
          function mk(n, t) { print "ECHO @@" n ":" t "@@" }
          $2 == "oid" && hp[$1] != "" { mk($1, "ob"); print (sy[$1] == "rq" ? "HGET " hp[$1] $3 "\" enqueued_at" : "HMGET " hp[$1] $3 "\" timestamp delay") }
          $2 == "fid" && hp[$1] != "" { mk($1, "fb"); print "HMGET " hp[$1] $3 "\" name failedReason" }
          END { mk(0, "end") }' "$TMPD/xa" > "$TMPD/xcmd"
        if [ "$(wc -l < "$TMPD/xcmd")" -gt 1 ]; then
          rbatch "$db" < "$TMPD/xcmd" | tr -d '\r' > "$TMPD/xres"
          [ "$(tail -1 "$TMPD/xres")" = "@@0:end@@" ] && awk -F'\t' -v qf="$TMPD/q" "$AWKFN"'
            BEGIN { while ((getline l < qf) > 0) { split(l, f, "\t"); sy[++m] = f[3]; qn[m] = f[6] } }
            /^@@[0-9]+:[a-z0-9]+@@$/ { split(substr($0, 3, length($0) - 4), a, ":"); n = a[1]; t = a[2]; k = 0; next }
            { k++; v[k] = $0 }
            t == "ob" && sy[n] == "rq" && k == 1 { e = ep($0); if (e > 0) printf "%s\to\t%d\n", n, e }
            t == "ob" && sy[n] == "bullmq" && k == 2 { if (v[1] ~ /^[0-9]+$/) printf "%s\to\t%d\n", n, int((v[1] + v[2]) / 1000) }
            t == "fb" && k == 2 { print n "\tfe\t" qn[n] "\t" v[1] "\t" $0 }' "$TMPD/xres" >> "$TMPD/xa"
        fi
      fi
    fi
    # failed jobs of the brokers in this database (every 5 minutes): Sidekiq - its dead set, BullMQ -
    # failed sets of the queues, RQ - failed registries by how they grew against the history
    if [ "$FAILRUN" = 1 ]; then
      awk -F'\t' -v qf="$TMPD/q" -v w="$W" -v now="$NOW" -v hf="$HIST" -v hn="$TMPD/hnew" -v us="$US" '
        BEGIN { OFS = "\t"
          while ((getline l < qf) > 0) { split(l, f, "\t"); m++; sy[m] = f[3]; db[m] = f[4]; ns[m] = f[5]; qn[m] = f[6]; inst = f[2] }
          while ((getline l < hf) > 0) { split(l, hx, "\t"); if (hx[1] != "H") continue; k = hx[2]
            if (hx[3] + 0 <= now - 3600) { v1[k] = hx[4] } if (hx[3] + 0 <= now - 86400) { v24[k] = hx[4]; t24[k] = hx[3] }
            if (!(k in o)) { o[k] = hx[4]; ot[k] = hx[3] } } }
        { n = $1; bk = w us sy[n] us db[n] us ns[n] }
        $2 == "f1" { f1[bk] += $3; hb[bk] = 1; if (sy[n] == "bullmq" && $3 > 0) { qj[bk] = qj[bk] "J" OFS bk OFS qn[n] OFS "" OFS $3 "\n"; if ($3 > bf[bk]) { bf[bk] = $3; bq[bk] = n } } }
        $2 == "f24" { f24[bk] += $3 }
        $2 == "fj" { cnt[bk OFS $3 OFS $4]++ }
        $2 == "fe" { if (sy[n] == "sidekiq" || (bq[bk] == n)) er[bk] = "E" OFS bk OFS $3 OFS $4 OFS $5 }
        $2 == "fc" { k = "rq|" inst "|" db[n] "|" qn[n]; print "H", k, now, $3 >> hn
          if (!(k in o)) next
          a = (k in v1) ? v1[k] : o[k]; b = (k in v24) ? v24[k] : o[k]; sp = now - ((k in t24) ? t24[k] : ot[k])
          h = $3 - a; d = $3 - b; if (h < 0) h = 0; if (d < 0) d = 0
          f1[bk] += h; f24[bk] += d; hb[bk] = 1; if (!(bk in span) || sp < span[bk]) span[bk] = sp
          if (h > 0) qj[bk] = qj[bk] "J" OFS bk OFS qn[n] OFS "" OFS h "\n" }
        END { for (bk in hb) { print "T", bk, f1[bk], f24[bk], (bk in span) ? (span[bk] > 86400 ? 86400 : span[bk]) : 86400
                printf "%s", qj[bk]; if (bk in er) print er[bk] }
              for (k in cnt) { split(k, c, OFS); print "J", c[1], c[2], c[3], cnt[k] } }' "$TMPD/xa" >> "$TMPD/frows"
    fi
    paste "$TMPD/map" "$TMPD/res" | awk -F'\t' -v qf="$TMPD/q" -v sk="$TMPD/sk" -v xa="$TMPD/xa" -v w="$W" '
      BEGIN { while ((getline l < qf) > 0) line[++n] = l
              while ((getline l < sk) > 0) { split(l, s, "\t"); work[s[1] "\t" s[2]] = s[3] }
              while ((getline l < xa) > 0) { split(l, s, "\t"); if (s[2] == "o") old[s[1]] = s[3] } }
      { v = ($3 ~ /^[0-9]+$/) ? $3 + 0 : 0
        if ($2 == "R") r[$1] += v; else if ($2 == "U") u[$1] += v; else { c[$1] = v; hc[$1] = 1 } }
      END { for (i = 1; i <= n; i++) { split(line[i], f, "\t"); cs = (i in hc) ? c[i] : ""
              if (f[3] == "sidekiq") cs = ((f[5] "\t" f[6]) in work) ? work[f[5] "\t" f[6]] : 0
              # the oldest job only of a queue that has ready ones: an empty list keeps nothing
              printf "%s\t%s\t%s\t%s\t%s\t%d\t%d\t%s\t%s\n", w, f[3], f[4], f[5], f[6], r[i], u[i], cs, (r[i] > 0 && (i in old)) ? old[i] : "" } }' >> "$TMPD/rows"
  done
  PW=""
done < "$TMPD/inst"

# -- job queues kept in database tables: PostgreSQL and MySQL/MariaDB --
DBCACHE=${KERVAX_QUEUES_DBCACHE:-/var/lib/kervax/queues-db.cache}
DB_TO=15          # seconds for one client call
MAX_ROWS=${KERVAX_QUEUES_MAXROWS:-1000000}  # a queue table with more rows (finished jobs kept) is not counted every minute
MAX_DBS=60
IPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/bitnami/postgresql/bin:/opt/bitnami/mysql/bin:/opt/bitnami/mariadb/bin
QT_RE='(^|_)(queue|jobs|messenger_messages|delayed_jobs|good_jobs|que_jobs|oban_jobs|river_job|solid_queue_ready_executions|solid_queue_claimed_executions|solid_queue_jobs|jobqueue)$'
QT_COLS="'channel','job','pushed_at','ttr','delay','reserved_at','done_at','queue','payload','attempts','available_at','created_at','body','headers','queue_name','delivered_at','handler','run_at','locked_at','failed_at','scheduled_at','performed_at','finished_at','expired_at','state','name','start_after','startafter','worker','kind','jobid','fetchedat','job_id','connection','exception','error','serialized_params','last_error','last_error_message','errors','discarded_at','finalized_at','completed_on','completedon','output'"
PG_FIND="select n.nspname, c.relname, greatest(c.reltuples, 0)::bigint, string_agg(lower(a.attname::text), ',' order by a.attname::text)
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
  join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
  where c.relkind in ('r', 'p') and n.nspname not in ('pg_catalog', 'information_schema') and n.nspname !~ '^pg_(toast|temp)'
    and (c.relname ~* '$QT_RE' or (c.relname = 'job' and n.nspname ~* 'pgboss'))
    and lower(a.attname::text) in ($QT_COLS)
  group by 1, 2, 3"
MY_FIND="select t.table_schema, t.table_name, ifnull(t.table_rows, 0), group_concat(lower(c.column_name) order by c.column_name)
  from information_schema.tables t join information_schema.columns c on c.table_schema = t.table_schema and c.table_name = t.table_name
  where t.table_schema not in ('mysql', 'information_schema', 'performance_schema', 'sys') and t.table_type = 'BASE TABLE'
    and lower(t.table_name) regexp '$QT_RE' and lower(c.column_name) in ($QT_COLS)
  group by 1, 2, 3"
# MySQL reads the statements one by one and goes on past an error (--force): of the limits each
# server knows its own, max_execution_time is MySQL's, max_statement_time MariaDB's
MY_PRE="set session transaction read only; set session max_execution_time = 5000; set session max_statement_time = 5; set session lock_wait_timeout = 2;"

secret_of() { # pid spec var -> its value in the container's environment, or the file named by var_FILE
  local v f
  v=$(spec_env "$2" "$3")
  if [ -z "$v" ]; then
    f=$(spec_env "$2" "${3}_FILE")
    [ -n "$f" ] && v=$(head -1 "/proc/$1/root$f" 2>/dev/null | tr -d '\r\n')
  fi
  # config.json is written by Go: & < > come as \u0026 \u003c \u003e
  printf '%s' "$v" | sed 's/\\u0026/\&/g; s/\\u003c/</g; s/\\u003e/>/g; s/\\\\/\\/g'
}
cli_in() { # pid candidates... -> the first client that exists inside the process' root
  local p=$1 c; shift
  for c in "$@"; do [ -x "/proc/$p/root$c" ] && { printf '%s' "$c"; return 0; }; done
  return 1
}
socks_of() { # pid -> "T <port>" and "U <path>" of its listening sockets
  local p=$1 ino
  ino=" $(ls -l "/proc/$p/fd" 2>/dev/null | sed -n 's/.*socket:\[\([0-9]*\)\].*/\1/p' | sort -u | tr '\n' ' ')"
  [ "$ino" = " " ] && return 0
  awk -v ino="$ino" 'FNR == 1 { next }
    FILENAME ~ /unix$/ { if ($6 == "01" && $8 != "" && index(ino, " " $7 " ")) print "U " $8; next }
    $4 == "0A" && index(ino, " " $10 " ") { split($2, a, ":"); port = 0
      for (i = 1; i <= length(a[2]); i++) port = port * 16 + index("0123456789ABCDEF", substr(a[2], i, 1)) - 1
      print "T " port }' "/proc/$p/net/tcp" "/proc/$p/net/tcp6" "/proc/$p/net/unix" 2>/dev/null | sort -u
}
uid_of() { awk -v k="$2:" '$1 == k { print $2 }' "/proc/$1/status" 2>/dev/null; }

# The instance's own client inside its mount and network namespaces; M_* is the way in: client,
# host (a socket directory or path, or an address), port, user, the container variable with the
# password, MySQL's defaults file. psql runs as the server's own user: peer authentication on the
# socket lets exactly that user in. The password goes in the environment, never on a command line.
pgrun() { # db sql
  local pw=""
  [ -n "$M_PW" ] && pw=$(secret_of "$P" "$SPEC" "$M_PW")
  timeout "$DB_TO" nsenter -t "$P" -m -n -S "$(uid_of "$P" Uid)" -G "$(uid_of "$P" Gid)" -- /usr/bin/env -i \
    PATH="$IPATH" HOME=/tmp PGPASSWORD="$pw" PGCONNECT_TIMEOUT=5 PGAPPNAME=kervax-queues \
    PGOPTIONS="-c statement_timeout=5000 -c lock_timeout=2000 -c default_transaction_read_only=on" \
    "$M_CLI" -X -A -t -q -F "$TAB" -v ON_ERROR_STOP=1 -h "$M_HOST" -p "$M_PORT" -U "$M_USER" -d "$1" -c "$2" 2>"$TMPD/dberr"
}
myrun() { # sql
  local pw="" envs=(PATH="$IPATH" HOME=/root) args=()
  [ -n "$M_PW" ] && pw=$(secret_of "$P" "$SPEC" "$M_PW")
  [ -n "$pw" ] && envs+=(MYSQL_PWD="$pw")
  [ -n "$M_DEF" ] && args+=(--defaults-file="$M_DEF")  # has to be the first option
  args+=(-N -B -r --force --connect-timeout=5)
  [ -n "$M_USER" ] && args+=(-u "$M_USER")
  case "$M_HOST" in /*) args+=(-S "$M_HOST") ;; ?*) args+=(-h "$M_HOST" -P "$M_PORT") ;; esac
  # on stdin: with -e the client stops at the first error even with --force
  printf '%s %s;\n' "$MY_PRE" "$1" | timeout "$DB_TO" nsenter -t "$P" -m -n -- /usr/bin/env -i "${envs[@]}" "$M_CLI" "${args[@]}" 2>"$TMPD/dberr"
}
dbrun() { if [ "$ENG" = pg ]; then pgrun "$@"; else shift; myrun "$@"; fi; }  # db sql
login_err() { grep -qiE 'authentication|password|access denied|pg_hba|role .* does not exist|not permitted' "$TMPD/dberr" 2>/dev/null; }

# A way in: sets M_*; 0 - in, 1 - refused (auth), 2 - did not answer
pg_login() {
  local u d pair pv refused=""
  M_CLI=$(cli_in "$P" "${EXE%/*}/psql" /usr/bin/psql /usr/local/bin/psql /opt/bitnami/postgresql/bin/psql \
          /usr/local/pgsql/bin/psql) || return 1
  M_PW="" M_DEF=""
  if [ -n "$HOST" ] && [ "$HOST" != 127.0.0.1 ]; then
    M_HOST=$HOST M_PORT=$PORT
    # the image's superuser, postgres, and the server's own system user: peer authentication maps
    # it to the role of the same name (GitLab: gitlab-psql)
    for u in $(spec_env "$SPEC" POSTGRES_USER) postgres \
             $(awk -F: -v u="$(uid_of "$P" Uid)" '$3 == u && $1 != "postgres" { print $1; exit }' "/proc/$P/root/etc/passwd" 2>/dev/null); do
      for d in postgres template1 $(spec_env "$SPEC" POSTGRES_DB); do
        M_USER=$u M_DB=$d
        [ "$(pgrun "$d" "select 1" | head -1)" = 1 ] && return 0
        login_err && refused=1
      done
    done
  fi
  [ -n "$TPORT" ] || { [ -n "$refused" ] && return 1; return 2; }
  M_HOST=127.0.0.1 M_PORT=$TPORT
  for pair in POSTGRES_USER:POSTGRES_PASSWORD postgres:POSTGRES_PASSWORD postgres:POSTGRESQL_POSTGRES_PASSWORD \
              POSTGRESQL_USERNAME:POSTGRESQL_PASSWORD; do
    u=${pair%%:*} pv=${pair#*:}
    case "$u" in [A-Z]*) u=$(spec_env "$SPEC" "$u") ;; esac
    [ -n "$u" ] && [ -n "$(secret_of "$P" "$SPEC" "$pv")" ] || continue
    M_USER=$u M_PW=$pv
    for d in postgres template1 $(spec_env "$SPEC" POSTGRES_DB) $(spec_env "$SPEC" POSTGRESQL_DATABASE); do
      M_DB=$d
      [ "$(pgrun "$d" "select 1" | head -1)" = 1 ] && return 0
      login_err && refused=1
    done
  done
  [ -n "$refused" ] && return 1
  return 2
}
my_login() {
  local f pair u pv refused=""
  M_CLI=$(cli_in "$P" /usr/bin/mysql /usr/bin/mariadb /usr/local/bin/mysql /usr/local/mysql/bin/mysql \
          /opt/bitnami/mysql/bin/mysql /opt/bitnami/mariadb/bin/mariadb /opt/bitnami/mariadb/bin/mysql) || return 1
  M_HOST=${HOST:-127.0.0.1} M_PORT=${TPORT:-3306} M_DB=-
  for f in /etc/mysql/debian.cnf /root/.my.cnf; do
    [ -r "/proc/$P/root$f" ] || continue
    M_DEF=$f M_USER="" M_PW=""
    [ "$(myrun "select 1" | tail -1)" = 1 ] && return 0
    login_err && refused=1
  done
  M_DEF=""
  for pair in root:MYSQL_ROOT_PASSWORD root:MARIADB_ROOT_PASSWORD MYSQL_USER:MYSQL_PASSWORD MARIADB_USER:MARIADB_PASSWORD \
              MARIADB_USER:MYSQL_PASSWORD root:; do
    u=${pair%%:*} pv=${pair#*:}
    case "$u" in [A-Z]*) u=$(spec_env "$SPEC" "$u") ;; esac
    [ -n "$u" ] || continue
    [ -z "$pv" ] || [ -n "$(secret_of "$P" "$SPEC" "$pv")" ] || continue
    M_USER=$u M_PW=$pv
    [ "$(myrun "select 1" | tail -1)" = 1 ] && return 0
    login_err && refused=1
  done
  [ -n "$refused" ] && return 1
  return 2
}

# A time column as epoch seconds: PostgreSQL's timestamp without a zone is taken as UTC (Rails, Oban,
# Symfony and Hangfire keep UTC there), MySQL's datetime too
epsql() { if [ "$ENG" = pg ]; then printf 'extract(epoch from %s)::bigint' "$1"; else printf "timestampdiff(second, '1970-01-01', %s)" "$1"; fi; }
QEND="select 'END', '', 0, 0, 0"

# The count of one queue table: rows "<idx>  <queue>  <ready>  <taken>  <oldest>" - the oldest is
# when the longest waiting ready job became due, epoch seconds; delayed jobs (their time has not
# come) and finished or failed ones are not counted
qsql() { # idx schema table system variant
  local i=$1 t tq q w r x u tx oc oi="" o
  if [ "$ENG" = pg ]; then t="\"$2\".\"$3\""; u="(now() at time zone 'utc')"; tx="::text"
  else t="\`$2\`.\`$3\`"; u="utc_timestamp()"; tx=""; fi
  case "$4" in
    yii) q=channel w="done_at is null" r="reserved_at is null and pushed_at + coalesce(delay, 0) <= $NOW" x="reserved_at is not null"
      oc="pushed_at + coalesce(delay, 0)" oi=1 ;;
    laravel) q=queue w="1 = 1" r="reserved_at is null and available_at <= $NOW" x="reserved_at is not null" oc=available_at oi=1 ;;
    symfony) q=queue_name w="1 = 1" r="delivered_at is null and available_at <= $u" x="delivered_at is not null" oc=available_at ;;
    delayed_job) q=queue; [ "$5" = q ] || q=null
      w="failed_at is null" r="locked_at is null and run_at <= $u" x="locked_at is not null" oc=run_at ;;
    good_job) q=queue_name w="finished_at is null" r="performed_at is null and (scheduled_at is null or scheduled_at <= $u)" x="performed_at is not null" oc=scheduled_at ;;
    que) q=queue w="finished_at is null and expired_at is null" r="run_at <= now()" x="1 = 0" oc=run_at ;;
    oban) q=queue w="state in ('available', 'executing')" r="state = 'available'" x="state = 'executing'" oc=scheduled_at ;;
    river) q=queue w="state in ('available', 'running')" r="state = 'available'" x="state = 'running'" oc=scheduled_at ;;
    pgboss) q=name w="state in ('created', 'retry', 'active')" r="state in ('created', 'retry') and $5 <= now()" x="state = 'active'" oc=$5 ;;
    # Hangfire's queue table keeps no time
    hangfire) q=queue w="1 = 1" r="fetchedat is null" x="fetchedat is not null" oc="" ;;
    solid_queue)
      if [ "$ENG" = pg ]; then tq="\"$2\".\"${5}"; else tq="\`$2\`.\`${5}"; fi
      printf "select '%s'%s, coalesce(queue_name%s, 'default'), count(*), 0, %s from %s group by 2 union all select '%s'%s, coalesce(j.queue_name%s, 'default'), 0, count(*), null from %sclaimed_executions%s c join %sjobs%s j on j.id = c.job_id group by 2" \
        "$i" "$tx" "$tx" "$(epsql "min(created_at)")" "$t" "$i" "$tx" "$tx" "$tq" "${tq:0:1}" "$tq" "${tq:0:1}"
      return ;;
    *) return 1 ;;
  esac
  if [ -z "$oc" ]; then o="null"
  elif [ -n "$oi" ]; then o="min(case when $r then $oc end)"
  else o=$(epsql "min(case when $r then $oc end)"); fi
  printf "select '%s'%s, coalesce(%s%s, 'default'), sum(case when %s then 1 else 0 end), sum(case when %s then 1 else 0 end), %s from %s where %s group by 2" \
    "$i" "$tx" "$q" "$tx" "$r" "$x" "$o" "$t" "$w"
}

# Failed jobs of one table, a union of selects with rows "<idx> <kind> <queue> <n1> <n2> <class> <error>":
# n - failed in the last hour (n1) and day (n2), s - the class of each of the last hundred failed in
# the hour, e - the class and the first line of the error of the last one, m - the highest row id
# (Laravel and Symfony: their times are in the app's own zone, so an hour is counted by row id; the
# ids an hour and a day ago, FID1 and FID24, come from the history). Nothing - the system keeps no
# failed jobs (Yii, Laravel's jobs table, Hangfire)
fsql() { # idx schema table system variant failvariant
  local i=$1 t f q c e o c1 c24 u1 u24 n1 n24 tq sn fl tx
  if [ "$ENG" = pg ]; then
    t="\"$2\".\"$3\""; u1="((now() at time zone 'utc') - interval '1 hour')"; u24="((now() at time zone 'utc') - interval '1 day')"
    n1="(now() - interval '1 hour')"; n24="(now() - interval '1 day')"
  else
    t="\`$2\`.\`$3\`"; u1="(utc_timestamp() - interval 1 hour)"; u24="(utc_timestamp() - interval 1 day)"; n1=$u1; n24=$u24
  fi
  f=$t
  case "$4" in
    laravel_failed|symfony)
      [ -n "$FID1" ] || { printf '%s' "(select '$i', 'm', '', max(id), 0, '', '' from $t)"; return 0; }
      o=id c1="id > $FID1" c24="id > $FID24"
      if [ "$4" = laravel_failed ]; then q=queue c=payload e=exception
      else
        q=queue_name c=headers c1="$c1 and queue_name like '%fail%'" c24="$c24 and queue_name like '%fail%'"
        # the error is in a stamp in the headers (the Symfony serializer), further than a first line
        if [ "$ENG" = pg ]; then e="substr(headers, greatest(strpos(headers, 'exceptionClass'), 1), 1500)"
        else e="substr(headers, greatest(locate('exceptionClass', headers), 1), 1500)"; fi
      fi ;;
    delayed_job) q=queue; [ "$5" = q ] || q=null
      c=handler e=last_error o=failed_at c1="failed_at > $u1" c24="failed_at > $u24" ;;
    good_job) [ "$6" = y ] || return 1
      q=queue_name c=serialized_params e=error o=finished_at c1="error is not null and finished_at > $u1" c24="error is not null and finished_at > $u24" ;;
    que) q=queue c=job_class o=expired_at c1="expired_at > $n1" c24="expired_at > $n24"
      if [ "$6" = m ]; then e=last_error_message; else e=last_error; fi ;;
    oban) [ "$6" = y ] || return 1
      q=queue c=worker e="errors[array_length(errors, 1)]->>'error'" o=discarded_at
      c1="state = 'discarded' and discarded_at > $u1" c24="state = 'discarded' and discarded_at > $u24" ;;
    river) [ "$6" = y ] || return 1
      q=queue c=kind e="errors[array_length(errors, 1)]->>'error'" o=finalized_at
      c1="state = 'discarded' and finalized_at > $n1" c24="state = 'discarded' and finalized_at > $n24" ;;
    pgboss) [ "${6%%:*}" != - ] && [ -n "$6" ] || return 1
      o=${6%%:*} q=name c=name e="''"; [ "${6#*:}" = o ] && e="output->>'message'"
      c1="state = 'failed' and $o > $n1" c24="state = 'failed' and $o > $n24" ;;
    solid_queue)
      if [ "$ENG" = pg ]; then tq="\"$2\".\"${5}"; else tq="\`$2\`.\`${5}"; fi
      f="${tq}failed_executions${tq:0:1} f join ${tq}jobs${tq:0:1} j on j.id = f.job_id"
      q=j.queue_name c=j.class_name e=f.error o=f.created_at c1="f.created_at > $u1" c24="f.created_at > $u24" ;;
    *) return 1 ;;
  esac
  # text without tabs and line breaks; the error - its first line
  if [ "$ENG" = pg ]; then
    tx="coalesce(($q)::text, '')"
    sn="coalesce(translate(substr(($c)::text, 1, 400), chr(9) || chr(13) || chr(10), '   '), '')"
    fl="coalesce(translate(split_part(substr(($e)::text, 1, 2000), chr(10), 1), chr(9) || chr(13), '  '), '')"
  else
    tx="coalesce($q, '')"
    sn="coalesce(replace(replace(replace(substr($c, 1, 400), char(9), ' '), char(13), ' '), char(10), ' '), '')"
    fl="coalesce(replace(replace(substring_index(substr($e, 1, 2000), char(10), 1), char(9), ' '), char(13), ' '), '')"
  fi
  printf '%s' "(select '$i', 'n', '', sum(case when $c1 then 1 else 0 end), count(*), '', '' from $f where $c24)"
  printf '%s' " union all (select '$i', 's', $tx, 0, 0, $sn, '' from $f where $c1 order by $o desc limit 100)"
  printf '%s' " union all (select '$i', 'e', $tx, 0, 0, $sn, $fl from $f where $c1 order by $o desc limit 1)"
  case "$4" in laravel_failed|symfony) printf '%s' " union all (select '$i', 'm', '', max(id), 0, '', '' from $t)" ;; esac
  return 0
}

# which queue system a table is, by its name and columns: "schema table rows cols" -> "... system
# variant failvariant"; the last tells how the system keeps its failed jobs ("-" - it does not)
qclassify() {
  awk 'BEGIN { FS = OFS = "\t" }
    $2 !~ /^[A-Za-z0-9_$]+$/ || $1 !~ /^[A-Za-z0-9_$-]+$/ { next }
    { delete c; n = split($4, a, ","); for (i = 1; i <= n; i++) c[a[i]] = 1; t = tolower($2); sy = ""; v = "-"; fv = "-"
      if (c["connection"] && c["queue"] && c["payload"] && c["exception"] && c["failed_at"]) { sy = "laravel_failed"; fv = "y" }
      else if (c["channel"] && c["job"] && c["pushed_at"] && c["ttr"] && c["delay"] && c["reserved_at"] && c["done_at"]) sy = "yii"
      else if (c["queue"] && c["payload"] && c["attempts"] && c["reserved_at"] && c["available_at"] && c["created_at"]) sy = "laravel"
      else if (c["body"] && c["headers"] && c["queue_name"] && c["available_at"] && c["delivered_at"]) { sy = "symfony"; fv = "y" }
      else if (c["handler"] && c["run_at"] && c["locked_at"] && c["failed_at"] && c["attempts"]) {
        sy = "delayed_job"; if (c["queue"]) v = "q"; if (c["last_error"]) fv = "y" }
      else if (t ~ /good_jobs$/ && c["queue_name"] && c["scheduled_at"] && c["performed_at"] && c["finished_at"]) {
        sy = "good_job"; if (c["error"] && c["serialized_params"]) fv = "y" }
      else if (t ~ /que_jobs$/ && c["queue"] && c["run_at"] && c["finished_at"] && c["expired_at"]) {
        sy = "que"; fv = c["last_error_message"] ? "m" : c["last_error"] ? "e" : "-" }
      else if (t ~ /oban_jobs$/ && c["state"] && c["queue"] && c["worker"] && c["scheduled_at"]) { sy = "oban"; if (c["errors"] && c["discarded_at"]) fv = "y" }
      else if (t ~ /river_job$/ && c["state"] && c["queue"] && c["kind"] && c["scheduled_at"]) { sy = "river"; if (c["errors"] && c["finalized_at"]) fv = "y" }
      else if (t == "job" && c["name"] && c["state"] && (c["start_after"] || c["startafter"])) {
        sy = "pgboss"; v = c["start_after"] ? "start_after" : "startafter"
        fv = (c["completed_on"] ? "completed_on" : c["completedon"] ? "completedon" : "-") ":" (c["output"] ? "o" : "-") }
      else if (t ~ /jobqueue$/ && c["jobid"] && c["queue"] && c["fetchedat"]) sy = "hangfire"
      else if (t ~ /solid_queue_ready_executions$/ && c["queue_name"] && c["job_id"]) { sy = "solid_queue"; v = substr($2, 1, length($2) - 16); fv = "y" }
      if (sy != "") print $1, $2, $3, sy, v, fv }'
}

# -- database instances: by process (the postmaster, not its backends), by network namespace and port --
# An instance is described once per process: its sockets, container and settings stay while the
# process lives (the key is the pid and its start time), and forty of them took four seconds
: > "$TMPD/dbinst"
: > "$TMPD/dbpid"
declare -A DBSEEN DBPID
[ -r "$DBCACHE" ] && while IFS=$'\t' read -r t k line; do [ "$t" = P ] && DBPID[$k]=$line; done < "$DBCACHE"
# one ps for all: a node with forty PostgreSQL has five hundred backends, and a few commands per
# process took eight seconds
for P in $(ps -e -o pid=,ppid=,comm= 2>/dev/null | awk '{ c[$1] = $3; pp[$1] = $2 }
             END { for (p in c) if (c[p] ~ /^(postgres|postmaster|mysqld|mariadbd)$/ && c[pp[p]] !~ /^(postgres|postmaster)$/) print p }'); do
  case "$(cat "/proc/$P/comm" 2>/dev/null)" in
    postgres|postmaster) ENG=pg ;;
    mysqld|mariadbd) ENG=mysql ;;
    *) continue ;;
  esac
  st=(); read -r -a st 2>/dev/null < "/proc/$P/stat" || continue
  k="$P:${st[21]:-}"
  if [ -n "${DBPID[$k]:-}" ]; then
    printf 'P\t%s\t%s\n' "$k" "${DBPID[$k]}" >> "$TMPD/dbpid"
    [ "${DBPID[$k]}" = - ] || printf '%s\n' "${DBPID[$k]}" >> "$TMPD/dbinst"
    continue
  fi
  socks=$(socks_of "$P")
  TPORT=$(printf '%s\n' "$socks" | sed -n 's/^T //p' | grep -vx 33060 | head -1)
  if [ "$ENG" = pg ]; then
    s=$(printf '%s\n' "$socks" | sed -n 's#^U \(/.*\)/\.s\.PGSQL\.\([0-9][0-9]*\)$#\2 \1#p' | head -1)
    if [ -n "$s" ]; then PORT=${s%% *} HOST=${s#* }; else PORT=$TPORT HOST=127.0.0.1; fi
  else
    HOST=$(printf '%s\n' "$socks" | sed -n 's#^U \(/.*\)$#\1#p' | grep -v mysqlx | head -1)
    PORT=${TPORT:-sock}
  fi
  key="$(readlink "/proc/$P/ns/net")|$ENG|$PORT|$HOST"
  if [ -z "$PORT" ] || [ -n "${DBSEEN[$key]:-}" ]; then printf 'P\t%s\t-\n' "$k" >> "$TMPD/dbpid"; continue; fi
  DBSEEN[$key]=1
  id=$(cid_of "$P"); spec=""
  [ -n "$id" ] && spec=$(spec_of "$id")
  # empty fields as "-": read with a tab IFS would merge two tabs into one
  line=$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' "$P" "$(where_of "$P" "$id" "$spec")" "$ENG" "${HOST:--}" "$PORT" "${TPORT:--}" \
    "${spec:--}" "$(readlink "/proc/$P/exe" 2>/dev/null || echo -)")
  printf '%s\n' "$line" >> "$TMPD/dbinst"
  printf 'P\t%s\t%s\n' "$k" "$line" >> "$TMPD/dbpid"
done
undash() { local v; for v in "$@"; do [ "${!v}" = - ] && printf -v "$v" '%s' ""; done; }

# -- finding queue tables of each instance: I (when, status, the way in), T (a table to count) and
# B (a table too big to count every minute) lines into the new cache --
cp "$TMPD/dbpid" "$TMPD/dbcache"
: > "$TMPD/dbskip"
while IFS=$'\t' read -r P W ENG HOST PORT TPORT SPEC EXE; do
  undash HOST TPORT SPEC EXE
  INST="$W|$ENG:$PORT"
  last=$(awk -F'\t' -v i="$INST" '$1 == "I" && $2 == i { print $3 "\t" $4; exit }' "$DBCACHE" 2>/dev/null)
  when=${last%%$'\t'*}; status=${last#*$'\t'}
  every=$FIND_EVERY; [ "$status" = down ] && every=$RETRY_DOWN
  # a table line of 0.3 does not say how the system keeps failed jobs: such an instance is looked at anew
  if [ -n "$last" ] && [ $((NOW - ${when:-0})) -lt $every ] \
     && ! awk -F'\t' -v i="$INST" '$1 == "T" && $2 == i && NF < 8 { f = 1 } END { exit !f }' "$DBCACHE"; then
    awk -F'\t' -v i="$INST" '($1 == "I" || $1 == "T" || $1 == "B") && $2 == i' "$DBCACHE" >> "$TMPD/dbcache"
    continue
  fi
  M_CLI="" M_HOST="" M_PORT="" M_USER="" M_PW="" M_DEF="" M_DB=""
  if [ "$ENG" = pg ]; then pg_login < /dev/null; else my_login < /dev/null; fi
  rc=$?
  if [ $rc != 0 ]; then
    printf 'I\t%s\t%s\t%s\n' "$INST" "$NOW" "$([ $rc = 1 ] && echo auth || echo down)" >> "$TMPD/dbcache"
    continue
  fi
  printf 'I\t%s\t%s\tok\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$INST" "$NOW" "$M_CLI" "${M_HOST:--}" "${M_PORT:--}" "${M_USER:--}" \
    "${M_PW:--}" "${M_DEF:--}" "${M_DB:--}" >> "$TMPD/dbcache"
  if [ "$ENG" = pg ]; then
    pgrun "$M_DB" "select datname from pg_database where datallowconn and not datistemplate order by 1 limit $MAX_DBS" > "$TMPD/dbs"
    while IFS= read -r d; do
      [ -n "$d" ] || continue
      pgrun "$d" "$PG_FIND" < /dev/null | sed "s#^#$d$TAB#"
    done < "$TMPD/dbs" > "$TMPD/found"
  else
    myrun "$MY_FIND" | awk -F'\t' -v OFS='\t' 'NF >= 4 { print $1, $0 }' > "$TMPD/found"
  fi
  # base, then schema table rows cols -> one line per queue table that answers
  while IFS=$'\t' read -r base sch tbl rows sys var fv; do
    if [ "$sys" = laravel_failed ]; then
      # failed jobs are read by row id: a big table is no trouble
      if [ "$ENG" = pg ]; then t="\"$sch\".\"$tbl\""; else t="\`$sch\`.\`$tbl\`"; fi
      out=$(dbrun "$base" "select 'END', max(id) from $t" < /dev/null | tail -1)
    else
      [ "${rows:-0}" -le $MAX_ROWS ] 2>/dev/null || { printf 'B\t%s\t%s\t%s\t%s\t%s\t%s\n' "$INST" "$base" "$sch" "$tbl" "$sys" "$rows" >> "$TMPD/dbcache"; continue; }
      sql=$(qsql 0 "$sch" "$tbl" "$sys" "$var") || continue
      out=$(dbrun "$base" "$sql union all $QEND" < /dev/null | tail -1)
    fi
    [ "${out%%$TAB*}" = END ] || continue
    printf 'T\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$INST" "$base" "$sch" "$tbl" "$sys" "$var" "${fv:--}" >> "$TMPD/dbcache"
  done < <(while IFS=$'\t' read -r base rest; do printf '%s\n' "$rest" | qclassify | sed "s#^#$base$TAB#"; done < "$TMPD/found")
done < "$TMPD/dbinst"
mv -f "$TMPD/dbcache" "$DBCACHE.new" && mv -f "$DBCACHE.new" "$DBCACHE"

# -- the count: one query per database (MySQL: per instance), all its queue tables in one union --
: > "$TMPD/dbrows"
while IFS=$'\t' read -r P W ENG HOST PORT TPORT SPEC EXE; do
  undash HOST TPORT SPEC EXE
  INST="$W|$ENG:$PORT"
  line=$(awk -F'\t' -v i="$INST" '$1 == "I" && $2 == i' "$DBCACHE")
  case "$line" in
    *$'\t'auth) printf '%s\t%s\n' "$W" "$ENG" >> "$TMPD/dbskip"; continue ;;
    *$'\t'ok$'\t'*) ;;
    *) continue ;;
  esac
  IFS=$'\t' read -r _ _ _ _ M_CLI M_HOST M_PORT M_USER M_PW M_DEF M_DB <<< "$line"
  undash M_HOST M_PORT M_USER M_PW M_DEF M_DB
  awk -F'\t' -v i="$INST" '$1 == "B" && $2 == i { print $3 "\t" $4 "\t" $5 "\t" $6 "\t" $7 }' "$DBCACHE" \
    | while IFS=$'\t' read -r base sch tbl sys rows; do
        printf '%s\t%s\tbig\t%s\t%s\t%s\t%s\n' "$W" "$ENG" "$base" "$sch.$tbl" "$sys" "$rows"
      done >> "$TMPD/dbskip"
  for base in $(awk -F'\t' -v i="$INST" -v e="$ENG" '$1 == "T" && $2 == i && $6 != "laravel_failed" { print (e == "pg" ? $3 : "-") }' "$DBCACHE" | sort -u); do
    awk -F'\t' -v i="$INST" -v b="$base" -v e="$ENG" '$1 == "T" && $2 == i && $6 != "laravel_failed" && (e != "pg" || $3 == b)' "$DBCACHE" > "$TMPD/t"
    sql=""; n=0
    while IFS=$'\t' read -r _ _ tb sch tbl sys var fv; do
      n=$((n + 1))
      sql="$sql$(qsql "$n" "$sch" "$tbl" "$sys" "$var") union all "
    done < "$TMPD/t"
    dbrun "$base" "${sql}$QEND" < /dev/null > "$TMPD/res"
    # a cut-short answer (timeout, an error) would read as empty queues: skip it this time
    [ "$(tail -1 "$TMPD/res" | cut -f1)" = END ] || continue
    awk -F'\t' -v tf="$TMPD/t" -v w="$W" -v e="$ENG" '
      BEGIN { OFS = "\t"; while ((getline l < tf) > 0) { split(l, f, "\t"); m++
                b[m] = f[3]; tb[m] = (e == "pg" && f[4] != "public") ? f[4] "." f[5] : f[5]; sy[m] = f[6] } }
      $1 ~ /^[0-9]+$/ && ($1 in b) { k = $1 SUBSEP $2; r[k] += $3; x[k] += $4
        if ($5 ~ /^[0-9]+$/ && (!(k in o) || $5 < o[k])) o[k] = $5 }
      END { for (k in r) { split(k, kk, SUBSEP); i = kk[1]; print w, e, b[i], tb[i], sy[i], kk[2], r[k], x[k], (r[k] > 0 && (k in o)) ? o[k] : "" }
            for (i = 1; i <= m; i++) print w, e, b[i], tb[i], sy[i], "", 0, 0, "" }' "$TMPD/res" >> "$TMPD/dbrows"
  done
  # failed jobs, every 5 minutes: one query per database (MySQL: per instance), apart from the count -
  # a slow one must not cost the queues
  [ "$FAILRUN" = 1 ] || continue
  for base in $(awk -F'\t' -v i="$INST" -v e="$ENG" '$1 == "T" && $2 == i && $8 != "" && $8 != "-" { print (e == "pg" ? $3 : "-") }' "$DBCACHE" | sort -u); do
    awk -F'\t' -v i="$INST" -v b="$base" -v e="$ENG" '$1 == "T" && $2 == i && $8 != "" && $8 != "-" && (e != "pg" || $3 == b)' "$DBCACHE" > "$TMPD/ft"
    sql=""; n=0; : > "$TMPD/fspan"
    while IFS=$'\t' read -r _ _ tb sch tbl sys var fv; do
      n=$((n + 1)); FID1="" FID24=""
      if [ "$sys" = laravel_failed ] || [ "$sys" = symfony ]; then
        hk="id|$INST|$tb|$sch.$tbl"
        set -- $(hist_at "$hk" 3600); FID1=${1:-}
        set -- $(hist_at "$hk" 86400); FID24=${1:-}
        [ -n "${2:-}" ] && printf '%s\t%s\n' "$n" "$2" >> "$TMPD/fspan"
      fi
      part=$(fsql "$n" "$sch" "$tbl" "$sys" "$var" "$fv") || continue
      sql="$sql${sql:+ union all }$part"
    done < "$TMPD/ft"
    [ -n "$sql" ] || continue
    dbrun "$base" "$sql union all (select 'END', '', '', 0, 0, '', '')" < /dev/null > "$TMPD/fres"
    [ "$(tail -1 "$TMPD/fres" | cut -f1)" = END ] || continue
    awk -F'\t' -v tf="$TMPD/ft" -v sp="$TMPD/fspan" -v w="$W" -v e="$ENG" -v us="$US" -v now="$NOW" -v inst="$INST" \
        -v hn="$TMPD/hnew" "$AWKFN"'
      BEGIN { OFS = "\t"
        while ((getline l < tf) > 0) { split(l, f, "\t"); m++; base[m] = f[3]; sch[m] = f[4]; tbl[m] = f[5]; sy[m] = f[6]
          key[m] = w us e us f[3] us ((e == "pg" && f[4] != "public") ? f[4] "." f[5] : f[5]) us f[6] }
        while ((getline l < sp) > 0) { split(l, f, "\t"); fts[f[1]] = f[2] } }
      function cls(i, c) {
        if (sy[i] == "laravel_failed") return js(c, "displayName")
        if (sy[i] == "symfony") return js(c, "type")
        if (sy[i] == "good_job") return js(c, "job_class")
        if (sy[i] == "delayed_job") {
          if (match(c, /job_class: *[A-Za-z0-9_:]+/)) { c = substr(c, RSTART, RLENGTH); sub(/job_class: */, "", c); return c }
          if (match(c, /!ruby\/object:[A-Za-z0-9_:]+/)) return substr(c, RSTART + 13, RLENGTH - 13)
          return "" }
        return c }
      function err(i, x,   a, b) {
        if (sy[i] == "symfony") { a = sjs(x, "exceptionClass"); b = sjs(x, "exceptionMessage"); return a (a != "" && b != "" ? ": " : "") b }
        if (sy[i] == "solid_queue" && x ~ /^\{/) { a = js(x, "exception_class"); b = js(x, "message"); return a (a != "" && b != "" ? ": " : "") b }
        return x }
      !($1 in key) { next }
      $2 == "m" { if ($4 ~ /^[0-9]+$/) print "H", "id|" inst "|" base[$1] "|" sch[$1] "." tbl[$1], now, $4 >> hn; next }
      $2 == "n" { s = 86400; if ($1 in fts) { s = now - fts[$1]; if (s > 86400) s = 86400 }
        print "T", key[$1], ($4 ~ /^[0-9]+$/) ? $4 : 0, ($5 ~ /^[0-9]+$/) ? $5 : 0, s; next }
      $2 == "s" { j[key[$1] OFS $3 OFS cls($1, $6)]++; next }
      $2 == "e" { print "E", key[$1], $3, cls($1, $6), err($1, $7) }
      END { for (k in j) print "J", k, j[k] }' "$TMPD/fres" >> "$TMPD/frows"
  done
done < "$TMPD/dbinst"

# -- the report: brokers by instance, system, database and namespace, the deepest queues first --
# failed jobs of a broker by its key: {"h1", "d1", "span", "top" (the job classes and queues that failed
# most, among the last hundred), "err" (the first line of the last error), "err_q", "err_job"}
FAILJS="$AWKFN"'
function e(s,   _jp, _jn, _ji) { gsub(/[[:cntrl:]]/, "", s); _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
function fload(ff,   l, f) {
  while ((getline l < ff) > 0) { split(l, f, "\t")
    if (f[1] == "T") FT[f[2]] = f[3] "\t" f[4] "\t" f[5]
    else if (f[1] == "J") { NJ[f[2]]++; JQ[f[2], NJ[f[2]]] = f[3]; JJ[f[2], NJ[f[2]]] = f[4]; JN[f[2], NJ[f[2]]] = f[5] + 0 }
    else if (f[1] == "E") FE[f[2]] = f[3] "\t" f[4] "\t" f[5] } }
function fjson(k,   t, a, i, n, best, cnt, out, r) {
  if (!(k in FT)) return ""
  split(FT[k], t, "\t"); delete used; out = ""; cnt = 0
  for (n = 0; n < 3; n++) { best = 0
    for (i = 1; i <= NJ[k]; i++) if (!(i in used) && (best == 0 || JN[k, i] > JN[k, best])) best = i
    if (best == 0) break
    used[best] = 1
    out = out (cnt++ ? "," : "") sprintf("{\"q\":\"%s\",\"job\":\"%s\",\"n\":%d}", e(JQ[k, best]), e(JJ[k, best]), JN[k, best]) }
  r = sprintf(",\"failed\":{\"h1\":%d,\"d1\":%d,\"span\":%d,\"top\":[%s]", t[1], t[2], t[3], out)
  if (k in FE) { split(FE[k], a, "\t"); r = r sprintf(",\"err_q\":\"%s\",\"err_job\":\"%s\",\"err\":\"%s\"", e(a[1]), e(a[2]), e(mask(a[3]))) }
  return r "}" }
'
json=$(sort -t$'\t' -k1,1 -k2,2 -k3,3n -k4,4 -k6,6nr "$TMPD/rows" | awk -F'\t' -v max=$MAX_Q -v ff="$TMPD/frows" -v us="$US" "$FAILJS"'
  BEGIN { fload(ff) }
  function flush() { if (g == "") return
    b = b (nb++ ? "," : "") sprintf("{\"system\":\"%s\",\"where\":\"%s\",\"db\":%d,\"ns\":\"%s\",\"total\":%d,\"queues\":[%s]%s}", gs, e(gw), gd, e(gn), cnt, qs,
      fjson(gw us gs us gd us gn)) }
  { k = $1 "\t" $2 "\t" $3 "\t" $4
    if (k != g) { flush(); g = k; gw = $1; gs = $2; gd = $3; gn = $4; cnt = 0; shown = 0; qs = "" }
    cnt++
    # every queue with messages (up to max), empty ones only to fill a short list: GitLab keeps
    # two hundred queue names of old versions, all empty
    if (($6 + $7 > 0 && shown < max) || shown < 20) qs = qs (shown++ ? "," : "") sprintf("{\"name\":\"%s\",\"ready\":%d%s%s%s}", e($5), $6,
      $7 > 0 ? ",\"unacked\":" $7 : "", $8 != "" ? ",\"consumers\":" $8 : "", $9 != "" ? ",\"oldest\":" $9 : "") }
  END { flush(); printf "%s", b }')
skip=$(sort -u "$TMPD/skip" | head -50 | while IFS= read -r w; do printf '{"where":"%s","why":"auth"},' "$(esc "$w")"; done | sed 's/,$//')
# queue tables: a broker per table, the deepest queues first; an empty table is a broker too
dbjson=$(sort -t$'\t' -k1,1 -k2,2 -k3,3 -k4,4 -k5,5 "$TMPD/dbrows" | awk -F'\t' -v max=$MAX_Q -v ff="$TMPD/frows" -v us="$US" "$FAILJS"'
  BEGIN { fload(ff) }
  function flush(   i, j, t, qs, shown, k) { if (g == "") return
    # the deepest first: a simple selection sort, a table rarely has more than a few queues
    for (i = 1; i <= nq; i++) for (j = i + 1; j <= nq; j++) if (dep[j] > dep[i]) {
      t = qn[i]; qn[i] = qn[j]; qn[j] = t; t = qr[i]; qr[i] = qr[j]; qr[j] = t
      t = qx[i]; qx[i] = qx[j]; qx[j] = t; t = dep[i]; dep[i] = dep[j]; dep[j] = t; t = qo[i]; qo[i] = qo[j]; qo[j] = t }
    qs = ""; shown = 0
    for (i = 1; i <= nq && shown < max; i++) qs = qs (shown++ ? "," : "") sprintf("{\"name\":\"%s\",\"ready\":%d%s%s}", e(qn[i]), qr[i],
      qx[i] > 0 ? ",\"unacked\":" qx[i] : "", qo[i] != "" ? ",\"oldest\":" qo[i] : "")
    k = gw us gen us gb us gt us gs; done[k] = 1
    b = b (nb++ ? "," : "") sprintf("{\"system\":\"%s\",\"engine\":\"%s\",\"where\":\"%s\",\"base\":\"%s\",\"table\":\"%s\",\"total\":%d,\"queues\":[%s]%s}", e(gs), gen, e(gw), e(gb), e(gt), nq, qs, fjson(k)) }
  { k = $1 "\t" $2 "\t" $3 "\t" $4 "\t" $5
    if (k != g) { flush(); g = k; gw = $1; gen = $2; gb = $3; gt = $4; gs = $5; nq = 0 }
    if ($6 == "") next
    nq++; qn[nq] = $6; qr[nq] = $7; qx[nq] = $8; dep[nq] = $7 + $8; qo[nq] = $9 }
  END { flush()
    # Laravel failed_jobs is not a queue: a broker of its own, when something failed in a day
    for (k in FT) { n = split(k, p, us); split(FT[k], t, "\t")
      if (n == 5 && p[5] == "laravel_failed" && !(k in done) && t[2] + t[1] > 0)
        b = b (nb++ ? "," : "") sprintf("{\"system\":\"laravel_failed\",\"engine\":\"%s\",\"where\":\"%s\",\"base\":\"%s\",\"table\":\"%s\",\"total\":0,\"queues\":[]%s}", p[2], e(p[1]), e(p[3]), e(p[4]), fjson(k)) }
    printf "%s", b }')
dbskip=$(sort -u "$TMPD/dbskip" | head -50 | while IFS=$'\t' read -r w eng why base tbl sys rows; do
  if [ "${why:-}" = big ]; then
    printf '{"where":"%s","why":"big","engine":"%s","base":"%s","table":"%s","system":"%s","rows":%d},' \
      "$(esc "$w")" "$eng" "$(esc "$base")" "$(esc "${tbl#public.}")" "$sys" "${rows:-0}"
  else
    printf '{"where":"%s","why":"auth","engine":"%s"},' "$(esc "$w")" "$eng"
  fi
done | sed 's/,$//')
[ -n "$json" ] && [ -n "$dbjson" ] && json="$json,"
json="$json$dbjson"
[ -n "$skip" ] && [ -n "$dbskip" ] && skip="$skip,"
skip="$skip$dbskip"
# the failed jobs pass and the counters for the next ones: an hour and a day back, a little more kept
if [ "$FAILRUN" = 1 ]; then
  { printf 'F\t%s\n' "$NOW"; cat "$TMPD/frows"; } > "$FCACHE.new" && mv -f "$FCACHE.new" "$FCACHE"
  { cat "$HIST" 2>/dev/null; cat "$TMPD/hnew"; } | awk -F'\t' -v t=$((NOW - 90000)) '$1 == "H" && $3 >= t' > "$HIST.new" \
    && mv -f "$HIST.new" "$HIST"
fi
mkdir -p "${OUT%/*}"
printf '{"v":1,"ts":%s,"brokers":[%s],"skipped":[%s]}\n' "$NOW" "$json" "$skip" > "$OUT.tmp.$$"
chmod 0644 "$OUT.tmp.$$"
mv -f "$OUT.tmp.$$" "$OUT"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-queues.service <<EOF
[Unit]
Description=Kervax: depth of the background job queues in Redis and database tables (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
TimeoutStartSec=5min
EOF
cat > /etc/systemd/system/kervax-queues.timer <<'EOF'
[Unit]
Description=Kervax: read the job queues in Redis and database tables every minute
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
echo "✓ queues-setup: job queues in Redis (Celery, Sidekiq, RQ, BullMQ, Laravel, Yii) and database tables (Yii, Laravel, Symfony, Delayed Job, Solid Queue, GoodJob, Que, Oban, River, pg-boss, Hangfire), the oldest job and failed jobs -> $OUT (every minute)."
