#!/usr/bin/env bash
# Kervax: database inventory (databases, their sizes, logins, engine version) for the
# Services section. The unprivileged agent (kervax) cannot reach into containers (docker
# exec is blocked by its own proxy), so a root helper queries the engines on a timer and
# writes /var/lib/kervax/db-stats.json. The agent ONLY READS that file.
#
# NO PASSWORDS ARE STORED OR PASSED AROUND: containers are entered with `docker exec` as a
# service user, and the password (when needed) is taken from THE CONTAINER'S OWN
# environment — exactly as the backup-setup dump helper already does. Only database and
# login names, sizes and the version leave this script.
#
# Since 0.5 it also checks database health and writes /var/lib/kervax/report.d/db-health.json
# (the agent passes report.d files through as they are): replication state and lag, the oldest
# open transaction, lock waits, and for PostgreSQL the transaction ID age (wraparound) and
# tables full of dead rows that have not been vacuumed for long. Query texts never leave: only
# database, table and application names and numbers.
#
# Since 0.6 the oldest idle transaction that holds a snapshot (backend_xmin) is reported on its
# own: only such a transaction keeps VACUUM from removing dead rows. One holding just an advisory
# lock was reported as "blocks VACUUM" while the real culprit sat next to it.
#
# Since 0.7 a container without the postgres role is queried as its POSTGRES_USER in an explicit
# database (POSTGRES_DB, else postgres). psql -U <user> picks a database named after the user, and
# a container created with POSTGRES_USER=app POSTGRES_DB=appdb has no such database: every query
# failed and the inventory stayed empty on a live PostgreSQL.
set -euo pipefail

KERVAX_SETUP_VERSION=0.7  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: without a database it collects an empty file

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-db-stats"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/db-stats.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes,
# and rewriting it in place (cat >) made a run that was in progress continue from the same
# byte offset in the new text - a syntax error in the middle of a line and a failed unit.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Collects the database inventory -> /var/lib/kervax/db-stats.json. Read only, no passwords leave.
set -u
OUT=/var/lib/kervax/db-stats.json
TMP="$OUT.tmp.$$"
HOUT=/var/lib/kervax/report.d/db-health.json
HEALTH=""
TO=10   # per-query timeout: a hung database must not hang the whole collection

have() { command -v "$1" >/dev/null 2>&1; }
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037'; }

# "name|size" per line -> a JSON array [{"name":...,"size":...}]. There is an upper bound:
# a node can hold close to a hundred instances (104 seen), and a full list would bloat the
# report.
MAXDB=20
MAXUSR=50
# IMPORTANT: the size is printed as a STRING (%s), not a number. In mawk (the default on
# Debian/Ubuntu) printf "%d" is 32-bit: anything above 2 GiB collapsed to exactly
# 2147483648, so a 2.6 GB database was shown as "2048.0 MB", and so was every large one.
# The value already arrives from the database as an integer; there is nothing to convert.
dbs_json() {
  awk -F'|' -v max="$MAXDB" 'BEGIN{printf "["; n=0}
    n>=max{next}
    { name=$1; size=$2;
      gsub(/^[ \t]+|[ \t]+$/,"",name); gsub(/^[ \t]+|[ \t]+$/,"",size);
      if (name=="") next;
      if (size !~ /^[0-9]+$/) size="0";
      gsub(/\\/,"\\\\",name); gsub(/"/,"\\\"",name);
      printf "%s{\"name\":\"%s\",\"size\":%s}", (n++?",":""), name, size }
    END{printf "]"}'
}
# a line-by-line list -> a JSON array of strings
list_json() {
  awk -v max="$MAXUSR" 'BEGIN{printf "["; n=0}
    n>=max{next}
    { s=$0; gsub(/^[ \t]+|[ \t]+$/,"",s); if (s=="") next;
      gsub(/\\/,"\\\\",s); gsub(/"/,"\\\"",s);
      printf "%s\"%s\"", (n++?",":""), s }
    END{printf "]"}'
}

