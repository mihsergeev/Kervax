#!/usr/bin/env bash
# Kervax: where the disk space went. When a filesystem fills up (75% used), a root helper
# measures the usual suspects - systemd journal, rotated and oversized logs, package caches,
# docker, container logs, old files in the temp directories, restic cache, core dumps, space
# held by deleted files that are still open - plus the biggest directories, and writes
# /var/lib/kervax/report.d/disk-usage.json. The agent hands report.d to the panel as is.
#
# The measurement deletes nothing: every finding carries the command that frees the space.
# Since 0.3 the panel can also start some of them itself ("Free up" with a preview first):
# a root fixer with a fixed catalogue of actions, reached through a spool like the other
# helpers, and only for the actions /etc/kervax/fix.conf on this node allows.
# Since 0.5 a filesystem that runs out of inodes (75% used) is broken down too: the
# directories with the most files, since df shows free gigabytes while nothing can be created.
# Since 0.6 the panel can ask to analyze a filesystem below 75% ("Analyze", or a forecast says
# it fills up in days): the fixer puts it on a watch list for a day, and the measurement treats
# it as filling up. Read only, so fix.conf does not gate it.
set -euo pipefail

KERVAX_SETUP_VERSION=0.6  # MAJOR.MINOR; compared component-wise
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

# Filesystems the panel asked to analyze below HOT_PCT (mount<TAB>until, written by the fixer):
# treated as filling up until the request expires. Expired lines are dropped here.
WATCH="$CACHE/watch"
WATCHED=""
if [ -s "$WATCH" ]; then
  WATCHED=$(awk -F'\t' -v now="$now" '$2 > now {print $1}' "$WATCH")
  awk -F'\t' -v now="$now" '$2 > now' "$WATCH" > "$WATCH.tmp" && mv -f "$WATCH.tmp" "$WATCH"
fi
is_watched() { [ -n "$WATCHED" ] && printf '%s\n' "$WATCHED" | grep -qxF -- "$1"; }

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

