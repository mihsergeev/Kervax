#!/usr/bin/env bash
# Kervax: enable statistics AND PROVISIONING of a backup server (rest-server) for the panel.
#
# Running as the unprivileged `kervax` with NoNewPrivileges, the agent cannot use sudo and
# cannot see the restic repositories in /app/rest-server/data/<client> (root 0700). So:
#  * a root cron job runs a read-only helper and writes statistics into
#    /var/lib/kervax/backupserver.json (world-readable) - the agent simply reads the file;
#  * PROVISIONING (htpasswd/init/ufw/prune/tls-front) goes THROUGH A SPOOL: the agent drops a
#    request into /var/lib/kervax/bsrv-req (0600, with hpass/repopass secrets inside), a root
#    path unit runs a narrow helper and writes the answer to /var/lib/kervax/bsrv-res.
# The agent stays fully isolated. Actions come from a strict allowlist, names and IPs are
# validated, and secrets in a request live only until it is processed and are then removed.
# Run as root.
set -euo pipefail

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-backupserver-helper"
STATE_DIR=/var/lib/kervax
STATS="$STATE_DIR/backupserver.json"
REQ_DIR="$STATE_DIR/bsrv-req"
RES_DIR="$STATE_DIR/bsrv-res"
CRON=/etc/cron.d/kervax-backupserver
AGENT_USER=kervax
# the helper serves the agent (the spool belongs to the kervax group), so without an agent it
# is pointless. Without this check it failed with an opaque "install: invalid group: kervax".
if ! getent group "$AGENT_USER" >/dev/null 2>&1; then
  echo "No Kervax agent on this node (group '$AGENT_USER' is missing)." >&2
  echo "Add the node in the panel first (Servers -> Add), then run this script." >&2
  exit 2
fi

# v2: provision-client connects to an ALREADY EXISTING repository (reusing its password)
#     instead of breaking it with a new one - otherwise the client, prune and restore all got
#     the wrong password
# v3: deploy-server - bring up a rest-server from scratch on a clean node (docker/htpasswd/
#     restic from the distribution repositories, plus a compose file with --append-only
#     --private-repos baked in)
# v4: stats also reads retention from the legacy monolith /etc/systemd-rest.conf (read only)
# v5: stats reports lock_ts - the panel tells a lock held by a RUNNING backup from a stale one
#     (the alert used to be false)
# v6 (0.14): TLS without a caddy layer - deploy_tls_front brings up a SECOND rest-server with
#           native --tls on :64101 (same data and htpasswd) and removes the old caddy front
#           (migration)
# v7 (0.15): the TLS project moved into its own directory /app/rest-server-tls (it used to be
#           buried in system/, next to scripts and metrics); deploy_tls_front migrates the old
#           layout
# v8 (0.16): FIX: prune-env was written WITHOUT `set -a`, so the variables were not exported,
#           restic inside the prune script never saw RESTIC_REPOSITORY/PASSWORD and the
#           cleanup silently did nothing ("repo not accessible", success=0) for EVERY
#           repository created by the panel. The env is exported now, and the installer
#           repairs already created envs (migration below).
# 0.23: who cleans each repository (its own prune script, the legacy monolith from cron, nobody)
#       and when snapshots were last removed from it, in report.d/backup-server.json; lock_ts
#       is the OLDEST stale lock when there is one; oldest_snapshot from the snapshot files
# 0.24: the monolith's blocks for repositories that no longer exist (it errors on them daily)
# 0.25: adopt-legacy moves repositories off the monolith onto their own prune scripts; prune
#       scripts run a weekly restic check (the monolith checked daily), stats report it
# 0.26: the cleanup runs 12 hours away from the client's backup (prune locks the repository
#       exclusively, a backup starting meanwhile failed)
# 0.27: restic-update brings the server's own restic to 0.19.1 (the installer runs it), stats
#       report the version the server runs
# 0.28: stats report the newest snapshot times (the panel learns how often each client backs
#       up) and the snapshot groups the prune script finds; the weekly check reads a slice
#       of the data too; forget-group removes an old group by hand
# 0.29: root crontab lines that run our prune scripts a second time (left by the ansible role)
#       or start a prune script that is gone are commented out; the prune minute comes from the
#       repository name, so updates no longer move it
# 0.30: the cleanup log keeps the reason restic gives when it cannot open the repository, stats
#       report the error of the last failed cleanup run
KERVAX_SETUP_VERSION=0.30  # MAJOR.MINOR; compared component-wise (0.13 > 0.2!)
# Which nodes need this helper at all. Read by the ansible playbook on the
# CONTROL machine and evaluated as a shell condition ON THE NODE, so a new
# helper lands where it belongs without anyone editing the playbook.
# Only on a backup server. Elsewhere there are no restic repositories to report on.
KERVAX_SETUP_WHEN="[ -d /app/rest-server ] || [ -d /app/rest-server-tls ] || [ -d /srv/rest-server ] || (command -v docker >/dev/null 2>&1 && docker ps 2>/dev/null | grep -qi rest-server)"
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" /var/lib/kervax/versions
echo "$KERVAX_SETUP_VERSION" > /var/lib/kervax/versions/backupserver-setup.ver
chmod 0644 /var/lib/kervax/versions/backupserver-setup.ver  # explicit: the agent (kervax) must read it
# the file and spool scheme needs no sudo - drop the old sudoers rule (if any)
rm -f /etc/sudoers.d/kervax-backupserver 2>/dev/null || true
# spool: the agent (kervax) drops requests (needs -wx on the directory) and reads answers (r-x)
install -d -o root -g "$AGENT_USER" -m 0730 "$REQ_DIR"
# 0770: the agent (kervax) must DELETE an answer once read, otherwise res files pile up
install -d -o root -g "$AGENT_USER" -m 0770 "$RES_DIR"
# under ProtectSystem=strict the agent may write only to its own bin directory, so the spool
# is allowed explicitly; otherwise /var/lib/kervax is read-only and the spool does not work.
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
# Kervax backup-server helper (root): read-only stats plus narrow client provisioning.
set -euo pipefail

DATA=/app/rest-server/data
HTPASSWD="$DATA/.htpasswd"
COMPOSE=/app/rest-server/docker-compose.yml

# The actual rest-server port. The panel allows deploying it on a custom port (the form has a
# field), while client provisioning used to use the CONSTANT 64100 - a server on a different
# port deployed successfully and the next step failed with an opaque "init failed". The port is
# taken from where it is actually recorded.
rest_port() {
  local p=""
  [ -f "$COMPOSE" ] && p="$(grep -oE '"[0-9]+:8000"' "$COMPOSE" | head -1 | grep -oE '^"[0-9]+' | tr -d '"')"
  printf '%s' "${p:-$REST_PORT}"
}
PRUNE_DIR=/app/rest-server/system/scripts
ENV_DIR=/app/rest-server/system/envs
LOG_DIR=/app/rest-server/system/logs
# Rotation metrics are written WHERE THEY ARE READ: the standard textfile_collector, parsed by
# both the agent and node-exporter. The own directory remains a second location (older builds
# expect them there), but nothing reads it - which is how a dead rotation looked green for 17
# days.
NE_METRICS_DIR=/var/lib/node_exporter/textfile_collector
METRICS_DIR=/app/rest-server/system/metrics
# The TLS rest-server lives in its OWN compose project next to the main one (not in system/
# among the scripts, and not as a second service in /app/rest-server/docker-compose.yml - that
# file is ansible-managed).
TLS_DIR=/app/rest-server-tls
TLS_DIR_OLD=/app/rest-server/system/kervax-tls  # the 0.14 layout (migrated in deploy_tls_front)
REST_PORT=64100
TLS_PORT=64101
HELPER_VER=1  # backupserver-helper version; the panel flags nodes with an old helper
REQ_DIR=/var/lib/kervax/bsrv-req
RES_DIR=/var/lib/kervax/bsrv-res
# the rest-server image is BAKED into the helper (the panel does not choose it - that would be an image substitution vector)
REST_IMAGE="restic/rest-server:0.14.0"
# restic for the server (init/cat config/prune). The system one, otherwise our own in /lib65
# (NOT /usr: /usr is excluded from backups). Order matters: live servers already have the
# system binary.
KERVAX_RESTIC=/lib65/kervax/restic
RESTIC_BIN="$(command -v restic || true)"
[ -n "$RESTIC_BIN" ] || RESTIC_BIN="$KERVAX_RESTIC"
# The restic the server runs itself is brought to this version (restic-update, the installer
# runs it too). The one from the distribution is old: Debian 12 ships 0.14, and before 0.16
# restic cannot wait for a lock by itself. The sha256 of the .bz2 from github is BAKED in here,
# the same version and sums as in backup-setup.sh.
RESTIC_TARGET_VER="0.19.1"
RESTIC_SHA_amd64="f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c"
RESTIC_SHA_arm64="a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465"
restic_ver() { "$1" version 2>/dev/null | grep -oE '^restic [0-9]+\.[0-9]+\.[0-9]+' | awk '{ print $2 }' || true; }
ver_lt() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$1" ]; }
# every restic the server runs: the one new prune scripts get plus whatever the existing scripts
# were generated with (their BIN= line), each path once
server_restic_bins() {
  { [ -x "$RESTIC_BIN" ] && echo "$RESTIC_BIN"; sed -n 's/^BIN=//p' "$PRUNE_DIR"/restic-prune-*.sh 2>/dev/null; } \
    | grep -E '^/[A-Za-z0-9._/+-]+$' | awk '!seen[$0]++' || true
}
# the oldest of them: that is the version the panel compares with the target
server_restic_min() {
  local b v min=""
  while IFS= read -r b; do
    [ -x "$b" ] || continue
    v="$(restic_ver "$b")"; [ -n "$v" ] || continue
    if [ -z "$min" ] || ver_lt "$v" "$min"; then min="$v"; fi
  done < <(server_restic_bins)
  printf '%s' "$min"
}

# where the certificate actually is: the new layout, otherwise the old one (a node may not
# have gone through the migration - stats/get-cert/ufw must see HTTPS either way)
tls_dir() {
  if [ -f "$TLS_DIR/cert.pem" ]; then echo "$TLS_DIR"
  elif [ -f "$TLS_DIR_OLD/cert.pem" ]; then echo "$TLS_DIR_OLD"
  else echo "$TLS_DIR"; fi
}