# an integer or 0: whatever the engine printed, the JSON stays valid
num() { case "${1:-}" in ''|*[!0-9-]*|-) echo 0 ;; *) echo "$1" ;; esac; }

ENTRIES=""
add_entry() { # engine container version dbs_json users_json conn_used conn_max
  local e="$1" c="$2" v="$3" d="$4" u="$5" cu="${6:-0}" cm="${7:-0}"
  [ "$d" = "[]" ] && [ "$u" = "[]" ] && [ -z "$v" ] && return 0  # nothing at all - stay quiet
  # conn_used/conn_max: connection slots in use and the configured limit. Running out of
  # them takes an application down as hard as a dead database, and the failure looks like
  # "FATAL: sorry, too many clients already" in the app log rather than anything on the
  # database dashboard. 0/0 means the engine did not report them - the panel then simply
  # shows nothing instead of a fake 0%.
  ENTRIES="$ENTRIES${ENTRIES:+,}{\"engine\":\"$(esc "$e")\",\"container\":\"$(esc "$c")\",\"version\":\"$(esc "$v")\",\"dbs\":$d,\"users\":$u,\"conn_used\":${cu:-0},\"conn_max\":${cm:-0}}"
}

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
# psql runs as the postgres service user (peer authentication over the socket), so no
# password is needed; falls back to the container's POSTGRES_USER for non-standard images.
pg_q() { # container query
  local c="$1" q="$2" u d
  if [ -n "$c" ]; then
    u=$(docker exec "$c" printenv POSTGRES_USER 2>/dev/null || true)
    d=$(docker exec "$c" printenv POSTGRES_DB 2>/dev/null || true)
    timeout $TO docker exec -u postgres "$c" psql -Atqc "$q" 2>/dev/null \
      || timeout $TO docker exec "$c" psql -d "${d:-postgres}" -Atqc "$q" ${u:+-U "$u"} 2>/dev/null
  else
    timeout $TO su -s /bin/sh postgres -c "psql -Atqc \"$q\"" 2>/dev/null
  fi
}
collect_pg() {
  local c="$1" v d u cu cm
  v=$(pg_q "$c" "show server_version" | head -1 | awk '{print $1}')
  # a version starts with a digit; anything else is docker's own error printed to stdout (a
  # postgres-exporter matches the image pattern but has no psql: "OCI runtime exec failed")
  case "$v" in [0-9]*) ;; *) return 0 ;; esac
  d=$(pg_q "$c" "select datname||'|'||pg_database_size(datname) from pg_database where not datistemplate order by pg_database_size(datname) desc" | dbs_json)
  u=$(pg_q "$c" "select rolname from pg_roles where rolcanlogin order by 1" | list_json)
  # Connection slots. Only client backends are counted: pg_stat_activity also lists the
  # server's own processes (checkpointer, walwriter, io workers, autovacuum launcher), and
  # they do not consume the client limit - counting them showed 26 of 20 on a live database.
  # The limit reported is the full max_connections. Subtracting superuser_reserved_connections
  # looked more precise but produced "18 of 17" on a live database - a superuser may occupy the
  # reserve, and a figure above 100% reads as a broken metric. An ordinary application does hit
  # the wall a few slots earlier, and the threshold covers exactly that gap.
  cu=$(pg_q "$c" "select count(*) from pg_stat_activity where backend_type = 'client backend'" | head -1)
  cm=$(pg_q "$c" "select current_setting('max_connections')::int" | head -1)
  add_entry pg "$c" "$v" "$d" "$u" "$cu" "$cm"
  health_pg "$c"
}

