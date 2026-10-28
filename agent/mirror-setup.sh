#!/usr/bin/env bash
# Kervax: make this node a MIRROR of backup repositories - a second copy of chosen restic
# repositories of the main backup servers, kept on a separate server.
#
# Built so that whoever breaks into a main backup server, a backed up host or the panel cannot
# remove what the mirror already has:
#  * the mirror PULLS. It logs in to the main backup server with its own key as the read-only user
#    kervax-mread (backupserver-setup 0.38, mirror-allow), which can only list and read the
#    repositories allowed to this mirror. The main server holds no key and no login here;
#  * restic files are encrypted and (config aside) named by the sha256 of their content, so they are
#    copied as they are, without repository passwords, and a file is taken only when its sha256
#    matches its name. A file already in the copy is never overwritten;
#  * a file removed on the main server (prune, forget, or an intruder) stays in the copy KEEP_DAYS
#    more (set here, in /etc/kervax/mirror.conf). When one run sees a large part of the repository
#    gone at once, its config replaced or removed, the copy freezes: nothing is removed from it and
#    nothing new is taken until root on this server runs `unfreeze`;
#  * the panel can only add a repository to the list, pause it or ask for a pull now (the mirror
#    action through the spool). It cannot remove a copy, change the address or host key of a main
#    server already on the list, or unfreeze - those are commands for root on this server;
#  * the transfer runs as the unprivileged user kervax-mirror in a sandboxed systemd unit that can
#    write only to its staging directory; root then checks every file and moves it into the copy.
# What the panel shows comes from /var/lib/kervax/report.d/backup-mirror.json.
# Run as root.
set -euo pipefail

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-mirror-helper"
STATE_DIR=/var/lib/kervax
REQ_DIR="$STATE_DIR/mirror-req"
RES_DIR="$STATE_DIR/mirror-res"
HOME_D=/var/lib/kervax-mirror
CONF=/etc/kervax/mirror.conf
CRON=/etc/cron.d/kervax-mirror
MUSER=kervax-mirror
AGENT_USER=kervax
if ! getent group "$AGENT_USER" >/dev/null 2>&1; then
  echo "No Kervax agent on this node (group '$AGENT_USER' is missing)." >&2
  echo "Add the node in the panel first (Servers -> Add), then run this script." >&2
  exit 2
fi

# 0.1: copies of chosen repositories pulled over SSH from the main backup servers, checked by
#      sha256, removals reach the copy only after KEEP_DAYS, a large loss at once freezes the copy
KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise (0.13 > 0.2!)
# Only where the panel made this server a mirror (Backups -> Mirror servers). Being a mirror is a
# decision of a person, not a property of the node: the playbook and the installer ask the panel.
KERVAX_SETUP_WHEN="sed -n s/^token=//p /etc/kervax-agent.conf | curl -fsS -m 15 --data-binary @- $(sed -n s/^url=//p /etc/kervax-agent.conf 2>/dev/null)/api/agent/mirror-wanted | grep -qx yes"

# apt without debconf questions and needrestart, with its own timeout (see the other helpers)
apt_q() {
  DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 NEEDRESTART_MODE=l timeout -k 10 "${APT_TIMEOUT:-120}" \
    apt-get -q -y -o DPkg::Lock::Timeout=60 -o Dpkg::Options::=--force-confdef \
    -o Dpkg::Options::=--force-confold "$@" </dev/null >/dev/null 2>&1
}
for c in ssh ssh-keygen systemd-run tar sha256sum flock; do
  command -v "$c" >/dev/null 2>&1 && continue
  if [ "$c" = ssh ] || [ "$c" = ssh-keygen ]; then
    command -v apt-get >/dev/null 2>&1 && APT_TIMEOUT=600 apt_q install openssh-client || true
    command -v "$c" >/dev/null 2>&1 && continue
  fi
  echo "mirror-setup: '$c' is required and missing" >&2
  exit 2
done

install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d" /etc/kervax
# the user the transfer runs as: no login, no password, its home is root's
if ! id "$MUSER" >/dev/null 2>&1; then
  useradd --system --home-dir "$HOME_D" --no-create-home --shell /usr/sbin/nologin "$MUSER"
fi
install -d -o root -g root -m 0755 "$HOME_D" "$HOME_D/known"
install -d -o root -g root -m 0700 "$HOME_D/sources"
install -d -o "$MUSER" -g "$MUSER" -m 0700 "$HOME_D/key"
if [ ! -f "$HOME_D/key/id_ed25519" ]; then
  runuser -u "$MUSER" -- ssh-keygen -q -t ed25519 -N '' -C "kervax-mirror@$(hostname -s 2>/dev/null || echo mirror)" -f "$HOME_D/key/id_ed25519"
fi
chmod 0644 "$HOME_D/key/id_ed25519.pub"

# The settings belong to this server: the panel neither writes nor changes them
if [ ! -f "$CONF" ]; then
  cat > "$CONF" <<'CONF_EOF'
# Kervax mirror of backup repositories. Only root on this server changes this file; the panel
# cannot. After editing PULL_AT run: /lib65/kervax/kervax-mirror-helper apply-conf
#
# Where the copies live, one directory per main server and repository. Change it before the
# first copy (or move the directory yourself and change it then).
DATA=/srv/kervax-mirror
# A file removed on the main server stays in the copy this many days
KEEP_DAYS=30
# The copy freezes when one run sees this share (%) of its snapshots or data gone at once
FREEZE_PCT=50
# Daily pull, local time of this server
PULL_AT=07:30
# Every file of a copy is re-hashed once in this many days (0 = no check)
CHECK_DAYS=7
# A pull that would leave less free space than this (%) on DATA is not started
MIN_FREE_PCT=5
CONF_EOF
  chmod 0644 "$CONF"
fi

