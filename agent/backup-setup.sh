#!/usr/bin/env bash
# Kervax: enable restic backup CONTROL from the panel (phase 2).
#
# Running as the unprivileged `kervax` with NoNewPrivileges, the agent cannot use sudo and
# cannot edit the backup configuration (a 0700 script, a root timer). So:
#  * a root cron job writes the current configuration (mode, schedule, paths) into
#    /var/lib/kervax/backup-config.json - the agent reads that file and shows "Control" in the
#    panel, without secrets;
#  * commands go THROUGH A SPOOL: the agent drops a request into /var/lib/kervax/backup-req and
#    a root path unit runs a narrow helper, writing the answer to /var/lib/kervax/backup-res.
# The agent stays fully isolated (no privilege escalation anywhere). Actions come from a strict
# allowlist, names and times are validated. Run as root on the client node.
set -euo pipefail

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-backup-helper"
STATE_DIR=/var/lib/kervax
CONF_JSON="$STATE_DIR/backup-config.json"
REQ_DIR="$STATE_DIR/backup-req"
RES_DIR="$STATE_DIR/backup-res"
CRON=/etc/cron.d/kervax-backup
AGENT_USER=kervax

# v2: paths are written and read only for the active mode (include OR exclude) - get-config
# used to hand the panel the same list in both fields
# v3: dump-setup - local database dumps into /backup/<engine> before the file backup
# v4: redis, rabbitmq and k8s engines (backing up a k0s/k3s cluster together with etcd)
# v5: get-config reports the state of enabled dumps (the panel shows status, not a one-off
#     "enabled")
# v6: dump-remove - disable the dump of ONE engine without touching the others
# v7: pg_dumpall takes the role from the container's POSTGRES_USER; mysqldump takes the password
#     from its environment
# v8: dumps are PER CONTAINER (engine@container): two postgres instances do not interfere
# HISTORY (the version is the edit date, YYYYMMDD; see KERVAX_SETUP_VERSION below)
# v9: atomic writes (.partial plus gzip -t): a truncated dump is never left looking complete
# v10: configurable dir/keep/minfree plus overflow protection (skip when space runs short)
# v11: enabling runs a light availability probe instead of a full dump; enabled_ts for the grace
#      period
# v12: pg - each database into its own file plus globals.sql.gz; rotation per run
# v13: restic-update - updating restic to 0.19.1 (sha256 verification, atomic replacement)
# v14: dumps WITHOUT a file backup - their own kervax-dumps.timer (a local copy on the node that
#      can be restored from); the helper is installed EVERYWHERE (ALWAYS): it is the panel's
#      transport for dump control, and without restic it simply did not exist before
KERVAX_SETUP_VERSION=0.23  # MAJOR.MINOR; compared component-wise (0.13 > 0.2!)
KERVAX_SETUP_ALWAYS=1  # safe on any node: installs only its own helper and spool and does not
                       # touch the backup configuration until the panel sends a command
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" /var/lib/kervax/versions
echo "$KERVAX_SETUP_VERSION" > /var/lib/kervax/versions/backup-setup.ver
chmod 0644 /var/lib/kervax/versions/backup-setup.ver  # explicit: the agent (kervax) must read it
# the file and spool scheme needs no sudo - drop the old sudoers rule (if any)
rm -f /etc/sudoers.d/kervax-backup 2>/dev/null || true
# spool: the agent (kervax) drops requests (needs -wx) and reads answers (needs r-x)
install -d -o root -g "$AGENT_USER" -m 0730 "$REQ_DIR"
# 0770: the agent (kervax) must DELETE an answer once read, otherwise res files pile up
install -d -o root -g "$AGENT_USER" -m 0770 "$RES_DIR"
# under ProtectSystem=strict the agent may write only to its own bin directory, so the spool is
# allowed explicitly; otherwise /var/lib/kervax is read-only and the spool does not work.
# A drop-in plus an agent restart.
if systemctl cat kervax-agent >/dev/null 2>&1; then
  install -d -m 0755 /etc/systemd/system/kervax-agent.service.d
  cat > /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf <<'DROPIN'
[Service]
ReadWritePaths=/var/lib/kervax
DROPIN
  systemctl daemon-reload 2>/dev/null || true
  systemctl try-restart kervax-agent 2>/dev/null || true
fi

cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Kervax backup helper (root) - narrow operations on the restic backup. get-config (read),
# set-paths/set-schedule/run-now (write), process-spool (execute requests from the spool).
set -euo pipefail

HELPER_VER=1  # backup-helper version; the panel flags nodes with an old helper (reinstall it)
REQ_DIR=/var/lib/kervax/backup-req
RES_DIR=/var/lib/kervax/backup-res
CONF_JSON=/var/lib/kervax/backup-config.json
SCRIPT_CANDS=("/etc/systemd-rest.conf")
TIMER_CANDS=("systemd-rest.timer" "restic-backup.timer" "restic.timer")
# provisioning (creating a backup from scratch) - the target artefacts are EXACTLY those the agent detects
RESTIC_BIN_DIR=/usr/local/lib/.restic
RESTIC_BIN="$RESTIC_BIN_DIR/restic"
PROV_ENV=/etc/systemd-resta.conf
PROV_SCRIPT=/etc/systemd-rest.conf
PROV_SERVICE=systemd-rest.service
PROV_TIMER=systemd-rest.timer
PROV_USER=bx231
PROV_HOME=/var/cache/bx231
PROV_METRICS_DIR=/var/lib/node_exporter/textfile_collector
PROV_METRICS_FILE=restic-backup.prom
PROV_CACERT="$RESTIC_BIN_DIR/backup-ca.pem"