# the same as pg_q, in a given database (table statistics live per database)
pg_q_db() { # container db query
  local c="$1" db="$2" q="$3" u
  if [ -n "$c" ]; then
    u=$(docker exec "$c" printenv POSTGRES_USER 2>/dev/null || true)
    timeout $TO docker exec -u postgres "$c" psql -d "$db" -Atqc "$q" 2>/dev/null \
      || timeout $TO docker exec "$c" psql -d "$db" -Atqc "$q" ${u:+-U "$u"} 2>/dev/null
  elif have runuser; then
    timeout $TO runuser -u postgres -- psql -d "$db" -Atqc "$q" 2>/dev/null
  fi
}

# "age|db|app" of the oldest client transaction in the given state (active / idle in transaction)
pg_oldest() { # container state-condition
  pg_q "$1" "select extract(epoch from now()-xact_start)::int||'|'||coalesce(datname,'')||'|'||coalesce(application_name,'') from pg_stat_activity where backend_type='client backend' and pid <> pg_backend_pid() and $2 and xact_start is not null order by xact_start limit 1" | head -1
}

health_pg() {
  local c="$1" rec r role n lag st xa xi lw xid dead db t dn ln va
  rec=$(pg_q "$c" "select pg_is_in_recovery()" | head -1)
  case "$rec" in t|f) ;; *) return 0 ;; esac
  if [ "$rec" = t ]; then
    # a replica: is the WAL receiver streaming, and how far behind is the replay. Equal LSNs mean
    # it has replayed everything it got - an idle primary is not lag
    role=replica; n=0
    r=$(pg_q "$c" "select coalesce((select status from pg_stat_wal_receiver limit 1),'none')||'|'||case when pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn() then 0 else coalesce(extract(epoch from now() - pg_last_xact_replay_timestamp()),0)::int end" | head -1)
    st=${r%%|*}; lag=$(num "${r#*|}")
  else
    role=primary; st=""
    r=$(pg_q "$c" "select count(*)||'|'||coalesce(max(extract(epoch from replay_lag)),0)::int from pg_stat_replication" | head -1)
    n=$(num "${r%%|*}"); lag=$(num "${r#*|}")
  fi
  xa=$(pg_oldest "$c" "state='active'")
  xi=$(pg_oldest "$c" "state like 'idle in transaction%'")
  xm=$(pg_oldest "$c" "state like 'idle in transaction%' and backend_xmin is not null")
  lw=$(pg_q "$c" "select count(*)||'|'||coalesce(max(extract(epoch from now()-query_start)),0)::int from pg_stat_activity where wait_event_type='Lock'" | head -1)
  xid=$(pg_q "$c" "select age(datfrozenxid)||'|'||datname from pg_database order by age(datfrozenxid) desc limit 1" | head -1)
  # tables with more than a million dead rows (and over 20% of the live ones) in the three biggest
  # databases, with seconds since the last vacuum (-1: never)
  dead=""
  while IFS= read -r db; do
    [ -n "$db" ] || continue
    while IFS='|' read -r t dn ln va; do
      [ -n "$t" ] || continue
      dead="$dead${dead:+,}{\"db\":\"$(esc "$db")\",\"table\":\"$(esc "$t")\",\"dead\":$(num "$dn"),\"live\":$(num "$ln"),\"vac\":$(num "$va")}"
    done < <(pg_q_db "$c" "$db" "select relname||'|'||n_dead_tup||'|'||n_live_tup||'|'||coalesce(extract(epoch from now()-greatest(last_vacuum,last_autovacuum))::bigint,-1) from pg_stat_user_tables where n_dead_tup > 1000000 and n_dead_tup > n_live_tup * 0.2 order by n_dead_tup desc limit 3")
  done < <(pg_q "$c" "select datname from pg_database where not datistemplate and datallowconn order by pg_database_size(datname) desc limit 3")
  HEALTH="$HEALTH${HEALTH:+,}{\"engine\":\"pg\",\"container\":\"$(esc "$c")\",\"role\":\"$role\",\"replicas\":$n,\"lag\":$lag,\"receiver\":\"$(esc "$st")\",\"xact\":$(num "${xa%%|*}"),\"xact_db\":\"$(esc "$(printf '%s' "$xa" | cut -d'|' -f2)")\",\"xact_app\":\"$(esc "$(printf '%s' "$xa" | cut -d'|' -f3)")\",\"idle_xact\":$(num "${xi%%|*}"),\"idle_db\":\"$(esc "$(printf '%s' "$xi" | cut -d'|' -f2)")\",\"idle_app\":\"$(esc "$(printf '%s' "$xi" | cut -d'|' -f3)")\",\"idle_xmin\":$(num "${xm%%|*}"),\"idle_xmin_db\":\"$(esc "$(printf '%s' "$xm" | cut -d'|' -f2)")\",\"idle_xmin_app\":\"$(esc "$(printf '%s' "$xm" | cut -d'|' -f3)")\",\"lock_waits\":$(num "${lw%%|*}"),\"lock_wait\":$(num "${lw#*|}"),\"xid\":$(num "${xid%%|*}"),\"xid_db\":\"$(esc "${xid#*|}")\",\"dead\":[$dead]}"
}