# spool: the agent drops requests (-wx on the directory) and reads answers (r-x). The answer
# directory is root's: with write access the agent could plant a symlink where root writes.
install -d -o root -g "$AGENT_USER" -m 0730 "$REQ_DIR"
install -d -o root -g "$AGENT_USER" -m 0750 "$RES_DIR"
if systemctl cat kervax-agent >/dev/null 2>&1 \
   && [ ! -f /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf ]; then
  install -d -m 0755 /etc/systemd/system/kervax-agent.service.d
  printf '[Service]\nReadWritePaths=/var/lib/kervax\n' \
    > /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf
  systemctl daemon-reload 2>/dev/null || true
  systemctl try-restart kervax-agent 2>/dev/null || true
fi

# Written next to the old one and swapped in with mv: bash reads a running script as it goes.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Kervax mirror helper (root): copies of chosen restic repositories pulled from the main backup
# servers. The design and its limits are described at the top of mirror-setup.sh.
set -euo pipefail
umask 022
export LC_ALL=C

CONF=/etc/kervax/mirror.conf
HOME_D=/var/lib/kervax-mirror
KEY="$HOME_D/key/id_ed25519"
KNOWN_D="$HOME_D/known"
SRC_D="$HOME_D/sources"
REQ_DIR=/var/lib/kervax/mirror-req
RES_DIR=/var/lib/kervax/mirror-res
REPORT=/var/lib/kervax/report.d/backup-mirror.json
VER_FILE=/var/lib/kervax/versions/mirror-setup.ver
MUSER=kervax-mirror
REMOTE_USER=kervax-mread
LOCK=/run/kervax-mirror.lock
CURRENT=/run/kervax-mirror.current
PULL_UNIT=kervax-mirror-pull
# one request to the main server takes at most this much (bytes / files), so a broken
# connection costs one batch and the next run goes on from what is already in the copy
BATCH_BYTES=$((4 * 1024 * 1024 * 1024))
BATCH_FILES=2000
# restic file names: config plus everything named by the sha256 of its content
RE_REL='^(config|keys/[0-9a-f]{64}|snapshots/[0-9a-f]{64}|index/[0-9a-f]{64}|data/[0-9a-f]{2}/[0-9a-f]{64})$'

DATA=/srv/kervax-mirror; KEEP_DAYS=30; FREEZE_PCT=50; PULL_AT=07:30; CHECK_DAYS=7; MIN_FREE_PCT=5
# The settings are read, not sourced, and every value is checked: a broken value keeps the default
load_conf() {
  local k v line
  [ -f "$CONF" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    k="${line%%=*}"; v="${line#*=}"; v="${v%%#*}"; v="${v%"${v##*[![:space:]]}"}"
    case "$k" in
      DATA) [[ "$v" =~ ^/[A-Za-z0-9._/-]*[A-Za-z0-9._-]$ ]] && [[ "$v" != *..* ]] && DATA="$v" ;;
      KEEP_DAYS) [[ "$v" =~ ^[0-9]{1,4}$ ]] && [ "$v" -ge 1 ] && KEEP_DAYS="$v" ;;
      FREEZE_PCT) [[ "$v" =~ ^[0-9]{1,3}$ ]] && [ "$v" -ge 1 ] && [ "$v" -le 100 ] && FREEZE_PCT="$v" ;;
      PULL_AT) [[ "$v" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] && PULL_AT="$v" ;;
      CHECK_DAYS) [[ "$v" =~ ^[0-9]{1,3}$ ]] && CHECK_DAYS="$v" ;;
      MIN_FREE_PCT) [[ "$v" =~ ^[0-9]{1,2}$ ]] && MIN_FREE_PCT="$v" ;;
    esac
  done < "$CONF"
  return 0
}