find_script() { for f in "${SCRIPT_CANDS[@]}"; do [ -f "$f" ] && { echo "$f"; return; }; done; }
find_timer()  { for t in "${TIMER_CANDS[@]}"; do
  [ -e "/etc/systemd/system/$t" ] && { echo "$t"; return; }
  systemctl cat "$t" >/dev/null 2>&1 && { echo "$t"; return; }
done; }
valid_path() { case "$1" in
  /*[!A-Za-z0-9._/+-]*) return 1 ;; *..*) return 1 ;; /*) return 0 ;; *) return 1 ;;
esac; }
json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

# ------- local database dumps: restic picks them up with the ordinary file backup -------
# A file snapshot of a live database may fail to restore, so a proper dump is placed next to it.
# Passwords are NOT needed and NOT stored: containers are entered with `docker exec` as a
# service user, native engines with `sudo -u <user>`, always over a local socket with peer
# authentication.
DUMP_DIR_BASE=/backup
# paths are written LITERALLY: this is the helper body inside a quoted heredoc, so installer
# variables (HELPER_DIR and friends) are not substituted here and do not exist at runtime
DUMPS_D=/lib65/kervax/dumps.d
RUN_DUMPS=/lib65/kervax/run-dumps.sh
DUMP_KEEP=2  # keep the last 2: restic holds the history, there is no point duplicating it on disk
DUMP_HOUR=3  # the hour of the dumps' own timer (when the node has no file backup)

# run-dumps.sh runs every enabled dump. It is attached to the backup service as
# ExecStartPre=- (note the minus): a failed dump must NOT cancel the file backup.
write_run_dumps() {
  install -d -m 0755 "$DUMPS_D"
  cat > "$RUN_DUMPS" <<'RD_EOF'
#!/usr/bin/env bash
# Kervax: run the enabled database dumps before the file backup. A failure of one engine is not
# fatal - the others, and the backup itself, must still run.
set -u
for d in /lib65/kervax/dumps.d/*.sh; do
  [ -x "$d" ] || continue
  "$d" || echo "kervax-dump: $(basename "$d") finished with an error" >&2
done
exit 0
RD_EOF
  chmod 0755 "$RUN_DUMPS"; chown root:root "$RUN_DUMPS"
  # WHAT TRIGGERS IT. If a file backup exists, we hook onto it (the dump runs right before
  # restic so it picks up a fresh one). If there is no backup, the dumps are still needed: a
  # local copy on the node itself that can be restored from. In that case we bring up
  # OUR OWN timer. The second case used to be ignored: the drop-in was written for a service
  # that did not exist, the dump never ran, and the panel still showed "enabled".
  if systemctl cat "$PROV_SERVICE" >/dev/null 2>&1; then
    local dir="/etc/systemd/system/$PROV_SERVICE.d"
    install -d -m 0755 "$dir"
    # prefixes: "+" means run WITH FULL PRIVILEGES (the backup service runs unprivileged while
    # the dump scripts are root:0700 - without "+" the loop silently found nothing and exited
    # with 0); "-" means a failed dump does NOT cancel the file backup.
    cat > "$dir/kervax-dumps.conf" <<EOF
[Service]
ExecStartPre=-+$RUN_DUMPS
EOF
    chmod 0644 "$dir/kervax-dumps.conf"
    # our own timer is no longer needed: the dumps ride along with the backup, otherwise they would run twice
    systemctl disable --now kervax-dumps.timer >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/kervax-dumps.timer /etc/systemd/system/kervax-dumps.service
  else
    cat > /etc/systemd/system/kervax-dumps.service <<EOF
[Unit]
Description=Kervax: local database dumps (this node has no file backup)
[Service]
Type=oneshot
ExecStart=$RUN_DUMPS
EOF
    cat > /etc/systemd/system/kervax-dumps.timer <<EOF
[Unit]
Description=Kervax: daily local database dumps
[Timer]
OnCalendar=*-*-* ${DUMP_HOUR}:00
RandomizedDelaySec=15min
Persistent=true
[Install]
WantedBy=timers.target
EOF
    chmod 0644 /etc/systemd/system/kervax-dumps.service /etc/systemd/system/kervax-dumps.timer
    systemctl daemon-reload 2>/dev/null || true
    systemctl enable --now kervax-dumps.timer >/dev/null 2>&1 || true
  fi
  systemctl daemon-reload 2>/dev/null || true
}

# A quick availability probe before enabling, instead of a full trial dump. On a large database
# a full dump takes minutes: the panel timed out and reported an "error" while the dump was
# still running. Here only availability and permissions are checked (schema-only/ping): it
# catches the main silent failures (role, password, access) without pulling data. The real dump
# runs on the backup schedule. k8s is not probed: a cluster snapshot is heavy and there is no
# light equivalent.
# Grafana: its state is the SQLite file grafana.db. We look both on the host and in volumes:
# under kubernetes the database lives in a PVC (local-path puts it straight onto the node's
# disk), so "a process on the host" does not mean "installed from a package".
grafana_db_path() {
  local p
  for p in /var/lib/grafana/grafana.db /var/lib/grafana/data/grafana.db; do
    [ -f "$p" ] && { echo "$p"; return 0; }
  done
  # PVCs and volumes: the depth and the usual roots are limited so as not to scan the whole disk
  p=$(find /data /var/lib/k0s /var/lib/rancher /opt/local-path-provisioner /var/lib/docker/volumes \
        -maxdepth 4 -name grafana.db -type f 2>/dev/null | head -1)
  [ -n "$p" ] && { echo "$p"; return 0; }
  return 1
}

# How to take a CONSISTENT copy of a live SQLite database. A plain cp will not do: the database
# is being written and the file may come out torn. sqlite3 is usually absent from the system (it
# was on the Grafana node), while python3 is almost always there and its sqlite3 module can do
# an online backup.
grafana_dumper() {
  if command -v sqlite3 >/dev/null 2>&1; then echo sqlite3; return 0; fi
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sqlite3' 2>/dev/null; then echo python3; return 0; fi
  return 1
}

dump_probe() {
  local engine="$1" container="${2:-}"
  case "$engine" in
    pg)
      if [ -n "$container" ]; then
        local pgu; pgu=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$container" 2>/dev/null | sed -n 's/^POSTGRES_USER=//p' | head -1)
        [ -n "$pgu" ] || pgu=postgres
        docker exec -u postgres "$container" pg_dumpall -U "$pgu" --schema-only >/dev/null 2>&1 \
          || docker exec "$container" pg_dumpall -U "$pgu" --schema-only >/dev/null 2>&1
      else sudo -u postgres pg_dumpall --schema-only >/dev/null 2>&1; fi ;;
    mysql)
      if [ -n "$container" ]; then
        docker exec "$container" sh -c 'MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-${MARIADB_ROOT_PASSWORD:-}}" mysqldump -u root --no-data --all-databases' >/dev/null 2>&1
      else mysqldump --no-data --all-databases >/dev/null 2>&1; fi ;;
    ch)
      if [ -n "$container" ]; then docker exec "$container" clickhouse-client --query "SELECT 1" >/dev/null 2>&1
      else clickhouse-client --query "SELECT 1" >/dev/null 2>&1; fi ;;
    redis)
      if [ -n "$container" ]; then docker exec "$container" redis-cli ping >/dev/null 2>&1
      else redis-cli ping >/dev/null 2>&1; fi ;;
    rabbitmq)
      if [ -n "$container" ]; then docker exec "$container" rabbitmqctl list_vhosts >/dev/null 2>&1
      else rabbitmqctl list_vhosts >/dev/null 2>&1; fi ;;
    k8s) return 0 ;;
    neo4j)
      # The dump is taken with the database STOPPED (see the generator), so we probe not the
      # dump itself but the presence of the tool: neo4j-admin in the container image or on the
      # host.
      if [ -n "$container" ]; then
        local img; img=$(docker inspect -f '{{.Config.Image}}' "$container" 2>/dev/null)
        [ -n "$img" ] || return 1
        docker run --rm --entrypoint neo4j-admin "$img" --version >/dev/null 2>&1
      else command -v neo4j-admin >/dev/null 2>&1; fi ;;
    grafana) [ -n "$(grafana_db_path)" ] && grafana_dumper >/dev/null ;;
  esac
}

cmd_dump_setup() {
  local engine="$1" container="${2:-}"
  case "$engine" in
    pg|mysql|ch|redis|rabbitmq|k8s|grafana|neo4j) ;;
    *) echo "unknown engine: $engine (supported: pg, mysql, ch, redis, rabbitmq, k8s, grafana, neo4j)" >&2; return 2 ;;
  esac
  case "$container" in ''|*[!A-Za-z0-9._-]*) container="" ;; esac
  # Settings come from the env (the agent places them through the spool; from the CLI the
  # defaults apply). They are validated HERE rather than trusted from the panel: dir ends up in
  # rm/mkdir as root, and keep drives the rotation.
  local dir_base="${KV_DUMP_DIR:-$DUMP_DIR_BASE}"
  local keep="${KV_DUMP_KEEP:-$DUMP_KEEP}"
  local minfree="${KV_DUMP_MINFREE:-10}"
  case "$dir_base" in
    /*) : ;; *) echo "the dump directory must be an absolute path: $dir_base" >&2; return 2 ;;
  esac
  case "$dir_base" in
    *..*|*[!A-Za-z0-9._/-]*) echo "invalid dump directory: $dir_base" >&2; return 2 ;;
    /) echo "the root / cannot be used for dumps" >&2; return 2 ;;
  esac
  case "$keep" in ''|*[!0-9]*) keep=2 ;; esac
  [ "$keep" -lt 1 ] && keep=1; [ "$keep" -gt 30 ] && keep=30
  case "$minfree" in ''|*[!0-9]*) minfree=10 ;; esac
  [ "$minfree" -gt 50 ] && minfree=50   # above 50% it is no longer a reserve but half the disk wasted
  # A node can carry several engines of the same type (kervax-db-1 and zabbix-postgres). Each
  # gets ITS OWN script and ITS OWN directory: otherwise the second would overwrite the first,
  # and a "keep the last N" cleanup would delete someone else's dumps. Without a container (a
  # native installation) it works as before.
  local slot="$engine" out="$dir_base/$engine"
  if [ -n "$container" ]; then
    slot="$engine@$container"
    out="$dir_base/$engine/$container"
  fi
  local skipf="$DUMPS_D/$slot.skip"   # marker: "the last run was skipped for lack of space"
  install -d -m 0700 "$dir_base" "$dir_base/$engine" "$out"
  write_run_dumps
  local f="$DUMPS_D/$slot.sh"
  {
    printf '#!/usr/bin/env bash\n# generated by kervax - dump of %s before the file backup\nset -uo pipefail\n' "$engine"
    printf 'OUT=%q\nKEEP=%s\nCONT=%q\nMINFREE=%s\nSKIPF=%q\n' "$out" "$keep" "$container" "$minfree" "$skipf"
    printf 'TS=$(date +%%Y%%m%%d-%%H%%M%%S)\n'
    printf 'FINAL="$OUT"\nMULTI=""\n'
    # OVERFLOW PROTECTION: the dump does not start if less than MINFREE% would be left on the
    # filesystem afterwards. The size of the upcoming dump is estimated from the last successful
    # one times 1.2 (for growth). The first dump (no estimate yet) is allowed - filling the disk
    # to zero is prevented by the atomic write into .partial (it fails with "No space" and the
    # fragment is removed). On a skip we write the SKIPF marker and exit 0: this is a DELIBERATE
    # skip, not a failure (otherwise run-dumps would treat it as an error).
    printf 'if [ "$MINFREE" -gt 0 ]; then\n'
    printf '  read -r AVAIL TOTAL <<<"$(df -kP "$FINAL" | awk %s)"\n' "'NR==2{print \$4, \$2}'"
    printf '  LAST=$(ls -1t "$FINAL"/* 2>/dev/null | head -1)\n'
    printf '  EST=0; [ -n "$LAST" ] && EST=$(du -sk "$LAST" 2>/dev/null | cut -f1)\n'
    printf '  NEED=$(( EST * 12 / 10 )); MIN=$(( TOTAL * MINFREE / 100 ))\n'
    printf '  if [ "$TOTAL" -gt 0 ] && [ $(( AVAIL - NEED )) -lt "$MIN" ]; then\n'
    printf '    printf %s "$(date +%%s)" "$(( AVAIL * 100 / TOTAL ))" "$(( NEED / 1024 ))" > "$SKIPF"\n' "'%s|%s|%s\\n'"
    printf '    echo "kervax-dump: %s SKIPPED - free space on the filesystem is $(( AVAIL*100/TOTAL ))%%, threshold ${MINFREE}%% (needs about $(( NEED/1024 )) MB)" >&2\n' "$slot"
    printf '    exit 0\n  fi\nfi\n'
    printf 'rm -f "$SKIPF"\n'   # there was enough space - clear the previous skip marker
    # ATOMICITY: we write into a temporary subdirectory and move the finished result. A dump
    # interrupted halfway (a reboot, an OOM kill, a killed session) would otherwise leave a
    # truncated .gz next to the real ones - the panel would count it as a dump file and restic
    # would carry a corrupt archive into the backup. Verified in practice: an interrupted
    # pg_dumpall does not pass `gzip -t`.
    printf 'OUT="$OUT/.partial"\nrm -rf "$OUT"; mkdir -p "$OUT"\n'
    case "$engine" in
      pg)
           # EACH database into its own file (pg_dump), plus globals.sql.gz with roles and
           # grants (pg_dumpall --globals-only) - a per-database pg_dump does NOT include them,
           # and without them a restore onto a clean server would give tables with no users and
           # no GRANTs. A run goes into a timestamped subdirectory ($OUT/<TS>) and rotation is
           # per run (MULTI=1). The role is read from the container's POSTGRES_USER (see v7);
           # -u postgres, or the default user where that does not exist (bitnami).
           pg_body='MULTI=1
if [ -n "$CONT" ]; then
  PGU=$(docker inspect -f "{{range .Config.Env}}{{println .}}{{end}}" "$CONT" 2>/dev/null | sed -n "s/^POSTGRES_USER=//p" | head -1)
  [ -n "$PGU" ] || PGU=postgres
  DEX() { docker exec -u postgres "$CONT" "$@" 2>/dev/null || docker exec "$CONT" "$@"; }
else PGU=""; DEX() { sudo -u postgres "$@"; }; fi
RUN="$FINAL/.partial-$TS"; rm -rf "$RUN"; mkdir -p "$RUN"
DEX pg_dumpall ${PGU:+-U "$PGU"} --globals-only | gzip -c > "$RUN/globals.sql.gz"
DBS=$(DEX psql ${PGU:+-U "$PGU"} -Atc "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname")
for db in $DBS; do
  safe=$(printf "%s" "$db" | tr -c "A-Za-z0-9._-" "_")
  DEX pg_dump ${PGU:+-U "$PGU"} "$db" | gzip -c > "$RUN/$safe.sql.gz"
done
'
           printf '%s' "$pg_body" ;;
      mysql) printf 'F="$OUT/mysqldump-all-$TS.sql.gz"\n'
           # --single-transaction: a snapshot without locking the tables (InnoDB).
           # The root password is expanded INSIDE the container (sh -c) rather than passed in
           # the docker exec arguments - otherwise it would show up in ps on the host. MYSQL_PWD
           # rather than -p: a password in argv is visible even inside the container.
           printf 'if [ -n "$CONT" ]; then docker exec "$CONT" sh -c %s | gzip -c > "$F"\n' \
                  "'MYSQL_PWD=\"\${MYSQL_ROOT_PASSWORD:-\${MARIADB_ROOT_PASSWORD:-}}\" mysqldump -u root --all-databases --single-transaction --quick'"
           printf 'else mysqldump --all-databases --single-transaction --quick | gzip -c > "$F"; fi\n' ;;
      redis) printf 'F="$OUT/redis-$TS.rdb"\n'
           # --rdb performs a full sync into a file; from a container we extract it with
           # docker cp because redis-cli writes a FILE (streaming to stdout is not supported
           # everywhere)
           printf 'if [ -n "$CONT" ]; then docker exec "$CONT" redis-cli --rdb /tmp/kervax.rdb >/dev/null 2>&1 && docker cp "$CONT":/tmp/kervax.rdb "$F" >/dev/null && docker exec "$CONT" rm -f /tmp/kervax.rdb\n'
           printf 'else redis-cli --rdb "$F" >/dev/null 2>&1; fi\n'
           printf 'gzip -f "$F" 2>/dev/null; F="$F.gz"\n' ;;
      rabbitmq) printf 'F="$OUT/rabbitmq-defs-$TS.json"\n'
           # export_definitions covers users, vhosts, queues and policies (MESSAGES are not included)
           printf 'if [ -n "$CONT" ]; then docker exec "$CONT" rabbitmqctl export_definitions /tmp/kervax.json >/dev/null 2>&1 && docker cp "$CONT":/tmp/kervax.json "$F" >/dev/null && docker exec "$CONT" rm -f /tmp/kervax.json\n'
           printf 'else rabbitmqctl export_definitions "$F" >/dev/null 2>&1; fi\n'
           printf 'gzip -f "$F" 2>/dev/null; F="$F.gz"\n' ;;
      k8s) printf 'F="$OUT/cluster-$TS"\n'
           # A cluster backup TOGETHER with etcd, using the distribution's own command, on the
           # controller as root. No kubectl exec: k0s and k3s can do this themselves.
           printf 'if command -v k0s >/dev/null 2>&1; then mkdir -p "$F" && k0s backup --save-path "$F" >/dev/null 2>&1\n'
           printf 'elif command -v k3s >/dev/null 2>&1; then mkdir -p "$F" && k3s etcd-snapshot save --dir "$F" >/dev/null 2>&1\n'
           printf 'else echo "no k0s/k3s found - an etcd snapshot has to be taken with etcdctl manually" >&2; exit 1; fi\n'
           printf 'tar -czf "$F.tar.gz" -C "$(dirname "$F")" "$(basename "$F")" 2>/dev/null && rm -rf "$F"; F="$F.tar.gz"\n' ;;
      grafana) printf 'F="$OUT/grafana-$TS.db"\n'
           # the standard ONLINE SQLite backup (.backup / Connection.backup): the snapshot is
           # taken under the engine's own lock, so Grafana does not have to be stopped. A plain
           # cp will not do - a live database is being written and the file may come out torn.
           printf 'DB=%q\n' "$(grafana_db_path)"
           printf 'if command -v sqlite3 >/dev/null 2>&1; then sqlite3 "$DB" ".backup $F" >/dev/null 2>&1\n'
           printf 'else python3 -c %s "$DB" "$F" >/dev/null 2>&1; fi\n' \
                  "'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()'"
           printf 'gzip -f "$F" 2>/dev/null; F="$F.gz"\n' ;;
      neo4j) printf 'F="$OUT/neo4j-$TS.tar.gz"\n'
           # Neo4j Community cannot do an online dump: neo4j-admin refuses to work on a live
           # database ("The database is in use"), and a hot backup exists only in Enterprise. So
           # the database is stopped for the duration of the dump and ALWAYS brought back up -
           # even if the dump failed (otherwise a single failure would leave the service down
           # until morning).
           #
           # The dump is taken by a THROWAWAY container from the same image with
           # --volumes-from: the container itself is stopped at that moment while its data stays
           # reachable through its volumes. --entrypoint neo4j-admin is mandatory: the image's
           # normal entrypoint drops privileges to the neo4j user, which then cannot write into
           # our directory (verified: AccessDeniedException on /kvdump).
           #
           # ALL databases are taken ("*"): users and roles live in system, and without it a
           # restore yields data with no access rights.
           printf 'if [ -n "$CONT" ]; then\n'
           printf '  IMG=$(docker inspect -f "{{.Config.Image}}" "$CONT" 2>/dev/null)\n'
           printf '  WASUP=$(docker inspect -f "{{.State.Running}}" "$CONT" 2>/dev/null)\n'
           printf '  [ "$WASUP" = "true" ] && docker stop "$CONT" >/dev/null 2>&1\n'
           printf '  docker run --rm --user root --entrypoint neo4j-admin --volumes-from "$CONT" \\\n'
           printf '    -v "$OUT":/kvdump "$IMG" database dump "*" --to-path=/kvdump >/dev/null 2>&1\n'
           printf '  rc2=$?\n'
           printf '  [ "$WASUP" = "true" ] && docker start "$CONT" >/dev/null 2>&1\n'
           printf 'else\n'
           printf '  WASUP=$(systemctl is-active neo4j 2>/dev/null)\n'
           printf '  [ "$WASUP" = "active" ] && systemctl stop neo4j >/dev/null 2>&1\n'
           printf '  neo4j-admin database dump "*" --to-path="$OUT" >/dev/null 2>&1; rc2=$?\n'
           printf '  [ "$WASUP" = "active" ] && systemctl start neo4j >/dev/null 2>&1\n'
           printf 'fi\n'
           # each database produces its own .dump - the run is packed into a single archive
           printf 'if [ "$rc2" = "0" ] && ls "$OUT"/*.dump >/dev/null 2>&1; then\n'
           printf '  tar -czf "$F" -C "$OUT" $(cd "$OUT" && ls *.dump) 2>/dev/null && rm -f "$OUT"/*.dump\n'
           # else false: rc below is read from $?, and without an explicit failure a broken
           # dump would look successful - the if branch would simply not run and return 0
           printf 'else false; fi\n' ;;
      ch)  printf 'F="$OUT/clickhouse-$TS.sql.gz"\n'
           # without clickhouse-backup: schema and data are exported with the standard client
           printf 'if [ -n "$CONT" ]; then docker exec "$CONT" clickhouse-client --query "SHOW DATABASES" > "$OUT/databases-$TS.txt" 2>/dev/null; docker exec "$CONT" clickhouse-client --query "SELECT create_table_query FROM system.tables WHERE database NOT IN (%s)" | gzip -c > "$F"\n' "'system','INFORMATION_SCHEMA','information_schema'"
           printf 'else clickhouse-client --query "SELECT create_table_query FROM system.tables WHERE database NOT IN (%s)" | gzip -c > "$F"; fi\n' "'system','INFORMATION_SCHEMA','information_schema'" ;;
    esac
    printf 'rc=$?\n'
    # Two finishing modes. MULTI (pg): a run is the subdirectory $FINAL/<TS> holding a set of
    # files - each is checked with gzip -t, the finished subdirectory is published as is (it was
    # written straight into $FINAL/$TS, not into .partial) and rotation happens per RUN.
    # Single-file engines behave as before: one $F, an atomic move out of .partial, rotation by
    # file.
    fin_multi='if [ -n "$MULTI" ]; then
  if [ -z "$(ls -A "$RUN" 2>/dev/null)" ]; then rm -rf "$OUT" "$RUN"; echo "pg dump: no file was produced" >&2; exit 1; fi
  for gz in "$RUN"/*.gz; do gzip -t "$gz" 2>/dev/null || { rm -rf "$RUN"; echo "pg dump is corrupt: $(basename "$gz")" >&2; exit 1; }; done
  chmod 0700 "$RUN"; chmod 0600 "$RUN"/*.gz; rm -rf "$OUT"
  mv -f "$RUN" "$FINAL/$TS"   # the run is published in a single move
  ls -1dt "$FINAL"/*/ 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -rf
  echo "kervax-dump: pg -> $FINAL/$TS ($(ls -1 "$FINAL/$TS"/*.gz 2>/dev/null | wc -l) files)"
  exit 0
fi
'
    printf '%s' "$fin_multi"
    printf 'if [ $rc -ne 0 ] || [ ! -s "$F" ]; then rm -rf "$OUT"; echo "%s dump failed (rc=$rc)" >&2; exit 1; fi\n' "$engine"
    # gzip -t catches exactly a truncation: the file is there and not empty but cannot be decompressed
    printf 'case "$F" in *.gz) gzip -t "$F" 2>/dev/null || { rm -rf "$OUT"; echo "%s dump is corrupt (write interrupted)" >&2; exit 1; } ;; esac\n' "$engine"
    printf 'chmod 0600 "$F"\n'
    # publication: the finished file is moved in one go and the temporary directory is removed
    printf 'mv -f "$F" "$FINAL/"; F="$FINAL/$(basename "$F")"; rm -rf "$OUT"; OUT="$FINAL"\n'
    # rotation: only the last KEEP are kept, otherwise the history eats the local disk
    printf 'ls -1t "$OUT"/*.gz 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f\n'
    printf 'echo "kervax-dump: %s → $F"\n' "$engine"
  } > "$f"
  chmod 0700 "$f"; chown root:root "$f"
  # slot metadata: get-config reads it from here rather than parsing the generated script.
  # dir is configurable, so it can no longer be derived from a fixed DUMP_DIR_BASE.
  printf 'dir=%s\nkeep=%s\nminfree=%s\n' "$out" "$keep" "$minfree" > "$DUMPS_D/$slot.meta"
  chmod 0644 "$DUMPS_D/$slot.meta"
  # A FAST availability probe instead of a full trial dump (see dump_probe). It catches silent
  # failures (role, password, access) in seconds without pulling data. The full dump runs on the
  # backup schedule. This used to run the whole dump: minutes on a large database, the panel
  # timed out and showed an "error" while the dump was still running.
  if ! dump_probe "$engine" "$container" >/tmp/kv-dump.$$ 2>&1; then
    local err; err="$(tr '\n' ' ' </tmp/kv-dump.$$ | tail -c 300)"; rm -f /tmp/kv-dump.$$
    rm -f "$f" "$DUMPS_D/$slot.meta"   # a dump known not to work is not left enabled
    echo "the dump probe failed, dumps were NOT enabled: $err" >&2; return 2
  fi
  rm -f /tmp/kv-dump.$$
  local trial_note=" - the first dump will be taken with the next scheduled backup"
  systemctl cat "$PROV_SERVICE" >/dev/null 2>&1 || trial_note=" - the first dump will be taken on the next timer run"
  # A dump is useless if restic does not pick it up. In include mode the dump directory has to
  # be listed explicitly, otherwise the dumps would silently pile up locally and go nowhere.
  local added=""
  local script; script="$(find_script || true)"
  if [ -n "$script" ]; then
    local cur_mode inc
    cur_mode="$(sed -n 's/^INCLUDE_MODE="\(.*\)".*/\1/p' "$script" | head -1)"
    if [ "$cur_mode" = include ]; then
      inc="$(sed -n 's/^INCLUDES_STR="\(.*\)"$/\1/p' "$script" | head -1)"
      # line format: "/etc" "/var/www". The value is built in a SEPARATE variable and only
      # then handed to sed - otherwise the quote escaping inside the sed command itself drifts
      # and the runner receives a path like \"/backup\" (verified: restic then looks for a
      # non-existent path).
      if ! printf '%s' "$inc" | grep -qF "\"$dir_base\""; then
        local newinc esc
        newinc="${inc:+$inc }\"$dir_base\""
        esc="$(printf '%s' "$newinc" | sed 's/[|&\\]/\\&/g')"   # sed metacharacters only
        sed -i "s|^INCLUDES_STR=\".*\"|INCLUDES_STR=\"$esc\"|" "$script"
        added=" ($dir_base was added to the backup list)"
      fi
    else
      # exclude mode: make sure the dump directory is not thrown out by an exclusion
      local exc; exc="$(sed -n 's/^EXCLUDES_STR="\(.*\)"$/\1/p' "$script" | head -1)"
      case "$exc" in
        *"--exclude=$dir_base"*) added=" WARNING: $dir_base is on the exclusion list - the dumps will not reach the backup!" ;;
      esac
    fi
  fi
  refresh_config
  # we report whatever actually triggers it: on a node without a file backup, promising "before
  # every backup" would be a lie, since its own timer runs there
  local when="before every backup"
  systemctl cat "$PROV_SERVICE" >/dev/null 2>&1 || when="daily, on its own timer (this node has no file backup - the copy is local only)"
  echo "OK $engine dumps enabled -> $out ($when, keeping $keep, space threshold ${minfree}%)$added$trial_note"
}

