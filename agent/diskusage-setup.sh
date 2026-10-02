#!/usr/bin/env bash
# Kervax: where the disk space went. When a filesystem fills up (75% used), a root helper
# measures the usual suspects - systemd journal, rotated and oversized logs, package caches, docker, container logs,
# old temp files, restic cache, core dumps, space held by deleted files that are still
# open - plus the biggest directories, and writes /var/lib/kervax/report.d/disk-usage.json.
# The agent hands report.d to the panel as is, so no agent release is needed.
#
# READ ONLY: nothing is deleted here. Every finding carries the command that frees the
# space; the panel shows it for a human to run.
set -euo pipefail

KERVAX_SETUP_VERSION=0.2  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: while disks have room it only reads df

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-disk-usage"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/disk-usage.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Where the disk space went -> /var/lib/kervax/report.d/disk-usage.json. Deletes nothing.
set -u
export LC_ALL=C
OUT=/var/lib/kervax/report.d/disk-usage.json
CACHE=/var/lib/kervax/disk-usage       # slow measurements (du, docker) between runs
TMP="$OUT.tmp.$$"
HOT_PCT=75                             # a filesystem is "filling up" from this usage
MIN_ITEM=$((32 * 1024 * 1024))         # smaller findings are not worth a line
MIN_LOG=$((256 * 1024 * 1024))         # a single log file this big gets its own line
SLOW_MIN=60                            # full du, docker and restic cache: once an hour
DU_TIMEOUT=240
MAX_INODES=3000000                     # a full walk of a backup server takes hours: skip it

now=$(date +%s)
TAB=$(printf '\t')
install -d -m 0700 "$CACHE"

have() { command -v "$1" >/dev/null 2>&1; }
# JSON string body: control characters dropped, backslash and quote escaped. A single raw
# control character makes the agent drop the whole file, so this is not optional.
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
# Sizes are summed in awk and printed with %.0f: mawk's %d is 32-bit and cuts at 2 GiB.
sum_bytes() { awk '{s += $1} END {printf "%.0f", s}'; }
size_of() { du -sxB1 -- "$@" 2>/dev/null | sum_bytes; }
mount_of() { stat -c %m -- "$1" 2>/dev/null || df -P -- "$1" 2>/dev/null | awk 'NR == 2 {print $NF}'; }
# A slow measurement is redone once an hour, otherwise the cached result is reused.
fresh() { [ -s "$1" ] && [ -n "$(find "$1" -mmin -"$SLOW_MIN" 2>/dev/null)" ]; }

# Local filesystems, one per device (bind mounts and docker volumes repeat the device):
# mount, size, used, avail.
fs_list() {
  df -P -B1 -l -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs -x ramfs -x nsfs \
     -x fuse.lxcfs 2>/dev/null \
  | awk 'NR > 1 && $2 > 0 {
      mnt = $6; for (i = 7; i <= NF; i++) mnt = mnt " " $i
      if (mnt ~ /^\/(proc|sys|run|dev|snap)(\/|$)/) next
      if (mnt ~ /^\/var\/lib\/(docker|containerd|kubelet|k0s|rancher)\//) next
      if (($1 in seen) && length(seen[$1]) <= length(mnt)) next
      seen[$1] = mnt; size[$1] = $2; used[$1] = $3; avail[$1] = $4 }
    END { for (d in seen) printf "%s\t%.0f\t%.0f\t%.0f\n", seen[d], size[d], used[d], avail[d] }'
}

HOT=""
while IFS="$TAB" read -r mnt size used avail; do
  [ -n "$mnt" ] || continue
  tot=$((used + avail))
  [ "$tot" -gt 0 ] || continue
  pct=$(( (used * 100 + tot - 1) / tot ))   # rounded up, as df does
  if [ "$pct" -ge "$HOT_PCT" ]; then
    HOT="$HOT$(printf '%s\t%s\t%s\t%s\t%s' "$mnt" "$size" "$used" "$avail" "$pct")