json_escape() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
# extract the keep-<x> number from a prune script (--keep-last 3 ...). 0 if absent (pipefail-safe).
keep_of() {
  [ -f "$2" ] || { echo 0; return 0; }
  local v; v=$(grep -oE -- "--$1[= ]+[0-9]+" "$2" 2>/dev/null | grep -oE '[0-9]+' | head -1 || true)
  echo "${v:-0}"
}
# LEGACY (read only, for display): on servers brought up by hand or by ansible the cleanup is
# sometimes a single monolith - repositories appear as blocks "RESTIC_REPOSITORY=<data>/<name>"
# ... "restic forget --keep-*". Without this the panel showed an empty policy for repositories
# that were in fact being cleaned. The panel changes NOTHING here: new backups always get their
# own per-repo script (install_prune).
LEGACY_PRUNE=/etc/systemd-rest.conf
keep_of_legacy() {
  local flag="$1" name="$2" v
  [ -f "$LEGACY_PRUNE" ] || { echo 0; return 0; }
  # a block runs from the line of THIS repository to the next RESTIC_REPOSITORY=. The match is
  # exact: a substring `backup-01` would also catch `backup-01-dev` and the policy would be
  # taken from a neighbour.
  v=$(awk -v repo="RESTIC_REPOSITORY=$DATA/$name" '
        $0 == repo { inblk = 1; next }
        inblk && /^[[:space:]]*RESTIC_REPOSITORY=/ { exit }
        inblk { print }
      ' "$LEGACY_PRUNE" 2>/dev/null \
      | grep -oE -- "--$flag[= ]+[0-9]+" | grep -oE '[0-9]+' | head -1 || true)
  echo "${v:-0}"
}
# Whether the monolith actually RUNS, and when. The policy in the file said nothing about it: on
# backup-a the monolith cleaned a dozen repositories from the root crontab, the panel
# labelled them "the server does not clean", and when a stale lock stopped the cleanup of one of
# them for 23 days nothing pointed at the right place.
# Sets L_WHERE (where it is scheduled), L_SCHED (the schedule) and L_LOG (where its output goes:
# the file's mtime is the end of the last run).
LEGACY_RE='/etc/systemd-rest\.conf'
legacy_schedule() {
  L_WHERE=""; L_SCHED=""; L_LOG=""
  local f line
  for f in /var/spool/cron/crontabs/root /var/spool/cron/root /etc/crontab /etc/cron.d/*; do
    [ -f "$f" ] || continue
    # the exact path: a substring would also catch systemd-rest.conf.bak
    line="$(grep -vE '^[[:space:]]*#' "$f" 2>/dev/null | grep -E "(^|[[:space:]])${LEGACY_RE}([[:space:];|&<>]|\$)" | head -1 || true)"
    [ -n "$line" ] || continue
    case "$f" in /var/spool/*) L_WHERE="crontab root" ;; *) L_WHERE="$f" ;; esac
    L_SCHED="$(printf '%s\n' "$line" | awk '{ if ($1 ~ /^@/) print $1; else print $1" "$2" "$3" "$4" "$5 }')"
    L_LOG="$(printf '%s\n' "$line" | grep -oE '(tee([[:space:]]+-a)?|>>?)[[:space:]]*/[^[:space:];|&<>]+' | tail -1 | grep -oE '/[^[:space:];|&<>]+$' || true)"
    case "$L_LOG" in /dev/*) L_LOG="" ;; esac
    return 0
  done
  # or a systemd service, with the schedule in the timer next to it
  for f in /etc/systemd/system/*.service; do
    [ -f "$f" ] || continue
    grep -qE "^ExecStart=.*${LEGACY_RE}" "$f" 2>/dev/null || continue
    L_WHERE="${f##*/}"
    L_SCHED="$(grep -m1 -E '^OnCalendar=' "${f%.service}.timer" 2>/dev/null | cut -d= -f2- || true)"
    return 0
  done
  return 0
}
# The agent forwards report.d blocks to the panel as they are, so new fields need no agent release
REPORT_EXTRA=/var/lib/kervax/report.d/backup-server.json
# Snapshot ids seen on the previous run, per repository. An id that is gone means somebody
# removed a snapshot: the server prune, the monolith or the client itself. Prune metrics exist
# only for our own scripts, so for everything else this is the only sign that rotation is alive.
SNAP_STATE=/var/lib/kervax/bsrv-snaps
# Snapshot groups of a repository (host and tags), written by its prune script once a night:
# stats have no repository password and must not run restic every minute anyway.
GROUPS_DIR=/var/lib/kervax/bsrv-groups
# The error of the last cleanup run of a repository, when it failed or skipped the cleanup: what
# the panel shows next to "cleanup is not working". The run's own ERROR or SAFETY line first
# (repo not accessible, with the restic reason since 0.30), otherwise the last restic Fatal or
# lock line of that run. Only the log tail is read; nothing for a run that went fine.
prune_last_error() {
  tail -n 400 "$1" 2>/dev/null | awk '
    /=== .* start forget\/prune ===/ { err = ""; fat = ""; ok = ""; next }
    (/^ERROR:/ || /^SAFETY:/) && err == "" { err = $0 }
    /^(Fatal|error):/ || /^unable to / { fat = $0 }
    /: done \(success=/ { ok = ($0 ~ /success=1/) ? "1" : "0" }
    END {
      if (err ~ /^SAFETY:/) print err
      else if (ok == "0") print (err != "" ? err : fat)
    }' | tr -cd '[:print:]' | cut -c1-200 || true
}
# client name validation (it is also the repository and htpasswd user name): hostname-safe characters only
valid_name() { case "$1" in ''|*[!A-Za-z0-9._-]*) return 1 ;; *) return 0 ;; esac; }
valid_ip()   { [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || [[ "$1" =~ ^[0-9A-Fa-f:]+$ ]]; }
valid_num()  { [[ "$1" =~ ^[0-9]+$ ]]; }

cmd_stats() {
  if [ ! -d "$DATA" ]; then echo '{"present":false}'; return; fi
  # the REAL version from the binary rather than the compose tag: a "latest" tag lied - an old
  # image (0.11) could be stuck on disk, docker does not re-pull it and the panel believed it
  # was current.
  local ver="" cid
  cid="$(docker ps -qf 'name=rest-server' 2>/dev/null | head -1)"
  [ -n "$cid" ] && ver="$(docker exec "$cid" rest-server --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  # fall back to the compose tag if the container is missing or did not answer
  [ -n "$ver" ] || { [ -f "$COMPOSE" ] && ver="$(grep -oE 'rest-server:[A-Za-z0-9._-]+' "$COMPOSE" | head -1 | cut -d: -f2)"; }
  local repos_json="" extra_json="" repo name snaps last locked lock_ts valid size prune kl kd kw km
  local oldest cleaner ids sf removed_ts seen_since chk_ts chk_ok chk_part chk_parts recent grp perr pmt now_ts legacy_list="" lcount=0
  now_ts="$(date +%s)"
  install -d -m 0700 "$SNAP_STATE" 2>/dev/null || true
  # repositories the monolith has a block for (the same exact line keep_of_legacy looks for)
  if [ -f "$LEGACY_PRUNE" ]; then
    legacy_list="$(awk -v p="RESTIC_REPOSITORY=$DATA/" 'index($0, p) == 1 { print substr($0, length(p) + 1) }' "$LEGACY_PRUNE" 2>/dev/null || true)"
    [ -n "$legacy_list" ] && lcount="$(grep -c . <<<"$legacy_list" || true)"
  fi
  for repo in "$DATA"/*/; do
    [ -d "$repo" ] || continue
    name="$(basename "$repo")"
    valid=false; [ -f "${repo}config" ] && valid=true
    [ "$valid" = true ] || [ -d "${repo}data" ] || [ -d "${repo}snapshots" ] || continue
    snaps=0
    [ -d "${repo}snapshots" ] && snaps=$(find "${repo}snapshots" -maxdepth 1 -type f 2>/dev/null | wc -l)
    last=0; oldest=0
    if [ -d "${repo}snapshots" ]; then
      last=$(find "${repo}snapshots" -maxdepth 1 -type f -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)
      # the oldest snapshot file: without prune metrics this is the only way to see that old
      # snapshots stopped going away (the panel compares it with the retention policy)
      oldest=$(find "${repo}snapshots" -maxdepth 1 -type f -printf '%T@\n' 2>/dev/null | sort -n | head -1 | cut -d. -f1 || true)
      [ -n "$oldest" ] || oldest=0
    fi
    [ -z "$last" ] || [ "$last" = 0 ] && last="$(stat -c %Y "$repo" 2>/dev/null || echo 0)"
    # A lock by itself is not a problem: restic holds one for the whole backup and refreshes it
    # every 5 minutes. A lock that has not been refreshed for 30+ minutes is stale, and it
    # blocks prune, forget and check whatever else is running. When there is one, lock_ts is
    # the OLDEST stale lock, so its age says how long the repository has been blocked: with the
    # newest lock a running backup hid a stale one, and a crash every night looked like a fresh
    # lock every morning. Without stale locks it is the newest lock, as before.
    locked=false; lock_ts=0
    if [ -d "${repo}locks" ]; then
      lock_ts=$(find "${repo}locks" -maxdepth 1 -type f -mmin +30 -printf '%T@\n' 2>/dev/null | sort -n | head -1 | cut -d. -f1 || true)
      [ -n "$lock_ts" ] || lock_ts=$(find "${repo}locks" -maxdepth 1 -type f -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1 || true)
      [ -z "$lock_ts" ] && lock_ts=0
      [ "$lock_ts" != 0 ] && locked=true
    fi
    size="$(du -sb "$repo" 2>/dev/null | cut -f1 || true)"; [ -z "$size" ] && size=0
    prune="$PRUNE_DIR/restic-prune-$name.sh"
    cleaner=""
    if [ -f "$prune" ]; then
      cleaner=script
      kl=$(keep_of keep-last "$prune"); kd=$(keep_of keep-daily "$prune")
      kw=$(keep_of keep-weekly "$prune"); km=$(keep_of keep-monthly "$prune")
    else
      # no dedicated script - the repository may be cleaned by the legacy monolith (see keep_of_legacy)
      [ -n "$legacy_list" ] && grep -qxF -- "$name" <<<"$legacy_list" && cleaner=legacy
      kl=$(keep_of_legacy keep-last "$name"); kd=$(keep_of_legacy keep-daily "$name")
      kw=$(keep_of_legacy keep-weekly "$name"); km=$(keep_of_legacy keep-monthly "$name")
    fi
    # removals: ids that were here on the previous run and are gone now
    ids="$(find "${repo}snapshots" -maxdepth 1 -type f -printf '%f\n' 2>/dev/null | LC_ALL=C sort || true)"
    sf="$SNAP_STATE/$name"
    if [ -f "$sf.ids" ]; then
      if [ -n "$(LC_ALL=C comm -13 <(printf '%s\n' "$ids") "$sf.ids" 2>/dev/null | head -1 || true)" ]; then
        echo "$now_ts" > "$sf.removed" 2>/dev/null || true
      fi
    else
      echo "$now_ts" > "$sf.since" 2>/dev/null || true  # watching starts now
    fi
    if [ "$ids" != "$(cat "$sf.ids" 2>/dev/null || true)" ]; then
      printf '%s\n' "$ids" > "$sf.ids.tmp" 2>/dev/null && mv -f "$sf.ids.tmp" "$sf.ids" 2>/dev/null || true
    fi
    removed_ts="$(cat "$sf.removed" 2>/dev/null || true)"; [[ "$removed_ts" =~ ^[0-9]+$ ]] || removed_ts=0
    seen_since="$(cat "$sf.since" 2>/dev/null || true)"; [[ "$seen_since" =~ ^[0-9]+$ ]] || seen_since=0
    # the weekly integrity check of our prune script (its metrics file; the agent does not read these)
    chk_ts=0; chk_ok=-1
    if [ -f "$NE_METRICS_DIR/restic_server_$name.prom" ]; then
      chk_ts="$(awk '$1 ~ /^restic_server_check_timestamp/ { v = $2 } END { print v + 0 }' "$NE_METRICS_DIR/restic_server_$name.prom" 2>/dev/null || true)"
      chk_ok="$(awk '$1 ~ /^restic_server_check_success/ { v = $2 } END { print (v == "" ? -1 : v) }' "$NE_METRICS_DIR/restic_server_$name.prom" 2>/dev/null || true)"
    fi
    [[ "$chk_ts" =~ ^[0-9]+$ ]] || chk_ts=0
    [[ "$chk_ok" =~ ^-?[01]$ ]] || chk_ok=-1
    chk_part=0; chk_parts=0
    if [ -f "$NE_METRICS_DIR/restic_server_$name.prom" ]; then
      chk_part="$(awk '$1 ~ /^restic_server_check_read_part\{/ { v = $2 } END { print v + 0 }' "$NE_METRICS_DIR/restic_server_$name.prom" 2>/dev/null || true)"
      chk_parts="$(awk '$1 ~ /^restic_server_check_read_parts\{/ { v = $2 } END { print v + 0 }' "$NE_METRICS_DIR/restic_server_$name.prom" 2>/dev/null || true)"
    fi
    [[ "$chk_part" =~ ^[0-9]+$ ]] || chk_part=0
    [[ "$chk_parts" =~ ^[0-9]+$ ]] || chk_parts=0
    # The newest snapshot times: the panel learns from them how often the client backs up and
    # notices a backup that did not come within hours instead of the fixed 3 days. Older ones
    # are thinned by the retention policy and say nothing about the rhythm.
    recent="$(find "${repo}snapshots" -maxdepth 1 -type f -printf '%T@\n' 2>/dev/null | cut -d. -f1 | sort -rn | head -8 | paste -sd, - || true)"
    [[ "$recent" =~ ^[0-9,]*$ ]] || recent=""
    perr=""
    [ -f "$LOG_DIR/restic-prune-$name.log" ] && perr="$(prune_last_error "$LOG_DIR/restic-prune-$name.log")"
    # when the prune script was (re)generated: a script that never ran within days of that is
    # stuck, while a repository just moved onto its own script simply waits for the night
    pmt=0; [ -f "$prune" ] && pmt="$(stat -c %Y "$prune" 2>/dev/null || echo 0)"
    [[ "$pmt" =~ ^[0-9]+$ ]] || pmt=0
    # groups from the prune script, when there is more than one (see GROUPS_PY); a damaged
    # file must not break the whole block, so only something that looks like one JSON object
    grp=null
    if [ -f "$GROUPS_DIR/$name.json" ] && [ "$(stat -c %s "$GROUPS_DIR/$name.json" 2>/dev/null || echo 99999)" -le 16384 ]; then
      grp="$(tr -d '\n' < "$GROUPS_DIR/$name.json" 2>/dev/null || true)"
      case "$grp" in '{"ts": '*'}') ;; *) grp=null ;; esac
    fi
    repos_json="${repos_json:+$repos_json,}{\"name\":\"$(json_escape "$name")\",\"valid\":$valid,\"snapshots\":$snaps,\"last_activity\":$last,\"oldest_snapshot\":$oldest,\"locked\":$locked,\"lock_ts\":$lock_ts,\"size_bytes\":$size,\"keep_last\":${kl:-0},\"keep_daily\":${kd:-0},\"keep_weekly\":${kw:-0},\"keep_monthly\":${km:-0}}"
    extra_json="${extra_json:+$extra_json,}\"$(json_escape "$name")\":{\"cleaner\":\"$cleaner\",\"removed_ts\":$removed_ts,\"seen_since\":$seen_since,\"check_ts\":$chk_ts,\"check_ok\":$chk_ok,\"check_part\":$chk_part,\"check_parts\":$chk_parts,\"recent\":[$recent],\"groups\":$grp,\"prune_err\":\"$(json_escape "$perr")\",\"prune_mtime\":$pmt}"
  done
  # watch state of repositories that are gone
  for sf in "$SNAP_STATE"/*.ids; do
    [ -f "$sf" ] || continue
    name="${sf##*/}"; name="${name%.ids}"
    [ -d "$DATA/$name" ] || rm -f "$SNAP_STATE/$name.ids" "$SNAP_STATE/$name.since" "$SNAP_STATE/$name.removed" 2>/dev/null || true
  done
  for sf in "$GROUPS_DIR"/*.json; do
    [ -f "$sf" ] || continue
    name="${sf##*/}"; name="${name%.json}"
    [ -d "$DATA/$name" ] || rm -f "$sf" 2>/dev/null || true
  done
  local legacy_json=null log_ts=0 missing="" ln
  if [ -n "$legacy_list" ]; then
    legacy_schedule
    [ -n "$L_LOG" ] && [ -f "$L_LOG" ] && log_ts="$(stat -c %Y "$L_LOG" 2>/dev/null || echo 0)"
    # Blocks for repositories that are gone. The monolith still checks them every run and sends
    # an error to Telegram for each one, every day: on backup-a 8 of its 15 blocks, and the
    # real error of app-a drowned in that noise.
    while IFS= read -r ln; do
      [ -n "$ln" ] || continue
      [ -d "$DATA/$ln" ] || missing="${missing:+$missing,}\"$(json_escape "$ln")\""
    done <<<"$legacy_list"
    legacy_json="{\"script\":\"$LEGACY_PRUNE\",\"repos\":${lcount:-0},\"where\":\"$(json_escape "$L_WHERE")\",\"schedule\":\"$(json_escape "$L_SCHED")\",\"log\":\"$(json_escape "$L_LOG")\",\"log_ts\":${log_ts:-0},\"missing\":[$missing]}"
  fi
  install -d -m 0755 "${REPORT_EXTRA%/*}" 2>/dev/null || true
  if printf '{"v":1,"ts":%s,"restic":"%s","restic_target":"%s","legacy":%s,"repos":{%s}}\n' \
      "$now_ts" "$(server_restic_min)" "$RESTIC_TARGET_VER" "$legacy_json" "$extra_json" > "$REPORT_EXTRA.tmp" 2>/dev/null; then
    chmod 0644 "$REPORT_EXTRA.tmp" 2>/dev/null && mv -f "$REPORT_EXTRA.tmp" "$REPORT_EXTRA" 2>/dev/null || true
  fi
  # Space on the volume WITH THE REPOSITORIES rather than on /: they often sit on a separate
  # disk, and the node's overall disk metric says nothing about backup storage filling up.
  # df -P: POSIX output, one line per filesystem (without -P long device names wrap).
  local dfl d_total=0 d_used=0 d_free=0
  dfl="$(df -PB1 "$DATA" 2>/dev/null | awk 'NR==2{printf "%s %s %s", $2, $3, $4}')"
  case "$dfl" in
    [0-9]*)
      d_total="${dfl%% *}"; dfl="${dfl#* }"
      d_used="${dfl%% *}"; d_free="${dfl##* }" ;;
  esac
  # whether our TLS front exists (tells the panel which transport it may offer)
  local tls=false; [ -f "$(tls_dir)/cert.pem" ] && tls=true
  # whether the container IS RUNNING: the agent can see that only through the docker proxy, and
  # a fresh backup server usually has none, so without this field the panel considered the
  # rest-server stopped and sent a false alert. The helper, running as root, knows for sure.
  local running=false
  if command -v docker >/dev/null 2>&1; then
    [ "$(docker inspect -f '{{.State.Running}}' rest-server 2>/dev/null)" = "true" ] && running=true
  fi
  # port is the ACTUAL rest-server port: the panel builds the client's repository address from
  # it. It used to substitute a constant and missed a server deployed on a different port.
  printf '{"present":true,"version":"%s","helper_version":%s,"running":%s,"port":%s,"tls_front":%s,"tls_port":%s,"data_dir":"%s","disk_total":%s,"disk_used":%s,"disk_free":%s,"repos":[%s]}\n' \
    "$ver" "$HELPER_VER" "$running" "$(rest_port)" "$tls" "$TLS_PORT" "$(json_escape "$DATA")" "$d_total" "$d_used" "$d_free" "$repos_json"
}
refresh_stats() { cmd_stats > "$STATE_DIR/backupserver.json.tmp" 2>/dev/null && mv -f "$STATE_DIR/backupserver.json.tmp" "$STATE_DIR/backupserver.json" && chmod 0644 "$STATE_DIR/backupserver.json"; }