# Disable the dump of ONE engine. Both the script and the accumulated files are removed:
# otherwise restic would keep carrying the same stale dump into the backup forever. No history
# is lost - previous dumps already sit in restic snapshots on the backup server.
# The ExecStartPre drop-in is left alone: run-dumps.sh on an empty directory simply exits 0, and
# re-enabling needs no extra work.
cmd_dump_remove() {
  local engine="$1" container="${2:-}"
  case "$engine" in
    pg|mysql|ch|redis|rabbitmq|k8s|grafana|neo4j) ;;
    *) echo "unknown engine: $engine" >&2; return 2 ;;
  esac
  case "$container" in ''|*[!A-Za-z0-9._-]*) container="" ;; esac
  local slot="$engine"
  [ -n "$container" ] && slot="$engine@$container"
  local f="$DUMPS_D/$slot.sh"
  [ -f "$f" ] || { echo "$slot dumps were not enabled anyway" >&2; return 2; }
  # the dump directory comes from .meta: it may have been configured non-standard. With no meta
  # (an older slot) the old DUMP_DIR_BASE scheme applies.
  local out="$DUMP_DIR_BASE/$engine"; [ -n "$container" ] && out="$DUMP_DIR_BASE/$engine/$container"
  local base="$DUMP_DIR_BASE"
  if [ -f "$DUMPS_D/$slot.meta" ]; then
    out="$(sed -n 's/^dir=//p' "$DUMPS_D/$slot.meta" | head -1)"
    base="$(dirname "$(dirname "$out")")"; [ -z "$container" ] && base="$(dirname "$out")"
  fi
  rm -f "$f" "$DUMPS_D/$slot.meta" "$DUMPS_D/$slot.skip"
  rm -rf "${out:?}"
  # the engine directory is removed only if no dumps of another container remain in it
  rmdir "$(dirname "$out")" 2>/dev/null || true
  refresh_config
  echo "OK $slot dumps disabled, local files removed (the history stays in restic)"
}

