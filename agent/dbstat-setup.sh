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
# database, table and application names and numbers (until 0.8, see below).
#
# Since 0.6 the oldest idle transaction that holds a snapshot (backend_xmin) is reported on its
# own: only such a transaction keeps VACUUM from removing dead rows. One holding just an advisory
# lock was reported as "blocks VACUUM" while the real culprit sat next to it.
#
# Since 0.7 a container without the postgres role is queried as its POSTGRES_USER in an explicit
# database (POSTGRES_DB, else postgres). psql -U <user> picks a database named after the user, and
# a container created with POSTGRES_USER=app POSTGRES_DB=appdb has no such database: every query
# failed and the inventory stayed empty on a live PostgreSQL.
#
# Since 0.8 PostgreSQL also reports what loads it, over about the last hour: temporary files, disk
# reads, WAL and its full page images, checkpoints by time and by WAL volume, WAL buffer overflows
# and, with pg_stat_statements, the statements that took the most time. Their texts are the
# normalized ones PostgreSQL itself keeps (constants replaced by $1, $2...), cut to 300 characters;
# a text that mentions a password, a secret or a token is not sent at all.
#
# Since 0.9 PostgreSQL also reports the settings that decide how it uses memory, WAL and the disk,
# with what the panel cannot see from inside the database: memory of the machine or the container
# limit, huge pages reserved by the kernel, page tables, how long docker waits before killing the
# container on stop. The panel compares them with the load and gives advice; nothing is changed here.
#
# Since 0.11 MySQL and MariaDB get the same: what loads them over about the last hour (status counters
# and, with performance_schema, the statements that took the most time), the settings against the
# machine, the purge backlog and whose transaction is the oldest. They are also reached where the
# helper did not get before: in Kubernetes pods (containerd, k0s, k3s, cri-o) and on a host where root
# needs a password (through /etc/mysql/debian.cnf or /root/.my.cnf). Replica status was read with \G,
# which newer clients print without field names: a replica would have read as stopped.
#
# 0.12: a backslash in a name or a line (App\Jobs\Mail, a regex in a query) made the report invalid
# JSON on Debian and Ubuntu, whose awk is mawk: there gsub(/\\/, "\\\\") puts back one backslash,
# not two, and the agent drops a block that is not valid JSON - silently, the whole of it. Backslashes
# are doubled by splitting the string now, the same in every awk.
#
# 0.13: in a PostgreSQL container whose superuser is named by POSTGRES_USER there is no role
# "postgres", and every query was tried as it first: each run left a score of
# "FATAL: role "postgres" does not exist" lines in the database log, every 15 minutes, on every
# such container. The POSTGRES_USER role is tried first now, and the other way only when the first
# could not connect at all, not after a query that failed.
#
# 0.14: what the panel needs to give ready commands for switching pg_stat_statements on: the current
# shared_preload_libraries and where it is set (a -c in the container command is not overridden by
# ALTER SYSTEM), the role the helper got in as, the compose project of the container, the systemd unit
# of a host instance. And whether the hour-old snapshot already had statements: for an hour after
# pg_stat_statements was switched on the top stayed empty, and that read as "the database is idle".
set -euo pipefail

KERVAX_SETUP_VERSION=0.14  # MAJOR.MINOR; compared component-wise
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
    # the superuser named by POSTGRES_USER first: such an image has no role "postgres", and asking
    # as it left "FATAL: role "postgres" does not exist" in the database log, a score of lines
    # every 15 minutes (0.13). The other way only when the first could not connect (psql exits 2,
    # docker exec 125-127): a query that failed, asked again as another role, adds that line too
    if [ -n "$u" ] && [ "$u" != postgres ]; then
      timeout $TO docker exec "$c" psql -d "${d:-postgres}" -U "$u" -Atqc "$q" 2>/dev/null
      case $? in 2|12[5-7]) timeout $TO docker exec -u postgres "$c" psql -Atqc "$q" 2>/dev/null ;; esac
    else
      timeout $TO docker exec -u postgres "$c" psql -Atqc "$q" 2>/dev/null
      case $? in 2|12[5-7]) timeout $TO docker exec "$c" psql -d "${d:-postgres}" -Atqc "$q" ${u:+-U "$u"} 2>/dev/null ;; esac
    fi
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
    if [ -n "$u" ] && [ "$u" != postgres ]; then
      timeout $TO docker exec "$c" psql -d "$db" -U "$u" -Atqc "$q" 2>/dev/null
      case $? in 2|12[5-7]) timeout $TO docker exec -u postgres "$c" psql -d "$db" -Atqc "$q" 2>/dev/null ;; esac
    else
      timeout $TO docker exec -u postgres "$c" psql -d "$db" -Atqc "$q" 2>/dev/null
      case $? in 2|12[5-7]) timeout $TO docker exec "$c" psql -d "$db" -Atqc "$q" ${u:+-U "$u"} 2>/dev/null ;; esac
    fi
  elif have runuser; then
    timeout $TO runuser -u postgres -- psql -d "$db" -Atqc "$q" 2>/dev/null
  fi
}

# "age|db|app" of the oldest client transaction in the given state (active / idle in transaction)
pg_oldest() { # container state-condition
  pg_q "$1" "select extract(epoch from now()-xact_start)::int||'|'||coalesce(datname,'')||'|'||coalesce(application_name,'') from pg_stat_activity where backend_type='client backend' and pid <> pg_backend_pid() and $2 and xact_start is not null order by xact_start limit 1" | head -1
}