name_ok() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; }
host_ok() {
  [[ "$1" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || [[ "$1" =~ ^[0-9A-Fa-f:]{2,39}$ ]] \
    || [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]]
}
port_ok() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }
hostkey_ok() {
  [[ "$1" =~ ^(ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|ssh-rsa)\ [A-Za-z0-9+/]{20,2000}={0,3}$ ]]
}
num() { [[ "${1:-}" =~ ^[0-9]+$ ]] && printf '%s' "$1" || printf 0; }
json_escape() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
# a key=value file: the value of a key (empty when missing)
kv() { sed -n "s/^$2=//p" "$1" 2>/dev/null | head -n1 || true; }
# rewrite a key=value file through a temporary name: key value [key value...]
kv_put() {
  local f="$1" tmp; shift
  # a dot name: the list of main servers (a glob over the directory) never sees it
  tmp="$(mktemp "$(dirname "$f")/.kv.XXXXXX")"
  { [ -f "$f" ] && cat "$f" || true; } > "$tmp"
  while [ $# -ge 2 ]; do
    if grep -q "^$1=" "$tmp"; then
      awk -v k="$1" -v v="$2" 'index($0, k "=") == 1 { print k "=" v; next } { print }' "$tmp" > "$tmp.n" && mv -f "$tmp.n" "$tmp"
    else
      printf '%s=%s\n' "$1" "$2" >> "$tmp"
    fi
    shift 2
  done
  chmod 0600 "$tmp" && mv -f "$tmp" "$f"
}
copy_dir() { printf '%s/%s/%s' "$DATA" "$1" "$2"; }
state_dir() { printf '%s/.state/%s/%s' "$DATA" "$1" "$2"; }
src_repos() { kv "$SRC_D/$1" repos | tr ',' '\n' | grep -E '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' || true; }
sources() { for f in "$SRC_D"/*; do [ -f "$f" ] || continue; case "$f" in *.pending) continue ;; esac; f="${f##*/}"; if name_ok "$f"; then echo "$f"; fi; done; }

# -- the transfer, as kervax-mirror in its sandbox ------------------------------------------------
# fetch <src> <repo> <host> <port> <staging> <budget bytes>
# Writes into the staging directory: list (what the main server has, taken BEFORE any data: a
# snapshot in it was complete when the copy started), list.ok, files/ (what was fetched), fail
# (why it stopped) and err (what ssh said).
cmd_fetch() {
  local src="$1" repo="$2" host="$3" port="$4" stg="$5" budget="$6" n total=0 cnt=0 rel size rest
  name_ok "$src" && name_ok "$repo" && host_ok "$host" && port_ok "$port" || { echo args > "$stg/fail"; return 2; }
  local -a SSH
  # shellcheck disable=SC2206
  SSH=(${KERVAX_MIRROR_SSH:-ssh} -4 -i "$KEY" -p "$port" -o BatchMode=yes -o IdentitiesOnly=yes
       -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$KNOWN_D/$src" -o GlobalKnownHostsFile=/dev/null
       -o "HostKeyAlias=kervax-src-$src" -o ConnectTimeout=20 -o ServerAliveInterval=30
       -o ServerAliveCountMax=10 -o Compression=no -o LogLevel=ERROR -o ControlMaster=no "$REMOTE_USER@$host")
  [[ "$host" == *:* ]] && SSH=("${SSH[@]/#-4/-6}")
  if ! "${SSH[@]}" "list $repo" > "$stg/list.raw" 2> "$stg/err"; then
    echo ssh > "$stg/fail"; return 3
  fi
  # a listing is whole only with its closing line and the same number of lines before it
  n="$(tail -n1 "$stg/list.raw" | sed -n 's/^end \([0-9][0-9]*\)$/\1/p')"
  if [ -z "$n" ] || [ "$(($(wc -l < "$stg/list.raw") - 1))" != "$n" ]; then
    echo list > "$stg/fail"; echo "the listing of $repo came incomplete" >> "$stg/err"; return 3
  fi
  # grep, not awk: mawk on older Debian and Ubuntu does not know {64} in a regular expression
  sed '$d' "$stg/list.raw" | grep -E "${RE_REL%\$} [0-9]+ [0-9]+\$" | sort -k1,1 -u > "$stg/list" || true
  [ "$(wc -l < "$stg/list")" = "$n" ] || { echo list > "$stg/fail"; echo "the listing of $repo has foreign lines" >> "$stg/err"; return 3; }
  touch "$stg/list.ok"
  # what to take: everything the copy does not have yet (config every time, to compare), data
  # first and snapshots last
  awk 'FILENAME == ARGV[1] { have[$1] = 1; next } $1 == "config" || !($1 in have)' "$stg/have" "$stg/list" \
    | awk '{ p = ($1 ~ /^data\//) ? 1 : ($1 ~ /^index\//) ? 2 : ($1 ~ /^keys\//) ? 3 : ($1 ~ /^snapshots\//) ? 4 : 5; print p, $0 }' \
    | sort -k1,1n -k2,2 | cut -d' ' -f2- > "$stg/need"
  total="$(awk '{ s += $2 } END { printf "%.0f", s }' "$stg/need")"
  if [ "$total" -gt "$budget" ]; then
    echo space > "$stg/fail"; echo "needs $total bytes, $budget may be used" >> "$stg/err"; return 4
  fi
  install -d -m 0700 "$stg/files"
  : > "$stg/batch"
  total=0
  while read -r rel size rest; do
    printf '%s\n' "$rel" >> "$stg/batch"
    total=$((total + size)); cnt=$((cnt + 1))
    if [ "$total" -ge "$BATCH_BYTES" ] || [ "$cnt" -ge "$BATCH_FILES" ]; then
      fetch_batch || return 5
      : > "$stg/batch"; total=0; cnt=0
    fi
  done < "$stg/need"
  if [ "$cnt" -gt 0 ]; then fetch_batch || return 5; fi
  touch "$stg/fetched"
}
# one request: the names on stdin, a tar of those files back. Unpacked as this user into the
# staging directory only; root takes from it nothing but regular files whose hash matches
fetch_batch() {
  if ! "${SSH[@]}" "get $repo" < "$stg/batch" 2>> "$stg/err" \
       | tar -x -f - -C "$stg/files" --no-same-owner --no-same-permissions --no-overwrite-dir 2>> "$stg/err"; then
    echo get > "$stg/fail"; return 1
  fi
}

# -- root side ----------------------------------------------------------------------------------
set_status() {  # src repo key value...
  local st; st="$(state_dir "$1" "$2")"; shift 2
  install -d -m 0700 "$st"
  kv_put "$st/status" "$@"
}
fail_status() {  # src repo code text
  set_status "$1" "$2" last_try "$(date +%s)" err_code "$3" err "$(printf '%s' "$4" | tr '\n\r\t' '   ' | cut -c1-300)"
}

# The copy freezes: a mark that only `unfreeze` removes. Nothing is removed from a frozen copy and
# nothing new is taken into it.
freeze() {  # src repo reason detail
  local st; st="$(state_dir "$1" "$2")"
  install -d -m 0700 "$st"
  [ -f "$st/frozen" ] || printf '%s\t%s\t%s\n' "$(date +%s)" "$3" "$4" > "$st/frozen"
  logger -t kervax-mirror "copy $1/$2 frozen: $3 ($4)" 2>/dev/null || true
}

# Did the main server lose a large part of the repository since the previous whole listing?
# Prints "reason detail" when it did.
drop_check() {  # prev cur
  awk -v pct="$FREEZE_PCT" '
    FILENAME == ARGV[1] { prev[$1] = $2; next }
    { cur[$1] = 1 }
    END {
      ps = 0; gs = 0; pd = 0; gd = 0; pc = 0; n = 0
      for (r in cur) n++
      for (r in prev) {
        if (r == "config") { pc = 1; continue }
        if (r ~ /^snapshots\//) { ps++; if (!(r in cur)) gs++ }
        if (r ~ /^data\//) { pd += prev[r]; if (!(r in cur)) gd += prev[r] }
      }
      if (pc && !("config" in cur)) { print "config_gone the config file is gone"; exit }
      if (ps > 0 && n == 0) { print "empty the repository is empty"; exit }
      if (ps >= 2 && gs * 100 >= pct * ps) { printf "snapshots %d of %d snapshots gone at once\n", gs, ps; exit }
      if (pd >= 1048576 && gd * 100 >= pct * pd) { printf "data %.0f of %.0f bytes of data gone at once\n", gd, pd; exit }
    }' "$1" "$2"
}

# take what the transfer left in the staging directory into the copy. Prints "files bytes bad"
ingest() {  # stg copy
  local stg="$1" copy="$2" rel h f files=0 bytes=0 bad=0 sz
  [ -d "$stg/files" ] || { echo "0 0 0"; return 0; }
  find "$stg/files" -type f -printf '%P\n' | grep -E "$RE_REL" | grep -v '^config$' > "$stg/got" || true
  : > "$stg/sums"
  if [ -s "$stg/got" ]; then
    (cd "$stg/files" && xargs -d '\n' -r sha256sum -- < "$stg/got") > "$stg/sums" 2>/dev/null || true
  fi
  while read -r h rel; do
    f="$stg/files/$rel"
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    # the name is the sha256 of the content; a data file also sits under its first two characters
    if [ "$h" != "${rel##*/}" ] || { [[ "$rel" == data/* ]] && [ "${rel:5:2}" != "${h:0:2}" ]; }; then
      bad=$((bad + 1)); continue
    fi
    [ -e "$copy/$rel" ] && continue
    install -d -m 0700 "$copy/${rel%/*}"
    sz="$(stat -c %s "$f")"
    chown root:root "$f" && chmod 0600 "$f" && mv -n "$f" "$copy/$rel" || continue
    files=$((files + 1)); bytes=$((bytes + sz))
  done < "$stg/sums"
  # a file the main server sent that is not in the sums was not readable or had a foreign name
  bad=$((bad + $(wc -l < "$stg/got") - $(wc -l < "$stg/sums")))
  [ "$bad" -ge 0 ] || bad=0
  echo "$files $bytes $bad"
}

pull_one() {  # src repo
  local src="$1" repo="$2" host port hk copy st stg now budget dfl total free rc fail err line
  local reason detail res
  copy="$(copy_dir "$src" "$repo")"; st="$(state_dir "$src" "$repo")"; stg="$DATA/.staging/$src--$repo"
  install -d -m 0700 "$copy" "$st"
  [ -f "$st/added" ] || date +%s > "$st/added"
  now="$(date +%s)"
  if [ -f "$st/frozen" ]; then
    fail_status "$src" "$repo" frozen "$(cut -f2- "$st/frozen" | tr '\t' ' ')"; return 0
  fi
  host="$(kv "$SRC_D/$src" host)"; port="$(kv "$SRC_D/$src" port)"; hk="$(kv "$SRC_D/$src" hostkey)"
  if ! host_ok "$host" || ! port_ok "$port" || ! hostkey_ok "$hk"; then
    fail_status "$src" "$repo" config "no valid address or host key of $src"; return 0
  fi
  printf 'kervax-src-%s %s\n' "$src" "$hk" > "$KNOWN_D/$src.tmp" && chmod 0644 "$KNOWN_D/$src.tmp" && mv -f "$KNOWN_D/$src.tmp" "$KNOWN_D/$src"
  # free space: whatever is left above the reserve may be taken in this run
  dfl="$(df -PB1 "$DATA" 2>/dev/null | awk 'NR == 2 { print $2, $4 }')"
  total="$(num "${dfl%% *}")"; free="$(num "${dfl##* }")"
  budget=$((free - total * MIN_FREE_PCT / 100))
  if [ "$total" -eq 0 ] || [ "$budget" -le 0 ]; then
    fail_status "$src" "$repo" space "less than $MIN_FREE_PCT% free on $DATA"; return 0
  fi
  rm -rf "$stg"
  install -d -o root -g root -m 0711 "$DATA/.staging"
  install -d -o "$MUSER" -g "$MUSER" -m 0700 "$stg"
  (cd "$copy" && find . -type f -printf '%P\n') | grep -E "$RE_REL" | sort > "$stg/have" || true
  chmod 0644 "$stg/have"
  printf '%s/%s %s\n' "$src" "$repo" "$now" > "$CURRENT"
  rc=0
  if [ "${KERVAX_MIRROR_NOSANDBOX:-0}" = 1 ]; then
    runuser -u "$MUSER" -- env "KERVAX_MIRROR_SSH=${KERVAX_MIRROR_SSH:-}" "$0" fetch "$src" "$repo" "$host" "$port" "$stg" "$budget" || rc=$?
  else
    # The unit ends with all its processes (KillMode=control-group): nothing of the transfer runs
    # any more when root starts checking the files it left
    systemd-run --quiet --wait --collect \
      -p User="$MUSER" -p Group="$MUSER" -p NoNewPrivileges=yes -p ProtectSystem=strict \
      -p ProtectHome=yes -p PrivateTmp=yes -p PrivateDevices=yes -p ProtectKernelTunables=yes \
      -p ProtectControlGroups=yes -p RestrictSUIDSGID=yes -p ReadWritePaths="$stg" \
      -p Nice=10 -p IOSchedulingClass=best-effort -p IOSchedulingPriority=7 -p RuntimeMaxSec=3d \
      ${KERVAX_MIRROR_SSH:+"--setenv=KERVAX_MIRROR_SSH=$KERVAX_MIRROR_SSH"} \
      "$0" fetch "$src" "$repo" "$host" "$port" "$stg" "$budget" >/dev/null 2>&1 || rc=$?
  fi
  rm -f "$CURRENT"
  fail="$(head -c 16 "$stg/fail" 2>/dev/null | tr -cd 'a-z_' || true)"
  err="$(grep -v '^[[:space:]]*$' "$stg/err" 2>/dev/null | tail -n 3 | tr '\n' ' ' | cut -c1-300 || true)"
  [ -n "$fail" ] || [ "$rc" -eq 0 ] || fail=run
  if [ ! -f "$stg/list.ok" ]; then
    fail_status "$src" "$repo" "${fail:-run}" "${err:-the transfer stopped (code $rc)}"
    rm -rf "$stg"; return 0
  fi
  # a whole listing: first, did the main server lose a large part of the repository at once?
  if [ -f "$st/list.prev" ] && [ ! -f "$st/accept-next" ]; then
    line="$(drop_check "$st/list.prev" "$stg/list")"
    if [ -n "$line" ]; then
      reason="${line%% *}"; detail="${line#* }"
      freeze "$src" "$repo" "$reason" "$detail"
      fail_status "$src" "$repo" frozen "$reason $detail"
      rm -rf "$stg"; return 0
    fi
  fi
  # config: taken once, after that it must stay the same (a new config is a new repository)
  if [ -f "$stg/files/config" ] && [ ! -L "$stg/files/config" ]; then
    if [ ! -f "$copy/config" ]; then
      [ "$(stat -c %s "$stg/files/config")" -le 65536 ] && install -m 0600 "$stg/files/config" "$copy/config"
    elif ! cmp -s "$stg/files/config" "$copy/config"; then
      freeze "$src" "$repo" config_changed "the config of the repository changed"
      fail_status "$src" "$repo" frozen "config_changed the config of the repository changed"
      rm -rf "$stg"; return 0
    fi
  fi
  res="$(ingest "$stg" "$copy")"
  set -- $res
  local nf="$1" nb="$2" bad="$3"
  # what the copy has now, when each file was last seen on the main server, and what to remove:
  # a file gone from the main server longer than KEEP_DAYS ago
  (cd "$copy" && find . -type f -printf '%P %s %T@\n') | grep -E '^(config|keys/|snapshots/|index/|data/)' | sort -k1,1 > "$stg/copy" || true
  touch "$st/seen"
  awk -v now="$now" -v keep="$((KEEP_DAYS * 86400))" -v seen="$stg/seen.new" -v rm_list="$stg/remove" -v stat="$stg/stat" '
    FILENAME == ARGV[1] { first[$1] = $2; last[$1] = $3; next }
    FILENAME == ARGV[2] { cur[$1] = 1; ns++; if ($1 ~ /^snapshots\//) { cs++; if ($3 > cn) cn = $3 } sb += $2; next }
    {
      f = ($1 in first) ? first[$1] : now
      l = ($1 in cur) ? now : (($1 in last) ? last[$1] : now)
      if (!($1 in cur)) {
        if (l < now - keep) { print $1 > rm_list; rmn++; next }
        gn++; gb += $2
      }
      print $1, f, l > seen
      files++; bytes += $2
      if ($1 ~ /^snapshots\//) { snaps++; if (int($3) > newest) newest = int($3) }
    }
    END {
      printf "files=%d\nbytes=%.0f\nsnaps=%d\nnewest=%d\nsrc_files=%d\nsrc_bytes=%.0f\nsrc_snaps=%d\nsrc_newest=%d\ngone_files=%d\ngone_bytes=%.0f\nremoved=%d\n", \
        files, bytes, snaps, newest, ns, sb, cs, cn, gn, gb, rmn > stat
    }' "$st/seen" "$stg/list" "$stg/copy"
  touch "$stg/remove" "$stg/seen.new"
  while read -r rel; do
    [[ "$rel" =~ $RE_REL ]] && rm -f -- "$copy/$rel"
  done < "$stg/remove"
  [ -s "$stg/remove" ] && logger -t kervax-mirror "copy $src/$repo: $(wc -l < "$stg/remove") files gone from $src more than $KEEP_DAYS days ago removed" 2>/dev/null || true
  mv -f "$stg/seen.new" "$st/seen"
  cp -f "$stg/list" "$st/list.prev"
  rm -f "$st/accept-next"
  # a run that stopped half way still took what came whole; it is not "ok" until it is complete
  local -a kvs=()
  while IFS='=' read -r k v; do kvs+=("$k" "$(num "$v")"); done < "$stg/stat"
  if [ -f "$stg/fetched" ]; then
    set_status "$src" "$repo" "${kvs[@]}" last_try "$now" last_ok "$now" err_code "" err "" \
      new_files "$nf" new_bytes "$nb" bad_new "$bad"
  else
    set_status "$src" "$repo" "${kvs[@]}" last_try "$now" err_code "${fail:-run}" \
      err "$(printf '%s' "${err:-the transfer stopped (code $rc)}" | cut -c1-300)" new_files "$nf" new_bytes "$nb" bad_new "$bad"
  fi
  rm -rf "$stg"
}

# Every file of a copy is re-hashed once in CHECK_DAYS days: today the files whose name starts
# with a byte that falls on this day. A damaged one is moved aside (it is still on the main
# server, the next pull takes it again) and reported.
check_one() {  # src repo
  local src="$1" repo="$2" copy st day n=0 bad=0 h rel now
  [ "$CHECK_DAYS" -gt 0 ] || return 0
  copy="$(copy_dir "$src" "$repo")"; st="$(state_dir "$src" "$repo")"
  [ -d "$copy" ] || return 0
  now="$(date +%s)"; day=$(( now / 86400 % CHECK_DAYS ))
  (cd "$copy" && find . -type f -printf '%P\n') | grep -E "$RE_REL" | grep -v '^config$' \
    | awk -v d="$day" -v c="$CHECK_DAYS" -v h=0123456789abcdef '{
        n = $0; sub(/.*\//, "", n)
        v = (index(h, substr(n, 1, 1)) - 1) * 16 + index(h, substr(n, 2, 1)) - 1
        if (v % c == d) print }' \
    > "$st/check.list" || true
  if [ -s "$st/check.list" ]; then
    while read -r h rel; do
      n=$((n + 1))
      [ "$h" = "${rel##*/}" ] && continue
      bad=$((bad + 1))
      install -d -m 0700 "$st/bad/${rel%/*}"
      mv -f "$copy/$rel" "$st/bad/$rel" 2>/dev/null && touch "$st/bad/$rel" || true
      printf '%s %s\n' "$now" "$rel" >> "$st/badlog"
      logger -t kervax-mirror "copy $src/$repo: $rel does not match its hash, moved aside" 2>/dev/null || true
    done < <(cd "$copy" && xargs -d '\n' -r sha256sum -- < "$st/check.list" 2>/dev/null || true)
  fi
  rm -f "$st/check.list"
  # what was moved aside stays KEEP_DAYS for a look, the log of damaged files as long
  [ -d "$st/bad" ] && find "$st/bad" -type f -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
  if [ -f "$st/badlog" ]; then
    awk -v t="$((now - KEEP_DAYS * 86400))" '$1 >= t' "$st/badlog" > "$st/badlog.n" && mv -f "$st/badlog.n" "$st/badlog"
  fi
  set_status "$src" "$repo" check_ts "$now" check_files "$n" check_bad "$bad"
}

cmd_pull() {  # [src [repo]]
  local only_src="${1:-}" only_repo="${2:-}" src repo
  exec 9> "$LOCK"
  flock -n 9 || { echo "a pull is already running"; return 0; }
  load_conf
  install -d -m 0755 "$DATA"
  install -d -m 0700 "$DATA/.state"
  for src in $(sources); do
    [ -z "$only_src" ] || [ "$src" = "$only_src" ] || continue
    for repo in $(src_repos "$src"); do
      [ -z "$only_repo" ] || [ "$repo" = "$only_repo" ] || continue
      pull_one "$src" "$repo" || fail_status "$src" "$repo" run "the pull failed"
      check_one "$src" "$repo" || true
      refresh_report
    done
  done
  refresh_report
}

# -- the panel's requests (spool) ---------------------------------------------------------------
# set: the list of main servers and repositories the panel wants copied. A new main server is
# added with its address and host key; for one already on the list only the set of repositories
# changes - a different address or key is kept aside until root confirms it (accept). Repositories
# left out are paused: the copy stays as it is. Nothing is removed here.
cmd_set() {  # file with "src host port ktype kb64 repos" lines
  local list="$1" src host port kt kb repos f added=0 out="" mentioned=" " r st
  load_conf
  install -d -m 0755 "$DATA"
  install -d -m 0700 "$DATA/.state"
  while read -r src host port kt kb repos; do
    [ -n "$src" ] || continue
    if ! name_ok "$src" || ! host_ok "$host" || ! port_ok "$port" || ! hostkey_ok "$kt $kb"; then
      out="$out; skipped a bad entry"; continue
    fi
    [ "$repos" = "-" ] && repos=""
    [[ "$repos" =~ ^([A-Za-z0-9][A-Za-z0-9._-]{0,127})?(,[A-Za-z0-9][A-Za-z0-9._-]{0,127})*$ ]] || { out="$out; $src: bad repository names"; continue; }
    mentioned="$mentioned$src "
    f="$SRC_D/$src"
    if [ ! -f "$f" ]; then
      kv_put "$f" host "$host" port "$port" hostkey "$kt $kb" repos "$repos"
      out="$out; $src added"
    else
      if [ "$(kv "$f" host)" != "$host" ] || [ "$(kv "$f" port)" != "$port" ] || [ "$(kv "$f" hostkey)" != "$kt $kb" ]; then
        kv_put "$f.pending" host "$host" port "$port" hostkey "$kt $kb" ts "$(date +%s)"
        out="$out; $src: a new address or host key waits for 'accept $src' on this server"
      else
        rm -f "$f.pending"
      fi
      kv_put "$f" repos "$repos"
    fi
    for r in $(src_repos "$src"); do
      st="$(state_dir "$src" "$r")"
      if [ ! -f "$st/added" ]; then install -d -m 0700 "$st"; date +%s > "$st/added"; added=1; fi
    done
  done < "$list"
  # main servers the panel no longer names: everything of theirs is paused, nothing removed
  for src in $(sources); do
    case "$mentioned" in *" $src "*) continue ;; esac
    [ -n "$(kv "$SRC_D/$src" repos)" ] && { kv_put "$SRC_D/$src" repos ""; out="$out; $src paused"; }
  done
  refresh_report
  [ "$added" = 1 ] && systemctl start --no-block "$PULL_UNIT.service" >/dev/null 2>&1 || true
  echo "ok${out}"
}

res_write() {  # $1 id, $2 ok, $3 output
  local tmp
  tmp="$(mktemp "$RES_DIR/.res.XXXXXX")" || return 1
  printf 'ok=%s\noutput=%s\n' "$2" "$3" > "$tmp" && chmod 0644 "$tmp" && mv -fT "$tmp" "$RES_DIR/$1.res" || rm -f "$tmp"
}

cmd_process_spool() {
  local req id action line k v out ok list
  find "$RES_DIR" -maxdepth 1 -type f -name '*.res' -mmin +10 -delete 2>/dev/null || true
  for req in "$REQ_DIR"/*.req; do
    [ -e "$req" ] || [ -L "$req" ] || continue
    id="$(basename "$req" .req)"
    if [ -L "$req" ] || [ ! -f "$req" ] || [[ ! "$id" =~ ^[0-9A-Za-z_-]{1,64}$ ]]; then rm -f "$req"; continue; fi
    action=""; list="$(mktemp)"
    while IFS= read -r line; do
      k="${line%%=*}"; v="${line#*=}"
      case "$k" in
        action) action="$v" ;;
        source) printf '%s\n' "$v" >> "$list" ;;
      esac
    done < <(head -c 262144 "$req")
    rm -f "$req"
    out=""; ok=false
    case "$action" in
      set)  if out="$(cmd_set "$list" 2>&1)"; then ok=true; fi ;;
      pull) systemctl start --no-block "$PULL_UNIT.service" >/dev/null 2>&1 && { ok=true; out="the pull started"; } || out="could not start $PULL_UNIT" ;;
      *) out="unknown action" ;;
    esac
    rm -f "$list"
    res_write "$id" "$ok" "$(printf '%s' "$out" | tr '\n' '\r')" || true
  done
}

# -- what the panel sees ------------------------------------------------------------------------
refresh_report() {
  local tmp
  tmp="$(mktemp "$REPORT.XXXXXX")" || return 0
  if cmd_report > "$tmp" 2>/dev/null; then
    chmod 0644 "$tmp" && mv -f "$tmp" "$REPORT"
  else
    rm -f "$tmp"
  fi
}

cmd_report() {
  local now src repo f st copies="" srcs="" first=1 k fr fts frw pend dfl d_total=0 d_free=0
  local enabled paused_list cur="" running=false next=0 pk ver n bl
  load_conf
  now="$(date +%s)"
  pk="$(head -n1 "$KEY.pub" 2>/dev/null | cut -d' ' -f1,2 || true)"
  ver="$(head -c 16 "$VER_FILE" 2>/dev/null | tr -cd '0-9.' || true)"
  if [ -d "$DATA" ]; then
    dfl="$(df -PB1 "$DATA" 2>/dev/null | awk 'NR == 2 { print $2, $4 }')"
    d_total="$(num "${dfl%% *}")"; d_free="$(num "${dfl##* }")"
  fi
  [ "$(systemctl is-active "$PULL_UNIT.service" 2>/dev/null || true)" = activating ] && running=true
  [ -f "$CURRENT" ] && cur="$(head -n1 "$CURRENT" | cut -d' ' -f1)"
  next="$(systemctl show "$PULL_UNIT.timer" -p NextElapseUSecRealtime --value 2>/dev/null || true)"
  next="$(date -d "$next" +%s 2>/dev/null || echo 0)"
  for src in $(sources); do
    f="$SRC_D/$src"
    pend=null
    if [ -f "$f.pending" ]; then
      pend="{\"host\":\"$(json_escape "$(kv "$f.pending" host)")\",\"port\":$(num "$(kv "$f.pending" port)"),\"ts\":$(num "$(kv "$f.pending" ts)")}"
    fi
    srcs="${srcs:+$srcs,}\"$src\":{\"host\":\"$(json_escape "$(kv "$f" host)")\",\"port\":$(num "$(kv "$f" port)"),\"repos\":[$(src_repos "$src" | sed 's/.*/"&"/' | paste -sd, -)],\"pending\":$pend}"
  done
  # every copy on disk: the ones on the list and the paused ones
  if [ -d "$DATA/.state" ]; then
    for st in "$DATA"/.state/*/*/; do
      [ -d "$st" ] || continue
      st="${st%/}"; repo="${st##*/}"; src="${st%/*}"; src="${src##*/}"
      name_ok "$src" && name_ok "$repo" || continue
      enabled=false
      src_repos "$src" | grep -qxF -- "$repo" && enabled=true
      fr=false; fts=0; frw=""
      if [ -f "$st/frozen" ]; then
        fr=true; fts="$(num "$(cut -f1 "$st/frozen" | head -n1)")"; frw="$(cut -f2- "$st/frozen" | head -n1 | tr '\t' ' ' | cut -c1-200)"
      fi
      bl=0; [ -f "$st/badlog" ] && bl="$(grep -c . "$st/badlog" || true)"
      local o="{\"src\":\"$src\",\"repo\":\"$repo\",\"enabled\":$enabled,\"added\":$(num "$(cat "$st/added" 2>/dev/null || true)"),\"frozen\":$fr,\"frozen_ts\":$fts,\"frozen_why\":\"$(json_escape "$frw")\",\"bad_files\":$(num "$bl")"
      for k in last_try last_ok files bytes snaps newest src_files src_bytes src_snaps src_newest gone_files gone_bytes removed new_files new_bytes bad_new check_ts check_files check_bad; do
        o="$o,\"$k\":$(num "$(kv "$st/status" "$k")")"
      done
      o="$o,\"err_code\":\"$(json_escape "$(kv "$st/status" err_code | cut -c1-24)")\",\"err\":\"$(json_escape "$(kv "$st/status" err | cut -c1-300)")\"}"
      copies="${copies:+$copies,}$o"
    done
  fi
  printf '{"v":1,"ts":%s,"helper":"%s","pubkey":"%s","data_dir":"%s","keep_days":%s,"freeze_pct":%s,"pull_at":"%s","check_days":%s,"min_free_pct":%s,"disk_total":%s,"disk_free":%s,"running":%s,"current":"%s","next_run":%s,"sources":{%s},"copies":[%s]}\n' \
    "$now" "$ver" "$(json_escape "$pk")" "$(json_escape "$DATA")" "$KEEP_DAYS" "$FREEZE_PCT" "$PULL_AT" "$CHECK_DAYS" "$MIN_FREE_PCT" \
    "$d_total" "$d_free" "$running" "$(json_escape "$cur")" "$(num "$next")" "$srcs" "$copies"
}

# -- commands for root on this server -----------------------------------------------------------
cmd_status() {
  local st src repo
  load_conf
  echo "copies in $DATA, removals reach a copy after $KEEP_DAYS days, freeze at $FREEZE_PCT% gone at once"
  for src in $(sources); do
    echo "$src: $(kv "$SRC_D/$src" host):$(kv "$SRC_D/$src" port), repositories: $(kv "$SRC_D/$src" repos)"
    [ -f "$SRC_D/$src.pending" ] && echo "  waits for 'accept $src': $(kv "$SRC_D/$src.pending" host):$(kv "$SRC_D/$src.pending" port) $(kv "$SRC_D/$src.pending" hostkey | cut -c1-40)..."
  done
  for st in "$DATA"/.state/*/*/; do
    [ -d "$st" ] || continue
    st="${st%/}"; repo="${st##*/}"; src="${st%/*}"; src="${src##*/}"
    printf '%s/%s: last ok %s, %s files, %s bytes%s%s\n' "$src" "$repo" \
      "$(d="$(kv "$st/status" last_ok)"; [ -n "$d" ] && [ "$d" != 0 ] && date -d "@$d" '+%F %R' || echo never)" \
      "$(kv "$st/status" files)" "$(kv "$st/status" bytes)" \
      "$([ -n "$(kv "$st/status" err_code)" ] && echo ", error: $(kv "$st/status" err_code) $(kv "$st/status" err)")" \
      "$([ -f "$st/frozen" ] && echo ", FROZEN: $(cut -f2- "$st/frozen" | tr '\t' ' ')")"
  done
}

# unfreeze <src> <repo>: the loss was expected (a large prune, the repository started anew). What
# the main server has now becomes the reference, and every file missing there gets KEEP_DAYS more
cmd_unfreeze() {
  local src="${1:-}" repo="${2:-}" st now
  name_ok "$src" && name_ok "$repo" || { echo "usage: unfreeze <server> <repository>" >&2; return 2; }
  load_conf
  st="$(state_dir "$src" "$repo")"
  [ -f "$st/frozen" ] || { echo "$src/$repo is not frozen"; return 0; }
  now="$(date +%s)"
  [ -f "$st/seen" ] && awk -v now="$now" '{ print $1, $2, now }' "$st/seen" > "$st/seen.n" && mv -f "$st/seen.n" "$st/seen"
  if cut -f2 "$st/frozen" | grep -q '^config'; then
    echo "the config changed: the old copy is kept in $(copy_dir "$src" "$repo").old-$now, a new copy starts"
    mv "$(copy_dir "$src" "$repo")" "$(copy_dir "$src" "$repo").old-$now"
    rm -f "$st/seen" "$st/list.prev"
  fi
  touch "$st/accept-next"
  rm -f "$st/frozen"
  set_status "$src" "$repo" err_code "" err ""
  logger -t kervax-mirror "copy $src/$repo unfrozen by hand" 2>/dev/null || true
  echo "$src/$repo unfrozen: the next pull takes the main server as it is now, files missing there stay $KEEP_DAYS more days"
  refresh_report
}

# accept <src>: take the address and host key the panel sent for a main server already on the list
cmd_accept() {
  local src="${1:-}" f
  name_ok "$src" || { echo "usage: accept <server>" >&2; return 2; }
  f="$SRC_D/$src"
  [ -f "$f.pending" ] || { echo "nothing waits for $src"; return 0; }
  kv_put "$f" host "$(kv "$f.pending" host)" port "$(kv "$f.pending" port)" hostkey "$(kv "$f.pending" hostkey)"
  rm -f "$f.pending"
  echo "$src: now $(kv "$f" host):$(kv "$f" port)"
  refresh_report
}

# drop <src> <repo> --yes: remove a copy for good (the only way a copy goes away)
cmd_drop() {
  local src="${1:-}" repo="${2:-}" yes="${3:-}"
  name_ok "$src" && name_ok "$repo" || { echo "usage: drop <server> <repository> --yes" >&2; return 2; }
  [ "$yes" = --yes ] || { echo "this removes the copy $src/$repo for good; repeat with --yes" >&2; return 2; }
  load_conf
  exec 9> "$LOCK"
  flock -w 60 9 || { echo "a pull is running, try later" >&2; return 2; }
  rm -rf -- "$(copy_dir "$src" "$repo")" "$(state_dir "$src" "$repo")"
  rmdir "$DATA/$src" "$DATA/.state/$src" 2>/dev/null || true
  logger -t kervax-mirror "copy $src/$repo removed by hand" 2>/dev/null || true
  echo "$src/$repo removed. If the panel still has it on the list, the next pull starts a new copy."
  refresh_report
}

# apply-conf: the daily pull at PULL_AT from the settings
cmd_apply_conf() {
  load_conf
  cat > /etc/systemd/system/kervax-mirror-pull.timer <<UNIT_EOF
[Unit]
Description=Kervax mirror: daily pull of backup repositories
[Timer]
OnCalendar=*-*-* $PULL_AT:00
RandomizedDelaySec=10min
Persistent=true
[Install]
WantedBy=timers.target
UNIT_EOF
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable --now "$PULL_UNIT.timer" >/dev/null 2>&1 || true
  echo "daily pull at $PULL_AT, copies in $DATA"
  refresh_report
}

case "${1:-}" in
  fetch)         shift; cmd_fetch "$@" ;;
  pull)          shift; cmd_pull "$@" ;;
  report)        refresh_report ;;
  status)        cmd_status ;;
  unfreeze)      shift; cmd_unfreeze "$@" ;;
  accept)        shift; cmd_accept "$@" ;;
  drop)          shift; cmd_drop "$@" ;;
  apply-conf)    cmd_apply_conf ;;
  process-spool) cmd_process_spool ;;
  *) echo "usage: $0 {status|pull [server [repository]]|unfreeze <server> <repository>|accept <server>|drop <server> <repository> --yes|apply-conf|report|process-spool}" >&2; exit 2 ;;