# which dumps are enabled - the panel shows this under Coverage
cmd_dump_status() {
  local j="" e f n sz
  for f in "$DUMPS_D"/*.sh; do
    [ -f "$f" ] || continue
    e="$(basename "$f" .sh)"
    n=$(find "$DUMP_DIR_BASE/$e" -maxdepth 1 -type f -name '*.gz' 2>/dev/null | wc -l)
    sz=$(du -sb "$DUMP_DIR_BASE/$e" 2>/dev/null | cut -f1); [ -z "$sz" ] && sz=0
    j="${j:+$j,}{\"engine\":\"$(json_escape "$e")\",\"files\":$n,\"size_bytes\":$sz}"
  done
  printf '[%s]\n' "$j"
}

cmd_get_config() {
  local script timer mode="" sched="" inc="" exc=""
  script="$(find_script || true)"; timer="$(find_timer || true)"
  if [ -n "$script" ]; then
    mode="$(sed -n 's/^INCLUDE_MODE="\(.*\)".*/\1/p' "$script" | head -1)"
    # only the list of the ACTIVE mode is read: on nodes provisioned by an older helper both
    # are filled in, and without this the panel would show the stale list of the other mode
    if [ "$mode" = include ]; then
      inc="$(sed -n 's/^INCLUDES_STR="\(.*\)"$/\1/p' "$script" | head -1)"
    else
      exc="$(sed -n 's/^EXCLUDES_STR="\(.*\)"$/\1/p' "$script" | head -1)"
    fi
  fi
  [ -n "$timer" ] && sched="$(systemctl cat "$timer" 2>/dev/null | sed -n 's/^OnCalendar=.* \([0-9]\{1,2\}:[0-9]\{2\}\):00$/\1/p' | head -1)"
  local ex_json="" in_json="" p
  for p in $(printf '%s\n' "$exc" | grep -oE -- '--exclude=[^ ]+' | sed 's/^--exclude=//'); do
    ex_json="${ex_json:+$ex_json,}\"$(json_escape "$p")\""; done
  for p in $(printf '%s\n' "$inc" | grep -oE '"[^"]+"' | tr -d '"'); do
    in_json="${in_json:+$in_json,}\"$(json_escape "$p")\""; done
  # the backup destination comes from the env, WITHOUT the password or htpasswd (the config file is world-readable!)
  local repo_masked=""
  if [ -f "$PROV_ENV" ]; then
    local url; url="$(sed -n 's/^RESTIC_REPOSITORY=//p' "$PROV_ENV" | head -1)"
    repo_masked="$(printf '%s' "$url" | sed -E 's#(rest:[a-z]+://)[^@]*@#\1#')"
  fi
  # The state of ENABLED dumps - a state, not a one-off command reply: the panel has to show
  # "dumps are running, N files, this much space, the last one at this time", otherwise the card
  # looks exactly the same after enabling as before it.
  # A slot is either "engine" or "engine@container": the panel shows the state of EACH database
  # separately, otherwise a second postgres would look covered by the first one's dump
  local dumps_json="" slot de dcont ddir dn dsz dts dkeep dminfree skipped skip_ts skip_free enabled_ts
  for slot in "$DUMPS_D"/*.sh; do
    [ -f "$slot" ] || continue
    slot="$(basename "$slot" .sh)"
    de="${slot%%@*}"; dcont=""
    case "$slot" in *@*) dcont="${slot#*@}" ;; esac
    enabled_ts=$(stat -c %Y "$DUMPS_D/$slot.sh" 2>/dev/null || echo 0)  # when the dump was enabled
    # dir/keep/minfree come from .meta (dir is configurable and cannot be derived from
    # DUMP_DIR_BASE). Older slots without .meta (before this version) fall back to the defaults
    # and the old path scheme.
    ddir="$DUMP_DIR_BASE/$de"; [ -n "$dcont" ] && ddir="$DUMP_DIR_BASE/$de/$dcont"
    dkeep="$DUMP_KEEP"; dminfree=10
    if [ -f "$DUMPS_D/$slot.meta" ]; then
      ddir="$(sed -n 's/^dir=//p' "$DUMPS_D/$slot.meta" | head -1)"
      dkeep="$(sed -n 's/^keep=//p' "$DUMPS_D/$slot.meta" | head -1)"
      dminfree="$(sed -n 's/^minfree=//p' "$DUMPS_D/$slot.meta" | head -1)"
    fi
    dn=$(find "$ddir" -type f -name "*.gz" ! -path "*/.partial-*" 2>/dev/null | wc -l)
    dsz=$(du -sb "$ddir" 2>/dev/null | cut -f1); [ -z "$dsz" ] && dsz=0
    dts=$(find "$ddir" -type f -name "*.gz" ! -path "*/.partial-*" -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)
    [ -z "$dts" ] && dts=0
    # the out-of-space skip marker (written by the dump script itself): "ts|free_pct|need_mb"
    skipped=false; skip_ts=0; skip_free=0
    if [ -f "$DUMPS_D/$slot.skip" ]; then
      skipped=true
      skip_ts="$(cut -d'|' -f1 "$DUMPS_D/$slot.skip")"; [ -z "$skip_ts" ] && skip_ts=0
      skip_free="$(cut -d'|' -f2 "$DUMPS_D/$slot.skip")"; [ -z "$skip_free" ] && skip_free=0
    fi
    dumps_json="${dumps_json:+$dumps_json,}{\"engine\":\"$(json_escape "$de")\",\"container\":\"$(json_escape "$dcont")\",\"files\":$dn,\"size_bytes\":$dsz,\"last_ts\":$dts,\"keep\":${dkeep:-2},\"min_free_pct\":${dminfree:-10},\"dir\":\"$(json_escape "$ddir")\",\"skipped\":$skipped,\"skip_ts\":$skip_ts,\"skip_free_pct\":$skip_free,\"enabled_ts\":${enabled_ts:-0}}"
  done
  # helper_version tells the panel that the helper on the node is outdated (it needs reinstalling)
  printf '{"manageable":true,"helper_version":%s,"mode":"%s","schedule":"%s","includes":[%s],"excludes":[%s],"repo_dest":"%s","dumps":[%s]}\n' \
    "$HELPER_VER" "${mode:-exclude}" "${sched:-}" "$in_json" "$ex_json" "$(json_escape "$repo_masked")" "$dumps_json"
}