HOT=""     # filling up with data: findings and the biggest directories
ALL=""     # filling up with data or running out of inodes: what the block lists
while IFS="$TAB" read -r mnt size used avail; do
  [ -n "$mnt" ] || continue
  tot=$((used + avail))
  [ "$tot" -gt 0 ] || continue
  pct=$(( (used * 100 + tot - 1) / tot ))   # rounded up, as df does
  # inodes: total and used; btrfs and the like report 0 total - nothing to run out of
  iv=$(df -P -i -- "$mnt" 2>/dev/null | awk 'NR == 2 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ {print $2 " " $3}')
  itot=${iv%% *}; iused=${iv##* }; ipct=0
  if [ -n "$iv" ] && [ "${itot:-0}" -gt 0 ]; then
    ipct=$(( (iused * 100 + itot - 1) / itot ))
  else
    itot=0; iused=0
  fi
  line=$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' "$mnt" "$size" "$used" "$avail" "$pct" "$ipct" "$iused" "$itot")
  w=0; is_watched "$mnt" && w=1
  if [ "$pct" -ge "$HOT_PCT" ] || [ "$w" = 1 ]; then
    HOT="$HOT$line
"
  fi
  if [ "$pct" -ge "$HOT_PCT" ] || [ "$ipct" -ge "$HOT_PCT" ] || [ "$w" = 1 ]; then
    ALL="$ALL$line
"
  fi
done <<EOF_FS
$(fs_list)
EOF_FS

# Nothing is filling up - no block at all, the panel shows nothing.
if [ -z "$ALL" ]; then
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

# -- old files in /tmp and /var/tmp, by what lies right under them --
# Ages as systemd-tmpfiles uses by default (10 and 30 days). Grouped by the top-level entry:
# "/tmp/dev-1 9.4 GB" says more than thousands of old files, and a blanket "delete files
# older than N days" leaves half a directory behind (a copy of a restic repository was found
# exactly like that). Each entry says what it looks like; deleting is for a human, and only
# the whole entry, offered only when it holds no fresh files.
tmp_scan() {  # dir age_days item_id
  local dir=$1 cut=$((now - $2 * 86400)) id=$3 b cnt fr dumps archs top p kind fix
  [ -d "$dir" ] || return 0
  find "$dir" -xdev -mindepth 1 -type f -printf '%s\t%T@\t%C@\t%P\n' 2>/dev/null \
  | awk -F'\t' -v cut="$cut" '
      { split($4, p, "/"); top = p[1]
        if ($2 < cut && $3 < cut) { old[top] += $1; n[top]++ } else fresh[top] = 1
        f = tolower($4)
        if (f ~ /\.(sql|sql\.gz|sql\.zst|dump|bak)$/) dump[top]++
        else if (f ~ /\.(tar|tar\.gz|tgz|tar\.zst|zip|7z|gz|xz|bz2)$/) arch[top]++ }
      END { for (t in old) printf "%.0f\t%d\t%d\t%d\t%d\t%s\n", old[t], n[t], (t in fresh), dump[t] + 0, arch[t] + 0, t }' \
  | sort -rn | head -5 > "$CACHE/tmp-scan"
  while IFS="$TAB" read -r b cnt fr dumps archs top; do
    [ -n "$top" ] || continue
    p="$dir/$top"
    kind=other
    if [ -d "$p" ] && [ -f "$p/config" ] && [ -d "$p/data" ] && [ -d "$p/index" ] && [ -d "$p/snapshots" ]; then
      kind=restic
    elif [ "$dumps" -gt 0 ]; then
      kind=dump
    elif [ "$archs" -gt 0 ]; then
      kind=archive
    fi
    fix=""
    if [ "$fr" = 0 ]; then
      case "$p" in *\'*) ;; *) fix="rm -rf '$p'" ;; esac
    fi
    add_item "$id" "$b" "$b" "$p" manual "$fix" \
      ",\"count\":$cnt,\"kind\":\"$kind\",\"fresh\":$([ "$fr" = 1 ] && echo true || echo false)"
  done < "$CACHE/tmp-scan"
  rm -f "$CACHE/tmp-scan"
}
tmp_scan /tmp 10 tmp-dir
tmp_scan /var/tmp 30 vartmp-dir

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
while IFS="$TAB" read -r mnt size used avail pct ipct iused itot; do
  [ -n "$mnt" ] || continue
  key=$(printf '%s' "$mnt" | md5sum | cut -c1-12)
  f="$CACHE/top-$key"
  if ! is_hot "$mnt"; then
    printf 'none\t%s\t%s\n' "$now" "$iused" > "$f"   # only the inodes run out here
  elif ! fresh "$f" || [ "$(head -1 "$f" | cut -f1)" = none ]; then
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
  # Where the files piled up: directories by the number of inodes, the same depth 3. A
  # filesystem that runs out of inodes is walked whatever its size - that is the point.
  itop=""; ist=""; its=0
  if [ "$ipct" -ge "$HOT_PCT" ]; then
    g="$CACHE/itop-$key"
    if ! fresh "$g"; then
      imin=$((itot / 100)); [ "$imin" -ge 10000 ] || imin=10000
      timeout "$DU_TIMEOUT" du -x --inodes -d 3 -- "$mnt" > "$g.raw" 2>/dev/null
      rc=$?
      ist=ok; [ "$rc" -eq 124 ] && ist=partial
      [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && [ ! -s "$g.raw" ] && ist=failed
      { printf '%s\t%s\n' "$ist" "$now"
        awk -F'\t' -v min="$imin" -v root="$mnt" '$1 >= min && $2 != root' "$g.raw" \
          | sort -t "$(printf '\t')" -k1,1nr | head -40
      } > "$g.tmp"
      mv -f "$g.tmp" "$g"
      rm -f "$g.raw"
    fi
    ist=$(head -1 "$g" | cut -f1); its=$(head -1 "$g" | cut -f2)
    while IFS="$TAB" read -r n p; do
      [ -n "$p" ] || continue
      itop="$itop${itop:+,}{\"path\":\"$(esc "$p")\",\"files\":$n}"
    done <<EOF_ITOP
$(tail -n +2 "$g")
EOF_ITOP
  fi
  FSJ="$FSJ${FSJ:+,}{\"mount\":\"$(esc "$mnt")\",\"size\":$size,\"used\":$used,\"avail\":$avail,\"pct\":$pct,\"inodes\":${ino:-0},\"top_state\":\"$st\",\"top_ts\":${tts:-0},\"top\":[$top],\"ipct\":$ipct,\"iused\":$iused,\"itotal\":$itot,\"itop_state\":\"$ist\",\"itop_ts\":${its:-0},\"itop\":[$itop]}"
done <<EOF_HOT2
$ALL
EOF_HOT2

# Which "Free up" actions the panel may start here: only with the fixer installed, and only
# what /etc/kervax/fix.conf allows.
FIXJ=""
if [ -x /lib65/kervax/kervax-disk-fix ]; then
  allow=$(grep -E '^allow=' /etc/kervax/fix.conf 2>/dev/null | tail -1 | cut -d= -f2- | tr ',' ' ')
  for a in $allow; do
    case "$a" in *[!a-z-]*) continue ;; esac
    FIXJ="$FIXJ${FIXJ:+,}\"$a\""
  done
  FIXJ=",\"fix\":{\"v\":1,\"allow\":[$FIXJ]}"
fi

printf '{"v":1,"ts":%s,"fs":[%s],"items":[%s]%s}\n' "$now" "$FSJ" "$ITEMS" "$FIXJ" > "$TMP"
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

# -- "Free up" actions started from the panel --
# The agent (unprivileged kervax) drops a request into /var/lib/kervax/du-req, a root path
# unit runs the fixer, the answer goes to /var/lib/kervax/du-res: the spool scheme of
# timesync-setup. The fixer knows a fixed catalogue of actions and runs only the ones listed
# in /etc/kervax/fix.conf. That file belongs to the node: the panel cannot add anything.
AGENT_USER=kervax
REQ_DIR="$STATE_DIR/du-req"
RES_DIR="$STATE_DIR/du-res"
FIXER="$HELPER_DIR/kervax-disk-fix"
if getent group "$AGENT_USER" >/dev/null 2>&1; then
  install -d -o root -g "$AGENT_USER" -m 0730 "$REQ_DIR"
  install -d -o root -g "$AGENT_USER" -m 0770 "$RES_DIR"
  # the agent runs under ProtectSystem=strict: let it write the spool in /var/lib/kervax
  if systemctl cat kervax-agent >/dev/null 2>&1 \
     && [ ! -f /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf ]; then
    install -d -m 0755 /etc/systemd/system/kervax-agent.service.d
    printf '[Service]\nReadWritePaths=/var/lib/kervax\n' \
      > /etc/systemd/system/kervax-agent.service.d/kervax-spool.conf
    systemctl daemon-reload 2>/dev/null || true
    systemctl try-restart kervax-agent 2>/dev/null || true
  fi
  install -d -m 0755 /etc/kervax
  if [ ! -f /etc/kervax/fix.conf ]; then
    cat > /etc/kervax/fix.conf <<'CONF_EOF'
# Kervax: actions the panel may run on this node from "Where the disk space went".
# The fixer refuses anything not listed here, and the panel cannot change this file.
# Remove an action to switch its button off; "allow=" with nothing disables them all.
allow=journal rotated-logs apt-cache dnf-cache coredumps crash-reports docker-dangling docker-build-cache container-log
CONF_EOF
    chmod 0644 /etc/kervax/fix.conf
  fi

  cat > "$FIXER" <<'FIXER_EOF'
#!/usr/bin/env bash
# Kervax "Free up" actions (root), started from the panel through the spool. A fixed
# catalogue; an action runs only when /etc/kervax/fix.conf on this node allows it.
#   preview - what would be removed and how much space that frees; nothing is touched;
#   run     - remove it and report how much was actually freed.
# The answer is one line of JSON in the spool format of the other Kervax helpers.
set -u
export LC_ALL=C
REQ_DIR=/var/lib/kervax/du-req
RES_DIR=/var/lib/kervax/du-res
CONF=/etc/kervax/fix.conf
CACHE=/var/lib/kervax/disk-usage

have() { command -v "$1" >/dev/null 2>&1; }
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
size_of() { du -sxB1 -- "$@" 2>/dev/null | awk '{s += $1} END {printf "%.0f", s}'; }
allowed() {
  grep -E '^allow=' "$CONF" 2>/dev/null | tail -1 | cut -d= -f2- | tr ', ' '\n\n' | grep -qxF -- "$1"
}
# docker prints sizes in powers of 1000: "1.2GB", "512kB", "0B"
dbytes() {
  awk '{ n = $1 + 0; u = $1; sub(/^[0-9.]+/, "", u)
         if (u == "kB" || u == "KB") n *= 1e3; else if (u == "MB") n *= 1e6
         else if (u == "GB") n *= 1e9; else if (u == "TB") n *= 1e12
         s += n } END {printf "%.0f", s}'
}
# rotated logs older than a week: the same selection as in the measurement
rotated() {
  find /var/log -xdev -path /var/log/journal -prune -o -type f \( -name '*.gz' -o -name '*.xz' \
    -o -name '*.bz2' -o -name '*.zst' -o -name '*.old' -o -regex '.*\.[0-9][0-9]*' \) -mtime +7 "$@"
}