# ── MySQL / MariaDB ────────────────────────────────────────────────────────────
# The password is read from the container's environment INSIDE it (MYSQL_PWD) and never
# passed as an argument — otherwise it would show up in `ps` on the host.
my_q() { # container query
  # The client is chosen by what actually exists. mariadb:11 no longer ships a `mysql`
  # command — only `mariadb` — and the query failed silently into /dev/null: the engine
  # appeared in the report while not a single database, size or login did. From outside it
  # looked like "the inventory is empty for some reason", with no hint that the client was
  # simply not found.
  local c="$1" q="$2"
  if [ -n "$c" ]; then
    # MYSQL_PWD is passed as a command PREFIX rather than a separate assignment: without
    # export the variable would stay in the shell and never reach the client — "using
    # password: NO" despite a password being set.
    timeout $TO docker exec "$c" sh -c 'if command -v mysql >/dev/null 2>&1; then CLI=mysql; else CLI=mariadb; fi
      MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}" exec "$CLI" -u root -N -B -e "'"$q"'"' 2>/dev/null
  elif have mysql; then
    timeout $TO mysql -N -B -e "$q" 2>/dev/null
  else
    timeout $TO mariadb -N -B -e "$q" 2>/dev/null
  fi
}
collect_mysql() {
  local c="$1" v d u cu cm
  v=$(my_q "$c" "select version()" | head -1)
  # a version starts with a digit; anything else is docker's own error printed to stdout (a
  # postgres-exporter matches the image pattern but has no psql: "OCI runtime exec failed")
  case "$v" in [0-9]*) ;; *) return 0 ;; esac
  d=$(my_q "$c" "select table_schema, ifnull(sum(data_length+index_length),0) from information_schema.tables where table_schema not in ('information_schema','performance_schema','sys') group by table_schema order by 2 desc" \
      | awk -F'\t' '{print $1"|"$2}' | dbs_json)
  u=$(my_q "$c" "select distinct user from mysql.user order by user" | list_json)
  # the same ceiling on the MySQL/MariaDB side: threads currently connected vs max_connections
  cu=$(my_q "$c" "select variable_value from performance_schema.global_status where variable_name='Threads_connected'" | head -1)
  [ -z "$cu" ] && cu=$(my_q "$c" "show status like 'Threads_connected'" | awk '{print $2}' | head -1)
  cm=$(my_q "$c" "select @@max_connections" | head -1)
  add_entry mysql "$c" "$v" "$d" "$u" "$cu" "$cm"
  health_mysql "$c"
}