# ------- client provisioning: htpasswd + init + ufw + prune (transport agnostic) -------
install_prune() {
  local name="$1" kl="$2" kd="$3" kw="$4" km="$5" repopass="$6"
  install -d -m 0755 "$PRUNE_DIR" "$ENV_DIR" "$LOG_DIR" "$METRICS_DIR" "$NE_METRICS_DIR"
  # per-client env (repo is a local path; the password is needed for forget/prune on the server)
  umask 077
  # `set -a` is mandatory: the prune script only sources the env, while restic is a CHILD
  # process and sees only EXPORTED variables. Without it the result was "repo not accessible"
  # and the cleanup did nothing.
  cat > "$ENV_DIR/$name.env" <<ENVEOF
# generated by kervax for $name
set -a
RESTIC_REPOSITORY="$DATA/$name"
RESTIC_PASSWORD="$repopass"
set +a
ENVEOF
  chown root:root "$ENV_DIR/$name.env"; chmod 0600 "$ENV_DIR/$name.env"
  umask 022
  write_prune_script "$name" "$kl" "$kd" "$kw" "$km"
}

# The hour of the repository's cleanup: 12 hours away from when its client backs up. prune holds
# an exclusive lock, and a client backup that starts meanwhile fails ("repository is already
# locked exclusively"): neither our client script nor the ansible one waits for the lock. The
# typical backup hour is the most frequent hour of the snapshot files (a snapshot is written when
# the backup ends). The legacy shared script ran at 16:30, away from the night backups, and never
# met them; moving its repositories into the night window put some prunes right on top of a
# backup. A repository without snapshots yet keeps the old night window (02:00-07:59).
prune_hour() {
  local h
  h="$(find "$DATA/$1/snapshots" -maxdepth 1 -type f -printf '%TH\n' 2>/dev/null | sort | uniq -c | sort -rn | awk 'NR == 1 { print $2 + 0 }' || true)"
  if [[ "$h" =~ ^[0-9]+$ ]]; then
    echo $(( (h + 12) % 24 ))
  else
    echo $(( ( $(printf '%s' "$1" | cksum | cut -d' ' -f1) % 6 ) + 2 ))
  fi
}