"
  fi
done <<EOF_FS
$(fs_list)
EOF_FS

# Nothing is filling up - no block at all, the panel shows nothing.
if [ -z "$HOT" ]; then
  rm -f "$OUT"
  exit 0
fi
is_hot() { printf '%s' "$HOT" | cut -f1 | grep -qxF -- "$1"; }

ITEMS=""
# id bytes free path level fix [extra json fields]. Only findings on a filesystem that is
# filling up: the journal on a roomy / says nothing about a full /data.
add_item() {
  local id=$1 b=${2:-0} f=${3:-0} p=$4 lvl=$5 fix=$6 extra=${7:-} m
  [ "$b" -ge "$MIN_ITEM" ] 2>/dev/null || return 0
  m=$(mount_of "$p")
  [ -n "$m" ] && is_hot "$m" || return 0
  ITEMS="$ITEMS${ITEMS:+,}{\"id\":\"$id\",\"bytes\":$b,\"free\":$f,\"mount\":\"$(esc "$m")\",\"path\":\"$(esc "$p")\",\"level\":\"$lvl\",\"fix\":\"$(esc "$fix")\"$extra}"
}

# -- systemd journal: keeps up to 10% of the disk by default --
j=$(size_of /var/log/journal)
jfree=$((j - 200 * 1024 * 1024)); [ "$jfree" -gt 0 ] || jfree=0
add_item journal "$j" "$jfree" /var/log/journal safe "journalctl --vacuum-size=200M"

# -- rotated logs older than a week (logrotate leftovers) --
FIX_ROT='find /var/log -xdev -type f \( -name "*.gz" -o -name "*.xz" -o -name "*.bz2" -o -name "*.zst" -o -name "*.old" -o -regex ".*\.[0-9][0-9]*" \) -mtime +7 -delete'
rot=$(find /var/log -xdev -path /var/log/journal -prune -o -type f \( -name '*.gz' -o -name '*.xz' \
        -o -name '*.bz2' -o -name '*.zst' -o -name '*.old' -o -regex '.*\.[0-9][0-9]*' \) \
        -mtime +7 -printf '%s\n' 2>/dev/null | awk '{s += $1; n++} END {printf "%.0f %d", s, n}')
set -- $rot
add_item rotated-logs "${1:-0}" "${1:-0}" /var/log safe "$FIX_ROT" ",\"count\":${2:-0}"

# -- single oversized logs: logrotate is missing or does not keep up --
while IFS="$TAB" read -r b p; do
  [ -n "$p" ] || continue
  case "$p" in *\'*) continue ;; esac
  add_item big-log "$b" "$b" "$p" careful "truncate -s 0 '$p'"
done <<EOF_LOGS
$(find /var/log -xdev -path /var/log/journal -prune -o -type f -size +$((MIN_LOG / 1024))k \
    ! -name '*.gz' ! -name '*.xz' ! -name '*.bz2' ! -name '*.zst' ! -name '*.old' \
    ! -regex '.*\.[0-9][0-9]*' -printf '%s\t%p\n' 2>/dev/null \
  | sort -rn | head -5)
EOF_LOGS

# -- package caches --
if have apt-get; then
  a=$(size_of /var/cache/apt/archives)
  add_item apt-cache "$a" "$a" /var/cache/apt/archives safe "apt-get clean"
fi
if have dnf; then
  a=$(size_of /var/cache/dnf)
  add_item dnf-cache "$a" "$a" /var/cache/dnf safe "dnf clean all"
elif have yum; then
  a=$(size_of /var/cache/yum)
  add_item dnf-cache "$a" "$a" /var/cache/yum safe "yum clean all"
fi