# like my_q, but with the header line: SHOW ... STATUS is read by column names. Vertical output
# (\G) is no good here - with -N newer clients drop the field names there too
my_q_hdr() { # container query
  local c="$1" q="$2"
  if [ -n "$c" ]; then
    timeout $TO docker exec "$c" sh -c 'if command -v mysql >/dev/null 2>&1; then CLI=mysql; else CLI=mariadb; fi
      MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}" exec "$CLI" -u root -B -e "'"$q"'"' 2>/dev/null
  elif have mysql; then
    timeout $TO mysql -B -e "$q" 2>/dev/null
  else
    timeout $TO mariadb -B -e "$q" 2>/dev/null
  fi
}
# a column of a one-row result with a header, by a name pattern: Seconds_Behind_Source -> 12
mcol() { printf '%s
' "$1" | awk -F'	' -v k="$2" 'NR == 1 { for (i = 1; i <= NF; i++) if ($i ~ k) c = i; next }
  NR == 2 && c { print $c; exit }'; }

health_mysql() {
  local c="$1" rs role io sq lag err xa lw
  rs=$(my_q "$c" "SHOW REPLICA STATUS\\G")
  [ -z "$rs" ] && rs=$(my_q "$c" "SHOW SLAVE STATUS\\G")
  if [ -n "$rs" ]; then
    role=replica
    io=$(mcol "$rs" '^(Replica|Slave)_IO_Running$'); sq=$(mcol "$rs" '^(Replica|Slave)_SQL_Running$')
    lag=$(mcol "$rs" '^Seconds_Behind_(Source|Master)$'); [ "$lag" = NULL ] && lag=-1
    err=$(mcol "$rs" '^Last_IO_Error$'); [ -n "$err" ] || err=$(mcol "$rs" '^Last_SQL_Error$')
  else
    role=primary; io=""; sq=""; lag=0; err=""
  fi
  xa=$(my_q "$c" "select count(*), coalesce(max(timestampdiff(second, trx_started, now())),0) from information_schema.innodb_trx" | head -1)
  lw=$(my_q "$c" "select count(*), coalesce(max(timestampdiff(second, trx_wait_started, now())),0) from information_schema.innodb_trx where trx_state='LOCK WAIT'" | head -1)
  HEALTH="$HEALTH${HEALTH:+,}{\"engine\":\"mysql\",\"container\":\"$(esc "$c")\",\"role\":\"$role\",\"io\":\"$(esc "$io")\",\"sql\":\"$(esc "$sq")\",\"lag\":$(num "$lag"),\"err\":\"$(esc "$(printf '%s' "$err" | cut -c1-200)")\",\"trx\":$(num "$(printf '%s' "$xa" | cut -f1)"),\"xact\":$(num "$(printf '%s' "$xa" | cut -f2)"),\"lock_waits\":$(num "$(printf '%s' "$lw" | cut -f1)"),\"lock_wait\":$(num "$(printf '%s' "$lw" | cut -f2)")}"
}

# ── ClickHouse ─────────────────────────────────────────────────────────────────
ch_q() { local c="$1" q="$2"; [ -n "$c" ] \
  && timeout $TO docker exec "$c" clickhouse-client --query "$q" 2>/dev/null \
  || timeout $TO clickhouse-client --query "$q" 2>/dev/null; }
collect_ch() {
  local c="$1" v d u
  v=$(ch_q "$c" "select version()" | head -1)
  # a version starts with a digit; anything else is docker's own error printed to stdout (a
  # postgres-exporter matches the image pattern but has no psql: "OCI runtime exec failed")
  case "$v" in [0-9]*) ;; *) return 0 ;; esac
  d=$(ch_q "$c" "select database, sum(bytes_on_disk) from system.parts where active group by database order by 2 desc format TabSeparated" \
      | awk -F'\t' '{print $1"|"$2}' | dbs_json)
  u=$(ch_q "$c" "select name from system.users order by name format TabSeparated" | list_json)
  add_entry clickhouse "$c" "$v" "$d" "$u"
}