esac
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-mirror-pull.service <<UNIT_EOF
[Unit]
Description=Kervax mirror: pull copies of backup repositories
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=$HELPER pull
TimeoutStartSec=infinity
Nice=10
UNIT_EOF
cat > /etc/systemd/system/kervax-mirror-req.service <<UNIT_EOF
[Unit]
Description=Kervax mirror: requests from the panel
[Service]
Type=oneshot
ExecStart=$HELPER process-spool
UNIT_EOF
cat > /etc/systemd/system/kervax-mirror-req.path <<UNIT_EOF
[Unit]
Description=Kervax mirror: watch the request spool
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-mirror-req.service
[Install]
WantedBy=multi-user.target
UNIT_EOF
"$HELPER" apply-conf >/dev/null
systemctl enable --now kervax-mirror-req.path >/dev/null 2>&1 || true
# what the panel sees: rebuilt every 5 minutes from the state files (cheap), and after every pull
cat > "$CRON" <<CRON_EOF
*/5 * * * * root $HELPER report
CRON_EOF
chmod 0644 "$CRON"

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/mirror-setup.ver"
chmod 0644 "$STATE_DIR/versions/mirror-setup.ver"
"$HELPER" report
"$HELPER" process-spool >/dev/null 2>&1 || true
echo "mirror-setup: done -> $HELPER; settings in $CONF; key $(cut -d' ' -f1,2 "$HOME_D/key/id_ed25519.pub" | cut -c1-40)..."
