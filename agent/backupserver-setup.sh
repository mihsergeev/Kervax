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
KERVAX_SETUP_VERSION=0.20  # MAJOR.MINOR; compared component-wise (0.13 > 0.2!)
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
  local repos_json="" repo name snaps last locked lock_ts valid size prune kl kd kw km
  for repo in "$DATA"/*/; do
    [ -d "$repo" ] || continue
    name="$(basename "$repo")"
    valid=false; [ -f "${repo}config" ] && valid=true
    [ "$valid" = true ] || [ -d "${repo}data" ] || [ -d "${repo}snapshots" ] || continue
    snaps=0
    [ -d "${repo}snapshots" ] && snaps=$(find "${repo}snapshots" -maxdepth 1 -type f 2>/dev/null | wc -l)
    last=0
    if [ -d "${repo}snapshots" ]; then
      last=$(find "${repo}snapshots" -maxdepth 1 -type f -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)
    fi
    [ -z "$last" ] || [ "$last" = 0 ] && last="$(stat -c %Y "$repo" 2>/dev/null || echo 0)"
    # a lock by itself is not a problem: restic holds one for the whole backup and refreshes it
    # every 5 minutes. We report the mtime of the newest lock, and by it the panel tells a
    # running backup from a lock left behind by a crashed process.
    locked=false; lock_ts=0
    if [ -d "${repo}locks" ]; then
      lock_ts=$(find "${repo}locks" -maxdepth 1 -type f -printf '%T@\n' 2>/dev/null | sort -rn | head -1 | cut -d. -f1)
      [ -z "$lock_ts" ] && lock_ts=0
      [ "$lock_ts" != 0 ] && locked=true
    fi
    size="$(du -sb "$repo" 2>/dev/null | cut -f1 || true)"; [ -z "$size" ] && size=0
    prune="$PRUNE_DIR/restic-prune-$name.sh"
    if [ -f "$prune" ]; then
      kl=$(keep_of keep-last "$prune"); kd=$(keep_of keep-daily "$prune")
      kw=$(keep_of keep-weekly "$prune"); km=$(keep_of keep-monthly "$prune")
    else
      # no dedicated script - the repository may be cleaned by the legacy monolith (see keep_of_legacy)
      kl=$(keep_of_legacy keep-last "$name"); kd=$(keep_of_legacy keep-daily "$name")
      kw=$(keep_of_legacy keep-weekly "$name"); km=$(keep_of_legacy keep-monthly "$name")
    fi
    repos_json="${repos_json:+$repos_json,}{\"name\":\"$(json_escape "$name")\",\"valid\":$valid,\"snapshots\":$snaps,\"last_activity\":$last,\"locked\":$locked,\"lock_ts\":$lock_ts,\"size_bytes\":$size,\"keep_last\":${kl:-0},\"keep_daily\":${kd:-0},\"keep_weekly\":${kw:-0},\"keep_monthly\":${km:-0}}"
  done
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
    printf 'KEEP=(--keep-last %q --keep-daily %q --keep-weekly %q --keep-monthly %q)\n' "$kl" "$kd" "$kw" "$km"
    cat <<'PRUNE_BODY'
set -a; . "$ENV_FILE"; set +a  # restic is a child process: without export it will not see the repository
mkdir -p "$(dirname "$LOG")" "$(dirname "$METRICS_FILE")" "$(dirname "$METRICS_FILE_ALT")"
ts_start=$(date +%s); success=0
snap_before=-1; snap_after=-1; removed=-1
bytes_before=-1; bytes_after=-1; oldest_ts=0

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
  if ! "$BIN" cat config >/dev/null 2>&1; then
    echo "ERROR: repo not accessible at $RESTIC_REPOSITORY"
  else
    snap_before="$(count_snaps)"; [ -n "$snap_before" ] || snap_before=-1
    bytes_before="$(repo_size)"; [ -n "$bytes_before" ] || bytes_before=-1
    "$BIN" unlock >/dev/null 2>&1 || true
    # SAFETY: keep-last<1 means DO NOT prune (otherwise forget wipes every snapshot). Defense in depth.
    kl_val=1; for _i in "${!KEEP[@]}"; do [ "${KEEP[$_i]}" = "--keep-last" ] && kl_val="${KEEP[$((_i+1))]}"; done
    if ! [ "${kl_val:-0}" -ge 1 ] 2>/dev/null; then
      echo "SAFETY: keep-last<1 -> forget/prune SKIPPED (protection against wiping everything)"
      success=1
    else
      # --group-by host,tags: by default restic groups by host+paths and applies the policy to
      # each group separately. If a client backs up a file with a date in its name
      # (shared-20260812-030002.zip.enc), every snapshot forms a group of one, becomes the
      # "last snapshot" in it and is never removed.
      forget_rc=0; "$BIN" forget --group-by host,tags "${KEEP[@]}" 2>&1 || forget_rc=$?
      prune_rc=0;  "$BIN" prune 2>&1 || prune_rc=$?
      [ "$forget_rc" -eq 0 ] && [ "${prune_rc:-0}" -eq 0 ] && success=1
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
  # cron: prune once a day (hour = 04:00 by default plus an offset derived from the name)
  local h=$(( ( $(printf '%s' "$name" | cksum | cut -d' ' -f1) % 6 ) + 2 ))
  cat > "/etc/cron.d/kervax-prune-$name" <<CRONEOF
$(( RANDOM % 60 )) $h * * * root $ps >/dev/null 2>&1
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
    # keep-last=0 in the template means "no prune at all" (see the safety check in the script
    # body): kept as is, so regeneration never changes the policy silently
    cp -f "$f" "$f.bak-$(date +%Y%m%d-%H%M%S)" 2>/dev/null || true
    write_prune_script "$name" "$kl" "$kd" "$kw" "$km"
    n=$((n+1))
  done
  echo "prune scripts regenerated: $n"
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
  refresh)          refresh_stats ;;
  deploy-server)    shift; cmd_deploy_server "$@" ;;
  update-image)     cmd_update_image ;;
  provision-client) shift; cmd_provision_client "$@" ;;
  deploy-tls-front) shift; cmd_deploy_tls_front "$@" ;;
  get-cert)         cmd_get_cert ;;
  get-client-creds) shift; cmd_get_client_creds "$@" ;;
  process-spool)    cmd_process_spool ;;
  *) echo "usage: $0 {stats|regen-prune|deploy-server [port]|update-image|provision-client <name> <hpass> <repopass> <ip> [kl kd kw km]|deploy-tls-front <ip> [dns]|get-cert|get-client-creds <name>|process-spool}" >&2; exit 2 ;;
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

echo "backupserver-setup: done -> $HELPER; statistics in $STATS (cron every minute), provisioning through the spool $REQ_DIR."
echo "backupserver-setup: current statistics:"
head -c 500 "$STATS" 2>/dev/null; echo