health_pg() {
  local c="$1" rec r role n lag st xa xi lw xid dead db t dn ln va ld cf
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
  ld=$(load_pg "$c")
  # the agent drops a report.d file over 256 KB whole, health and all: past LOAD_BUDGET the
  # statements are left out (a node can hold dozens of instances)
  case "$ld" in *',"top":['*) [ $((LOAD_BYTES + ${#ld})) -gt "$LOAD_BUDGET" ] && ld="${ld%%,\"top\":*},\"top\":[],\"cut\":1}" ;; esac
  LOAD_BYTES=$((LOAD_BYTES + ${#ld}))
  cf=$(cfg_pg "$c")
  HEALTH="$HEALTH${HEALTH:+,}{\"engine\":\"pg\",\"container\":\"$(esc "$c")\",\"role\":\"$role\",\"replicas\":$n,\"lag\":$lag,\"receiver\":\"$(esc "$st")\",\"xact\":$(num "${xa%%|*}"),\"xact_db\":\"$(esc "$(printf '%s' "$xa" | cut -d'|' -f2)")\",\"xact_app\":\"$(esc "$(printf '%s' "$xa" | cut -d'|' -f3)")\",\"idle_xact\":$(num "${xi%%|*}"),\"idle_db\":\"$(esc "$(printf '%s' "$xi" | cut -d'|' -f2)")\",\"idle_app\":\"$(esc "$(printf '%s' "$xi" | cut -d'|' -f3)")\",\"idle_xmin\":$(num "${xm%%|*}"),\"idle_xmin_db\":\"$(esc "$(printf '%s' "$xm" | cut -d'|' -f2)")\",\"idle_xmin_app\":\"$(esc "$(printf '%s' "$xm" | cut -d'|' -f3)")\",\"lock_waits\":$(num "${lw%%|*}"),\"lock_wait\":$(num "${lw#*|}"),\"xid\":$(num "${xid%%|*}"),\"xid_db\":\"$(esc "${xid#*|}")\",\"dead\":[$dead]${ld:+,\"load\":$ld}${cf:+,\"cfg\":$cf}}"
}

# ── What loads PostgreSQL (0.8) ────────────────────────────────────────────────
# The engine counts temporary files, disk reads, WAL and checkpoints since its start, and
# pg_stat_statements counts time per statement since its last reset (on one node - seven months).
# Such totals say nothing about now, so every run keeps a snapshot and reports the difference with
# the newest one at least LOAD_BASE seconds old: about an hour of load, not 15 minutes of noise.
# Snapshots live in LOAD_DIR, a directory per instance; without texts a statement takes ~50 bytes
# (half a megabyte at the default 10 000 statements).
#
# Texts are read only for the few top statements and cached here. pg_stat_statements reads its
# WHOLE texts file into memory on every read with texts: on one node that file was 3.9 GB
# (multi-row INSERTs with a varying number of rows, each variant an entry with an 8 MB text) and a
# single read took 21 seconds. Above QTEXT_BIG new texts are read at most every QTEXT_EVERY seconds.
LOAD_DIR=/var/lib/kervax/db-load
LOAD_BASE=3000
LOAD_KEEP=4800
QTEXT_MAX=300
QTEXT_BIG=1073741824
QTEXT_EVERY=21600
TOP_TIME=5
TOP_TEMP=2
LOAD_BUDGET=131072
LOAD_BYTES=0

# numbers separated by "|" (a negative one means "this version does not count it")
nums() { printf '%s' "$1" | grep -Eq '^-?[0-9]+(\|-?[0-9]+)*$'; }

load_pg() { # container -> a JSON object on stdout, nothing when the engine does not answer
  local c="$1" dir now vn bs cnt wal ck pre pss=off db="" at tcol topq="" snap ts age base="" fb="" win=0
  local qtext miss line reset pb tb tf xa rd wr fp wb wf kt kr msall top
  dir="$LOAD_DIR/$(printf '%s' "${c:-host}" | tr -c 'A-Za-z0-9_.-' '_')"
  install -d -m 0700 "$LOAD_DIR" "$dir" 2>/dev/null || return 0
  now=$(date +%s)
  vn=$(num "$(pg_q "$c" "show server_version_num" | head -1)")
  [ "$vn" -ge 100000 ] || return 0
  bs=$(num "$(pg_q "$c" "show block_size" | head -1)"); [ "$bs" -gt 0 ] || bs=8192
  cnt=$(pg_q "$c" "select coalesce(sum(temp_bytes),0)||'|'||coalesce(sum(temp_files),0)||'|'||coalesce(sum(xact_commit),0)||'|'||coalesce(sum(blks_read),0) from pg_stat_database" | head -1)
  nums "$cnt" || return 0
  wal="-1|-1|-1|-1"
  [ "$vn" -ge 140000 ] && wal=$(pg_q "$c" "select wal_records||'|'||wal_fpi||'|'||wal_bytes||'|'||wal_buffers_full from pg_stat_wal" | head -1)
  nums "$wal" || wal="-1|-1|-1|-1"
  if [ "$vn" -ge 170000 ]; then ck=$(pg_q "$c" "select num_timed||'|'||num_requested from pg_stat_checkpointer" | head -1)
  else ck=$(pg_q "$c" "select checkpoints_timed||'|'||checkpoints_req from pg_stat_bgwriter" | head -1); fi
  nums "$ck" || ck="-1|-1"

  # pg_stat_statements: loaded at start (shared_preload_libraries) and its view created by CREATE
  # EXTENSION in at least one database - the view exists only there. The database found last time
  # is checked first; "not created anywhere" is rechecked every LOAD_BASE seconds, a node can hold
  # dozens of instances
  pre=$(pg_q "$c" "show shared_preload_libraries" | head -1 | tr -d " '\"")
  case ",$pre," in
    *,pg_stat_statements,*)
      pss=noext
      at=$(num "$(cat "$dir/pssno" 2>/dev/null)")
      if [ -s "$dir/pssdb" ] || [ $((now - at)) -ge "$LOAD_BASE" ]; then
        while IFS= read -r db; do
          [ -n "$db" ] || continue
          if [ "$(pg_q_db "$c" "$db" "select 1 from pg_extension where extname = 'pg_stat_statements'" | head -1)" = 1 ]; then
            pss=on; break
          fi
        done < <(cat "$dir/pssdb" 2>/dev/null; pg_q "$c" "select datname from pg_database where not datistemplate and datallowconn order by datname = 'postgres' desc, pg_database_size(datname) desc limit 20")
      fi
      if [ "$pss" = on ]; then printf '%s\n' "$db" > "$dir/pssdb"; rm -f "$dir/pssno"
      else rm -f "$dir/pssdb"; [ $((now - at)) -ge "$LOAD_BASE" ] && echo "$now" > "$dir/pssno"; fi
      ;;
  esac

  # the snapshot: instance counters, then statements without texts ("P" - statements were read, a
  # snapshot without them must not turn seven months of totals into one hour)
  snap="$dir/$now.snap"
  {
    echo "C|$cnt|$wal|$ck"
    if [ "$pss" = on ]; then
      tcol=total_exec_time; [ "$vn" -ge 130000 ] || tcol=total_time
      [ "$vn" -ge 140000 ] && topq=" and toplevel"
      echo "P|1"
      pg_q_db "$c" "$db" "select 'Q|'||queryid||'|'||dbid||'|'||userid||'|'||calls||'|'||round($tcol)::bigint||'|'||temp_blks_written||'|'||shared_blks_read from pg_stat_statements(false) where queryid is not null$topq"
    fi
  } > "$snap.tmp" && mv -f "$snap.tmp" "$snap"

  # the base: the newest snapshot at least LOAD_BASE old, else the oldest there is
  for ts in $(ls "$dir" | sed -n 's/^\([0-9][0-9]*\)\.snap$/\1/p' | sort -n); do
    age=$((now - ts))
    [ "$age" -gt 0 ] || continue
    [ -n "$fb" ] || fb=$ts
    [ "$age" -ge "$LOAD_BASE" ] && base=$ts
  done
  [ -n "$base" ] || base=$fb
  for ts in $(ls "$dir" | sed -n 's/^\([0-9][0-9]*\)\.snap$/\1/p'); do
    [ $((now - ts)) -gt "$LOAD_KEEP" ] && [ "$ts" != "$base" ] && rm -f "$dir/$ts.snap"
  done

  qtext=0
  [ "$pss" = on ] && qtext=$(num "$(pg_q_db "$c" "$db" "select coalesce((pg_stat_file('pg_stat_tmp/pgss_query_texts.stat', true)).size, 0)" | head -1)")
  if [ -z "$base" ]; then
    printf '{"win":0,"pss":"%s","qtext":%s}' "$pss" "$qtext"
    return 0
  fi
  win=$((now - base))

  # differences. A counter below its base means a reset (pg_stat_reset, a crash) - no window this
  # time. A statement missing from the base is new or came back after eviction: its counters
  # started from zero inside the window. Block counts become bytes
  awk -F'|' -v bs="$bs" '
    FILENAME == ARGV[1] { if ($1 == "C") for (i = 2; i <= NF; i++) b[i] = $i
                else if ($1 == "P") havep = 1
                else if ($1 == "Q") { k = $2 FS $3 FS $4; pc[k] = $5; pm[k] = $6; pt[k] = $7; pr[k] = $8 }
                next }
    $1 == "C" { s = "D"
                for (i = 2; i <= NF; i++) {
                  if ($i < 0 || b[i] == "" || b[i] < 0) d = -1
                  else { d = $i - b[i]; if (d < 0) { reset = 1; d = -1 } }
                  if (i == 5 && d > 0) d = d * bs
                  s = s "|" sprintf("%.0f", d) }
                print s; next }
    $1 == "Q" && havep { k = $2 FS $3 FS $4
                if ((k in pc) && $5 >= pc[k]) { dc = $5 - pc[k]; dm = $6 - pm[k]; dt = $7 - pt[k]; dr = $8 - pr[k] }
                else { dc = $5; dm = $6; dt = $7; dr = $8 }
                if (dc <= 0) next
                all += dm
                if (dm > 0) printf "Q|%s|%.0f|%.0f|%.0f|%.0f\n", k, dc, dm, dt * bs, dr * bs }
    END { printf "S|%.0f|%d|%d\n", all, reset, havep }' "$dir/$base.snap" "$snap" > "$dir/delta"
  line=$(sed -n 's/^D|//p' "$dir/delta")
  reset=$(sed -n 's/^S|[0-9]*|\([0-9]*\)|.*/\1/p' "$dir/delta")
  # the base had statements: right after pg_stat_statements was switched on it has none (0.14)
  pb=$(num "$(sed -n 's/^S|[0-9]*|[0-9]*|\([0-9]*\)$/\1/p' "$dir/delta")")
  msall=$(num "$(sed -n 's/^S|\([0-9]*\)|.*/\1/p' "$dir/delta")")
  if [ "$reset" = 1 ] || [ -z "$line" ]; then
    rm -f "$dir/delta"
    printf '{"win":0,"pss":"%s","qtext":%s}' "$pss" "$qtext"
    return 0
  fi
  IFS='|' read -r tb tf xa rd wr fp wb wf kt kr <<< "$line"

  top=""
  if [ "$pss" = on ]; then
    # the top: TOP_TIME by time, plus up to TOP_TEMP more by temporary files
    { grep '^Q|' "$dir/delta" | sort -t'|' -k6,6nr | head -n "$TOP_TIME"
      grep '^Q|' "$dir/delta" | awk -F'|' '$7 > 0' | sort -t'|' -k7,7nr | head -n $((TOP_TIME + TOP_TEMP))
    } | awk -F'|' '!seen[$2 FS $3 FS $4]++' | head -n $((TOP_TIME + TOP_TEMP)) > "$dir/top"
    touch "$dir/texts"
    miss=$(awk -F'|' 'FILENAME == ARGV[1] { have[$1 FS $2 FS $3] = 1; next } !(($2 FS $3 FS $4) in have) { print $2 }' "$dir/texts" "$dir/top" | sort -u | paste -sd, -)
    at=$(num "$(cat "$dir/texts.at" 2>/dev/null)")
    if [ -n "$miss" ] && { [ "$qtext" -le "$QTEXT_BIG" ] || [ $((now - at)) -ge "$QTEXT_EVERY" ]; }; then
      echo "$now" > "$dir/texts.at"
      # whitespace is collapsed on the first 2000 characters only: regexp_replace over thousands of
      # 8 MB texts hung for minutes. The secret check runs on the whole text
      ( TO=120; pg_q_db "$c" "$db" "select s.queryid||'|'||s.dbid||'|'||s.userid||'|'||coalesce(d.datname,'')||'|'||case when s.query ~* '(passw|secret|token|identified|pgp_sym|encrypt)' then '' else left(regexp_replace(left(s.query, 2000), '\s+', ' ', 'g'), $QTEXT_MAX) end from pg_stat_statements(true) s left join pg_database d on d.oid = s.dbid where s.queryid in ($miss)$topq" ) > "$dir/texts.new"
      cat "$dir/texts.new" "$dir/texts" | awk -F'|' 'NF >= 5 && !seen[$1 FS $2 FS $3]++' | head -n 200 > "$dir/texts.tmp" && mv -f "$dir/texts.tmp" "$dir/texts"
      rm -f "$dir/texts.new"
    fi
    top=$(awk -F'|' 'function j(s,   _jp, _jn, _ji) { _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
      FILENAME == ARGV[1] { k = $1 FS $2 FS $3; db[k] = $4; q = $0; for (i = 0; i < 4; i++) q = substr(q, index(q, "|") + 1); tx[k] = q; next }
      { k = $2 FS $3 FS $4
        printf "%s{\"id\":\"%s\",\"db\":\"%s\",\"q\":\"%s\",\"calls\":%.0f,\"ms\":%.0f,\"temp\":%.0f,\"rd\":%.0f}", (n++ ? "," : ""), j($2), j(db[k]), j(tx[k]), $5, $6, $7, $8 }' "$dir/texts" "$dir/top" | tr -d '\000-\037')
  fi
  rm -f "$dir/delta" "$dir/top"
  printf '{"win":%s,"pss":"%s","qtext":%s,"temp_b":%s,"temp_f":%s,"xact":%s,"rd":%s,"wal_rec":%s,"fpi":%s,"wal_b":%s,"wbf":%s,"ck_t":%s,"ck_r":%s,"ms_all":%s,"pbase":%s,"top":[%s]}' \
    "$win" "$pss" "$qtext" "$(num "$tb")" "$(num "$tf")" "$(num "$xa")" "$(num "$rd")" "$(num "$wr")" "$(num "$fp")" \
    "$(num "$wb")" "$(num "$wf")" "$(num "$kt")" "$(num "$kr")" "$msall" "$pb" "$top"
}

# ── PostgreSQL settings against the machine (0.9) ──────────────────────────────
# Sizes come as bytes (pg_size_bytes), so the panel does not parse units. Huge pages: what the
# engine needs (shared_memory_size_in_huge_pages, PG15+), whether it got them (huge_pages_status,
# PG17+) and what the kernel holds. A container stop timeout of -1 means docker's default, 10 seconds.
flt() { case "${1:-}" in ''|*[!0-9.]*|.) echo 0 ;; *) echo "$1" ;; esac; }

cfg_pg() { # container -> a JSON object, nothing when the engine does not answer
  local c="$1" r sb wb mws wm mwm ecs ckt mc walc hp hps hpn rpc cct vn mem hpt hpsz pt lim="0" sto="-1" sig=""
  local role="" su="" src="" spl="" cp="" cs="" cfs="" cdir="" unit="" pid
  r=$(pg_q "$c" "select concat_ws('|', pg_size_bytes(current_setting('shared_buffers')), pg_size_bytes(current_setting('wal_buffers')), pg_size_bytes(current_setting('max_wal_size')), pg_size_bytes(current_setting('work_mem')), pg_size_bytes(current_setting('maintenance_work_mem')), pg_size_bytes(current_setting('effective_cache_size')), extract(epoch from current_setting('checkpoint_timeout')::interval)::int, current_setting('max_connections'), current_setting('wal_compression'), current_setting('huge_pages'), coalesce(current_setting('huge_pages_status', true), ''), coalesce(current_setting('shared_memory_size_in_huge_pages', true), ''), current_setting('random_page_cost'), current_setting('checkpoint_completion_target'), current_setting('server_version_num'))" | head -1)
  IFS='|' read -r sb wb mws wm mwm ecs ckt mc walc hp hps hpn rpc cct vn <<< "$r"
  [ "$(num "${sb:-}")" -gt 0 ] || return 0
  IFS='|' read -r mem hpt hpsz pt < <(awk '/^MemTotal:/ {m = $2} /^HugePages_Total:/ {h = $2} /^Hugepagesize:/ {s = $2}
    /^PageTables:/ {p = $2} END {printf "%.0f|%.0f|%.0f|%.0f\n", m * 1024, h, s * 1024, p * 1024}' /proc/meminfo)
  # 0.14: for the commands that switch pg_stat_statements on. The current list and where it is set: a
  # "command line" value (-c in the container command) is not overridden by ALTER SYSTEM. A query of
  # its own: shared_preload_libraries is readable by superusers only, and an error must not cost the
  # settings above. pg_settings hides the row from others, then the source is empty (= unknown)
  IFS='|' read -r role su src spl < <(pg_q "$c" "select concat_ws('|', current_user, current_setting('is_superuser'), coalesce((select source from pg_settings where name = 'shared_preload_libraries'), ''), coalesce((select setting from pg_settings where name = 'shared_preload_libraries'), ''))" | head -1)
  if [ -n "$c" ]; then
    IFS='|' read -r sto sig lim cp cs cfs cdir < <(docker inspect -f '{{if .Config.StopTimeout}}{{.Config.StopTimeout}}{{else}}-1{{end}}|{{.Config.StopSignal}}|{{.HostConfig.Memory}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.project.config_files"}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$c" 2>/dev/null)
  else
    # the systemd unit of a host instance (postgresql@16-main on Debian and Ubuntu): the postmaster's cgroup
    pid=$(num "$(head -1 "$(pg_q "" "show data_directory" | head -1)/postmaster.pid" 2>/dev/null)")
    [ "$pid" -gt 0 ] && unit=$(sed -n 's#.*/\([^/]*\.service\)$#\1#p' "/proc/$pid/cgroup" 2>/dev/null | head -1)
  fi
  [ "$(num "${lim:-0}")" -gt 0 ] && mem=$lim
  printf '{"v":%s,"sb":%s,"wb":%s,"mws":%s,"wm":%s,"mwm":%s,"ecs":%s,"ckt":%s,"cct":%s,"mc":%s,"walc":"%s","hp":"%s","hps":"%s","hpn":%s,"rpc":%s,"mem":%s,"mem_lim":%s,"hpt":%s,"hpsz":%s,"pt":%s,"stop_to":%s,"stop_sig":"%s","role":"%s","su":"%s","spl_src":"%s","spl":"%s","cproj":"%s","csvc":"%s","cfiles":"%s","cdir":"%s","unit":"%s"}' \
    "$(num "${vn:-}")" "$(num "$sb")" "$(num "${wb:-}")" "$(num "${mws:-}")" "$(num "${wm:-}")" "$(num "${mwm:-}")" "$(num "${ecs:-}")" \
    "$(num "${ckt:-}")" "$(flt "${cct:-}")" "$(num "${mc:-}")" "$(esc "${walc:-}")" "$(esc "${hp:-}")" "$(esc "${hps:-}")" "$(num "${hpn:-}")" \
    "$(flt "${rpc:-}")" "$(num "${mem:-}")" "$( [ "$(num "${lim:-0}")" -gt 0 ] && echo true || echo false)" "$(num "${hpt:-}")" "$(num "${hpsz:-}")" "$(num "${pt:-}")" \
    "$(num "${sto:--1}")" "$(esc "${sig:-}")" "$(esc "${role:-}")" "$(esc "${su:-}")" "$(esc "${src:-}")" "$(esc "${spl:-}")" \
    "$(esc "${cp:-}")" "$(esc "${cs:-}")" "$(esc "${cfs:-}")" "$(esc "${cdir:-}")" "$(esc "${unit:-}")"
}

# ── MySQL / MariaDB ────────────────────────────────────────────────────────────
# Where an instance lives decides how it is queried:
#   ""        - on the host: the client as root over the socket, else with the options file
#               /etc/mysql/debian.cnf (Debian/Ubuntu maintenance user) or /root/.my.cnf
#   a name    - a docker container: docker exec, the password from the container's own environment
#   pid:<pid> - a container of another runtime (Kubernetes pods under containerd, k0s, k3s, cri-o):
#               docker does not see it, the host has no client. The client from the container's own
#               image runs inside its mount, network and PID namespaces (nsenter) under the uid of the
#               server process, so it has no capabilities and cannot reach anything the database
#               itself cannot. The password comes from the container's settings kept by the runtime
#               (config.json): the official images remove it from the server process environment.
# The password is never an argument (it would show up in `ps`): MYSQL_PWD in the environment.
MYAUTH_SET=""
MYAUTH=""
my_host() { # flags query
  local b=mysql
  have mysql || b=mariadb
  timeout $TO "$b" ${MYAUTH:+"--defaults-file=$MYAUTH"} $1 -e "$2" 2>/dev/null
}
# once per run: the options files first, then socket authentication as root (MariaDB packages). A
# MySQL 8 package from Ubuntu has root with a password and no socket login: there the inventory stayed
# empty while debian.cnf would have let it in. A refused login counts in Aborted_connects, so the one
# that works comes first and the helper does not add to the counter it reports
my_host_auth() {
  [ -n "$MYAUTH_SET" ] && return 0
  MYAUTH_SET=1
  local f v
  for f in /etc/mysql/debian.cnf /root/.my.cnf ""; do
    [ -z "$f" ] || [ -r "$f" ] || continue
    MYAUTH="$f"
    v=$(my_host "-N -B" "select version()" | head -1)
    case "$v" in [0-9]*) return 0 ;; esac
  done
  MYAUTH=""
}
# the root password of a container of another runtime, from its config.json; *_FILE is read inside it
my_pod_pw() { # pid
  local id f
  id=$(grep -o '[0-9a-f]\{64\}' "/proc/$1/cgroup" 2>/dev/null | tail -1)
  [ -n "$id" ] || return 0
  for f in /run/containerd/io.containerd.runtime.v2.task/*/"$id"/config.json \
           /run/*/containerd/io.containerd.runtime.v2.task/*/"$id"/config.json \
           /run/docker/containerd/daemon/io.containerd.runtime.v2.task/*/"$id"/config.json \
           /run/containers/storage/overlay-containers/"$id"/userdata/config.json; do
    [ -r "$f" ] || continue
    if have python3; then
      python3 -c 'import json, sys
e = (json.load(open(sys.argv[1])).get("process") or {}).get("env") or []
d = dict(x.split("=", 1) for x in e if "=" in x)
p = d.get("MARIADB_ROOT_PASSWORD") or d.get("MYSQL_ROOT_PASSWORD") or ""
fp = d.get("MARIADB_ROOT_PASSWORD_FILE") or d.get("MYSQL_ROOT_PASSWORD_FILE") or ""
if not p and fp.startswith("/"):
    try:
        p = open("/proc/" + sys.argv[2] + "/root" + fp).read().strip()
    except OSError:
        pass
sys.stdout.write(p)' "$f" "$1" 2>/dev/null
    else
      # without python only a value with no JSON escapes: a wrong password is worse than none
      grep -o '"\(MARIADB\|MYSQL\)_ROOT_PASSWORD=[^"\\]*"' "$f" | head -1 | sed 's/^"[A-Z_]*=//; s/"$//'
    fi
    return 0
  done
}
# a readable name: namespace/pod from the runtime's annotations, a docker container by its name, else
# the HOSTNAME of the container
my_pod_name() { # pid
  local id f ns pod
  id=$(grep -o '[0-9a-f]\{64\}' "/proc/$1/cgroup" 2>/dev/null | tail -1)
  if [ -n "$id" ] && have docker; then
    pod=$(docker inspect -f '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##')
    [ -n "$pod" ] && { printf '%s' "$pod"; return 0; }
  fi
  for f in /run/containerd/io.containerd.runtime.v2.task/*/"$id"/config.json \
           /run/*/containerd/io.containerd.runtime.v2.task/*/"$id"/config.json \
           /run/docker/containerd/daemon/io.containerd.runtime.v2.task/*/"$id"/config.json \
           /run/containers/storage/overlay-containers/"$id"/userdata/config.json; do
    [ -n "$id" ] && [ -r "$f" ] || continue
    ns=$(grep -o '"io.kubernetes.\(cri.sandbox-namespace\|pod.namespace\)":"[^"]*"' "$f" | head -1 | sed 's/.*:"//; s/"$//')
    pod=$(grep -o '"io.kubernetes.\(cri.sandbox-name\|pod.name\)":"[^"]*"' "$f" | head -1 | sed 's/.*:"//; s/"$//')
    break
  done
  [ -n "${pod:-}" ] || pod=$(tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null | sed -n 's/^HOSTNAME=//p' | head -1)
  if [ -n "${pod:-}" ]; then printf '%s%s' "${ns:+$ns/}" "$pod"; else printf 'pid %s' "$1"; fi
}
my_run() { # container flags query; flags "-N -B" (values only) or "-B" (with the header line)
  local c="$1" fl="$2" q="$3" p u g
  case "$c" in
    pid:*)
      p=${c#pid:}
      u=$(awk '/^Uid:/ {print $2}' "/proc/$p/status" 2>/dev/null); g=$(awk '/^Gid:/ {print $2}' "/proc/$p/status" 2>/dev/null)
      case "$u" in ''|0) u=65534; g=65534 ;; esac
      MYSQL_PWD="$(my_pod_pw "$p")" timeout $TO nsenter -t "$p" -m -n -p -S "$u" -G "${g:-$u}" -- \
        sh -c 'if command -v mysql >/dev/null 2>&1; then exec mysql "$@"; else exec mariadb "$@"; fi' sh -u root $fl -e "$q" 2>/dev/null
      ;;
    "")
      my_host_auth
      my_host "$fl" "$q"
      ;;
    *)
      # The client is chosen by what actually exists. mariadb:11 no longer ships a `mysql` command,
      # only `mariadb`, and the query failed silently into /dev/null: the engine appeared in the
      # report while not a single database, size or login did. MYSQL_PWD is a command PREFIX: a
      # separate assignment without export would never reach the client ("using password: NO")
      timeout $TO docker exec "$c" sh -c 'if command -v mysql >/dev/null 2>&1; then CLI=mysql; else CLI=mariadb; fi
        PW="${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}"
        F="${MARIADB_ROOT_PASSWORD_FILE:-${MYSQL_ROOT_PASSWORD_FILE:-}}"
        [ -z "$PW" ] && [ -n "$F" ] && PW=$(cat "$F" 2>/dev/null)
        MYSQL_PWD="$PW" exec "$CLI" -u root "$@"' sh $fl -e "$q" 2>/dev/null
      ;;
  esac
}
my_q() { my_run "$1" "-N -B" "$2"; }
# the name an instance is shown under: a pod by its name, the rest by the container
my_name() { case "$1" in pid:*) my_pod_name "${1#pid:}" ;; *) printf '%s' "$1" ;; esac; }

collect_mysql() {
  local c="$1" n v d u cu cm
  v=$(my_q "$c" "select version()" | head -1)
  # a version starts with a digit; anything else is docker's own error printed to stdout (a
  # postgres-exporter matches the image pattern but has no psql: "OCI runtime exec failed")
  case "$v" in [0-9]*) ;; *) return 0 ;; esac
  n=$(my_name "$c")
  d=$(my_q "$c" "select table_schema, ifnull(sum(data_length+index_length),0) from information_schema.tables where table_schema not in ('information_schema','performance_schema','sys') group by table_schema order by 2 desc" \
      | awk -F'\t' '{print $1"|"$2}' | dbs_json)
  u=$(my_q "$c" "select distinct user from mysql.user order by user" | list_json)
  # the same ceiling on the MySQL/MariaDB side: threads currently connected vs max_connections.
  # SHOW STATUS works with performance_schema off (MariaDB's default), its tables are empty then
  cu=$(my_q "$c" "show global status like 'Threads_connected'" | awk '{print $2}' | head -1)
  cm=$(my_q "$c" "select @@max_connections" | head -1)
  add_entry mysql "$n" "$v" "$d" "$u" "$cu" "$cm"
  health_mysql "$c" "$n"
}

