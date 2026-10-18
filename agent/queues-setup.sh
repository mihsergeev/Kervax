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
# Only queue names and numbers leave the node: no job contents, no other keys or rows.
set -euo pipefail

KERVAX_SETUP_VERSION=0.3  # MAJOR.MINOR; compared component-wise
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
# Read only: in Redis PING, INFO, SCAN, TYPE, EXISTS, SMEMBERS, HGET, LLEN, ZCARD, SCARD; in a
# database the catalog and count(*) of the queue tables in a read-only session.
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

exec 9>/run/kervax-queues.lock
flock -n 9 || exit 0
TMPD=$(mktemp -d /tmp/kervax-queues.XXXXXX) || exit 0
trap 'rm -rf "$TMPD"' EXIT
NOW=$(date +%s)
TAB=$'\t'

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
  every=$FIND_EVERY; [ "$status" = down ] && every=$RETRY_DOWN
  if [ -n "$last" ] && [ $((NOW - ${when:-0})) -lt $every ]; then
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

# -- job queues kept in database tables: PostgreSQL and MySQL/MariaDB --
DBCACHE=${KERVAX_QUEUES_DBCACHE:-/var/lib/kervax/queues-db.cache}
DB_TO=15          # seconds for one client call
MAX_ROWS=${KERVAX_QUEUES_MAXROWS:-1000000}  # a queue table with more rows (finished jobs kept) is not counted every minute
MAX_DBS=60
IPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/bitnami/postgresql/bin:/opt/bitnami/mysql/bin:/opt/bitnami/mariadb/bin
QT_RE='(^|_)(queue|jobs|messenger_messages|delayed_jobs|good_jobs|que_jobs|oban_jobs|river_job|solid_queue_ready_executions|solid_queue_claimed_executions|solid_queue_jobs|jobqueue)$'
QT_COLS="'channel','job','pushed_at','ttr','delay','reserved_at','done_at','queue','payload','attempts','available_at','created_at','body','headers','queue_name','delivered_at','handler','run_at','locked_at','failed_at','scheduled_at','performed_at','finished_at','expired_at','state','name','start_after','startafter','worker','kind','jobid','fetchedat','job_id'"
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
  args+=(-N -B --force --connect-timeout=5)
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

# The count of one queue table: rows "<idx>  <queue>  <ready>  <taken>"; delayed jobs (their time has
# not come) and finished or failed ones are not counted
qsql() { # idx schema table system variant
  local i=$1 t tq q w r x u tx
  if [ "$ENG" = pg ]; then t="\"$2\".\"$3\""; u="(now() at time zone 'utc')"; tx="::text"
  else t="\`$2\`.\`$3\`"; u="utc_timestamp()"; tx=""; fi
  case "$4" in
    yii) q=channel w="done_at is null" r="reserved_at is null and pushed_at + coalesce(delay, 0) <= $NOW" x="reserved_at is not null" ;;
    laravel) q=queue w="1 = 1" r="reserved_at is null and available_at <= $NOW" x="reserved_at is not null" ;;
    symfony) q=queue_name w="1 = 1" r="delivered_at is null and available_at <= $u" x="delivered_at is not null" ;;
    delayed_job) q=queue; [ "$5" = q ] || q=null
      w="failed_at is null" r="locked_at is null and run_at <= $u" x="locked_at is not null" ;;
    good_job) q=queue_name w="finished_at is null" r="performed_at is null and (scheduled_at is null or scheduled_at <= $u)" x="performed_at is not null" ;;
    que) q=queue w="finished_at is null and expired_at is null" r="run_at <= now()" x="1 = 0" ;;
    oban) q=queue w="state in ('available', 'executing')" r="state = 'available'" x="state = 'executing'" ;;
    river) q=queue w="state in ('available', 'running')" r="state = 'available'" x="state = 'running'" ;;
    pgboss) q=name w="state in ('created', 'retry', 'active')" r="state in ('created', 'retry') and $5 <= now()" x="state = 'active'" ;;
    hangfire) q=queue w="1 = 1" r="fetchedat is null" x="fetchedat is not null" ;;
    solid_queue)
      if [ "$ENG" = pg ]; then tq="\"$2\".\"${5}"; else tq="\`$2\`.\`${5}"; fi
      printf "select '%s'%s, coalesce(queue_name%s, 'default'), count(*), 0 from %s group by 2 union all select '%s'%s, coalesce(j.queue_name%s, 'default'), 0, count(*) from %sclaimed_executions%s c join %sjobs%s j on j.id = c.job_id group by 2" \
        "$i" "$tx" "$tx" "$t" "$i" "$tx" "$tx" "$tq" "${tq:0:1}" "$tq" "${tq:0:1}"
      return ;;
    *) return 1 ;;
  esac
  printf "select '%s'%s, coalesce(%s%s, 'default'), sum(case when %s then 1 else 0 end), sum(case when %s then 1 else 0 end) from %s where %s group by 2" \
    "$i" "$tx" "$q" "$tx" "$r" "$x" "$t" "$w"
}