# Generates ONLY the script and its cron entry, never the env: that holds the repository
# password, and on regeneration there is nowhere to take it from (nor any need).
write_prune_script() {
  local name="$1" kl="$2" kd="$3" kw="$4" km="$5"
  install -d -m 0755 "$PRUNE_DIR" "$LOG_DIR" "$METRICS_DIR" "$NE_METRICS_DIR"
  # per-client prune script: retention lives in the KEEP=(...) line, which the panel reads via keep_of.
  local ps="$PRUNE_DIR/restic-prune-$name.sh"
  {
    printf '#!/usr/bin/env bash\n'
    printf '# generated by kervax — forget/prune %s\nset -uo pipefail\n' "$name"
    printf 'CLIENT=%q\n' "$name"
    printf 'BIN=%q\n' "$RESTIC_BIN"
    printf 'ENV_FILE=%q\n' "$ENV_DIR/$name.env"
    printf 'LOG=%q\n' "$LOG_DIR/restic-prune-$name.log"
    printf 'METRICS_FILE=%q\n' "$NE_METRICS_DIR/restic_server_$name.prom"
    printf 'METRICS_FILE_ALT=%q\n' "$METRICS_DIR/restic_server_$name.prom"
    printf 'GROUPS_FILE=%q\n' "$GROUPS_DIR/$name.json"
    printf 'KEEP=(--keep-last %q --keep-daily %q --keep-weekly %q --keep-monthly %q)\n' "$kl" "$kd" "$kw" "$km"
    cat <<'PRUNE_BODY'
set -a; . "$ENV_FILE"; set +a  # restic is a child process: without export it will not see the repository
mkdir -p "$(dirname "$LOG")" "$(dirname "$METRICS_FILE")" "$(dirname "$METRICS_FILE_ALT")"
ts_start=$(date +%s); success=0
snap_before=-1; snap_after=-1; removed=-1
bytes_before=-1; bytes_after=-1; oldest_ts=0
# "<time> <1|0|-1> <part> <parts>" of the last weekly integrity check, see below
CHECK_STATE="/var/tmp/restic-check-${CLIENT}.state"
check_ts=0; check_ok=-1; check_part=0; check_parts=0
CHECK_SLICE=$(( 50 * 1024 * 1024 * 1024 ))  # at most this much data is read per weekly check
# restic snapshots --json on stdin -> the groups file (argv[1]): host, tags, a few paths,
# count, first and last snapshot. More than one group only, otherwise the file is removed.
GROUPS_PY='
import json, os, re, sys, time
from datetime import datetime
out = sys.argv[1]
def unix(t):
    t = re.sub(r"\.\d+", "", t or "").replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(t).timestamp())
    except ValueError:
        return 0
groups = {}
for s in json.load(sys.stdin) or []:
    host = s.get("hostname") or ""
    tags = sorted(s.get("tags") or [])
    g = groups.setdefault(host + "\t" + ",".join(tags),
                          {"host": host, "tags": tags, "paths": [], "n": 0, "first": 0, "last": 0})
    g["n"] += 1
    for p in s.get("paths") or []:
        if p not in g["paths"] and len(g["paths"]) < 3:
            g["paths"].append(p[:120])
    ts = unix(s.get("time"))
    if ts:
        g["first"] = min(g["first"] or ts, ts)
        g["last"] = max(g["last"], ts)
if len(groups) < 2:
    if os.path.exists(out):
        os.remove(out)
    sys.exit(0)
with open(out + ".tmp", "w") as f:
    json.dump({"ts": int(time.time()), "groups": sorted(groups.values(), key=lambda g: -g["last"])[:20]}, f)
os.chmod(out + ".tmp", 0o644)
os.replace(out + ".tmp", out)
'
[ -s "$CHECK_STATE" ] && read -r check_ts check_ok < "$CHECK_STATE"

# Snapshots are counted WITHOUT jq: backup servers do not have it, and the metric sat at -1
# for years. In the --json output there is exactly one "short_id" per snapshot.
count_snaps() { "$BIN" snapshots --json 2>/dev/null | grep -o '"short_id"' | wc -l | tr -d ' '; }
# The timestamp of the OLDEST snapshot (unix). It is the main sign of a living rotation: it
# does not depend on why the rotation stopped - broken grouping, a failed prune, a removed
# cron entry. Fractional seconds are trimmed: not every date accepts them.
oldest_snap_ts() {
  local iso
  iso="$("$BIN" snapshots --json 2>/dev/null | grep -o '"time":"[^"]*"' | cut -d'"' -f4 | sort | head -1)"
  [ -n "$iso" ] || { echo 0; return; }
  date -d "$(printf '%s' "$iso" | sed 's/\.[0-9]*//')" +%s 2>/dev/null || echo 0
}
repo_size() { du -sb "$RESTIC_REPOSITORY" 2>/dev/null | awk '{print $1}'; }
{
  echo "=== $(date -Is) ${CLIENT}: start forget/prune ==="
  # the reason restic gives goes into the log too (wrong password, no config, no access): the
  # panel shows it next to "cleanup is not working" instead of a bare "not accessible"
  if ! cfg_err="$("$BIN" cat config 2>&1 >/dev/null)"; then
    echo "ERROR: repo not accessible at $RESTIC_REPOSITORY: $(printf '%s\n' "$cfg_err" | grep -v '^[[:space:]]*$' | tail -n1)"
  else
    snap_before="$(count_snaps)"; [ -n "$snap_before" ] || snap_before=-1
    bytes_before="$(repo_size)"; [ -n "$bytes_before" ] || bytes_before=-1
    "$BIN" unlock >/dev/null 2>&1 || true
    # Клиентский бэкап и серверная очистка приходят к одному репозиторию, и restic
    # разводит их локом. Расписания рано или поздно встречаются (бэкап в 03:33,
    # очистка в 03:35), и prune просто падал: "repository is already locked" ->
    # success=0 -> алерт "prune упал" на ровном месте, хотя чистить было НЕЧЕГО
    # ровно две минуты. Это очередь, а не ошибка: ждём, пока освободится.
    #
    # --retry-lock умеет ждать внутри самой команды, но появился в restic 0.16;
    # на серверах встречается и 0.14, поэтому там ждём сами, глядя на список локов.
    RETRY_LOCK=""
    if "$BIN" forget --help 2>/dev/null | grep -q -- "--retry-lock"; then
      RETRY_LOCK="--retry-lock 20m"
    else
      waited=0
      while [ "$waited" -lt 1200 ] && [ "$("$BIN" list locks 2>/dev/null | grep -c .)" -gt 0 ]; do
        sleep 60; waited=$((waited+60))
      done
      [ "$waited" -gt 0 ] && echo "ждали освобождения репозитория: $((waited/60)) мин"
    fi
    # SAFETY: prune only when the policy keeps something. An EMPTY policy (every keep-* is 0)
    # would let forget wipe every snapshot. keep-last on its own saying 0 is normal: the
    # hand-written scripts this template replaced never passed --keep-last and rotated fine on
    # keep-daily/weekly/monthly. Reading that absence as "do not prune" once stopped the
    # rotation of an entire backup server for eight days.
    keep_sum=0; for _v in "${KEEP[@]}"; do case "$_v" in [0-9]*) keep_sum=$((keep_sum+_v));; esac; done
    if ! [ "${keep_sum:-0}" -ge 1 ] 2>/dev/null; then
      echo "SAFETY: every keep-* is 0 -> forget/prune SKIPPED (protection against wiping everything)"
      success=1
    else
      # --group-by host,tags: by default restic groups by host+paths and applies the policy to
      # each group separately. If a client backs up a file with a date in its name
      # (shared-20260812-030002.zip.enc), every snapshot forms a group of one, becomes the
      # "last snapshot" in it and is never removed.
      forget_rc=0; "$BIN" forget --group-by host,tags ${RETRY_LOCK} "${KEEP[@]}" 2>&1 || forget_rc=$?
      prune_rc=0;  "$BIN" prune ${RETRY_LOCK} 2>&1 || prune_rc=$?
      [ "$forget_rc" -eq 0 ] && [ "${prune_rc:-0}" -eq 0 ] && success=1
    fi
    # Integrity check once a week. The legacy shared script ran it every day, and moving a
    # repository onto this script must not lose it. The first check of each repository is
    # spread over the week by its name, so a helper update does not check everything in one
    # night. A repository busy with a backup is checked on the next run, not reported broken.
    # Structure and index alone do not see a damaged pack file: that shows up only on a
    # restore. So each check also reads a slice of the data, part N of T, the next part every
    # week: in T weeks every pack has been read once. T keeps a slice at about 50 GiB at most,
    # because restic check holds an exclusive lock and a backup starting meanwhile waits for it
    # (clients wait up to 2 hours since backup-setup 0.33). Repositories up to 400 GiB are read
    # whole in 8 weeks.
    if [ ! -s "$CHECK_STATE" ]; then
      echo "$(( $(date +%s) - ( $(printf '%s' "$CLIENT" | cksum | cut -d' ' -f1) % 7 ) * 86400 )) -1" > "$CHECK_STATE"
    fi
    read -r check_ts check_ok check_part check_parts < "$CHECK_STATE" || true
    if [ $(( $(date +%s) - ${check_ts:-0} )) -ge $(( 7 * 86400 )) ]; then
      parts=8
      if [ "${bytes_before:--1}" -gt 0 ] 2>/dev/null; then
        parts=$(( (bytes_before + CHECK_SLICE - 1) / CHECK_SLICE )); [ "$parts" -ge 8 ] || parts=8
      fi
      part=$(( ${check_part:-0} % parts + 1 ))
      chk_rc=0; chk_out="$("$BIN" check ${RETRY_LOCK} --read-data-subset="$part/$parts" 2>&1)" || chk_rc=$?
      printf '%s\n' "$chk_out"
      if [ "$chk_rc" -eq 0 ]; then
        echo "$(date +%s) 1 $part $parts" > "$CHECK_STATE"
      elif ! printf '%s' "$chk_out" | grep -qi "already locked"; then
        echo "$(date +%s) 0 $part $parts" > "$CHECK_STATE"
      fi
      read -r check_ts check_ok check_part check_parts < "$CHECK_STATE" || true
    fi
    # Snapshot groups the way forget sees them (host and tags), for the panel. An old group,
    # left by a renamed host, a server that is gone or a one-off backup, is kept by the policy
    # forever: the panel shows it with the choice to archive it or remove it (forget-group).
    # Written only when the repository has more than one group.
    if command -v python3 >/dev/null 2>&1; then
      mkdir -p "$(dirname "$GROUPS_FILE")"
      "$BIN" snapshots --json 2>/dev/null | python3 -c "$GROUPS_PY" "$GROUPS_FILE" 2>/dev/null || true
    fi
  fi
  # Measured AFTER the prune: the size used to be taken before the cleanup, so a 1.5 GB
  # repository reported 4.6 GiB - the metric showed what was already gone.
  snap_after="$(count_snaps)"; [ -n "$snap_after" ] || snap_after=-1
  bytes_after="$(repo_size)"; [ -n "$bytes_after" ] || bytes_after=-1
  oldest_ts="$(oldest_snap_ts)"
  if [ "$snap_before" -ge 0 ] && [ "$snap_after" -ge 0 ] 2>/dev/null; then
    removed=$(( snap_before - snap_after ))
    [ "$removed" -lt 0 ] && removed=0
  fi
  echo "=== $(date -Is) ${CLIENT}: done (success=${success}, snapshots ${snap_before}->${snap_after}, removed ${removed}) ==="
} >> "$LOG" 2>&1
ts_end=$(date +%s)
{
  # prune_success reports whether the COMMANDS ran without error. It is NOT "the rotation
  # happened": a forget with nothing to remove also returns 0. Whether it happened is answered
  # by forget_removed and oldest_snapshot_timestamp below.
  echo "restic_server_prune_success{client=\"${CLIENT}\"} ${success}"
  echo "restic_server_prune_timestamp{client=\"${CLIENT}\"} ${ts_end}"
  echo "restic_server_prune_duration_seconds{client=\"${CLIENT}\"} $((ts_end-ts_start))"
  echo "restic_server_repo_bytes{client=\"${CLIENT}\"} ${bytes_after}"
  echo "restic_server_repo_bytes_before{client=\"${CLIENT}\"} ${bytes_before}"
  echo "restic_server_repo_snapshots{client=\"${CLIENT}\"} ${snap_after}"
  echo "restic_server_repo_snapshots_before{client=\"${CLIENT}\"} ${snap_before}"
  echo "restic_server_forget_removed{client=\"${CLIENT}\"} ${removed}"
  echo "restic_server_oldest_snapshot_timestamp{client=\"${CLIENT}\"} ${oldest_ts}"
  # the weekly integrity check: when and whether it passed (-1 = not checked yet)
  echo "restic_server_check_timestamp{client=\"${CLIENT}\"} ${check_ts:-0}"
  echo "restic_server_check_success{client=\"${CLIENT}\"} ${check_ok:--1}"
  # which slice of the data that check read: part N of T (0 - the check did not read data)
  echo "restic_server_check_read_part{client=\"${CLIENT}\"} ${check_part:-0}"
  echo "restic_server_check_read_parts{client=\"${CLIENT}\"} ${check_parts:-0}"
} > "${METRICS_FILE}.partial" && {
  chmod 0644 "${METRICS_FILE}.partial"
  # node-exporter reads the file as a whole: we publish by rename so it never catches it
  # mid-write
  cp -f "${METRICS_FILE}.partial" "${METRICS_FILE_ALT}.partial" 2>/dev/null &&
    mv -f "${METRICS_FILE_ALT}.partial" "${METRICS_FILE_ALT}"
  mv -f "${METRICS_FILE}.partial" "${METRICS_FILE}"
}
PRUNE_BODY
  } > "$ps"
  chmod 0755 "$ps"; chown root:root "$ps"
  # cron: prune once a day, as far as possible from the client's backup (see prune_hour). The
  # minute comes from the name, not at random: a random one moved on every helper update, and
  # an update in the same hour could put it on a minute already gone, skipping that day.
  local h m; h="$(prune_hour "$name")"; m=$(( $(printf '%s' "$name" | cksum | cut -d' ' -f1) % 60 ))
  cat > "/etc/cron.d/kervax-prune-$name" <<CRONEOF