# get-creds returns the data needed for a restore (repo URL plus password). A secret: only on
# an explicit panel request, through the spool (res 0770, removed at once). NEVER into the config file.
cmd_get_creds() {
  [ -f "$PROV_ENV" ] || { echo "env not found (the backup was not configured by this panel)" >&2; return 2; }
  local url pass cacert=""
  url="$(sed -n 's/^RESTIC_REPOSITORY=//p' "$PROV_ENV" | head -1)"
  pass="$(sed -n 's/^RESTIC_PASSWORD=//p' "$PROV_ENV" | head -1)"
  [ -f "$PROV_CACERT" ] && cacert="$PROV_CACERT"
  # a single base64 line survives the spool (tr '\n'); the panel decodes it
  printf 'repo_url=%s\nrepopass=%s\ncacert_file=%s\n' "$url" "$pass" "$cacert" | base64 -w0
}
refresh_config() { cmd_get_config > "$CONF_JSON.tmp" 2>/dev/null && mv -f "$CONF_JSON.tmp" "$CONF_JSON" && chmod 0644 "$CONF_JSON"; }

cmd_set_paths() {
  local mode="$1"; shift
  [ "$mode" = include ] || [ "$mode" = exclude ] || { echo "bad mode" >&2; return 2; }
  local script; script="$(find_script)" || { echo "script not found" >&2; return 2; }
  local paths=() p; for p in "$@"; do valid_path "$p" || { echo "bad path: $p" >&2; return 2; }; paths+=("$p"); done
  [ "${#paths[@]}" -gt 0 ] || { echo "no paths" >&2; return 2; }
  cp -a "$script" "$script.kervax.bak"
  if [ "$mode" = include ]; then
    local inc=""; for p in "${paths[@]}"; do inc="${inc:+$inc }\"$p\""; done
    sed -i "s|^INCLUDE_MODE=\".*\"|INCLUDE_MODE=\"include\"|; s|^INCLUDES_STR=\".*\"|INCLUDES_STR=\"$inc\"|" "$script"
  else
    local exc=""; for p in "${paths[@]}"; do exc="${exc:+$exc }--exclude=$p"; done
    sed -i "s|^INCLUDE_MODE=\".*\"|INCLUDE_MODE=\"exclude\"|; s|^EXCLUDES_STR=\".*\"|EXCLUDES_STR=\"$exc\"|" "$script"
  fi
  echo OK
}
cmd_set_schedule() {
  local hhmm="$1"
  [[ "$hhmm" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "bad time" >&2; return 2; }
  local timer; timer="$(find_timer)" || { echo "timer not found" >&2; return 2; }
  local file="/etc/systemd/system/$timer"; [ -f "$file" ] || { echo "timer file not found" >&2; return 2; }
  cp -a "$file" "$file.kervax.bak"
  sed -i "s|^OnCalendar=.*|OnCalendar=*-*-* ${hhmm}:00|" "$file"
  systemctl daemon-reload
  echo OK
}
cmd_run_now() {
  local timer; timer="$(find_timer)" || { echo "timer not found" >&2; return 2; }
  systemctl start --no-block "${timer%.timer}.service"
  echo "OK (started)"
}

# ---- provisioning a backup from scratch (restic plus env, script, service and timer) ----
ensure_restic() {
  local ver="$1" arch f base
  [ -x "$RESTIC_BIN" ] && return 0
  install -d -m 0755 "$RESTIC_BIN_DIR"
  # restic is already in the system - a copy is placed into our directory (as the ansible role does)
  if command -v restic >/dev/null 2>&1; then
    cp "$(command -v restic)" "$RESTIC_BIN"; chmod 0755 "$RESTIC_BIN"; return 0
  fi
  case "$(uname -m)" in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; *) echo "unknown arch." >&2; return 2;; esac
  command -v bunzip2 >/dev/null 2>&1 || { apt-get update -qq >/dev/null 2>&1 || true; apt-get install -y -qq bzip2 >/dev/null 2>&1 || true; }
  base="https://github.com/restic/restic/releases/download/v$ver"; f="restic_${ver}_linux_${arch}.bz2"
  curl -fsSL --connect-timeout 20 "$base/$f" -o "/tmp/$f" || { echo "could not download restic" >&2; return 2; }
  if curl -fsSL --connect-timeout 20 "$base/SHA256SUMS" -o /tmp/restic-sums 2>/dev/null && [ -s /tmp/restic-sums ]; then
    ( cd /tmp && grep " $f\$" restic-sums | sha256sum -c - >/dev/null 2>&1 ) || { echo "the checksum did not match" >&2; rm -f "/tmp/$f"; return 2; }
  fi
  bunzip2 -f "/tmp/$f" || { echo "bunzip2 fail" >&2; return 2; }
  install -m 0755 "/tmp/restic_${ver}_linux_${arch}" "$RESTIC_BIN"
  rm -f "/tmp/restic_${ver}_linux_${arch}" /tmp/restic-sums
}