# -- old temp files: the ages systemd-tmpfiles uses by default (10 and 30 days) --
t=$(find /tmp -xdev -type f -mtime +10 -ctime +10 -printf '%s\n' 2>/dev/null | sum_bytes)
add_item tmp-old "$t" "$t" /tmp careful "find /tmp -xdev -type f -mtime +10 -ctime +10 -delete"
t=$(find /var/tmp -xdev -type f -mtime +30 -ctime +30 -printf '%s\n' 2>/dev/null | sum_bytes)
add_item vartmp-old "$t" "$t" /var/tmp careful "find /var/tmp -xdev -type f -mtime +30 -ctime +30 -delete"

# -- core dumps --
c=$(size_of /var/lib/systemd/coredump)
add_item coredumps "$c" "$c" /var/lib/systemd/coredump safe "rm -f /var/lib/systemd/coredump/*"
c=$(size_of /var/crash)
add_item crash-reports "$c" "$c" /var/crash safe "rm -f /var/crash/*"

# -- restic cache: rebuilt by the next backup; on busy repositories it grows to tens of GB --
for d in /root/.cache/restic /var/cache/restic /home/*/.cache/restic; do
  [ -d "$d" ] || continue
  case "$d" in *\'*) continue ;; esac
  f="$CACHE/restic-$(printf '%s' "$d" | md5sum | cut -c1-12)"
  # the hourly measurement is redone early when the cache was removed and built again
  if ! fresh "$f" || [ -n "$(find "$d" -maxdepth 0 -newer "$f" 2>/dev/null)" ]; then
    size_of "$d" > "$f"
  fi
  r=$(cat "$f" 2>/dev/null)
  add_item restic-cache "${r:-0}" "${r:-0}" "$d" careful "rm -rf '$d'"
done

# -- docker: images, build cache, stopped containers, volumes, container logs --
if have docker && timeout 20 docker info >/dev/null 2>&1; then
  root=$(timeout 20 docker info -f '{{.DockerRootDir}}' 2>/dev/null)
  [ -n "$root" ] || root=/var/lib/docker
  f="$CACHE/docker-df"
  if ! fresh "$f"; then
    # docker counts in powers of 1000: "12.3GB", "512kB", "1.2GB (45%)"
    { timeout 120 docker system df --format '{{.Type}}|{{.Size}}|{{.Reclaimable}}' 2>/dev/null
      printf 'Dangling|'
      timeout 60 docker images -f dangling=true --format '{{.Size}}' 2>/dev/null | tr '\n' ' '
      printf '|\n'
    } | awk -F'|' '
      function b(s,   n, u) {
        n = s + 0; u = s; sub(/^[ ]*[0-9.]+/, "", u); sub(/[ (].*/, "", u)
        if (u == "kB" || u == "KB") n *= 1e3; else if (u == "MB") n *= 1e6
        else if (u == "GB") n *= 1e9; else if (u == "TB") n *= 1e12
        return n }
      $1 == "Dangling" { k = split($2, a, " "); s = 0; for (i = 1; i <= k; i++) s += b(a[i]); printf "Dangling\t%.0f\t%.0f\n", s, s; next }
      NF >= 3 { printf "%s\t%.0f\t%.0f\n", $1, b($2), b($3) }' > "$f.tmp"
    mv -f "$f.tmp" "$f"
  fi
  # "Reclaimable" of images already includes the dangling ones, which get their own (safe)
  # line: the careful line counts only tagged images that no container uses.
  dang=$(awk -F'\t' '$1 == "Dangling" {print $3}' "$f")
  while IFS="$TAB" read -r typ sz rc; do
    case "$typ" in
      Images)
        rc=$((rc - ${dang:-0})); [ "$rc" -gt 0 ] || rc=0
        add_item docker-images "$rc" "$rc" "$root" careful "docker image prune -af" ;;
      "Build Cache") add_item docker-build-cache "$rc" "$rc" "$root" safe "docker builder prune -af" ;;
      Containers) add_item docker-containers "$rc" "$rc" "$root" careful "docker container prune -f" ;;
      "Local Volumes") add_item docker-volumes "$rc" "$rc" "$root" manual "docker volume ls -f dangling=true" ;;
      Dangling) add_item docker-dangling "$rc" "$rc" "$root" safe "docker image prune -f" ;;
    esac
  done < "$f"

  # Container logs without max-size: the json-file driver never rotates them by itself.
  # Measured on every run, not cached: one docker inspect is cheap, and a log that was just
  # truncated must not hang in the panel at its old size for an hour.
  f="$CACHE/docker-logs"
  ids=$(timeout 30 docker ps -aq 2>/dev/null | tr '\n' ' ')
  if [ -n "$ids" ]; then
    # shellcheck disable=SC2086
    timeout 60 docker inspect --format '{{.Name}}|{{.LogPath}}' $ids 2>/dev/null \
    | while IFS='|' read -r name lp; do
        [ -n "$lp" ] && [ -f "$lp" ] && printf '%s\t%s\t%s\n' "$(stat -c %s -- "$lp")" "${name#/}" "$lp"
      done | sort -rn | head -5 > "$f"
  else
    : > "$f"
  fi
  while IFS="$TAB" read -r b name lp; do
    [ -n "$lp" ] || continue
    [ "$b" -ge "$MIN_LOG" ] 2>/dev/null || continue
    case "$lp" in *\'*) continue ;; esac
    add_item container-log "$b" "$b" "$lp" careful "truncate -s 0 '$lp'" ",\"name\":\"$(esc "$name")\""
  done < "$f"
