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
set -euo pipefail

KERVAX_SETUP_VERSION=0.3  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: without a database it collects an empty file

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-db-stats"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/db-stats.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions"

cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Collects the database inventory -> /var/lib/kervax/db-stats.json. Read only, no passwords leave.
set -u
OUT=/var/lib/kervax/db-stats.json
TMP="$OUT.tmp.$$"
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

ENTRIES=""
add_entry() { # engine container version dbs_json users_json
  local e="$1" c="$2" v="$3" d="$4" u="$5"
  [ "$d" = "[]" ] && [ "$u" = "[]" ] && [ -z "$v" ] && return 0  # nothing at all - stay quiet
  ENTRIES="$ENTRIES${ENTRIES:+,}{\"engine\":\"$(esc "$e")\",\"container\":\"$(esc "$c")\",\"version\":\"$(esc "$v")\",\"dbs\":$d,\"users\":$u}"
}

# ── PostgreSQL ─────────────────────────────────────────────────────────────────
# psql runs as the postgres service user (peer authentication over the socket), so no
# password is needed; falls back to the container's POSTGRES_USER for non-standard images.
pg_q() { # container query
  local c="$1" q="$2" u
  if [ -n "$c" ]; then
    u=$(docker exec "$c" printenv POSTGRES_USER 2>/dev/null || true)
    timeout $TO docker exec -u postgres "$c" psql -Atqc "$q" 2>/dev/null \
      || timeout $TO docker exec "$c" psql -Atqc "$q" ${u:+-U "$u"} 2>/dev/null
  else
    timeout $TO su -s /bin/sh postgres -c "psql -Atqc \"$q\"" 2>/dev/null
  fi
}
collect_pg() {
  local c="$1" v d u
  v=$(pg_q "$c" "show server_version" | head -1 | awk '{print $1}')
  [ -z "$v" ] && return 0
  d=$(pg_q "$c" "select datname||'|'||pg_database_size(datname) from pg_database where not datistemplate order by pg_database_size(datname) desc" | dbs_json)
  u=$(pg_q "$c" "select rolname from pg_roles where rolcanlogin order by 1" | list_json)
  add_entry pg "$c" "$v" "$d" "$u"
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
  local c="$1" v d u
  v=$(my_q "$c" "select version()" | head -1)
  [ -z "$v" ] && return 0
  d=$(my_q "$c" "select table_schema, ifnull(sum(data_length+index_length),0) from information_schema.tables where table_schema not in ('information_schema','performance_schema','sys') group by table_schema order by 2 desc" \
      | awk -F'\t' '{print $1"|"$2}' | dbs_json)
  u=$(my_q "$c" "select distinct user from mysql.user order by user" | list_json)
  add_entry mysql "$c" "$v" "$d" "$u"
}

# ── ClickHouse ─────────────────────────────────────────────────────────────────
ch_q() { local c="$1" q="$2"; [ -n "$c" ] \
  && timeout $TO docker exec "$c" clickhouse-client --query "$q" 2>/dev/null \
  || timeout $TO clickhouse-client --query "$q" 2>/dev/null; }
collect_ch() {
  local c="$1" v d u
  v=$(ch_q "$c" "select version()" | head -1)
  [ -z "$v" ] && return 0
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
  [ -z "$v" ] && return 0
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
HELPER_EOF
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
"$HELPER" || true   # run once immediately

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/dbstat-setup.ver"
chmod 0644 "$STATE_DIR/versions/dbstat-setup.ver"
echo "✓ dbstat-setup: database inventory -> $OUT (refreshed at boot and every 15 minutes)."