# a column of a one-row result with a header, by a name pattern: Seconds_Behind_Source -> 12.
# Vertical output (\G) is no good here: with -N newer clients drop the field names there too, and
# until 0.11 a replica would have read as "IO and SQL threads stopped"
mcol() { printf '%s\n' "$1" | awk -F'\t' -v k="$2" 'NR == 1 { for (i = 1; i <= NF; i++) if ($i ~ k) c = i; next }
  NR == 2 && c { print $c; exit }'; }
# a value from "name<TAB>value" lines by its lower-case name
mval() { printf '%s\n' "$1" | awk -F'\t' -v k="$2" 'tolower($1) == k { print $2; exit }'; }

health_mysql() {
  local c="$1" n="$2" rs role io sq lag err nt xa xi lw st im hist ld cf
  rs=$(my_run "$c" -B "SHOW REPLICA STATUS")
  [ -z "$rs" ] && rs=$(my_run "$c" -B "SHOW SLAVE STATUS")
  if [ -n "$rs" ]; then
    role=replica
    io=$(mcol "$rs" '^(Replica|Slave)_IO_Running$'); sq=$(mcol "$rs" '^(Replica|Slave)_SQL_Running$')
    lag=$(mcol "$rs" '^Seconds_Behind_(Source|Master)$'); [ "$lag" = NULL ] && lag=-1
    err=$(mcol "$rs" '^Last_IO_Error$'); [ -n "$err" ] || err=$(mcol "$rs" '^Last_SQL_Error$')
  else
    role=primary; io=""; sq=""; lag=0; err=""
  fi
  nt=$(my_q "$c" "select count(*) from information_schema.innodb_trx" | head -1)
  # the oldest transaction and whose it is. A dump with --single-transaction holds one for its whole
  # run: its statements carry SQL_NO_CACHE, and the panel then waits 12 hours instead of one
  xa=$(my_q "$c" "select timestampdiff(second, t.trx_started, now()), ifnull(p.db, ''), if(p.info like '%SQL_NO_CACHE%', 'mysqldump', ifnull(p.user, '')) from information_schema.innodb_trx t left join information_schema.processlist p on p.id = t.trx_mysql_thread_id order by t.trx_started limit 1" | head -1)
  # an open transaction whose connection does nothing (Sleep): it holds its row locks and keeps
  # InnoDB from purging old row versions in the whole server, the MySQL side of "idle in transaction"
  xi=$(my_q "$c" "select timestampdiff(second, t.trx_started, now()), p.time, ifnull(p.db, ''), ifnull(p.user, '') from information_schema.innodb_trx t join information_schema.processlist p on p.id = t.trx_mysql_thread_id where p.command = 'Sleep' order by t.trx_started limit 1" | head -1)
  lw=$(my_q "$c" "select count(*), coalesce(max(timestampdiff(second, trx_wait_started, now())),0) from information_schema.innodb_trx where trx_state='LOCK WAIT'" | head -1)
  # the purge backlog (history list length): MariaDB has it as a status variable, MySQL in innodb_metrics
  st=$(my_q "$c" "SHOW GLOBAL STATUS")
  im=$(my_q "$c" "select name, count from information_schema.innodb_metrics where name in ('trx_rseg_history_len', 'lock_deadlocks') and status = 'enabled'")
  hist=$(mval "$st" innodb_history_list_length); [ -n "$hist" ] || hist=$(mval "$im" trx_rseg_history_len)
  ld=$(load_my "$c" "$n" "$st" "$im")
  case "$ld" in *',"top":['*) [ $((LOAD_BYTES + ${#ld})) -gt "$LOAD_BUDGET" ] && ld="${ld%%,\"top\":*},\"top\":[],\"cut\":1}" ;; esac
  LOAD_BYTES=$((LOAD_BYTES + ${#ld}))
  cf=$(cfg_my "$c")
  HEALTH="$HEALTH${HEALTH:+,}{\"engine\":\"mysql\",\"container\":\"$(esc "$n")\",\"role\":\"$role\",\"io\":\"$(esc "$io")\",\"sql\":\"$(esc "$sq")\",\"lag\":$(num "$lag"),\"err\":\"$(esc "$(printf '%s' "$err" | cut -c1-200)")\",\"trx\":$(num "$nt"),\"xact\":$(num "$(printf '%s' "$xa" | cut -f1)"),\"xact_db\":\"$(esc "$(printf '%s' "$xa" | cut -f2)")\",\"xact_app\":\"$(esc "$(printf '%s' "$xa" | cut -f3)")\",\"idle_xact\":$(num "$(printf '%s' "$xi" | cut -f1)"),\"idle_sleep\":$(num "$(printf '%s' "$xi" | cut -f2)"),\"idle_db\":\"$(esc "$(printf '%s' "$xi" | cut -f3)")\",\"idle_app\":\"$(esc "$(printf '%s' "$xi" | cut -f4)")\",\"lock_waits\":$(num "$(printf '%s' "$lw" | cut -f1)"),\"lock_wait\":$(num "$(printf '%s' "$lw" | cut -f2)"),\"hist\":$(num "${hist:--1}")${ld:+,\"load\":$ld}${cf:+,\"cfg\":$cf}}"
}

# ── What loads MySQL / MariaDB (0.11) ──────────────────────────────────────────
# The same approach as for PostgreSQL: a snapshot of the status counters every run, the difference
# with the newest one at least LOAD_BASE old. Statements come from performance_schema digests (texts
# normalized by the server: constants are "?"), when performance_schema is on - MySQL's default,
# MariaDB keeps it off. This helper's own statements (SHOW, information_schema, performance_schema,
# version()) are left out: on an idle server they would top the list
MY_CTR="uptime questions com_select com_insert com_update com_delete com_replace slow_queries created_tmp_tables created_tmp_disk_tables innodb_buffer_pool_reads innodb_buffer_pool_read_requests innodb_data_written innodb_os_log_written innodb_log_waits innodb_row_lock_waits innodb_row_lock_time innodb_deadlocks aborted_connects connection_errors_max_connections select_full_join sort_merge_passes bytes_sent bytes_received"
load_my() { # container name status innodb_metrics -> a JSON object on stdout
  local c="$1" n="$2" st="$3" im="$4" dir now ps snap ts age base="" fb="" win=0 reset line top msall pg miss
  dir="$LOAD_DIR/my-$(printf '%s' "${n:-host}" | tr -c 'A-Za-z0-9_.-' '_')"
  install -d -m 0700 "$LOAD_DIR" "$dir" 2>/dev/null || return 0
  [ -n "$(mval "$st" uptime)" ] || return 0
  now=$(date +%s)
  ps=off; [ "$(my_q "$c" "select @@performance_schema" | head -1)" = 1 ] && ps=on
  pg=$(num "$(mval "$st" innodb_page_size)"); [ "$pg" -gt 0 ] || pg=16384
  snap="$dir/$now.snap"
  {
    printf '%s\n' "$st" | awk -F'\t' -v keep="$MY_CTR" 'BEGIN { n = split(keep, a, " "); for (i = 1; i <= n; i++) w[a[i]] = 1 }
      { k = tolower($1) } (k in w) && $2 ~ /^[0-9]+$/ { print "C|" k "|" $2 }'
    printf '%s\n' "$im" | awk -F'\t' '$1 == "lock_deadlocks" && $2 ~ /^[0-9]+$/ { print "C|im_deadlocks|" $2 }'
    if [ "$ps" = on ]; then
      echo "P|1"
      my_q "$c" "select concat_ws('|', 'Q', ifnull(schema_name, ''), digest, count_star, round(sum_timer_wait / 1000000000), sum_rows_examined, sum_created_tmp_disk_tables, sum_no_index_used + sum_no_good_index_used) from performance_schema.events_statements_summary_by_digest where digest is not null and count_star > 0 and digest_text not like 'SHOW %' and digest_text not like '%information_schema%' and digest_text not like '%performance_schema%' and digest_text not like 'SELECT @@%' and digest_text not like concat('SELECT ', char(96), 'version', char(96), '%') and digest_text not like concat('%', char(96), 'mysql', char(96), ' . ', char(96), 'user', char(96), '%')"
    fi
  } > "$snap.tmp" && mv -f "$snap.tmp" "$snap"
  for ts in $(ls "$dir" | sed -n 's/^\([0-9][0-9]*\)\.snap$/\1/p' | sort -n); do
    age=$((now - ts))
    [ "$age" -gt 0 ] || continue
    [ -n "$fb" ] || fb=$ts
    [ "$age" -ge "$LOAD_BASE" ] && base=$ts
  done
  [ -n "$base" ] || base=$fb
  for ts in $(ls "$dir" | sed -n 's/^\([0-9][0-9]*\)\.snap$/\1/p'); do
    [ $((now - ts)) -gt "$LOAD_KEEP" ] && [ "$ts" != "$base" ] && rm -f "$dir/$ts.snap"
  done
  if [ -z "$base" ]; then
    printf '{"win":0,"ps":"%s"}' "$ps"
    return 0
  fi
  win=$((now - base))
  # differences. A counter below its base (a restart, FLUSH STATUS) - no window this time. A digest
  # missing from the base, or with fewer calls (the table was truncated or the row evicted), counts
  # from zero inside the window
  awk -F'|' '
    FILENAME == ARGV[1] { if ($1 == "C") b[$2] = $3
                else if ($1 == "P") havep = 1
                else if ($1 == "Q") { k = $2 FS $3; pc[k] = $4; pm[k] = $5; pr[k] = $6; pt[k] = $7; pn[k] = $8 }
                next }
    $1 == "C" { if (!($2 in b)) next
                d = $3 - b[$2]; if (d < 0) reset = 1
                printf "D|%s|%.0f\n", $2, d; next }
    $1 == "Q" && havep { k = $2 FS $3
                if ((k in pc) && $4 >= pc[k]) { dc = $4 - pc[k]; dm = $5 - pm[k]; dr = $6 - pr[k]; dt = $7 - pt[k]; dn = $8 - pn[k] }
                else { dc = $4; dm = $5; dr = $6; dt = $7; dn = $8 }
                if (dc <= 0) next
                all += dm
                if (dm > 0) printf "Q|%s|%.0f|%.0f|%.0f|%.0f|%.0f\n", k, dc, dm, dr, dt, dn }
    END { printf "S|%.0f|%d\n", all, reset }' "$dir/$base.snap" "$snap" > "$dir/delta"
  reset=$(sed -n 's/^S|[0-9]*|//p' "$dir/delta")
  msall=$(num "$(sed -n 's/^S|\([0-9]*\)|.*/\1/p' "$dir/delta")")
  if [ "$reset" = 1 ]; then
    rm -f "$dir/delta"
    printf '{"win":0,"ps":"%s"}' "$ps"
    return 0
  fi
  # "D|name|diff" -> "name":diff; a counter this version does not have stays out (the panel: unknown)
  line=$(awk -F'|' '$1 == "D" { printf ",\"%s\":%.0f", $2, $3 }' "$dir/delta")
  top=""
  if [ "$ps" = on ]; then
    # the top: TOP_TIME by time, plus up to TOP_TEMP more by temporary tables on disk
    { grep '^Q|' "$dir/delta" | sort -t'|' -k5,5nr | head -n "$TOP_TIME"
      grep '^Q|' "$dir/delta" | awk -F'|' '$7 > 0' | sort -t'|' -k7,7nr | head -n $((TOP_TIME + TOP_TEMP))
    } | awk -F'|' '!seen[$2 FS $3]++' | head -n $((TOP_TIME + TOP_TEMP)) > "$dir/top"
    miss=$(awk -F'|' '{ printf "%s'\''%s'\''", (n++ ? "," : ""), $3 }' "$dir/top" | tr -cd "0-9a-f,'")
    if [ -n "$miss" ]; then
      # texts of the top only; one that mentions a password or a secret is not sent at all
      my_q "$c" "select concat_ws('|', digest, case when lower(digest_text) regexp 'passw|secret|token|identified|aes_encrypt|des_encrypt' then '' else left(digest_text, $QTEXT_MAX) end) from performance_schema.events_statements_summary_by_digest where digest in ($miss)" > "$dir/texts"
    fi
    top=$(awk -F'|' 'function j(s,   _jp, _jn, _ji) { _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
      FILENAME == ARGV[1] { d = $1; q = substr($0, length($1) + 2); if (!(d in tx)) tx[d] = q; next }
      { printf "%s{\"id\":\"%s\",\"db\":\"%s\",\"q\":\"%s\",\"calls\":%.0f,\"ms\":%.0f,\"rows\":%.0f,\"tmp\":%.0f,\"noidx\":%.0f}", (n++ ? "," : ""), j($3), j($2), j(tx[$3]), $4, $5, $6, $7, $8 }' "$dir/texts" "$dir/top" 2>/dev/null | tr -d '\000-\037')
  fi
  rm -f "$dir/delta" "$dir/top" "$dir/texts"
  printf '{"win":%s,"ps":"%s"%s,"page":%s,"run":%s,"max_used":%s,"ms_all":%s,"top":[%s]}' "$win" "$ps" "$line" "$pg" \
    "$(num "$(mval "$st" threads_running)")" "$(num "$(mval "$st" max_used_connections)")" "$msall" "$top"
}

# ── MySQL / MariaDB settings against the machine (0.11) ────────────────────────
# SHOW VARIABLES WHERE ... IN returns only what this version has (innodb_redo_log_capacity is MySQL
# 8.0.30+, innodb_log_files_in_group is gone there): a select of a missing variable would fail whole.
# Memory: the container limit when there is one (docker, or the cgroup of a pod), else the machine.
# Binary logs: their total size, they fill the disk when nothing expires them
my_cg_limit() { # pid -> the memory limit of its cgroup or its parent (a pod), 0 - none
  local p="$1" cg v best=0 d
  cg=$(sed -n 's/^0:://p' "/proc/$p/cgroup" 2>/dev/null | head -1)
  if [ -n "$cg" ] && [ -d "/sys/fs/cgroup$cg" ]; then
    for d in "/sys/fs/cgroup$cg" "/sys/fs/cgroup${cg%/*}"; do
      v=$(cat "$d/memory.max" 2>/dev/null)
      case "$v" in ''|max|*[!0-9]*) continue ;; esac
      { [ "$best" = 0 ] || [ "$v" -lt "$best" ]; } && best=$v
    done
  else
    cg=$(awk -F: '$2 ~ /(^|,)memory(,|$)/ {print $3; exit}' "/proc/$p/cgroup" 2>/dev/null)
    v=$(cat "/sys/fs/cgroup/memory$cg/memory.limit_in_bytes" 2>/dev/null)
    case "$v" in ''|*[!0-9]*) ;; *) [ "$v" -lt 4611686018427387904 ] && best=$v ;; esac
  fi
  echo "$best"
}
cfg_my() { # container -> a JSON object, nothing when the engine does not answer
  local c="$1" vars mem lim=0 sto="-1" bl p rt=docker
  vars=$(my_q "$c" "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('version', 'innodb_buffer_pool_size', 'innodb_log_file_size', 'innodb_log_files_in_group', 'innodb_redo_log_capacity', 'innodb_log_buffer_size', 'innodb_flush_log_at_trx_commit', 'sync_binlog', 'log_bin', 'max_connections', 'tmp_table_size', 'max_heap_table_size', 'performance_schema', 'slow_query_log', 'binlog_expire_logs_seconds', 'expire_logs_days', 'innodb_flush_method')")
  [ -n "$(mval "$vars" innodb_buffer_pool_size)" ] || return 0
  mem=$(awk '/^MemTotal:/ {printf "%.0f", $2 * 1024}' /proc/meminfo)
  case "$c" in
    pid:*) rt=pod; lim=$(my_cg_limit "${c#pid:}") ;;
    "") rt=host ;;
    *)
      IFS='|' read -r sto lim p < <(docker inspect -f '{{if .Config.StopTimeout}}{{.Config.StopTimeout}}{{else}}-1{{end}}|{{.HostConfig.Memory}}|{{.State.Pid}}' "$c" 2>/dev/null)
      [ "$(num "${lim:-0}")" -gt 0 ] || lim=$(my_cg_limit "${p:-0}")
      ;;
  esac
  [ "$(num "${lim:-0}")" -gt 0 ] && mem=$lim
  bl=0
  [ "$(mval "$vars" log_bin)" = ON ] && bl=$(my_q "$c" "SHOW BINARY LOGS" | awk -F'\t' '$2 ~ /^[0-9]+$/ { s += $2 } END { printf "%.0f", s }')
  printf '%s\n' "$vars" | awk -F'\t' -v mem="$(num "$mem")" -v lim="$( [ "$(num "${lim:-0}")" -gt 0 ] && echo true || echo false)" \
      -v sto="$(num "${sto:--1}")" -v bl="$(num "$bl")" -v rt="$rt" '
    function j(s,   _jp, _jn, _ji) { _jn = split(s, _jp, "\\"); s = _jp[1]; for (_ji = 2; _ji <= _jn; _ji++) s = s "\\\\" _jp[_ji]; gsub(/"/, "\\\"", s); return s }
    NF >= 2 { printf "%s\"%s\":\"%s\"", (n++ ? "," : "{"), j(tolower($1)), j($2) }
    END { printf "%s\"mem\":%s,\"mem_lim\":%s,\"stop_to\":%s,\"binlog_b\":%s,\"rt\":\"%s\"}", (n ? "," : "{"), mem, lim, sto, bl, rt }'
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
  health_ch "$c"
}

# ── ClickHouse health (0.10) ───────────────────────────────────────────────────
# What breaks ClickHouse writes: too many parts in one partition (from parts_to_delay_insert inserts
# slow down, from parts_to_throw_insert they fail with TOO_MANY_PARTS), inserts already rejected or
# delayed since the last run, a replica gone read-only or lagging, a replication queue that stopped,
# mutations that fail or hang. Plus what loads it over the last hour, from system.query_log where
# query logging is on: normalized texts (normalizeQuery, literals become ?), the ones that look like
# a secret are not sent. Insert counters since the last run come from a small state file per
# instance in LOAD_DIR (the engine counts them since its start)
TOP_CH=5
# several queries in one docker exec: on one node there are dozens of ClickHouse instances, and an
# exec per query took longer than the queries. Every query returns exactly one row, so the answer
# is read by line number
ch_mq() { # container queries
  if [ -n "$1" ]; then timeout $TO docker exec "$1" clickhouse-client -n --format TSV --query "$2" 2>/dev/null
  else timeout $TO clickhouse-client -n --format TSV --query "$2" 2>/dev/null; fi
}
ch_f() { printf '%s' "$1" | cut -f"$2"; }
health_ch() {
  local c="$1" f now out up pm lim mu rp rq det mg ev ci cf cr cd pt pu pi pf pr pd
  local win=0 ins=0 fail=0 rej=0 del=0 ql=false qe=0 top="" cut=0 entry
  f="$LOAD_DIR/ch-$(printf '%s' "${c:-host}" | tr -c 'A-Za-z0-9_.-' '_')"
  install -d -m 0700 "$LOAD_DIR" 2>/dev/null || return 0
  now=$(date +%s)
  out=$(ch_mq "$c" "select uptime();
select max(c), argMax(t, c), argMax(p, c) from (select database || '.' || table as t, partition_id as p, count() as c from system.parts where active group by t, p);
select anyIf(value, name = 'parts_to_delay_insert'), anyIf(value, name = 'parts_to_throw_insert') from system.merge_tree_settings;
select count(), if(count() = 0, 0, toUInt32(now()) - toUInt32(min(create_time))), any(database || '.' || table), replaceRegexpAll(substring(anyIf(latest_fail_reason, latest_fail_reason != ''), 1, 200), '[\t\n\r]+', ' ') from system.mutations where not is_done;
select count(), countIf(is_readonly), max(absolute_delay), max(queue_size), anyIf(database || '.' || table, is_readonly) from system.replicas;
select count(), if(count() = 0, 0, toUInt32(now()) - toUInt32(min(create_time))), replaceRegexpAll(substring(anyIf(last_exception, last_exception != ''), 1, 200), '[\t\n\r]+', ' ') from system.replication_queue;
select count() from system.detached_parts;
select count(), toUInt32(max(elapsed)) from system.merges;
select sumIf(value, event = 'InsertQuery'), sumIf(value, event = 'FailedInsertQuery'), sumIf(value, event = 'RejectedInserts'), sumIf(value, event = 'DelayedInserts') from system.events;
select count() from system.tables where database = 'system' and name = 'query_log'")
  # a query that failed stops the rest: without all ten lines nothing here can be trusted
  [ "$(printf '%s\n' "$out" | grep -c .)" = 10 ] || return 0
  up=$(num "$(printf '%s\n' "$out" | sed -n 1p)")
  [ "$up" -gt 0 ] || return 0
  pm=$(printf '%s\n' "$out" | sed -n 2p); lim=$(printf '%s\n' "$out" | sed -n 3p)
  mu=$(printf '%s\n' "$out" | sed -n 4p); rp=$(printf '%s\n' "$out" | sed -n 5p)
  rq=$(printf '%s\n' "$out" | sed -n 6p); det=$(printf '%s\n' "$out" | sed -n 7p)
  mg=$(printf '%s\n' "$out" | sed -n 8p); ev=$(printf '%s\n' "$out" | sed -n 9p)
  ci=$(num "$(ch_f "$ev" 1)"); cf=$(num "$(ch_f "$ev" 2)"); cr=$(num "$(ch_f "$ev" 3)"); cd=$(num "$(ch_f "$ev" 4)")
  if [ -s "$f" ]; then
    IFS='|' read -r pt pu pi pf pr pd < "$f"
    pt=$(num "$pt"); pu=$(num "$pu")
    # a smaller uptime - the engine restarted and counts from zero: no difference this time
    if [ "$up" -ge "$pu" ] && [ "$now" -gt "$pt" ] && [ $((now - pt)) -le 7200 ]; then
      win=$((now - pt)); ins=$((ci - $(num "$pi"))); fail=$((cf - $(num "$pf")))
      rej=$((cr - $(num "$pr"))); del=$((cd - $(num "$pd")))
    fi
  fi
  printf '%s|%s|%s|%s|%s|%s\n' "$now" "$up" "$ci" "$cf" "$cr" "$cd" > "$f"
  if [ "$(printf '%s\n' "$out" | sed -n 10p)" = 1 ]; then
    ql=true
    out=$(ch_mq "$c" "select count() from system.query_log where event_time > now() - 3600 and type in ('ExceptionBeforeStart', 'ExceptionWhileProcessing');
select count(), sum(query_duration_ms), sum(read_bytes), max(memory_usage), replaceRegexpAll(substring(normalizeQuery(any(query)), 1, $QTEXT_MAX), '[\t\n\r]+', ' ') from system.query_log where event_time > now() - 3600 and type = 'QueryFinish' group by normalized_query_hash order by 2 desc limit $TOP_CH")
    qe=$(num "$(printf '%s\n' "$out" | sed -n 1p)")
    top=$(printf '%s\n' "$out" | sed 1d | awk -F'\t' 'BEGIN { n = 0 }
      NF >= 5 && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ && $4 ~ /^[0-9]+$/ {
        q = $5; if (tolower(q) ~ /passw|secret|token|identified|encrypt/) q = ""
        gsub(/\\/, " ", q); gsub(/"/, "'"'"'", q); gsub(/[\001-\037]/, " ", q)
        printf "%s{\"n\":%s,\"ms\":%s,\"rd\":%s,\"mem\":%s,\"q\":\"%s\"}", (n++ ? "," : ""), $1, $2, $3, $4, q }')
    # the agent drops a report.d file over 256 KB whole: the texts share the budget with PostgreSQL
    if [ $((LOAD_BYTES + ${#top})) -gt "$LOAD_BUDGET" ]; then top=""; cut=1; fi
    LOAD_BYTES=$((LOAD_BYTES + ${#top}))
  fi
  entry="{\"engine\":\"clickhouse\",\"container\":\"$(esc "$c")\",\"role\":\"\",\"replicas\":0,\"lag\":0"
  entry="$entry,\"parts\":{\"max\":$(num "$(ch_f "$pm" 1)"),\"tbl\":\"$(esc "$(ch_f "$pm" 2)")\",\"part\":\"$(esc "$(ch_f "$pm" 3)")\",\"delay\":$(num "$(ch_f "$lim" 1)"),\"throw\":$(num "$(ch_f "$lim" 2)")}"
  entry="$entry,\"mut\":{\"n\":$(num "$(ch_f "$mu" 1)"),\"age\":$(num "$(ch_f "$mu" 2)"),\"tbl\":\"$(esc "$(ch_f "$mu" 3)")\",\"err\":\"$(esc "$(ch_f "$mu" 4)")\"}"
  entry="$entry,\"rep\":{\"n\":$(num "$(ch_f "$rp" 1)"),\"ro\":$(num "$(ch_f "$rp" 2)"),\"delay\":$(num "$(ch_f "$rp" 3)"),\"queue\":$(num "$(ch_f "$rp" 4)"),\"ro_tbl\":\"$(esc "$(ch_f "$rp" 5)")\",\"q_n\":$(num "$(ch_f "$rq" 1)"),\"q_age\":$(num "$(ch_f "$rq" 2)"),\"q_err\":\"$(esc "$(ch_f "$rq" 3)")\"}"
  entry="$entry,\"detached\":$(num "$det"),\"merges\":$(num "$(ch_f "$mg" 1)"),\"merge_max\":$(num "$(ch_f "$mg" 2)")"
  entry="$entry,\"ins\":{\"win\":$win,\"n\":$ins,\"fail\":$fail,\"rej\":$rej,\"del\":$del},\"qlog\":$ql,\"qerr\":$qe,\"top\":[$top],\"cut\":$cut}"
  HEALTH="$HEALTH${HEALTH:+,}$entry"
}

# ── Redis ──────────────────────────────────────────────────────────────────────
# Redis "databases" are db0..dbN; instead of a size we show the key count (only a total
# size exists, via used_memory, and it is not broken down per database).
#
# A Redis with a password (0.10): compose usually puts --requirepass into the container command,
# sometimes it is REDIS_PASSWORD in the environment or requirepass in the config file the server
# starts with. It is read here and handed to redis-cli on stdin (REDISCLI_AUTH), so it never shows
# up in the process list, and it does not leave the node
RDPW=""
rd_pw() { # container -> the password or nothing
  local c="$1" cmd pw conf
  if [ -z "$c" ]; then
    [ -r /etc/redis/redis.conf ] && awk '$1 == "requirepass" {print $2; exit}' /etc/redis/redis.conf | tr -d "\"'"
    return 0
  fi
  cmd=$(docker inspect -f '{{range .Config.Cmd}}{{println .}}{{end}}' "$c" 2>/dev/null | tr ' \t' '\n\n')
  pw=$(printf '%s\n' "$cmd" | awk 'p { print; exit } $0 == "--requirepass" { p = 1; next } /^--requirepass=/ { sub(/^--requirepass=/, ""); print; exit }' | tr -d "\"'")
  [ -z "$pw" ] && pw=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$c" 2>/dev/null | sed -n 's/^REDIS_PASSWORD=//p' | head -1)
  if [ -z "$pw" ]; then
    conf=$(printf '%s\n' "$cmd" | grep -m1 '\.conf$')
    [ -n "$conf" ] && pw=$(timeout $TO docker exec "$c" cat "$conf" 2>/dev/null | awk '$1 == "requirepass" {print $2; exit}' | tr -d "\"'")
  fi
  printf '%s' "$pw"
}
rd_cli() { # container args... -> redis-cli output; the password (RDPW) goes on stdin
  local c="$1"; shift
  if [ -z "$RDPW" ]; then
    if [ -n "$c" ]; then timeout $TO docker exec "$c" redis-cli "$@" 2>/dev/null
    else timeout $TO redis-cli "$@" 2>/dev/null; fi
  elif [ -n "$c" ]; then
    printf '%s\n' "$RDPW" | timeout $TO docker exec -i "$c" sh -c 'read -r REDISCLI_AUTH; export REDISCLI_AUTH; exec redis-cli "$@"' sh "$@" 2>/dev/null
  else
    printf '%s\n' "$RDPW" | timeout $TO sh -c 'read -r REDISCLI_AUTH; export REDISCLI_AUTH; exec redis-cli "$@"' sh "$@" 2>/dev/null
  fi
}
rd_q() { rd_cli "$1" info "$2"; }
collect_redis() {
  local c="$1" v d cl
  RDPW=$(rd_pw "$c")
  v=$(rd_q "$c" server | awk -F: '/^redis_version:/{print $2}' | tr -d '\r' | head -1)
  # a version starts with a digit; anything else is docker's own error printed to stdout (a
  # postgres-exporter matches the image pattern but has no psql: "OCI runtime exec failed")
  case "$v" in [0-9]*) ;; *) RDPW=""; return 0 ;; esac
  d=$(rd_q "$c" keyspace | tr -d '\r' | awk -F'[:,=]' '/^db[0-9]+:/{print $1"|"$3}' | dbs_json)
  cl=$(rd_q "$c" clients | tr -d '\r')
  add_entry redis "$c" "$v" "$d" "[]" "$(num "$(printf '%s\n' "$cl" | awk -F: '$1 == "connected_clients" {print $2}')")" \
    "$(num "$(printf '%s\n' "$cl" | awk -F: '$1 == "maxclients" {print $2}')")"
  health_redis "$c"
  RDPW=""
}

# ── Redis health (0.10) ────────────────────────────────────────────────────────
# What makes Redis refuse writes or lose data: a snapshot or AOF that cannot be written (with
# stop-writes-on-bgsave-error, the default, every write then fails with MISCONF), memory at
# maxmemory under noeviction (OOM on every write), a replica that lost its primary, connections
# refused at maxclients. Plus the heaviest commands since the last run, from INFO commandstats: only
# command names and time, no keys or values
TOP_RD=5
health_redis() {
  local c="$1" f now inf up role link down rep mem maxm pol rdb chg last aof aofs evk rjc sw cs
  local pt pu pe pr win=0 ev=0 rj=0 cmds="" age=-1
  f="$LOAD_DIR/rd-$(printf '%s' "${c:-host}" | tr -c 'A-Za-z0-9_.-' '_')"
  install -d -m 0700 "$LOAD_DIR" 2>/dev/null || return 0
  now=$(date +%s)
  inf=$(rd_cli "$c" info all | tr -d '\r')
  up=$(num "$(printf '%s\n' "$inf" | awk -F: '$1 == "uptime_in_seconds" {print $2; exit}')")
  [ "$up" -gt 0 ] || return 0
  rg() { printf '%s\n' "$inf" | awk -F: -v k="$1" '$1 == k {print $2; exit}'; }
  role=$(rg role); link=$(rg master_link_status); down=$(num "$(rg master_link_down_since_seconds)")
  rep=$(num "$(rg connected_slaves)"); mem=$(num "$(rg used_memory)"); maxm=$(num "$(rg maxmemory)")
  pol=$(rg maxmemory_policy); rdb=$(rg rdb_last_bgsave_status); chg=$(num "$(rg rdb_changes_since_last_save)")
  last=$(num "$(rg rdb_last_save_time)"); aof=$(num "$(rg aof_enabled)"); aofs=$(rg aof_last_write_status)
  evk=$(num "$(rg evicted_keys)"); rjc=$(num "$(rg rejected_connections)")
  [ "$last" -gt 0 ] && age=$((now - last))
  # CONFIG may be renamed or disabled - then the flag stays unknown
  sw=$(rd_cli "$c" config get stop-writes-on-bgsave-error | tr -d '\r' | sed -n 2p)
  case "$sw" in yes|no) ;; *) sw="" ;; esac
  # cmdstat_get:calls=10,usec=20,usec_per_call=2.00,... -> "get 10 20". INFO and CONFIG are this
  # helper's own: on an idle Redis they would top the list
  cs=$(printf '%s\n' "$inf" | awk -F'[:,=]' '/^cmdstat_/ { sub(/^cmdstat_/, "", $1); gsub(/[^A-Za-z0-9_|-]/, "", $1); if ($1 ~ /^(info|config)($|[|])/) next; if ($3 ~ /^[0-9]+$/ && $5 ~ /^[0-9]+$/) print $1, $3, $5 }')
  if [ -s "$f" ]; then
    read -r pt pu pe pr < "$f"
    pt=$(num "$pt"); pu=$(num "$pu")
    if [ "$up" -ge "$pu" ] && [ "$now" -gt "$pt" ] && [ $((now - pt)) -le 7200 ]; then
      win=$((now - pt)); ev=$((evk - $(num "$pe"))); rj=$((rjc - $(num "$pr")))
      cmds=$(printf '%s\n' "$cs" | awk 'FILENAME == ARGV[1] { if (FNR > 1) { pc[$1] = $2; pu[$1] = $3 } next }
          NF == 3 { dc = $2 - pc[$1]; du = $3 - pu[$1]; if (dc > 0 && du > 0) print du, dc, $1 }' "$f" - \
        | sort -rn | head -"$TOP_RD" | awk 'BEGIN { n = 0 } { printf "%s{\"c\":\"%s\",\"n\":%s,\"us\":%s}", (n++ ? "," : ""), $3, $2, $1 }')
    fi
  fi
  { printf '%s %s %s %s\n' "$now" "$up" "$evk" "$rjc"; printf '%s\n' "$cs"; } > "$f"
  case "$role" in master) role=primary ;; slave) role=replica ;; esac
  HEALTH="$HEALTH${HEALTH:+,}{\"engine\":\"redis\",\"container\":\"$(esc "$c")\",\"role\":\"$(esc "$role")\",\"replicas\":$rep,\"lag\":0,\"link\":\"$(esc "$link")\",\"down\":$down,\"mem\":$mem,\"maxmem\":$maxm,\"policy\":\"$(esc "$pol")\",\"rdb\":\"$(esc "$rdb")\",\"rdb_changes\":$chg,\"rdb_age\":$age,\"aof\":$aof,\"aof_status\":\"$(esc "$aofs")\",\"stop_writes\":\"$sw\",\"win\":$win,\"evicted\":$ev,\"rejected\":$rj,\"cmds\":[$cmds]}"
}

# -- sweep: containers by image plus host processes --------------------------------
MY_NS=""  # mount namespaces of the MySQL containers docker already went through
if have docker; then
  docker ps --format '{{.Names}}|{{.Image}}' 2>/dev/null | while IFS='|' read -r name img; do
    echo "$name|$img"
  done > /tmp/kv-dbs.$$ 2>/dev/null || : > /tmp/kv-dbs.$$
  while IFS='|' read -r name img; do
    [ -n "$name" ] || continue
    case "$(printf '%s' "$img" | tr 'A-Z' 'a-z')" in
      *postgres*|*timescale*|*pgvector*) collect_pg "$name" ;;
      *mariadb*|*mysql*|*percona*)       collect_mysql "$name"
                                         MY_NS="$MY_NS $(readlink "/proc/$(docker inspect -f '{{.State.Pid}}' "$name" 2>/dev/null)/ns/mnt" 2>/dev/null)" ;;
      *clickhouse*)                      collect_ch "$name" ;;
      *redis*|*valkey*)                  collect_redis "$name" ;;
    esac
  done < /tmp/kv-dbs.$$
  rm -f /tmp/kv-dbs.$$
fi
# host engines (not in docker) - only if one is actually listening locally
pgrep -x postgres        >/dev/null 2>&1 && have psql             && collect_pg ""
# MySQL/MariaDB by the server process: one in the host's own mount namespace is a host engine, one in
# a container docker did not go through (a Kubernetes pod, an image named otherwise) is reached inside
# its namespaces. Pods on other nodes are not visible from here
HOSTMNT=$(readlink /proc/1/ns/mnt)
MYHOST=""
for p in $(pgrep -x mysqld; pgrep -x mariadbd); do
  pns=$(readlink "/proc/$p/ns/mnt" 2>/dev/null) || continue
  if [ "$pns" = "$HOSTMNT" ]; then MYHOST=1; continue; fi
  case " $MY_NS " in *" $pns "*) continue ;; esac
  MY_NS="$MY_NS $pns"
  have nsenter && collect_mysql "pid:$p"
done
[ -n "$MYHOST" ] && { have mysql || have mariadb; } && collect_mysql ""
pgrep -x redis-server    >/dev/null 2>&1 && have redis-cli        && collect_redis ""
pgrep -x clickhouse-serv >/dev/null 2>&1 && have clickhouse-client && collect_ch ""

# snapshots of instances that are gone (a pod got a new name, a container was removed)
find "$LOAD_DIR" -mindepth 1 -maxdepth 1 -mtime +2 -exec rm -rf {} + 2>/dev/null

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