# which queue system a table is, by its name and columns: "schema table rows cols" -> "... system variant"
qclassify() {
  awk 'BEGIN { FS = OFS = "\t" }
    $2 !~ /^[A-Za-z0-9_$]+$/ || $1 !~ /^[A-Za-z0-9_$-]+$/ { next }
    { delete c; n = split($4, a, ","); for (i = 1; i <= n; i++) c[a[i]] = 1; t = tolower($2); sy = ""; v = "-"
      if (c["channel"] && c["job"] && c["pushed_at"] && c["ttr"] && c["delay"] && c["reserved_at"] && c["done_at"]) sy = "yii"
      else if (c["queue"] && c["payload"] && c["attempts"] && c["reserved_at"] && c["available_at"] && c["created_at"]) sy = "laravel"
      else if (c["body"] && c["headers"] && c["queue_name"] && c["available_at"] && c["delivered_at"]) sy = "symfony"
      else if (c["handler"] && c["run_at"] && c["locked_at"] && c["failed_at"] && c["attempts"]) { sy = "delayed_job"; if (c["queue"]) v = "q" }
      else if (t ~ /good_jobs$/ && c["queue_name"] && c["scheduled_at"] && c["performed_at"] && c["finished_at"]) sy = "good_job"
      else if (t ~ /que_jobs$/ && c["queue"] && c["run_at"] && c["finished_at"] && c["expired_at"]) sy = "que"
      else if (t ~ /oban_jobs$/ && c["state"] && c["queue"] && c["worker"] && c["scheduled_at"]) sy = "oban"
      else if (t ~ /river_job$/ && c["state"] && c["queue"] && c["kind"] && c["scheduled_at"]) sy = "river"
      else if (t == "job" && c["name"] && c["state"] && (c["start_after"] || c["startafter"])) { sy = "pgboss"; v = c["start_after"] ? "start_after" : "startafter" }
      else if (t ~ /jobqueue$/ && c["jobid"] && c["queue"] && c["fetchedat"]) sy = "hangfire"
      else if (t ~ /solid_queue_ready_executions$/ && c["queue_name"] && c["job_id"]) { sy = "solid_queue"; v = substr($2, 1, length($2) - 16) }
      if (sy != "") print $1, $2, $3, sy, v }'
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
  if [ -n "$last" ] && [ $((NOW - ${when:-0})) -lt $every ]; then
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
  while IFS=$'\t' read -r base sch tbl rows sys var; do
    [ "${rows:-0}" -le $MAX_ROWS ] 2>/dev/null || { printf 'B\t%s\t%s\t%s\t%s\t%s\t%s\n' "$INST" "$base" "$sch" "$tbl" "$sys" "$rows" >> "$TMPD/dbcache"; continue; }
    sql=$(qsql 0 "$sch" "$tbl" "$sys" "$var") || continue
    out=$(dbrun "$base" "$sql union all select 'END', '', 0, 0" < /dev/null | tail -1)
    [ "${out%%$TAB*}" = END ] || continue
    printf 'T\t%s\t%s\t%s\t%s\t%s\t%s\n' "$INST" "$base" "$sch" "$tbl" "$sys" "$var" >> "$TMPD/dbcache"
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
  for base in $(awk -F'\t' -v i="$INST" -v e="$ENG" '$1 == "T" && $2 == i { print (e == "pg" ? $3 : "-") }' "$DBCACHE" | sort -u); do
    awk -F'\t' -v i="$INST" -v b="$base" -v e="$ENG" '$1 == "T" && $2 == i && (e != "pg" || $3 == b)' "$DBCACHE" > "$TMPD/t"
    sql=""; n=0
    while IFS=$'\t' read -r _ _ tb sch tbl sys var; do
      n=$((n + 1))
      sql="$sql$(qsql "$n" "$sch" "$tbl" "$sys" "$var") union all "
    done < "$TMPD/t"
    dbrun "$base" "${sql}select 'END', '', 0, 0" < /dev/null > "$TMPD/res"
    # a cut-short answer (timeout, an error) would read as empty queues: skip it this time
    [ "$(tail -1 "$TMPD/res" | cut -f1)" = END ] || continue
    awk -F'\t' -v tf="$TMPD/t" -v w="$W" -v e="$ENG" '
      BEGIN { OFS = "\t"; while ((getline l < tf) > 0) { split(l, f, "\t"); m++
                b[m] = f[3]; tb[m] = (e == "pg" && f[4] != "public") ? f[4] "." f[5] : f[5]; sy[m] = f[6] } }
      $1 ~ /^[0-9]+$/ && ($1 in b) { r[$1 SUBSEP $2] += $3; x[$1 SUBSEP $2] += $4 }
      END { for (k in r) { split(k, kk, SUBSEP); i = kk[1]; print w, e, b[i], tb[i], sy[i], kk[2], r[k], x[k] }
            for (i = 1; i <= m; i++) print w, e, b[i], tb[i], sy[i], "", 0, 0 }' "$TMPD/res" >> "$TMPD/dbrows"
  done
done < "$TMPD/dbinst"

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
# queue tables: a broker per table, the deepest queues first; an empty table is a broker too
dbjson=$(sort -t$'\t' -k1,1 -k2,2 -k3,3 -k4,4 -k5,5 "$TMPD/dbrows" | awk -F'\t' -v max=$MAX_Q '
  function e(s) { gsub(/[[:cntrl:]]/, "", s); gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); return s }
  function flush(   i, j, t, qs, shown) { if (g == "") return
    # the deepest first: a simple selection sort, a table rarely has more than a few queues
    for (i = 1; i <= nq; i++) for (j = i + 1; j <= nq; j++) if (dep[j] > dep[i]) {
      t = qn[i]; qn[i] = qn[j]; qn[j] = t; t = qr[i]; qr[i] = qr[j]; qr[j] = t
      t = qx[i]; qx[i] = qx[j]; qx[j] = t; t = dep[i]; dep[i] = dep[j]; dep[j] = t }
    qs = ""; shown = 0
    for (i = 1; i <= nq && shown < max; i++) qs = qs (shown++ ? "," : "") sprintf("{\"name\":\"%s\",\"ready\":%d%s}", e(qn[i]), qr[i], qx[i] > 0 ? ",\"unacked\":" qx[i] : "")
    b = b (nb++ ? "," : "") sprintf("{\"system\":\"%s\",\"engine\":\"%s\",\"where\":\"%s\",\"base\":\"%s\",\"table\":\"%s\",\"total\":%d,\"queues\":[%s]}", e(gs), gen, e(gw), e(gb), e(gt), nq, qs) }
  { k = $1 "\t" $2 "\t" $3 "\t" $4 "\t" $5
    if (k != g) { flush(); g = k; gw = $1; gen = $2; gb = $3; gt = $4; gs = $5; nq = 0 }
    if ($6 == "") next
    nq++; qn[nq] = $6; qr[nq] = $7; qx[nq] = $8; dep[nq] = $7 + $8 }
  END { flush(); printf "%s", b }')
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
echo "✓ queues-setup: job queues in Redis (Celery, Sidekiq, RQ, BullMQ, Laravel, Yii) and database tables (Yii, Laravel, Symfony, Delayed Job, Solid Queue, GoodJob, Que, Oban, River, pg-boss, Hangfire) -> $OUT (every minute)."