$m $h * * * root $ps >/dev/null 2>&1
CRONEOF
  chmod 0644 "/etc/cron.d/kervax-prune-$name"
}

# Regenerates the scripts of already provisioned clients from the current template. Retention
# is read from the script itself (the KEEP=(...) line, the same source the panel reads) and the
# env is left alone. The previous script is kept next to it with a .bak-<date> suffix, so there
# is something to return to if the new template turns out worse.
cmd_regen_prune() {
  local n=0 f name kl kd kw km
  [ -d "$PRUNE_DIR" ] || { echo "no prune scripts ($PRUNE_DIR)"; return 0; }
  for f in "$PRUNE_DIR"/restic-prune-*.sh; do
    [ -f "$f" ] || continue
    case "$f" in *.bak-*) continue;; esac
    name="${f##*/restic-prune-}"; name="${name%.sh}"
    [ -n "$name" ] || continue
    kl=$(keep_of keep-last "$f"); kd=$(keep_of keep-daily "$f")
    kw=$(keep_of keep-weekly "$f"); km=$(keep_of keep-monthly "$f")
    # The retention is carried over as it stands, so regeneration never changes the policy
    # silently. keep-last is often 0 here - the scripts of an older generation did not pass the
    # flag at all - and that is fine: the script body prunes as long as ANY keep-* is set.
    cp -f "$f" "$f.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    write_prune_script "$name" "$kl" "$kd" "$kw" "$km"
    n=$((n+1))
  done
  echo "prune scripts regenerated: $n"
}

# ------- moving repositories off the legacy shared script (/etc/systemd-rest.conf) -------
# The monolith checks, forgets and prunes every repository in one cron job, without restic
# unlock (a stale lock stopped the cleanup of app-a for 23 days), without metrics and
# with the default host+paths grouping (a group that no longer backs up is kept forever).
# adopt-legacy gives such a repository its own prune script, the same one the panel creates,
# with the same retention and the same password. The password is read here from the
# repository's block and is never printed. The block itself is commented out in the monolith
# (a copy of the file is kept next to it), and so are the blocks of repositories that are gone.
# Without --apply it is a dry run: what forget removes today and with the new grouping.