fi

# -- deleted files still held open: df counts them, du does not --
# Device numbers of the filesystems that are filling up, "dev mount" per line: a deleted
# file is counted on the filesystem it actually lives on.
DEVMAP=""
while IFS="$TAB" read -r mnt _; do
  [ -n "$mnt" ] || continue
  d=$(stat -c %d -- "$mnt" 2>/dev/null) && DEVMAP="$DEVMAP$d $mnt
"
done <<EOF_HOT
$HOT
EOF_HOT
find /proc/[0-9]*/fd -lname '*(deleted)' -printf '%p\t%l\n' 2>/dev/null > "$CACHE/deleted.tmp"
SEEN=""
while IFS="$TAB" read -r fd target; do
  case "$target" in /memfd:*|/dev/*|/SYSV*|/run/*|/proc/*) continue ;; esac
  # shellcheck disable=SC2046
  set -- $(stat -L -c '%d %i %s' -- "$fd" 2>/dev/null)
  [ $# -eq 3 ] || continue
  printf '%s' "$DEVMAP" | grep -q "^$1 " || continue
  case " $SEEN " in *" $1:$2 "*) continue ;; esac   # one file opened many times counts once
  SEEN="$SEEN $1:$2"
  pid=${fd#/proc/}; pid=${pid%%/*}
  printf '%s\t%s\t%s\t%s\n' "$1" "$3" "$pid" "$(tr -d '\000-\037' < "/proc/$pid/comm" 2>/dev/null)"
done < "$CACHE/deleted.tmp" > "$CACHE/deleted.sum"
rm -f "$CACHE/deleted.tmp"
for d in $(cut -f1 "$CACHE/deleted.sum" | sort -u); do
  b=$(awk -F'\t' -v d="$d" '$1 == d {s += $2} END {printf "%.0f", s}' "$CACHE/deleted.sum")
  [ "$b" -ge "$MIN_ITEM" ] 2>/dev/null || continue
  procs=$(awk -F'\t' -v d="$d" '$1 == d {b[$3 "\t" $4] += $2} END {for (k in b) printf "%.0f\t%s\n", b[k], k}' "$CACHE/deleted.sum" \
    | sort -rn | head -3 \
    | awk -F'\t' '{gsub(/[\\"]/, "", $3); printf "%s{\"pid\":%s,\"comm\":\"%s\",\"bytes\":%s}", (NR > 1 ? "," : ""), $2, $3, $1}')
  mnt=$(printf '%s' "$DEVMAP" | awk -v d="$d" '$1 == d {sub(/^[^ ]+ /, ""); print; exit}')
  add_item deleted-open "$b" "$b" "$mnt" manual "" ",\"procs\":[$procs]"
done
rm -f "$CACHE/deleted.sum"

# -- /boot full of old kernels: apt then cannot install updates --
if is_hot /boot && have apt-get; then
  k=$(ls /boot/vmlinuz-* 2>/dev/null | wc -l)
  if [ "$k" -gt 2 ]; then
    add_item old-kernels "$(size_of /boot)" 0 /boot careful "apt-get autoremove --purge" ",\"count\":$k"
  fi
fi

# -- the biggest directories of every filesystem that is filling up --
FSJ=""
while IFS="$TAB" read -r mnt size used avail pct; do
  [ -n "$mnt" ] || continue
  f="$CACHE/top-$(printf '%s' "$mnt" | md5sum | cut -c1-12)"
  if ! fresh "$f"; then
    ino=$(df -P -i -- "$mnt" 2>/dev/null | awk 'NR == 2 {print $3}')
    if [ "${ino:-0}" -gt "$MAX_INODES" ] 2>/dev/null; then
      printf 'skipped\t%s\t%s\n' "$now" "$ino" > "$f"
    else
      min=$((size / 100)); [ "$min" -ge $((128 * 1024 * 1024)) ] || min=$((128 * 1024 * 1024))
      timeout "$DU_TIMEOUT" du -x -B1 -d 3 -- "$mnt" > "$f.raw" 2>/dev/null
      rc=$?
      st=ok; [ "$rc" -eq 124 ] && st=partial
      { printf '%s\t%s\t%s\n' "$st" "$now" "${ino:-0}"
        awk -F'\t' -v min="$min" -v root="$mnt" '$1 >= min && $2 != root' "$f.raw" \
          | sort -t "$(printf '\t')" -k1,1nr | head -40
      } > "$f.tmp"
      mv -f "$f.tmp" "$f"
      rm -f "$f.raw"
    fi
  fi
  hdr=$(head -1 "$f")
  st=$(printf '%s' "$hdr" | cut -f1); tts=$(printf '%s' "$hdr" | cut -f2); ino=$(printf '%s' "$hdr" | cut -f3)
  top=""
  while IFS="$TAB" read -r b p; do
    [ -n "$p" ] || continue
    top="$top${top:+,}{\"path\":\"$(esc "$p")\",\"bytes\":$b}"
  done <<EOF_TOP
$(tail -n +2 "$f")
EOF_TOP
  FSJ="$FSJ${FSJ:+,}{\"mount\":\"$(esc "$mnt")\",\"size\":$size,\"used\":$used,\"avail\":$avail,\"pct\":$pct,\"inodes\":${ino:-0},\"top_state\":\"$st\",\"top_ts\":${tts:-0},\"top\":[$top]}"
done <<EOF_HOT2
$HOT
EOF_HOT2

printf '{"v":1,"ts":%s,"fs":[%s],"items":[%s]}\n' "$now" "$FSJ" "$ITEMS" > "$TMP"
mv -f "$TMP" "$OUT"
chmod 0644 "$OUT"
HELPER_EOF
chmod 0755 "$HELPER"

# Idle CPU and IO priority: a full du on a busy disk must not compete with the workload.
cat > /etc/systemd/system/kervax-disk-usage.service <<EOF
[Unit]
Description=Kervax: where the disk space went (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=15min
EOF
cat > /etc/systemd/system/kervax-disk-usage.timer <<'EOF'
[Unit]
Description=Kervax: check where the disk space went periodically
[Timer]
OnBootSec=4min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-disk-usage.timer >/dev/null 2>&1 || true
# The first run in the background: on a full disk the du may take minutes, and the
# installer (or an ansible run over the fleet) must not wait for it.
systemctl start --no-block kervax-disk-usage.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/diskusage-setup.ver"
chmod 0644 "$STATE_DIR/versions/diskusage-setup.ver"
echo "✓ diskusage-setup: where the disk space went -> $OUT (every 10 minutes, only while a disk fills up)."