# Every action prints "<bytes> <count>" on the first line and sample lines after it.
act_journal() {
  local b a
  b=$(size_of /var/log/journal)
  if [ "$1" = run ]; then
    journalctl --vacuum-size=200M >/dev/null 2>&1 || { echo "journalctl --vacuum-size failed" >&2; return 1; }
    a=$(size_of /var/log/journal)
    echo "$((b - a)) 0"
  else
    a=$((b - 200 * 1024 * 1024)); [ "$a" -gt 0 ] || a=0
    echo "$a 0"
  fi
}
act_rotated() {
  local list
  list=$(rotated -printf '%s\t%p\n' 2>/dev/null | sort -rn)
  printf '%s\n' "$list" | awk -F'\t' 'NF {s += $1; n++} END {printf "%.0f %d\n", s, n}'
  printf '%s\n' "$list" | head -10 | cut -f2-
  if [ "$1" = run ]; then rotated -delete 2>/dev/null; fi
  return 0
}
act_pkgcache() {  # mode action
  local d=/var/cache/apt/archives tool="apt-get clean" b a n
  if [ "$2" = dnf-cache ]; then
    if have dnf; then d=/var/cache/dnf; tool="dnf clean all"; else d=/var/cache/yum; tool="yum clean all"; fi
  fi
  b=$(size_of "$d")
  n=$(find "$d" -xdev -type f \( -name '*.deb' -o -name '*.rpm' \) 2>/dev/null | wc -l)
  if [ "$1" = run ]; then
    $tool >/dev/null 2>&1 || { echo "$tool failed" >&2; return 1; }
    a=$(size_of "$d")
    echo "$((b - a)) $n"
  else
    echo "$b $n"
  fi
}
act_files_in() {  # mode dir: regular files right inside a directory (core dumps, crash reports)
  local list
  list=$(find "$2" -xdev -maxdepth 1 -type f -printf '%s\t%p\n' 2>/dev/null | sort -rn)
  printf '%s\n' "$list" | awk -F'\t' 'NF {s += $1; n++} END {printf "%.0f %d\n", s, n}'
  printf '%s\n' "$list" | head -10 | cut -f2-
  if [ "$1" = run ]; then find "$2" -xdev -maxdepth 1 -type f -delete 2>/dev/null; fi
  return 0
}
act_dangling() {
  local list b out
  list=$(timeout 60 docker images -f dangling=true --format '{{.ID}} {{.Size}} {{.CreatedSince}}' 2>/dev/null)
  b=$(printf '%s\n' "$list" | awk 'NF {print $2}' | dbytes)
  if [ "$1" = run ]; then
    out=$(timeout 900 docker image prune -f 2>&1) || { printf '%s\n' "$out" | tail -3 >&2; return 1; }
    b=$(printf '%s\n' "$out" | awk -F': ' 'tolower($0) ~ /reclaimed space/ {print $2}' | dbytes)
  fi
  echo "$b $(printf '%s\n' "$list" | grep -c .)"
  printf '%s\n' "$list" | head -10
}
act_buildcache() {
  local b out
  if [ "$1" = run ]; then
    out=$(timeout 900 docker builder prune -af 2>&1) || { printf '%s\n' "$out" | tail -3 >&2; return 1; }
    b=$(printf '%s\n' "$out" | awk 'tolower($1) ~ /^total/ {print $NF}' | dbytes)
  else
    b=$(timeout 120 docker system df --format '{{.Type}}|{{.Reclaimable}}' 2>/dev/null \
        | awk -F'|' '$1 == "Build Cache" {print $2}' | dbytes)
  fi
  echo "$b 0"
}
act_container_log() {  # mode container
  local root lp s
  root=$(timeout 20 docker info -f '{{.DockerRootDir}}' 2>/dev/null)
  [ -n "$root" ] || root=/var/lib/docker
  lp=$(timeout 20 docker inspect --format '{{.LogPath}}' "$2" 2>/dev/null)
  # only a json-file log under the docker root: the path comes from docker, not from the panel
  case "$lp" in
    "$root"/containers/*/*-json.log) ;;
    *) echo "container $2 has no json-file log" >&2; return 1 ;;
  esac
  [ -f "$lp" ] || { echo "the log file of $2 is missing" >&2; return 1; }
  s=$(stat -c %s -- "$lp")
  if [ "$1" = run ]; then truncate -s 0 -- "$lp" || { echo "truncate failed" >&2; return 1; }; fi
  echo "$s 1"
  echo "$lp"
}
# Analyze a filesystem below 75% for a day: put it on the watch list of the measurement. Only a
# local filesystem df knows about; deletes nothing, so fix.conf does not gate it.
act_analyze() {  # mount
  local m=$1 until
  df -P -l 2>/dev/null | awk 'NR > 1 {print $NF}' | grep -qxF -- "$m" \
    || { echo "no local filesystem is mounted at $m" >&2; return 1; }
  install -d -m 0700 "$CACHE"
  until=$(( $(date +%s) + 86400 ))
  { awk -F'\t' -v m="$m" '$1 != m' "$CACHE/watch" 2>/dev/null; printf '%s\t%s\n' "$m" "$until"; } > "$CACHE/watch.tmp"
  mv -f "$CACHE/watch.tmp" "$CACHE/watch"
  echo "0 0"
  echo "$m"
}

run_action() {  # action mode container mount
  case "$1" in
    journal) act_journal "$2" ;;
    rotated-logs) act_rotated "$2" ;;
    apt-cache|dnf-cache) act_pkgcache "$2" "$1" ;;
    coredumps) act_files_in "$2" /var/lib/systemd/coredump ;;
    crash-reports) act_files_in "$2" /var/crash ;;
    docker-dangling) act_dangling "$2" ;;
    docker-build-cache) act_buildcache "$2" ;;
    container-log) act_container_log "$2" "$3" ;;
    analyze) act_analyze "$4" ;;
    *) echo "unknown action" >&2; return 1 ;;
  esac
}

process_spool() {
  local req id line k v act mode cont mnt tmpd first b n sample ok out refresh=""
  for req in "$REQ_DIR"/*.req; do
    [ -f "$req" ] || continue
    id=$(basename "$req" .req)
    act=""; mode=""; cont=""; mnt=""
    while IFS= read -r line; do
      k=${line%%=*}; v=${line#*=}
      case "$k" in action) act=$v ;; mode) mode=$v ;; container) cont=$v ;; mount) mnt=$v ;; esac
    done < "$req"
    rm -f "$req"
    ok=false
    case "$mode" in preview|run) ;; *) act="" ;; esac
    if [ "$act" = container-log ]; then
      case "$cont" in [A-Za-z0-9]*) ;; *) act="" ;; esac
      case "$cont" in *[!A-Za-z0-9._-]*) act="" ;; esac
    fi
    if [ "$act" = analyze ]; then
      [ "$mode" = run ] || act=""
      case "$mnt" in /*) ;; *) act="" ;; esac
      case "$mnt" in *[!A-Za-z0-9/._-]*|*..*) act="" ;; esac
    fi
    if [ -z "$act" ]; then
      out="bad request"
    elif [ "$act" != analyze ] && ! allowed "$act"; then
      out="the action $act is not allowed on this node (/etc/kervax/fix.conf)"
    else
      tmpd=$(mktemp -d)
      if run_action "$act" "$mode" "$cont" "$mnt" > "$tmpd/out" 2> "$tmpd/err"; then
        first=$(head -1 "$tmpd/out"); b=${first%% *}; n=${first#* }
        case "$b" in ''|*[!0-9-]*) b=0 ;; esac
        case "$n" in ''|*[!0-9]*) n=0 ;; esac
        [ "$b" -ge 0 ] 2>/dev/null || b=0
        sample=""
        while IFS= read -r line; do
          [ -n "$line" ] && sample="$sample${sample:+,}\"$(esc "$line")\""
        done <<EOF_S
$(tail -n +2 "$tmpd/out" | head -10)
EOF_S
        out="{\"action\":\"$act\",\"mode\":\"$mode\",\"bytes\":$b,\"count\":$n,\"sample\":[$sample]}"
        ok=true
        if [ "$mode" = run ]; then refresh=1; fi
      else
        out=$(tr '\n' ' ' < "$tmpd/err" | cut -c1-300)
        [ -n "$out" ] || out="the action failed"
      fi
      rm -rf "$tmpd"
    fi
    printf 'ok=%s\noutput=%s\n' "$ok" "$out" > "$RES_DIR/$id.res.tmp"
    mv -f "$RES_DIR/$id.res.tmp" "$RES_DIR/$id.res"
    chmod 0644 "$RES_DIR/$id.res"
  done
  # after a run the panel should see the new numbers at once, not in ten minutes
  if [ -n "$refresh" ]; then
    rm -f "$CACHE/docker-df"
    systemctl start --no-block kervax-disk-usage.service >/dev/null 2>&1 || true
  fi
}

case "${1:-}" in
  process-spool) process_spool ;;
  preview|run) run_action "${2:-}" "$1" "${3:-}" "${3:-}" ;;
  *) echo "usage: $0 process-spool | preview|run <action> [container|mount]" >&2; exit 2 ;;
esac
FIXER_EOF
  chmod 0755 "$FIXER"
  chown root:root "$FIXER"

  cat > /etc/systemd/system/kervax-du-req.service <<UNIT_EOF
[Unit]
Description=Kervax: "Free up" requests from the panel
[Service]
Type=oneshot
ExecStart=$FIXER process-spool
TimeoutStartSec=20min
UNIT_EOF
  cat > /etc/systemd/system/kervax-du-req.path <<UNIT_EOF
[Unit]
Description=Kervax: watch the "Free up" request spool
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-du-req.service
[Install]
WantedBy=multi-user.target
UNIT_EOF
  systemctl daemon-reload
  systemctl enable --now kervax-du-req.path >/dev/null 2>&1 || true
else
  echo "· no Kervax agent group on this node: the \"Free up\" actions are not installed." >&2
fi

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/diskusage-setup.ver"
chmod 0644 "$STATE_DIR/versions/diskusage-setup.ver"
echo "✓ diskusage-setup: where the disk space went -> $OUT (every 10 minutes, while a disk fills up or the panel asked);"
echo "  \"Free up\" actions through $REQ_DIR, allowed by /etc/kervax/fix.conf; \"Analyze\" needs no allow."