# "start end" line numbers of the repository's block: from its header ("####...name") to its
# "restic prune" (or to the line before the next header). Nothing if there is no block.
legacy_block_lines() {
  awk -v repo="RESTIC_REPOSITORY=$DATA/$1" '
    function header() { return substr($0, 1, 20) == "####################" && $0 ~ /[^#]/ }
    found && !done && header() { print start, NR - 1; done = 1 }
    header() { hdr = NR }
    $0 == repo && !found { found = 1; start = hdr ? hdr : NR }
    found && !done && NR > start && $0 ~ /^[[:space:]]*restic[[:space:]]+prune/ { print start, NR; done = 1 }
    END { if (found && !done) print start, NR }
  ' "$LEGACY_PRUNE"
}

# Puts the block's RESTIC_PASSWORD and RESTIC_REPOSITORY into the environment of the current
# (sub)shell, evaluated exactly as the monolith evaluates them. Fails if they are not there.
legacy_env() {  # name start end
  local pw_line repo_line
  pw_line="$(sed -n "${2},${3}p" "$LEGACY_PRUNE" | grep -m1 -E '^[[:space:]]*(export[[:space:]]+)?RESTIC_PASSWORD=' || true)"
  repo_line="$(sed -n "${2},${3}p" "$LEGACY_PRUNE" | grep -m1 -E '^[[:space:]]*(export[[:space:]]+)?RESTIC_REPOSITORY=' || true)"
  [ -n "$pw_line" ] && [ -n "$repo_line" ] || return 1
  eval "$pw_line"; eval "$repo_line"
  export RESTIC_PASSWORD RESTIC_REPOSITORY
  [ -n "$RESTIC_PASSWORD" ] && [ "$RESTIC_REPOSITORY" = "$DATA/$1" ]
}

# What forget removes today (the monolith groups by host+paths) and after the move (host+tags).
legacy_dry() {  # name start end kl kd kw km
  local name="$1" keep=() today after
  [ "$4" -gt 0 ] && keep+=(--keep-last "$4")
  [ "$5" -gt 0 ] && keep+=(--keep-daily "$5")
  [ "$6" -gt 0 ] && keep+=(--keep-weekly "$6")
  [ "$7" -gt 0 ] && keep+=(--keep-monthly "$7")
  today="$(mktemp)"; after="$(mktemp)"
  (
    legacy_env "$name" "$2" "$3" || exit 3
    "$RESTIC_BIN" --no-lock --no-cache forget --dry-run --json "${keep[@]}" > "$today" 2>/dev/null
    "$RESTIC_BIN" --no-lock --no-cache forget --dry-run --json --group-by host,tags "${keep[@]}" > "$after" 2>/dev/null
  )
  local rc=$?
  if [ "$rc" -eq 3 ]; then
    echo "$name: no password or repository in its block - left as is"
  else
    NAME="$name" POLICY="$4/$5/$6/$7" python3 - "$today" "$after" <<'PY'
import json, os, sys
def load(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    i = raw.find("[")
    try:
        return json.loads(raw[i:]) if i >= 0 else None
    except ValueError:
        return None
def removed(groups):
    out = []
    for g in groups or []:
        for s in g.get("remove") or []:
            out.append((s.get("time", "")[:10], s.get("hostname", ""), ",".join(s.get("paths") or [])))
    return sorted(out)
def kept(groups):
    return sum(len(g.get("keep") or []) for g in groups or [])
today, after = load(sys.argv[1]), load(sys.argv[2])
name = os.environ["NAME"]
if today is None or after is None:
    print(f"{name}: restic did not answer (wrong password or broken repository?) - left as is")
    sys.exit(0)
t, a = removed(today), removed(after)
print(f"{name}: policy {os.environ['POLICY']}, snapshots {kept(today) + len(t)}")
print(f"  today, groups by host+paths: forget removes {len(t)}")
line = f"  after the move, groups by host+tags: forget removes {len(a)}"
if a:
    hosts = sorted({h for _, h, _ in a})
    paths = sorted({p for _, _, p in a})
    line += f": {a[0][0]} .. {a[-1][0]}, host {', '.join(hosts)}, paths {'; '.join(paths)[:120]}"
print(line)
PY
  fi
  rm -f "$today" "$after"
  [ "$rc" -ne 3 ]
}

# The repository's own env (password from its block) and prune script with the same retention.
legacy_apply() {  # name start end kl kd kw km
  local pw
  pw="$( legacy_env "$1" "$2" "$3" && printf '%s' "$RESTIC_PASSWORD" )" || return 1
  install -d -m 0755 "$PRUNE_DIR" "$ENV_DIR"
  umask 077
  {
    printf '# generated by kervax for %s (moved off %s)\nset -a\n' "$1" "$LEGACY_PRUNE"
    printf 'RESTIC_REPOSITORY=%q\n' "$DATA/$1"
    printf 'RESTIC_PASSWORD=%q\n' "$pw"
    printf 'set +a\n'
  } > "$ENV_DIR/$1.env"
  umask 022
  chown root:root "$ENV_DIR/$1.env"; chmod 0600 "$ENV_DIR/$1.env"
  write_prune_script "$1" "$4" "$5" "$6" "$7"
}

# Comments out line ranges of the monolith ("start:end:tag ..."). A copy of the file is kept,
# and the result must still parse, otherwise the monolith is left untouched.
legacy_comment_out() {
  local bak tmp="$LEGACY_PRUNE.kervax-tmp"
  bak="$LEGACY_PRUNE.kervax-bak-$(date +%Y%m%d-%H%M%S)"
  cp -p "$LEGACY_PRUNE" "$bak" || return 1
  if ! awk -v spec="$1" '
      BEGIN { n = split(spec, parts, " "); for (i = 1; i <= n; i++) { split(parts[i], f, ":"); s[i] = f[1] + 0; e[i] = f[2] + 0; t[i] = f[3] } }
      { tag = ""; for (i = 1; i <= n; i++) if (NR >= s[i] && NR <= e[i]) tag = t[i]
        if (tag != "" && $0 !~ /^# kervax-/) print "# kervax-" tag " " $0; else print }
    ' "$LEGACY_PRUNE" > "$tmp"; then
    rm -f "$tmp"; return 1
  fi
  if ! sh -n "$tmp"; then
    echo "the edited $LEGACY_PRUNE does not parse - left as it was"; rm -f "$tmp"; return 1
  fi
  chmod --reference="$LEGACY_PRUNE" "$tmp"; chown --reference="$LEGACY_PRUNE" "$tmp"
  mv -f "$tmp" "$LEGACY_PRUNE"
  echo "$LEGACY_PRUNE: blocks commented out, the previous version is $bak"
}

cmd_adopt_legacy() {  # [--apply] [name...]   no names = every repository of the monolith
  local apply=0 names=() a n r start end kl kd kw km ranges="" moved=0 dropped=0
  for a in "$@"; do
    case "$a" in
      --apply) apply=1 ;;
      *) valid_name "$a" && names+=("$a") || { echo "bad name: $a"; return 2; } ;;
    esac
  done
  [ -f "$LEGACY_PRUNE" ] || { echo "no $LEGACY_PRUNE here"; return 0; }
  if [ ${#names[@]} -eq 0 ]; then
    mapfile -t names < <(awk -v p="RESTIC_REPOSITORY=$DATA/" 'index($0, p) == 1 { print substr($0, length(p) + 1) }' "$LEGACY_PRUNE")
  fi
  [ ${#names[@]} -gt 0 ] || { echo "$LEGACY_PRUNE has no repository blocks left"; return 0; }
  [ "$apply" = 1 ] || echo "dry run, nothing is changed (add --apply to move)"
  for n in "${names[@]}"; do
    r="$(legacy_block_lines "$n")"
    [ -n "$r" ] || { echo "$n: no block in $LEGACY_PRUNE"; continue; }
    start="${r% *}"; end="${r#* }"
    if [ ! -d "$DATA/$n" ]; then
      echo "$n: the repository is gone, its block only sends a daily error - the block will be commented out"
      ranges="$ranges $start:$end:gone"; dropped=$((dropped + 1)); continue
    fi
    if [ -f "$PRUNE_DIR/restic-prune-$n.sh" ]; then
      echo "$n: already has its own prune script, the block cleans it a second time - the block will be commented out"
      ranges="$ranges $start:$end:dup"; dropped=$((dropped + 1)); continue
    fi
    kl=$(keep_of_legacy keep-last "$n"); kd=$(keep_of_legacy keep-daily "$n")
    kw=$(keep_of_legacy keep-weekly "$n"); km=$(keep_of_legacy keep-monthly "$n")
    if [ $(( kl + kd + kw + km )) -eq 0 ]; then
      echo "$n: its block has no keep-* policy - left as is"; continue
    fi
    legacy_dry "$n" "$start" "$end" "$kl" "$kd" "$kw" "$km" || continue
    if [ "$apply" = 1 ]; then
      if legacy_apply "$n" "$start" "$end" "$kl" "$kd" "$kw" "$km"; then
        ranges="$ranges $start:$end:moved"; moved=$((moved + 1))
        echo "  moved: $PRUNE_DIR/restic-prune-$n.sh, $(cut -d' ' -f1-2 "/etc/cron.d/kervax-prune-$n" | awk '{ printf "%02d:%02d", $2, $1 }') every night"
      else
        echo "  $n: could not move - left as is"
      fi
    fi
  done
  if [ "$apply" = 1 ] && [ -n "$ranges" ]; then
    legacy_comment_out "$ranges" || return 1
    refresh_stats || true
    echo "moved: $moved, blocks removed for gone or doubly cleaned repositories: $dropped"
  fi
}

# ------- deploying a rest-server from scratch (clean node -> backup server) -------
# Installed only from the distribution's own repositories (no curl|sh from foreign domains).
ensure_pkgs() {
  local need=() p
  command -v docker    >/dev/null 2>&1 || need+=(docker.io)
  docker compose version >/dev/null 2>&1 || need+=(docker-compose-v2)
  command -v htpasswd  >/dev/null 2>&1 || need+=(apache2-utils)
  command -v bunzip2   >/dev/null 2>&1 || need+=(bzip2)
  [ ${#need[@]} -eq 0 ] && return 0
  command -v apt-get >/dev/null 2>&1 || { echo "packages are required (${need[*]}) but apt-get is missing - install them manually" >&2; return 2; }
  DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || true
  for p in "${need[@]}"; do
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$p" >/dev/null 2>&1 \
      || { echo "could not install $p" >&2; return 2; }
  done
  systemctl enable --now docker >/dev/null 2>&1 || true
  return 0
}

# restic on the backup server: the system one, otherwise downloaded with a sha256 check (as on the client)
ensure_restic_srv() {
  [ -x "$RESTIC_BIN" ] && return 0
  local ver="0.18.1" arch f base
  case "$(uname -m)" in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; *) echo "unknown architecture" >&2; return 2;; esac
  install -d -m 0755 "$(dirname "$KERVAX_RESTIC")"
  base="https://github.com/restic/restic/releases/download/v$ver"; f="restic_${ver}_linux_${arch}.bz2"
  curl -fsSL --connect-timeout 20 "$base/$f" -o "/tmp/$f" || { echo "could not download restic" >&2; return 2; }
  if curl -fsSL --connect-timeout 20 "$base/SHA256SUMS" -o /tmp/restic-sums 2>/dev/null && [ -s /tmp/restic-sums ]; then
    ( cd /tmp && grep " $f\$" restic-sums | sha256sum -c - >/dev/null 2>&1 ) \
      || { echo "the restic checksum did not match" >&2; rm -f "/tmp/$f" /tmp/restic-sums; return 2; }
  fi
  bunzip2 -f "/tmp/$f" || { echo "bunzip2 fail" >&2; return 2; }
  install -m 0755 "/tmp/restic_${ver}_linux_${arch}" "$KERVAX_RESTIC"
  rm -f "/tmp/restic_${ver}_linux_${arch}" /tmp/restic-sums
  RESTIC_BIN="$KERVAX_RESTIC"
}

cmd_deploy_server() {
  local port="${1:-$REST_PORT}"
  valid_num "$port" || { echo "bad port" >&2; return 2; }
  [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || { echo "port outside the range 1024-65535" >&2; return 2; }
  ensure_pkgs || return 2
  ensure_restic_srv || return 2
  # the upper directories are 0700 (as on existing servers): below them live prune-env files
  # with repository passwords - local unprivileged users may not even list them
  install -d -m 0700 /app/rest-server /app/rest-server/system
  install -d -m 0755 "$PRUNE_DIR" "$ENV_DIR" "$LOG_DIR" "$METRICS_DIR" "$NE_METRICS_DIR"
  install -d -m 0700 "$DATA"
  # htpasswd must exist: with --private-repos an empty file means nobody gets in
  [ -f "$HTPASSWD" ] || { : > "$HTPASSWD"; chown root:root "$HTPASSWD"; chmod 0600 "$HTPASSWD"; }
  # an EXISTING compose file is left alone: it may carry caddy labels, networks or tuning for a
  # particular server shared by several projects. Deploying onto a live server must not break
# its configuration.
  local existed=0
  if [ -f "$COMPOSE" ]; then
    existed=1
  else
    cat > "$COMPOSE" <<COMPOSEEOF
services:
  rest-server:
    hostname: rest-server
    container_name: rest-server
    image: $REST_IMAGE
    restart: always
    ports:
      - "$port:8000"
    volumes:
      - "$DATA:/data"
    environment:
      OPTIONS: "--append-only --path /data --private-repos"
COMPOSEEOF
    chmod 0644 "$COMPOSE"
  fi
  ( cd /app/rest-server && docker compose up -d ) >/tmp/kv-rs.$$ 2>&1 \
    || { echo "docker compose up failed: $(tr '\n' ' ' </tmp/kv-rs.$$)" >&2; rm -f /tmp/kv-rs.$$; return 2; }
  rm -f /tmp/kv-rs.$$
  # health: with an empty htpasswd the rest-server must answer 401 - it listens and requires auth
  local code="" i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$port/" 2>/dev/null || echo 000)"
    [ "$code" != "000" ] && break
    sleep 1
  done
  [ "$code" = "000" ] && { echo "rest-server does not answer on 127.0.0.1:$port" >&2; return 2; }
  refresh_stats
  local ufw_state="inactive"
  command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^Status: active' && ufw_state="active"
  if [ "$existed" -eq 1 ]; then
    echo "OK rest-server was already deployed and is running (port $port, HTTP $code, ufw $ufw_state)"
  else
    echo "OK rest-server deployed: port $port, append-only plus private-repos, HTTP $code, ufw $ufw_state"
  fi
}

# cmd_update_image updates the rest-server image to the baked-in REST_IMAGE. The data (the
# repositories) lives in the bind-mounted /data and is NOT in the image, so updating the image
# does not touch it. The tag in the existing compose file is changed, then pull, up -d and wait
# for HTTP 401 (the server listens again). The image is BAKED into the helper (the panel does
# not choose it - that would be a substitution vector).
cmd_update_image() {
  [ -f "$COMPOSE" ] || { echo "rest-server was not deployed by this panel (no $COMPOSE)" >&2; return 2; }
  local port cur
  port="$(rest_port)"
  cur="$(grep -oE 'restic/rest-server:[A-Za-z0-9._-]+' "$COMPOSE" | head -1)"
  # ONLY the image line is edited, the rest of the compose file is left as is (--append-only and other flags)
  sed -i "s#image:.*restic/rest-server:[A-Za-z0-9._-]*#image: $REST_IMAGE#" "$COMPOSE"
  ( cd /app/rest-server && docker compose pull && docker compose up -d ) >/tmp/kv-rsu.$$ 2>&1 \
    || { echo "the image update failed: $(tr '\n' ' ' </tmp/kv-rsu.$$ | tail -c 300)" >&2; rm -f /tmp/kv-rsu.$$; return 2; }
  rm -f /tmp/kv-rsu.$$
  # health: a rest-server with private repos answers 401 on "/" - it is listening
  local code="" i
  for i in $(seq 1 15); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$port/" 2>/dev/null || echo 000)"
    [ "$code" != "000" ] && break
    sleep 1
  done
  if [ "$code" = "000" ]; then
    echo "rest-server does not answer after the update (port $port) - check docker logs rest-server" >&2; return 2
  fi
  local newver cid; cid="$(docker ps -qf 'name=rest-server' 2>/dev/null | head -1)"
  [ -n "$cid" ] && newver="$(docker exec "$cid" rest-server --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  refresh_stats
  echo "OK rest-server updated -> ${REST_IMAGE#*:} (was ${cur#*:}, now ${newver:-?}), answering $code"
}

cmd_provision_client() {
  local name="$1" hpass="$2" repopass="$3" client_ip="$4" kl="${5:-3}" kd="${6:-7}" kw="${7:-4}" km="${8:-6}"
  valid_name "$name" || { echo "bad name" >&2; return 2; }
  valid_ip "$client_ip" || { echo "bad ip" >&2; return 2; }
  for n in "$kl" "$kd" "$kw" "$km"; do valid_num "$n" || { echo "bad retention" >&2; return 2; }; done
  # SAFETY: every keep is at least 1 (we always hold one last/daily/weekly/monthly snapshot,
  # otherwise forget/prune wipes those slices). The floor is enforced on the backup server
  # itself - the panel and backend cannot send a 0 and destroy the history.
  [ "$kl" -ge 1 ] 2>/dev/null || kl=1
  [ "$kd" -ge 1 ] 2>/dev/null || kd=1
  [ "$kw" -ge 1 ] 2>/dev/null || kw=1
  [ "$km" -ge 1 ] 2>/dev/null || km=1
  [ -d "$DATA" ] || { echo "no rest-server data dir" >&2; return 2; }
  # 0) RE-PROVISIONING: the repository already exists.
  # `restic init` is idempotent, but a repository keeps its FIRST password FOREVER. Sending a
  # new one is impossible: the client would get "wrong password" and prune-env would start
  # lying, breaking both the rotation and the recovery password (get-client-creds would return
  # garbage). So we CONNECT to the existing repository (the snapshot history is preserved)
  # instead of recreating it: its password is taken from prune-env and verified to actually
  # open the repository. There is no deletion here and there will not be - removing a
  # repository stays a manual root operation on this server.
  local existing=0 envf="$ENV_DIR/$name.env"
  if [ -f "$DATA/$name/config" ]; then
    existing=1
    local old=""
    [ -f "$envf" ] && old="$(sed -n 's/^RESTIC_PASSWORD="\?\([^"]*\)"\?$/\1/p' "$envf" | head -1)"
    if [ -z "$old" ]; then
      echo "repository '$name' already exists but its password was not found on the server ($envf) - it was provisioned by a different panel. Take the password from the vault and configure the client manually, or remove the repository as root on the backup server: rm -rf $DATA/$name (THIS ERASES THE HISTORY)" >&2
      return 2
    fi
    if ! RESTIC_REPOSITORY="$DATA/$name" RESTIC_PASSWORD="$old" "$RESTIC_BIN" cat config >/dev/null 2>&1; then
      echo "repository '$name' exists but the password stored on the server does NOT open it ($envf) - the correct password from the vault is required. This cannot be repaired automatically" >&2
      return 2
    fi
    repopass="$old"
    # the retention of an existing repository is left alone - it is set at first provisioning
    local ps="$PRUNE_DIR/restic-prune-$name.sh"
    if [ -f "$ps" ]; then
      local okl okd okw okm
      okl="$(keep_of keep-last "$ps")"; okd="$(keep_of keep-daily "$ps")"
      okw="$(keep_of keep-weekly "$ps")"; okm="$(keep_of keep-monthly "$ps")"
      [ "${okl:-0}" -ge 1 ] 2>/dev/null && kl="$okl"
      [ "${okd:-0}" -ge 1 ] 2>/dev/null && kd="$okd"
      [ "${okw:-0}" -ge 1 ] 2>/dev/null && kw="$okw"
      [ "${okm:-0}" -ge 1 ] 2>/dev/null && km="$okm"
    fi
  fi
  # 1) htpasswd (bcrypt cost 10) - the client's access to the rest-server (this transport
  # password can be rotated freely: it is not about data encryption, and the panel hands the
  # client a new one)
  htpasswd -bB -C 10 "$HTPASSWD" "$name" "$hpass" >/dev/null 2>&1 || { echo "htpasswd failed" >&2; return 2; }
  # 1a) WAIT until the rest-server sees the new user. It re-reads htpasswd on file change, and
  # between the write and the re-read there is a window: an init starting immediately gets a
  # 401 and the whole provisioning fails (seen on a live node - the first run 401, every
  # subsequent one fine). We poll until the credentials work.
  if command -v curl >/dev/null 2>&1; then
    local i code
    for i in 1 2 3 4 5 6 7 8 9 10; do
      code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
             -u "$name:$hpass" "http://127.0.0.1:$(rest_port)/$name/config" 2>/dev/null || echo 000)
      [ "$code" = "401" ] || break   # 404/403/200 - the credentials are already accepted
      sleep 1
    done
    [ "$code" = "401" ] && { echo "the rest-server did not accept the new user within 10s (htpasswd written, authentication still fails)" >&2; return 2; }
  else
    sleep 2   # without curl there is nothing to check with - give the server time to re-read the file
  fi
  # 2) initialise the repository through the local rest-server (correct file permissions inside)
  if [ "$existing" -eq 0 ]; then
    RESTIC_REPOSITORY="rest:http://$name:$hpass@127.0.0.1:$(rest_port)/$name" RESTIC_PASSWORD="$repopass" \
      "$RESTIC_BIN" init >/tmp/kv-init.$$ 2>&1
    local rc=$?
    if [ $rc -ne 0 ] && ! grep -q 'config file already exists\|already initialized' /tmp/kv-init.$$; then
      echo "init failed: $(tr '\n' ' ' </tmp/kv-init.$$)" >&2; rm -f /tmp/kv-init.$$; return 2
    fi
    rm -f /tmp/kv-init.$$
  fi
  # 3) ufw allow for the client IP to the rest port and the TLS port (if a front exists) - best effort
  if command -v ufw >/dev/null 2>&1; then
    ufw allow proto tcp from "$client_ip" to any port "$REST_PORT" >/dev/null 2>&1 || true
    ufw route allow proto tcp from "$client_ip" to any port 8000 >/dev/null 2>&1 || true
    [ -f "$(tls_dir)/cert.pem" ] && ufw allow proto tcp from "$client_ip" to any port "$TLS_PORT" >/dev/null 2>&1 || true
  fi
  # 4) prune env, script and cron with retention (for an existing repository with the same,
  # CORRECT password: this also repairs an env that a previous re-provisioning corrupted)
  install_prune "$name" "$kl" "$kd" "$kw" "$km" "$repopass"
  refresh_stats
  if [ "$existing" -eq 1 ]; then
    # the panel does not know the password of an existing repository - return it, otherwise the client cannot be set up
    printf 'OK existing %s\nREPOPASS_B64=%s\n' "$name" "$(printf '%s' "$repopass" | base64 -w0)"
  else
    echo "OK provisioned $name"
  fi
}

# ------- native TLS rest-server on :64101 (WITHOUT an extra caddy layer) -------
# A second rest-server container with --tls over THE SAME data and htpasswd (append-only plus
# private-repos) and a self-signed certificate (openssl, 3650 days, no renewal). HTTP :64100 is
# left alone, so HTTP and HTTPS work at the same time. There used to be a caddy front here; it
# is now removed (migration).
cmd_deploy_tls_front() {
  local san_ip="$1" san_dns="${2:-}"
  valid_ip "$san_ip" || { echo "bad san ip" >&2; return 2; }
  command -v docker >/dev/null 2>&1 || { echo "no docker" >&2; return 2; }
  install -d -m 0755 "$TLS_DIR"
  # MIGRATION of the 0.14 layout (system/kervax-tls -> its own directory). Strictly BEFORE
  # generating the certificate: the certificate is moved rather than reissued, otherwise
  # clients pinned with --cacert would break.
  if [ -f "$TLS_DIR_OLD/cert.pem" ] || [ -f "$TLS_DIR_OLD/docker-compose.yml" ]; then
    if [ -f "$TLS_DIR_OLD/docker-compose.yml" ]; then
      ( cd "$TLS_DIR_OLD" && docker compose down ) >/dev/null 2>&1 || true
    fi
    docker rm -f kervax-rest-tls >/dev/null 2>&1 || true  # the name is held by the old project
    for f in cert.pem key.pem; do
      if [ -f "$TLS_DIR_OLD/$f" ] && [ ! -f "$TLS_DIR/$f" ]; then mv -f "$TLS_DIR_OLD/$f" "$TLS_DIR/$f"; fi
    done
    rm -rf "$TLS_DIR_OLD"
  fi
  if [ ! -f "$TLS_DIR/cert.pem" ] || [ ! -f "$TLS_DIR/key.pem" ]; then
    local ext="subjectAltName=IP:$san_ip"; [ -n "$san_dns" ] && ext="$ext,DNS:$san_dns"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" \
      -subj "/CN=${san_dns:-$san_ip}" -addext "$ext" >/dev/null 2>&1 || { echo "cert gen failed" >&2; return 2; }
    chmod 0600 "$TLS_DIR/key.pem"; chmod 0644 "$TLS_DIR/cert.pem"
  fi
  # remove the old caddy front, if any (migration to the native TLS rest-server)
  docker rm -f kervax-tls-front >/dev/null 2>&1 || true
  rm -f "$TLS_DIR/Caddyfile" 2>/dev/null || true
  # a second rest-server with native TLS over the same data; the image is BAKED in (like the
  # main one, not chosen by the panel). The entrypoint adds --path /data --htpasswd-file
  # /data/.htpasswd itself; OPTIONS carries the rest.
  cat > "$TLS_DIR/docker-compose.yml" <<COMPOSEEOF
services:
  rest-server-tls:
    image: $REST_IMAGE
    hostname: rest-server-tls
    container_name: kervax-rest-tls
    restart: always
    ports:
      - "$TLS_PORT:8000"
    environment:
      OPTIONS: "--append-only --path /data --private-repos --tls --tls-cert /certs/cert.pem --tls-key /certs/key.pem"
    volumes:
      - "$DATA:/data"
      - "$TLS_DIR/cert.pem:/certs/cert.pem:ro"
      - "$TLS_DIR/key.pem:/certs/key.pem:ro"
COMPOSEEOF
  ( cd "$TLS_DIR" && docker compose up -d ) >/tmp/kv-tls.$$ 2>&1 || { echo "rest-tls up failed: $(tr '\n' ' ' </tmp/kv-tls.$$)" >&2; rm -f /tmp/kv-tls.$$; return 2; }
  rm -f /tmp/kv-tls.$$
  refresh_stats
  echo "OK native TLS rest-server on :$TLS_PORT"
}

# the certificate is returned as a single base64 line (it survives the spool's tr '\n'; the client decodes it into cacert)
cmd_get_cert() { local d; d="$(tls_dir)"; [ -f "$d/cert.pem" ] && base64 -w0 "$d/cert.pem" || { echo "no cert" >&2; return 2; }; }

# DR: the client's repository password from prune-env (repopass is duplicated here next to the
# backups) for the case where the client is dead and nothing can be recovered from it. A single
# base64 line.
cmd_get_client_creds() {
  local name="$1"
  valid_name "$name" || { echo "bad name" >&2; return 2; }
  local env="$ENV_DIR/$name.env"
  [ -f "$env" ] || { echo "no prune-env for $name (was the repository provisioned by a different panel?)" >&2; return 2; }
  local pass repo
  pass="$(sed -n 's/^RESTIC_PASSWORD="\?\([^"]*\)"\?$/\1/p' "$env" | head -1)"
  repo="$(sed -n 's/^RESTIC_REPOSITORY="\?\([^"]*\)"\?$/\1/p' "$env" | head -1)"
  printf 'repopass=%s\nrepo_local=%s\n' "$pass" "$repo" | base64 -w0
}

# ------- spool: execute provisioning requests (secrets are 0600 and removed at once) -------
# cmd_restic_update brings every restic the server runs (server_restic_bins) up to
# RESTIC_TARGET_VER: only the ones that are older, a newer one is never touched. Downloaded from
# github, checked against the baked sha256, then swapped in atomically. A binary that came from a
# package gets the package put on hold, otherwise apt would bring the old one back on the next
# upgrade.
cmd_restic_update() {
  local arch want b cur old=() f tmp got newbin out="" pkg held=""
  case "$(uname -m)" in
    x86_64) arch=amd64; want="$RESTIC_SHA_amd64" ;;
    aarch64|arm64) arch=arm64; want="$RESTIC_SHA_arm64" ;;
    *) echo "unknown architecture: $(uname -m)" >&2; return 2 ;;
  esac
  while IFS= read -r b; do
    [ -x "$b" ] || continue
    cur="$(restic_ver "$b")"
    if [ -z "$cur" ]; then echo "$b does not report a restic version, left alone" >&2; continue; fi
    if ver_lt "$cur" "$RESTIC_TARGET_VER"; then old+=("$b"); echo "$b: restic $cur"; fi
  done < <(server_restic_bins)
  if [ "${#old[@]}" -eq 0 ]; then echo "restic is $RESTIC_TARGET_VER or newer, nothing to update"; return 0; fi
  command -v bunzip2 >/dev/null 2>&1 || { apt-get update -qq >/dev/null 2>&1 || true; apt-get install -y -qq bzip2 >/dev/null 2>&1 || true; }
  f="restic_${RESTIC_TARGET_VER}_linux_${arch}.bz2"
  tmp="$(mktemp -d /tmp/kv-restic.XXXXXX)"
  if ! curl -fsSL --connect-timeout 20 --max-time 600 "https://github.com/restic/restic/releases/download/v$RESTIC_TARGET_VER/$f" -o "$tmp/$f"; then
    rm -rf "$tmp"; echo "could not download restic $RESTIC_TARGET_VER from github" >&2; return 2
  fi
  got="$(sha256sum "$tmp/$f" | awk '{ print $1 }')"
  if [ "$got" != "$want" ]; then
    rm -rf "$tmp"; echo "the sha256 did not match (expected $want, got $got), nothing was replaced" >&2; return 2
  fi
  bunzip2 -f "$tmp/$f" || { rm -rf "$tmp"; echo "bunzip2 failed" >&2; return 2; }
  newbin="$tmp/${f%.bz2}"
  chmod 0755 "$newbin"
  [ "$(restic_ver "$newbin")" = "$RESTIC_TARGET_VER" ] || { rm -rf "$tmp"; echo "the downloaded restic does not run" >&2; return 2; }
  for b in "${old[@]}"; do
    # a copy next to it plus mv: same filesystem, so the swap is atomic and a running prune keeps the old file
    if cp -f "$newbin" "$b.kv-new" && chmod 0755 "$b.kv-new" && mv -f "$b.kv-new" "$b"; then
      out="$out $b"
      pkg="$(dpkg -S "$b" 2>/dev/null | head -n1 | cut -d: -f1 || true)"
      if [ -n "$pkg" ] && apt-mark hold "$pkg" >/dev/null 2>&1; then
        held=" (package $pkg is on hold now, apt will not bring the old version back)"
      fi
    else
      rm -f "$b.kv-new"; echo "could not replace $b" >&2
    fi
  done
  rm -rf "$tmp"
  [ -n "$out" ] || return 2
  echo "restic updated to $RESTIC_TARGET_VER:$out$held"
}

# Root crontab lines that run one of our prune scripts are left over from the ansible role that
# used to provision clients: it scheduled each cleanup at the backup hour + 21. The helper owns
# the schedule now (/etc/cron.d/kervax-prune-<name>, 12 hours away from the backup), so such a
# line runs the same script a second time a day, at a time that ignores the client's backup.
# These lines are commented out, and so are lines starting a prune script that no longer exists
# (the client is gone, cron kept starting a missing file every night). Only a line whose whole
# command is the script, with nothing but output redirection after it; everything else in the
# crontab stays as it is. The previous crontab is kept in $CRONTAB_BAK_DIR.
CRONTAB_BAK_DIR=/root
cmd_dedup_cron() {
  local cur new line cmd s n rest changed=0 bak
  cur="$(crontab -l -u root 2>/dev/null)" || { echo "root crontab: none"; return 0; }
  [ -n "$cur" ] || { echo "root crontab: empty"; return 0; }
  new=""
  while IFS= read -r line || [ -n "$line" ]; do
    s=""
    case "$line" in
      '#'*|''|'@'*) ;;
      *)
        cmd="$(printf '%s\n' "$line" | awk '{ $1 = $2 = $3 = $4 = $5 = ""; sub(/^ +/, ""); print }')"
        s="$(printf '%s\n' "$cmd" | grep -oE "^$PRUNE_DIR/restic-prune-[A-Za-z0-9._-]+\.sh" || true)"
        if [ -n "$s" ]; then
          rest="$(printf '%s' "${cmd#"$s"}" | sed -E 's#[[:space:]]*(2>&1|&>[^[:space:]]+|[12]?>>?[[:space:]]*[^[:space:]]+)##g')"
          [ -z "${rest//[[:space:]]/}" ] || s=""
        fi
        ;;
    esac
    if [ -n "$s" ]; then
      n="${s##*/restic-prune-}"; n="${n%.sh}"
      if [ ! -f "$s" ]; then
        line="# kervax-gone (no such script): $line"; changed=$((changed + 1))
      elif grep -qF "$s" "/etc/cron.d/kervax-prune-$n" 2>/dev/null; then
        line="# kervax-moved to /etc/cron.d/kervax-prune-$n: $line"; changed=$((changed + 1))
      fi
    fi
    new="$new$line"$'\n'
  done <<<"$cur"
  if [ "$changed" -eq 0 ]; then echo "root crontab: no duplicate prune lines"; return 0; fi
  bak="$CRONTAB_BAK_DIR/crontab.kervax-bak-$(date +%Y%m%d-%H%M%S)"
  printf '%s\n' "$cur" > "$bak" && chmod 0600 "$bak" || { echo "could not save $bak, crontab left as is" >&2; return 2; }
  if ! printf '%s' "$new" | crontab -u root -; then
    echo "could not install the new crontab, the old one stays (copy in $bak)" >&2; return 2
  fi
  echo "root crontab: $changed prune line(s) commented out, the previous crontab is in $bak"
}

# cmd_forget_group removes every snapshot of one old group: a host that no longer backs up into
# the repository (renamed, gone, a one-off backup). The policy keeps the last snapshots of such
# a group forever, so it goes only by hand. Only an old group: when the host has a snapshot
# from the last 7 days the group is alive and nothing is removed. Without --apply it only says
# what would go. The space comes back with the next nightly prune of the repository.
cmd_forget_group() {
  local repo="${1:-}" host="${2:-}" apply="${3:-}" env bin
  valid_name "$repo" || { echo "bad repository name" >&2; return 2; }
  valid_name "$host" || { echo "bad host name" >&2; return 2; }
  env="$ENV_DIR/$repo.env"
  [ -f "$env" ] || { echo "no $env: the repository has no prune script of its own, nothing to take the password from" >&2; return 2; }
  command -v python3 >/dev/null 2>&1 || { echo "python3 is needed" >&2; return 2; }
  bin="$(sed -n 's/^BIN=//p' "$PRUNE_DIR/restic-prune-$repo.sh" 2>/dev/null | head -n1)"
  [ -x "$bin" ] || bin="$RESTIC_BIN"
  (
    set -a; . "$env"; set +a
    snaps="$("$bin" snapshots --host "$host" --json 2>&1)" || { echo "restic snapshots failed: $snaps" >&2; exit 2; }
    info="$(printf '%s' "$snaps" | python3 -c '
import json, re, sys
from datetime import datetime
def unix(t):
    t = re.sub(r"\.\d+", "", t or "").replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(t).timestamp())
    except ValueError:
        return 0
rows = sorted((unix(s.get("time")), s.get("id") or "") for s in json.load(sys.stdin) or [])
print(len(rows), rows[0][0] if rows else 0, rows[-1][0] if rows else 0)
for t, i in rows:
    print(i)
')" || { echo "could not read the snapshot list" >&2; exit 2; }
    read -r n first last <<<"$(head -n1 <<<"$info")"
    if [ "${n:-0}" -eq 0 ]; then echo "no snapshots of host $host in $repo"; exit 0; fi
    if [ $(( $(date +%s) - last )) -lt $(( 7 * 86400 )) ]; then
      echo "host $host backed up into $repo $(( ($(date +%s) - last) / 3600 )) h ago: the group is alive, nothing removed" >&2
      exit 2
    fi
    echo "group $host in $repo: $n snapshots, $(date -d "@$first" +%F) .. $(date -d "@$last" +%F)"
    if [ "$apply" != "--apply" ]; then
      echo "dry run, nothing removed. To remove: $0 forget-group $repo $host --apply"
      exit 0
    fi
    rl=""; "$bin" forget --help 2>/dev/null | grep -q -- "--retry-lock" && rl="--retry-lock 20m"
    # shellcheck disable=SC2046
    "$bin" forget $rl $(tail -n +2 <<<"$info" | grep -E '^[0-9a-f]{64}$') || { echo "restic forget failed" >&2; exit 2; }
    rm -f "$GROUPS_DIR/$repo.json"  # the next prune writes it anew
    echo "removed $n snapshots of $host from $repo; the space comes back with the next nightly prune"
  )
}

cmd_process_spool() {
  local req id action name hpass repopass client_ip kl kd kw km san_ip san_dns port out ok k v line
  for req in "$REQ_DIR"/*.req; do
    [ -f "$req" ] || continue
    id="$(basename "$req" .req)"
    action=""; name=""; hpass=""; repopass=""; client_ip=""; kl=3; kd=7; kw=4; km=6; san_ip=""; san_dns=""; port="$REST_PORT"
    # read the whole line and split on the first '=' (this keeps a trailing '=' inside values)
    while IFS= read -r line; do
      k="${line%%=*}"; v="${line#*=}"
      case "$k" in
        action) action="$v";; name) name="$v";; hpass) hpass="$v";; repopass) repopass="$v";;
        client_ip) client_ip="$v";; keep_last) kl="$v";; keep_daily) kd="$v";;
        keep_weekly) kw="$v";; keep_monthly) km="$v";; san_ip) san_ip="$v";; san_dns) san_dns="$v";;
        port) port="$v";;
      esac
    done < "$req"
    rm -f "$req"  # secrets do not linger on disk
    out=""; ok=false
    case "$action" in
      deploy_server)    if out="$(cmd_deploy_server "$port" 2>&1)"; then ok=true; fi ;;
      update_image)     if out="$(cmd_update_image 2>&1)"; then ok=true; fi ;;
      provision_client) if out="$(cmd_provision_client "$name" "$hpass" "$repopass" "$client_ip" "$kl" "$kd" "$kw" "$km" 2>&1)"; then ok=true; fi ;;
      deploy_tls_front) if out="$(cmd_deploy_tls_front "$san_ip" "$san_dns" 2>&1)"; then ok=true; fi ;;
      get_cert)         if out="$(cmd_get_cert 2>&1)"; then ok=true; fi ;;
      get_client_creds) if out="$(cmd_get_client_creds "$name" 2>&1)"; then ok=true; fi ;;
      *) out="unknown action" ;;
    esac
    printf 'ok=%s\noutput=%s\n' "$ok" "$(printf '%s' "$out" | tr '\n' '\r')" > "$RES_DIR/$id.res.tmp"
    mv -f "$RES_DIR/$id.res.tmp" "$RES_DIR/$id.res"; chmod 0644 "$RES_DIR/$id.res"
  done
}

STATE_DIR=/var/lib/kervax
case "${1:-}" in
  stats)            cmd_stats ;;
  regen-prune)      cmd_regen_prune ;;
  adopt-legacy)     shift; cmd_adopt_legacy "$@" ;;
  refresh)          refresh_stats ;;
  deploy-server)    shift; cmd_deploy_server "$@" ;;
  update-image)     cmd_update_image ;;
  restic-update)    cmd_restic_update ;;
  forget-group)     shift; cmd_forget_group "$@" ;;
  dedup-cron)       cmd_dedup_cron ;;
  provision-client) shift; cmd_provision_client "$@" ;;
  deploy-tls-front) shift; cmd_deploy_tls_front "$@" ;;
  get-cert)         cmd_get_cert ;;
  get-client-creds) shift; cmd_get_client_creds "$@" ;;
  process-spool)    cmd_process_spool ;;
  *) echo "usage: $0 {stats|regen-prune|adopt-legacy [--apply] [name...]|deploy-server [port]|update-image|restic-update|forget-group <repo> <host> [--apply]|dedup-cron|provision-client <name> <hpass> <repopass> <ip> [kl kd kw km]|deploy-tls-front <ip> [dns]|get-cert|get-client-creds <name>|process-spool}" >&2; exit 2 ;;
esac
HELPER_EOF
chmod 0755 "$HELPER"; chown root:root "$HELPER"

# MIGRATION (0.16): repair prune-env files already created by the panel without `set -a`.
# Reinstalling the helper does not regenerate the envs by itself (only re-provisioning a client
# does), and without the export restic inside the prune script does not see the repository, so
# the cleanup silently does nothing. Ansible-managed envs (they carry their own header) are left
# alone - they have `set -a` anyway.
for _f in /app/rest-server/system/envs/*.env; do
  [ -f "$_f" ] || continue
  grep -q '^# generated by kervax' "$_f" || continue
  grep -q '^set -a' "$_f" && continue
  if awk 'NR==1{print; print "set -a"; next} {print} END{print "set +a"}' "$_f" > "$_f.kvtmp"; then
    chmod 0600 "$_f.kvtmp"; chown root:root "$_f.kvtmp"; mv -f "$_f.kvtmp" "$_f"
    echo "backupserver-setup: repaired prune-env $_f (it was not exported)"
  else
    rm -f "$_f.kvtmp"
  fi
done

# The server's own restic (prune, forget, check) up to the version baked into the helper. A
# failed download changes nothing: the old restic keeps working and the panel shows it is old.
if _out="$("$HELPER" restic-update 2>&1)"; then
  printf '%s\n' "$_out" | sed 's/^/backupserver-setup: /'
else
  printf '%s\n' "$_out" | sed 's/^/backupserver-setup: restic not updated: /' >&2
fi

# one immediate run (so the file appears) plus a cron entry every minute (stats are cheap)
"$HELPER" stats > "$STATS.tmp" 2>/dev/null && mv -f "$STATS.tmp" "$STATS" || true
chmod 0644 "$STATS" 2>/dev/null || true
cat > "$CRON" <<CRON_EOF
* * * * * root $HELPER stats > $STATS.tmp 2>/dev/null && mv -f $STATS.tmp $STATS && chmod 0644 $STATS
CRON_EOF
chmod 0644 "$CRON"

# path unit: as soon as the agent drops a request into the spool, root executes it immediately
cat > /etc/systemd/system/kervax-bsrv-req.service <<UNIT_EOF
[Unit]
Description=Kervax backup-server request processor
[Service]
Type=oneshot
ExecStart=$HELPER process-spool
UNIT_EOF
cat > /etc/systemd/system/kervax-bsrv-req.path <<UNIT_EOF
[Unit]
Description=Kervax backup-server request spool watch
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-bsrv-req.service
[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload 2>/dev/null || true
systemctl enable --now kervax-bsrv-req.path >/dev/null 2>&1 || true
"$HELPER" process-spool >/dev/null 2>&1 || true

# The scripts of already provisioned clients are copies of the previous template: they are
# refreshed right away, otherwise fixes (rotation metrics, --group-by) would reach new clients
# only.
"$HELPER" regen-prune 2>/dev/null || true
# ...and the cleanups the ansible role scheduled in root's crontab are not run a second time
"$HELPER" dedup-cron 2>&1 | sed 's/^/backupserver-setup: /' || true

echo "backupserver-setup: done -> $HELPER; statistics in $STATS (cron every minute), provisioning through the spool $REQ_DIR."
echo "backupserver-setup: current statistics:"
head -c 500 "$STATS" 2>/dev/null; echo