# The target restic version for the whole fleet plus its sha256 (verification is MANDATORY).
# Older nodes (0.13/0.14/0.15) are updated to it. Repository formats are backward compatible -
# 0.19 reads and writes repositories created by older versions, so the update is safe.
RESTIC_TARGET_VER="0.19.1"
RESTIC_SHA_amd64="f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c"
RESTIC_SHA_arm64="a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465"

# cmd_restic_update updates the restic binary (or binaries) to RESTIC_TARGET_VER. It updates
# both the panel's own ($RESTIC_BIN) and the system one (from PATH) if that is actually in use.
# It downloads from github, VERIFIES the sha256 against the baked-in value (we do not rely on
# SHA256SUMS from the same host alone) and replaces atomically. The backup configuration is not
# touched - only the binary.
cmd_restic_update() {
  local arch want
  case "$(uname -m)" in
    x86_64) arch=amd64; want="$RESTIC_SHA_amd64";;
    aarch64|arm64) arch=arm64; want="$RESTIC_SHA_arm64";;
    *) echo "unknown architecture: $(uname -m)" >&2; return 2;;
  esac
  # which binaries to update: the panel one plus the system one (they may be the same - dedup below)
  local targets=() b
  [ -x "$RESTIC_BIN" ] && targets+=("$RESTIC_BIN")
  b="$(command -v restic 2>/dev/null || true)"; [ -n "$b" ] && targets+=("$b")
  if [ "${#targets[@]}" -eq 0 ]; then
    echo "no restic found on the node (neither $RESTIC_BIN nor in PATH)" >&2; return 2
  fi
  # already at the target? checked on the first binary so nothing is downloaded in vain
  local cur; cur="$("${targets[0]}" version 2>/dev/null | grep -oE 'restic [0-9.]+' | awk '{print $2}')"
  if [ "$cur" = "$RESTIC_TARGET_VER" ]; then
    echo "restic is already $RESTIC_TARGET_VER - no update needed"; return 0
  fi
  command -v bunzip2 >/dev/null 2>&1 || { apt-get update -qq >/dev/null 2>&1 || true; apt-get install -y -qq bzip2 >/dev/null 2>&1 || true; }
  local base f tmp
  base="https://github.com/restic/restic/releases/download/v$RESTIC_TARGET_VER"
  f="restic_${RESTIC_TARGET_VER}_linux_${arch}.bz2"
  tmp="$(mktemp -d /tmp/kv-restic.XXXXXX)"
  curl -fsSL --connect-timeout 20 "$base/$f" -o "$tmp/$f" || { rm -rf "$tmp"; echo "could not download restic $RESTIC_TARGET_VER" >&2; return 2; }
  # the sha256 of the compressed .bz2 is compared with the value BAKED into the helper, not only with github's SHA256SUMS
  local got; got="$(sha256sum "$tmp/$f" | awk '{print $1}')"
  if [ "$got" != "$want" ]; then
    rm -rf "$tmp"; echo "the sha256 did not match (expected $want, got $got) - the update was cancelled" >&2; return 2
  fi
  bunzip2 -f "$tmp/$f" || { rm -rf "$tmp"; echo "bunzip2 fail" >&2; return 2; }
  local newbin="$tmp/restic_${RESTIC_TARGET_VER}_linux_${arch}"
  chmod 0755 "$newbin"
  "$newbin" version >/dev/null 2>&1 || { rm -rf "$tmp"; echo "the downloaded restic does not run" >&2; return 2; }
  # atomic replacement of each unique binary: cp to a temporary next to it plus mv (same filesystem)
  local done="" out=""
  for b in "${targets[@]}"; do
    case " $done " in *" $b "*) continue;; esac   # dedup (the panel binary may be the system one)
    done="$done $b"
    cp -f "$newbin" "$b.kv-new" && chmod 0755 "$b.kv-new" && mv -f "$b.kv-new" "$b" \
      && out="$out $b" || echo "could not replace $b" >&2
  done
  rm -rf "$tmp"
  echo "restic updated to $RESTIC_TARGET_VER (was ${cur:-?}):$out"
}