# ── Redis ──────────────────────────────────────────────────────────────────────
# Redis "databases" are db0..dbN; instead of a size we show the key count (only a total
# size exists, via used_memory, and it is not broken down per database).
rd_q() { local c="$1" a="$2"; [ -n "$c" ] \
  && timeout $TO docker exec "$c" redis-cli info "$a" 2>/dev/null \
  || timeout $TO redis-cli info "$a" 2>/dev/null; }
collect_redis() {
  local c="$1" v d
  v=$(rd_q "$c" server | awk -F: '/^redis_version:/{print $2}' | tr -d '\r' | head -1)
  # a version starts with a digit; anything else is docker's own error printed to stdout (a
  # postgres-exporter matches the image pattern but has no psql: "OCI runtime exec failed")
  case "$v" in [0-9]*) ;; *) return 0 ;; esac
  d=$(rd_q "$c" keyspace | tr -d '\r' | awk -F'[:,=]' '/^db[0-9]+:/{print $1"|"$3}' | dbs_json)
  add_entry redis "$c" "$v" "$d" "[]"
}

# -- sweep: containers by image plus host processes --------------------------------
if have docker; then
  docker ps --format '{{.Names}}|{{.Image}}' 2>/dev/null | while IFS='|' read -r name img; do
    echo "$name|$img"
  done > /tmp/kv-dbs.$$ 2>/dev/null || : > /tmp/kv-dbs.$$
  while IFS='|' read -r name img; do
    [ -n "$name" ] || continue
    case "$(printf '%s' "$img" | tr 'A-Z' 'a-z')" in
      *postgres*|*timescale*|*pgvector*) collect_pg "$name" ;;
      *mariadb*|*mysql*|*percona*)       collect_mysql "$name" ;;
      *clickhouse*)                      collect_ch "$name" ;;
      *redis*|*valkey*)                  collect_redis "$name" ;;
    esac
  done < /tmp/kv-dbs.$$
  rm -f /tmp/kv-dbs.$$
fi
# host engines (not in docker) - only if one is actually listening locally
pgrep -x postgres        >/dev/null 2>&1 && have psql             && collect_pg ""
pgrep -x mysqld          >/dev/null 2>&1 && { have mysql || have mariadb; } && collect_mysql ""
pgrep -x mariadbd        >/dev/null 2>&1 && { have mysql || have mariadb; } && collect_mysql ""
pgrep -x redis-server    >/dev/null 2>&1 && have redis-cli        && collect_redis ""
pgrep -x clickhouse-serv >/dev/null 2>&1 && have clickhouse-client && collect_ch ""

printf '{"ts":%s,"items":[%s]}\n' "$(date +%s)" "$ENTRIES" > "$TMP"
mv -f "$TMP" "$OUT"
chmod 0644 "$OUT"
mkdir -p "${HOUT%/*}"
printf '{"v":1,"ts":%s,"items":[%s]}\n' "$(date +%s)" "$HEALTH" > "$HOUT.tmp.$$"
chmod 0644 "$HOUT.tmp.$$"
mv -f "$HOUT.tmp.$$" "$HOUT"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chmod 0755 "$HELPER"

cat > /etc/systemd/system/kervax-db-stats.service <<EOF
[Unit]
Description=Kervax: database inventory (databases, sizes, logins)
[Service]
Type=oneshot
ExecStart=$HELPER
EOF
cat > /etc/systemd/system/kervax-db-stats.timer <<'EOF'
[Unit]
Description=Kervax: refresh the database inventory periodically
[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
Persistent=true
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-db-stats.timer >/dev/null 2>&1 || true
# the first collection right away, in the background: on a node with dozens of containers it
# takes minutes, and the installer (ansible waits 120 seconds) must not wait for it
systemctl start --no-block kervax-db-stats.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/dbstat-setup.ver"
chmod 0644 "$STATE_DIR/versions/dbstat-setup.ver"
echo "dbstat-setup: database inventory -> $OUT, health -> $STATE_DIR/report.d/db-health.json (refreshed at boot and every 15 minutes)."