write_runner() {
  local mode="$1"; shift
  local paths=("$@") p exc="" inc=""
  # ONLY the list of the active mode is filled in: both used to be written, and get-config
  # handed the panel paths in includes and excludes at once (it did not affect the backup - the
  # runner picks the right one - but the UI showed the wrong list and could save it on a mode
  # change)
  for p in "${paths[@]}"; do
    if [ "$mode" = include ]; then
      inc="${inc:+$inc }\"$p\""
    else
      exc="${exc:+$exc }--exclude=$p"
    fi
  done
  local restic="$RESTIC_BIN"; [ -f "$PROV_CACERT" ] && restic="$RESTIC_BIN --cacert $PROV_CACERT"
  {
    printf '#!/usr/bin/env bash\nset -euo pipefail\n'
    printf 'RESTIC=%q\n' "$restic"
    printf 'ENV_FILE=%q\n' "$PROV_ENV"
    printf 'LOCKFILE=%q\n' "$PROV_HOME/rk.lock"
    printf 'METRICS_DIR=%q\n' "$PROV_METRICS_DIR"
    printf 'METRICS_FILE=%q\n' "$PROV_METRICS_DIR/$PROV_METRICS_FILE"
    printf 'HOST=%q\n' "$(hostname)"
    printf 'INCLUDE_MODE="%s"\n' "$mode"
    printf 'EXCLUDES_STR="%s"\n' "$exc"
    printf 'INCLUDES_STR="%s"\n' "$inc"
    cat <<'RUNNER_BODY'
mkdir -p "$(dirname "$LOCKFILE")" "$METRICS_DIR"
prev_val(){ local n="$1" d="$2" v=""; [ -f "$METRICS_FILE" ] && v="$(awk -v m="$n" 'index($0,m"{")==1{print $NF}' "$METRICS_FILE"|tail -n1)"; [ -n "$v" ]&&echo "$v"||echo "$d"; }
write_metrics(){ local s="$1" sk="$2" du="$3" ts tmp; ts="$(date +%s)"; tmp="${METRICS_FILE}.$$.tmp"
  { echo "restic_last_backup_success{host=\"${HOST}\"} ${s}"; echo "restic_last_backup_timestamp{host=\"${HOST}\"} ${ts}"
    echo "restic_last_backup_skipped{host=\"${HOST}\"} ${sk}"; echo "restic_last_backup_duration_seconds{host=\"${HOST}\"} ${du}"; } > "$tmp"; mv -f "$tmp" "$METRICS_FILE"; }
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  ps="$(prev_val restic_last_backup_success 0)"; pt="$(prev_val restic_last_backup_timestamp "$(date +%s)")"; pd="$(prev_val restic_last_backup_duration_seconds 0)"
  tmp="${METRICS_FILE}.$$.tmp"
  { echo "restic_last_backup_success{host=\"${HOST}\"} ${ps}"; echo "restic_last_backup_timestamp{host=\"${HOST}\"} ${pt}"
    echo "restic_last_backup_skipped{host=\"${HOST}\"} 1"; echo "restic_last_backup_duration_seconds{host=\"${HOST}\"} ${pd}"; } > "$tmp"; mv -f "$tmp" "$METRICS_FILE"; exit 0
fi
source "$ENV_FILE"
$RESTIC unlock >/dev/null 2>&1 || true
if [ "$INCLUDE_MODE" = include ]; then
  [ -n "$INCLUDES_STR" ] || { echo "include mode without paths" >&2; exit 2; }
  BACKUP_CMD="$RESTIC backup ${INCLUDES_STR}"
else
  BACKUP_CMD="$RESTIC backup / --exclude-caches${EXCLUDES_STR:+ $EXCLUDES_STR}"
fi
ts_start=$(date +%s); set +e; eval "$BACKUP_CMD"; RC=$?; set -e; dur=$(( $(date +%s) - ts_start ))
if [ $RC -eq 0 ] || [ $RC -eq 3 ]; then write_metrics 1 0 "$dur"; else write_metrics 0 0 "$dur"; fi
exit $RC
RUNNER_BODY
  } > "$PROV_SCRIPT"
  chmod 0700 "$PROV_SCRIPT"; chown "$PROV_USER:$PROV_USER" "$PROV_SCRIPT" 2>/dev/null || true
}

cmd_provision() {
  # provision <repo_url> <repopass> <mode> <schedule> <delay> <restic_ver> <cacert_b64|-> <path...>
  local repo_url="$1" repopass="$2" mode="$3" sched="$4" delay="$5" ver="$6" cacert_b64="$7"; shift 7
  local paths=("$@")
  case "$repo_url" in rest:*) ;; *) echo "bad repo url" >&2; return 2;; esac
  [ -n "$repopass" ] || { echo "no repopass" >&2; return 2; }
  [ "$mode" = include ] || [ "$mode" = exclude ] || { echo "bad mode" >&2; return 2; }
  [[ "$sched" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || { echo "bad time" >&2; return 2; }
  [ "${#paths[@]}" -gt 0 ] || { echo "no paths" >&2; return 2; }
  local p; for p in "${paths[@]}"; do valid_path "$p" || { echo "bad path: $p" >&2; return 2; }; done
  [[ "$delay" =~ ^[0-9]+[smh]?$ ]] || delay=1h
  # ONLY the helper decides the restic version. It used to be sent by the panel (with the agent
  # substituting its own default), both numbers drifted away from RESTIC_TARGET_VER, and a fresh
  # node got 0.18.1 - after which the panel immediately offered to "update to 0.19.1". The value
  # sent in is ignored deliberately: that way the fix works with older agents too, without
  # updating them.
  ver="$RESTIC_TARGET_VER"
  # 1) the backup user
  id -u "$PROV_USER" >/dev/null 2>&1 || useradd --system --home-dir "$PROV_HOME" --create-home --shell /usr/sbin/nologin "$PROV_USER"
  # 2) restic
  ensure_restic "$ver" || return 2
  install -d -o "$PROV_USER" -g "$PROV_USER" -m 0755 "$RESTIC_BIN_DIR" 2>/dev/null || true
  chown -R "$PROV_USER:$PROV_USER" "$RESTIC_BIN_DIR" 2>/dev/null || true
  # 3) cacert (for a self-signed backup server), if supplied
  if [ "$cacert_b64" != "-" ] && [ -n "$cacert_b64" ]; then
    printf '%s' "$cacert_b64" | base64 -d > "$PROV_CACERT" 2>/dev/null && chmod 0644 "$PROV_CACERT" || { echo "bad cacert" >&2; return 2; }
  else
    rm -f "$PROV_CACERT" 2>/dev/null || true
  fi
  # 4) the env file (0600 root)
  umask 077
  { printf 'RESTIC_PASSWORD=%s\n' "$repopass"; printf 'RESTIC_REPOSITORY=%s\n' "$repo_url"; } > "$PROV_ENV"
  chown root:root "$PROV_ENV"; chmod 0600 "$PROV_ENV"; umask 022
  # 5) the metrics directory (the backup user must be able to write)
  install -d -o "$PROV_USER" -g "$PROV_USER" -m 0775 "$PROV_METRICS_DIR" 2>/dev/null || \
    { mkdir -p "$PROV_METRICS_DIR"; chown "$PROV_USER:$PROV_USER" "$PROV_METRICS_DIR" 2>/dev/null || true; }
  # 6) the backup script
  write_runner "$mode" "${paths[@]}"
  # 7) service + timer
  cat > "/etc/systemd/system/$PROV_SERVICE" <<UNIT
[Unit]
Description=Kervax restic backup
Wants=network-online.target
After=network-online.target
[Service]
Type=oneshot
User=$PROV_USER
EnvironmentFile=$PROV_ENV
ExecStart=$PROV_SCRIPT
AmbientCapabilities=CAP_DAC_READ_SEARCH
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
TimeoutStartSec=infinity
[Install]
WantedBy=multi-user.target
UNIT
  cat > "/etc/systemd/system/$PROV_TIMER" <<UNIT
[Unit]
Description=Kervax restic backup timer
[Timer]
OnCalendar=*-*-* ${sched}:00
AccuracySec=1m
RandomizedDelaySec=$delay
Persistent=true
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now "$PROV_TIMER" >/dev/null 2>&1 || { echo "could not enable the timer" >&2; return 2; }
  refresh_config
  echo "OK provisioned ($mode, $sched)"
}

# cmd_adopt migrates an OLD (ansible) backup layout under the panel. It reads the URL and
# password, the excludes/mode and the schedule from the existing systemd units and switches them
# to the panel runner (which writes restic-backup.prom, so the panel sees metrics and can manage
# it). The repository stays the same - NO restic init, we simply keep appending. The password
# never leaves the node (it is read locally). Custom ExecStopPost hooks of the old unit
# (heartbeat, telegram) are not carried over - by the operator's decision alerting is taken over
# by the panel.
cmd_adopt() {
  local ver="${1:-0.19.1}"   # which restic version to raise to during the migration
  # 1) find the backup service
  local svc="" s
  for s in systemd-rest.service restic-backup.service restic.service; do
    systemctl cat "$s" >/dev/null 2>&1 && { svc="$s"; break; }
  done
  [ -n "$svc" ] || { echo "no systemd backup service found to migrate" >&2; return 2; }
  local unit; unit="$(systemctl cat "$svc" 2>/dev/null)"
  # already the panel layout? (runner in /etc/systemd-rest.conf, env in resta.conf) - no-op
  if printf '%s\n' "$unit" | grep -q "ExecStart=$PROV_SCRIPT" && [ -f "$PROV_ENV" ]; then
    echo "already managed by the panel - no migration needed"; return 0
  fi
  # 2) the env file with the old credentials (EnvironmentFile=, possibly prefixed with '-')
  local envf; envf="$(printf '%s\n' "$unit" | sed -nE 's/^EnvironmentFile=-?(.+)$/\1/p' | head -1)"
  [ -n "$envf" ] && [ -f "$envf" ] || { echo "no EnvironmentFile with the old credentials found" >&2; return 2; }
  # 3) extract the URL and password (never written to the log)
  local repo_url repopass
  repo_url="$(sed -nE 's/^(export )?RESTIC_REPOSITORY=(.*)$/\2/p' "$envf" | head -1 | sed -E 's/^["'\'']//; s/["'\'']$//')"
  repopass="$(sed -nE 's/^(export )?RESTIC_PASSWORD=(.*)$/\2/p' "$envf" | head -1 | sed -E 's/^["'\'']//; s/["'\'']$//')"
  case "$repo_url" in rest:*) ;; *) echo "the old RESTIC_REPOSITORY is not a rest: URL - refusing to migrate blindly" >&2; return 2;; esac
  [ -n "$repopass" ] || { echo "RESTIC_PASSWORD is empty in the old env" >&2; return 2; }
  # 4) mode and paths from ExecStart (or from the wrapper script it points to)
  local execline; execline="$(printf '%s\n' "$unit" | sed -nE 's/^ExecStart=(.*)$/\1/p' | head -1)"
  local scan="$execline"
  case "$execline" in
    *" backup "*) ;;                                  # inline restic
    /*) local sp; sp="$(printf '%s' "$execline" | awk '{print $1}')"
        [ -f "$sp" ] && scan="$(cat "$sp" 2>/dev/null)" ;;   # ExecStart points to a script
  esac
  local mode="exclude" paths=() e
  # every --exclude=X (a trailing '/' is stripped and the value validated)
  while IFS= read -r e; do
    e="${e%/}"; [ -n "$e" ] && valid_path "$e" && paths+=("$e")
  done < <(printf '%s\n' "$scan" | grep -oE -- "--exclude=[^ \"']+" | sed 's/^--exclude=//')
  # include mode: explicit paths are backed up (not '/') and there are no excludes
  if ! printf '%s' "$scan" | grep -qE 'backup +/( |$|")' && [ "${#paths[@]}" -eq 0 ]; then
    local tok seen=0
    for tok in $scan; do
      if [ "$seen" = 1 ]; then
        case "$tok" in --*) ;; /*) e="${tok%/}"; valid_path "$e" && paths+=("$e");; esac
      fi
      [ "$tok" = backup ] && seen=1
    done
    [ "${#paths[@]}" -gt 0 ] && mode="include"
  fi
  # could not parse -> a safe default: back up / with the usual excludes (no data is lost)
  if [ "${#paths[@]}" -eq 0 ]; then
    echo "could not parse the paths from ExecStart - falling back to the default excludes" >&2
    mode="exclude"; paths=(/proc /sys /run /dev /usr /var/lib/lxcfs /tmp /var/tmp /boot /var/cache /snap /lib/modules /lib/firmware /lost+found /swapfile)
  fi
  # 5) the schedule from the timer
  local sched="23:00" tmr="" t
  for t in "${svc%.service}.timer" systemd-rest.timer restic-backup.timer restic.timer; do
    systemctl cat "$t" >/dev/null 2>&1 && { tmr="$t"; break; }
  done
  if [ -n "$tmr" ]; then
    local oc; oc="$(systemctl cat "$tmr" 2>/dev/null | sed -nE 's/^OnCalendar=.*[ *]([0-9]{1,2}:[0-9]{2}).*$/\1/p' | head -1)"
    [ -n "$oc" ] && sched="$oc"
  fi
  sched="$(printf '%s' "$sched" | awk -F: '{printf "%02d:%02d", $1, $2}')"
  # 6) the panel provisioning with the same credentials, paths and schedule, WITHOUT restic init (delay=0, as before)
  echo "adopt: repository $(printf '%s' "$repo_url" | sed -E 's#//[^@]*@#//***@#'), mode=$mode, paths=${#paths[@]}, time=$sched, restic->$ver"
  # dry-run: show what was parsed and leave the node untouched (a check before the real migration)
  if [ -n "${KV_ADOPT_DRYRUN:-}" ]; then
    printf 'DRY-RUN. env=%s paths:\n' "$envf"; printf '  - %s\n' "${paths[@]}"; return 0
  fi
  cmd_provision "$repo_url" "$repopass" "$mode" "$sched" 0 "$ver" - "${paths[@]}" || { echo "provisioning failed" >&2; return 2; }
  echo "OK adopted -> the panel layout ($mode, $sched)"
}

cmd_process_spool() {
  local req id action mode schedule paths out ok k v line repo_url repopass delay ver cacert_b64 engine container
  for req in "$REQ_DIR"/*.req; do
    [ -f "$req" ] || continue
    id="$(basename "$req" .req)"
    action=""; mode="exclude"; schedule=""; paths=(); repo_url=""; repopass=""; delay="1h"; ver="$RESTIC_TARGET_VER"; cacert_b64="-"; engine=""; container=""
    # IMPORTANT: not `IFS='=' read -r k v` - that trims a trailing '=' (base64 padding of cacert).
    # The line is read whole and split on the first '=' with parameter expansion.
    while IFS= read -r line; do
      k="${line%%=*}"; v="${line#*=}"
      case "$k" in
        action) action="$v";; mode) mode="$v";; schedule) schedule="$v";; path) paths+=("$v");;
        repo_url) repo_url="$v";; repopass) repopass="$v";; delay) delay="$v";;
        restic_version) ver="$v";; cacert_b64) cacert_b64="$v";;
        engine) engine="$v";; container) container="$v";;
        dump_dir) export KV_DUMP_DIR="$v";;
        dump_keep) export KV_DUMP_KEEP="$v";;
        dump_minfree) export KV_DUMP_MINFREE="$v";;
      esac
    done < "$req"
    rm -f "$req"  # secrets (repopass) do not linger on disk
    out=""; ok=false
    case "$action" in
      set_schedule) if out="$(cmd_set_schedule "$schedule" 2>&1)"; then ok=true; fi ;;
      set_paths)    if out="$(cmd_set_paths "$mode" "${paths[@]}" 2>&1)"; then ok=true; fi ;;
      run_now)      if out="$(cmd_run_now 2>&1)"; then ok=true; fi ;;
      restic_update) if out="$(cmd_restic_update 2>&1)"; then ok=true; fi ;;
      provision)    if out="$(cmd_provision "$repo_url" "$repopass" "$mode" "$schedule" "$delay" "$ver" "$cacert_b64" "${paths[@]}" 2>&1)"; then ok=true; fi ;;
      adopt)        if out="$(cmd_adopt "$ver" 2>&1)"; then ok=true; fi ;;
      get_creds)    if out="$(cmd_get_creds 2>&1)"; then ok=true; fi ;;
      dump_setup)   if out="$(cmd_dump_setup "$engine" "$container" 2>&1)"; then ok=true; fi ;;
      dump_remove)  if out="$(cmd_dump_remove "$engine" "$container" 2>&1)"; then ok=true; fi ;;
      *) out="unknown action" ;;
    esac
    printf 'ok=%s\noutput=%s\n' "$ok" "$(printf '%s' "$out" | tr '\n' ' ')" > "$RES_DIR/$id.res.tmp"
    mv -f "$RES_DIR/$id.res.tmp" "$RES_DIR/$id.res"; chmod 0644 "$RES_DIR/$id.res"
  done
  refresh_config  # refresh the config file for the panel after the changes
}

case "${1:-}" in
  get-config)    cmd_get_config ;;
  refresh)       refresh_config ;;
  set-paths)     shift; cmd_set_paths "$@" ;;
  set-schedule)  shift; cmd_set_schedule "$@" ;;
  run-now)       cmd_run_now ;;
  restic-update) cmd_restic_update ;;
  provision)     shift; cmd_provision "$@" ;;
  adopt)         shift; cmd_adopt "$@" ;;
  get-creds)     cmd_get_creds ;;
  dump-setup)    shift; cmd_dump_setup "$@" ;;
  dump-remove)   shift; cmd_dump_remove "$@" ;;
  dump-status)   cmd_dump_status ;;
  process-spool) cmd_process_spool ;;
  *) echo "usage: $0 {get-config|set-paths <mode> <path...>|set-schedule <HH:MM>|run-now|provision <url> <repopass> <mode> <HH:MM> <delay> <ver> <cacert_b64|-> <path...>|dump-setup <engine> [container]|dump-remove <engine>|dump-status|process-spool}" >&2; exit 2 ;;
esac
HELPER_EOF
chmod 0755 "$HELPER"; chown root:root "$HELPER"

# one immediate run plus a cron entry every minute: the config file for the panel
"$HELPER" refresh 2>/dev/null || true
cat > "$CRON" <<CRON_EOF
* * * * * root $HELPER refresh >/dev/null 2>&1
CRON_EOF
chmod 0644 "$CRON"

# path unit: as soon as the agent drops a request into the spool, root executes it immediately
cat > /etc/systemd/system/kervax-backup-req.service <<UNIT_EOF
[Unit]
Description=Kervax backup request processor
[Service]
Type=oneshot
ExecStart=$HELPER process-spool
UNIT_EOF
cat > /etc/systemd/system/kervax-backup-req.path <<UNIT_EOF
[Unit]
Description=Kervax backup request spool watch
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-backup-req.service
[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
systemctl enable --now kervax-backup-req.path >/dev/null 2>&1 || true
# in case a trigger was missed, pick up any requests already waiting
"$HELPER" process-spool >/dev/null 2>&1 || true

echo "backup-setup: done -> $HELPER; config in $CONF_JSON (cron), commands through the spool $REQ_DIR."
echo "backup-setup: current config:"; head -c 500 "$CONF_JSON" 2>/dev/null; echo
