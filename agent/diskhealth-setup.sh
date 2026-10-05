#!/usr/bin/env bash
# Kervax: health of the physical disks. A root helper watches software RAID (/proc/mdstat),
# SMART of the disks (smartctl: NVMe, SATA, SAS), disks that stopped answering or vanished,
# and bursts of I/O errors in the kernel log, and writes /var/lib/kervax/report.d/disk-health.json
# every 2 minutes. The agent hands report.d to the panel as is, so no agent release is needed.
#
# Read only. On bare metal it installs smartmontools when missing - without the recommended
# packages, which would pull in a mail server. On a virtual machine there is no SMART, so only
# RAID and the kernel log are watched.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: without RAID or SMART it reports an empty block

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-disk-health"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/disk-health.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

virt=$(systemd-detect-virt 2>/dev/null || true)
[ -n "$virt" ] || virt=none
if [ "$virt" = none ] && ! command -v smartctl >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt_in() { timeout 300 apt-get -o DPkg::Lock::Timeout=60 install -y --no-install-recommends smartmontools >/dev/null 2>&1; }
    apt_in || { timeout 300 apt-get -o DPkg::Lock::Timeout=60 update >/dev/null 2>&1 && apt_in; } \
      || echo "· smartmontools could not be installed: SMART is not watched on this node." >&2
  elif command -v dnf >/dev/null 2>&1; then
    timeout 300 dnf install -y smartmontools >/dev/null 2>&1 \
      || echo "· smartmontools could not be installed: SMART is not watched on this node." >&2
  fi
fi

cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Health of the physical disks -> /var/lib/kervax/report.d/disk-health.json. Read only.
set -u
export LC_ALL=C
OUT=/var/lib/kervax/report.d/disk-health.json
STATE=/var/lib/kervax/disk-health        # SMART cache, error counter history, disks seen before
TMP="$OUT.tmp.$$"
SMART_MIN=30                             # SMART of a healthy disk is read twice an hour
GONE_DAYS=7                              # a disk seen within a week and missing now is reported

now=$(date +%s)
install -d -m 0700 "$STATE"

# JSON string body: control characters dropped, backslash and quote escaped. One raw control
# character makes the agent drop the whole file. Disk models come from firmware: anything.
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
num() { case "$1" in ''|*[!0-9]*) echo null ;; *) echo "$1" ;; esac; }
clean() { printf '%s' "$1" | tr -d '|\n'; }

virt=$(systemd-detect-virt 2>/dev/null || true)
[ -n "$virt" ] || virt=none
# In a container /proc/mdstat and the kernel log belong to the host: nothing of ours there
container=false
systemd-detect-virt -c -q 2>/dev/null && container=true
have_smart=false
command -v smartctl >/dev/null 2>&1 && have_smart=true

# -- software RAID: degraded, failed members, rebuild in progress, inactive arrays --
RAID=""
if ! $container && [ -r /proc/mdstat ]; then
  RAID=$(awk '
    function flush() {
      if (dev == "") return
      printf "%s{\"dev\":\"%s\",\"state\":\"%s\",\"level\":\"%s\",\"total\":%d,\"active\":%d,\"map\":\"%s\",\"members\":\"%s\",\"failed\":\"%s\",\"spare\":\"%s\",\"sync\":\"%s\"}", (n++ ? "," : ""), dev, st, lvl, tot, act, map, members, failed, spare, sync
      dev = ""
    }
    /^md[0-9A-Za-z_]+ *:/ {
      flush(); dev = $1; st = $3; lvl = ""; members = ""; failed = ""; spare = ""; tot = 0; act = 0; map = ""; sync = ""
      gsub(/[^A-Za-z0-9_-]/, "", st)
      for (i = 3; i <= NF; i++) {
        if ($i ~ /^(raid[0-9]+|linear|multipath)$/) lvl = $i
        else if ($i ~ /\[[0-9]+\]/) {
          m = $i; sub(/\[.*/, "", m); gsub(/[^A-Za-z0-9_.-]/, "", m)
          if ($i ~ /\(F\)/) failed = failed (failed != "" ? " " : "") m
          else if ($i ~ /\(S\)/) spare = spare (spare != "" ? " " : "") m
          else members = members (members != "" ? " " : "") m
        }
      }
      next
    }
    dev != "" && /blocks/ {
      if (match($0, /\[[0-9]+\/[0-9]+\]/)) { s = substr($0, RSTART + 1, RLENGTH - 2); split(s, a, "/"); tot = a[1]; act = a[2] }
      if (match($0, /\[[U_]+\]/)) map = substr($0, RSTART, RLENGTH)
      next
    }
    dev != "" && /(recovery|resync|reshape|check) *=/ {
      if (match($0, /(recovery|resync|reshape|check) *= *[0-9.]+%/)) { sync = substr($0, RSTART, RLENGTH); gsub(/ /, "", sync) }
      next
    }
    END { flush() }' /proc/mdstat)
fi

# -- physical disks and their SMART (bare metal only: a virtual disk has no SMART) --
DISKS=""
SEEN_NOW=""     # serial|model|ts|dev of the healthy disks seen now
PRESENT=""      # serials of every disk present now, the dead ones too
SKIPDEV=""      # disks that are not local (USB, iSCSI, SAN): their I/O errors are not ours

add_disk() {    # dev model serial type size rota ok err [smart fields as k=v...]
  local dev=$1 model=$2 serial=$3 tran=$4 size=$5 rota=$6 ok=$7 err=$8
  shift 8
  local health="" failing=0 temp="" wear="" spare="" sparethr="" media="" crit="" poh=""
  local realloc="" pending="" uncorr="" crc="" grow="" kv
  for kv in "$@"; do
    case "$kv" in
      health=*) health=${kv#*=} ;; failing=*) failing=${kv#*=} ;; temp=*) temp=${kv#*=} ;;
      wear=*) wear=${kv#*=} ;; spare=*) spare=${kv#*=} ;; sparethr=*) sparethr=${kv#*=} ;;
      media=*) media=${kv#*=} ;; crit=*) crit=${kv#*=} ;; poh=*) poh=${kv#*=} ;;
      realloc=*) realloc=${kv#*=} ;; pending=*) pending=${kv#*=} ;; uncorr=*) uncorr=${kv#*=} ;;
      crc=*) crc=${kv#*=} ;; grow=*) grow=${kv#*=} ;;
    esac
  done
  DISKS="$DISKS${DISKS:+,}{\"dev\":\"$(esc "$dev")\",\"model\":\"$(esc "$model")\",\"serial\":\"$(esc "$serial")\",\"type\":\"$(esc "$tran")\",\"size\":$(num "$size"),\"rota\":$(num "$rota"),\"ok\":$ok,\"err\":\"$(esc "$err")\",\"health\":\"$(esc "$health")\",\"failing\":$( [ "$failing" = 1 ] && echo true || echo false),\"temp\":$(num "$temp"),\"wear\":$(num "$wear"),\"spare\":$(num "$spare"),\"spare_thr\":$(num "$sparethr"),\"media\":$(num "$media"),\"crit\":\"$(esc "$crit")\",\"poh\":$(num "$poh"),\"realloc\":$(num "$realloc"),\"pending\":$(num "$pending"),\"uncorr\":$(num "$uncorr"),\"crc\":$(num "$crc"),\"grow\":{$grow}}"
}

if [ "$virt" = none ]; then
  # lsblk -P (KEY="value") turned into "|"-separated lines: with spaces or tabs read would
  # collapse an empty column (no TRAN, no model) and every field after it would shift
  while IFS='|' read -r name type size tran rota rm serial model; do
    [ "$type" = disk ] || continue
    case "$name" in loop*|ram*|zram*|sr*|fd*|rbd*|nbd*|md*|dm-*|drbd*) continue ;; esac
    case "$name" in ''|*[!A-Za-z0-9_-]*) continue ;; esac
    model=$(printf '%b' "$model"); serial=$(printf '%b' "$serial")
    # Removable media, USB, iSCSI and SAN LUNs are not the disks of this server; the virtual
    # media of a BMC ("Virtual Floppy", size 0) would look like a dead disk
    if [ "$rm" = 1 ]; then SKIPDEV="$SKIPDEV|$name"; continue; fi
    case "$tran" in usb|iscsi|fc|fcoe|srp) SKIPDEV="$SKIPDEV|$name"; continue ;; esac
    [ -n "$tran" ] || case "$name" in nvme*) tran=nvme ;; esac
    [ -n "$serial" ] && PRESENT="$PRESENT$(clean "$serial")
"
    ok=true; err=""; refreshed=0
    set --
    # "live" for an NVMe controller, "running" for a SCSI disk; "offline", "dead" and the
    # like mean the kernel gave up on the device
    dst=$(cat "/sys/block/$name/device/state" 2>/dev/null || true)
    if [ "${size:-0}" = 0 ]; then
      ok=false; err=dead
    elif [ -n "$dst" ] && [ "$dst" != live ] && [ "$dst" != running ]; then
      ok=false; err="state_$(printf '%s' "$dst" | tr -cd 'A-Za-z0-9_-')"
    elif $have_smart; then
      cache="$STATE/smart-$name"
      # A healthy disk is read twice an hour; a sick one - every run. -n standby keeps a
      # sleeping hard disk asleep (then the previous reading stays).
      if [ ! -s "$cache" ] || [ -n "$(find "$cache" -mmin +"$SMART_MIN" 2>/dev/null)" ] \
         || grep -q '^ok=false' "$cache" 2>/dev/null || [ "$(sed -n 's/^serial=//p' "$cache" 2>/dev/null)" != "$serial" ]; then
        out=$(timeout 40 smartctl -H -A -n standby "/dev/$name" 2>&1); rc=$?
        if printf '%s' "$out" | grep -E -q -i 'device is in [a-z_ ]+ mode'; then
          :   # asleep: keep the previous reading
        else
          {
            echo "serial=$serial"
            # bit 1 of the exit code: the device could not be opened. Behind a hardware RAID
            # controller or an unknown bridge that only means "no SMART here", not a dead disk
            if [ $((rc & 2)) -ne 0 ]; then
              if printf '%s' "$out" | grep -E -q -i 'please (try adding|specify)|unknown usb bridge|megaraid|cciss|aacraid|areca|3ware'; then
                echo "ok=true"; echo "err=nosmart"
              else
                echo "ok=false"; echo "err=no_answer"
              fi
            else
              echo "ok=true"
            fi
            # bit 3: the disk itself reports it is failing
            [ $((rc & 8)) -ne 0 ] && echo "failing=1"
            printf '%s\n' "$out" | awk '
              /self-assessment test result:/ { sub(/.*: */, ""); print "health=" $0 }
              /^SMART Health Status:/ { sub(/.*: */, ""); print "health=" $0 }
              /^Critical Warning:/ { print "crit=" $NF }
              /^Temperature:/ { print "temp=" $2 }
              /^Current Drive Temperature:/ { print "temp=" $4 }
              /^Available Spare:/ { v = $NF; sub(/%/, "", v); print "spare=" v }
              /^Available Spare Threshold:/ { v = $NF; sub(/%/, "", v); print "sparethr=" v }
              /^Percentage Used:/ { v = $NF; sub(/%/, "", v); print "wear=" v }
              /^Media and Data Integrity Errors:/ { v = $NF; gsub(/,/, "", v); print "media=" v }
              /^Power On Hours:/ { v = $NF; gsub(/,/, "", v); print "poh=" v }
              /^Elements in grown defect list:/ { print "realloc=" $NF }
              /^Accumulated power on time, hours:minutes/ { v = $NF; sub(/:.*/, "", v); print "poh=" v }
              $1 ~ /^[0-9]+$/ && NF >= 10 {
                raw = $10
                if ($1 == 5) print "realloc=" raw
                else if ($1 == 197) print "pending=" raw
                else if ($1 == 198) print "uncorr=" raw
                else if ($1 == 199) print "crc=" raw
                else if ($1 == 9) print "poh=" raw
                else if ($1 == 194 || $1 == 190) print "temp=" raw
                # wear of a SATA SSD: the normalized value of its "life left" attribute
                if ($2 ~ /^(Wear_Leveling_Count|SSD_Life_Left|Media_Wearout_Indicator|Percent_Lifetime_Remain|Percent_Life_Remaining|Remaining_Lifetime_Perc)$/ && $4 ~ /^[0-9]+$/ && $4 + 0 <= 100) {
                  w = 100 - $4; if (w > wmax) wmax = w; haswear = 1
                }
              }
              END { if (haswear) print "wear=" wmax }'
          } > "$cache.tmp"
          mv -f "$cache.tmp" "$cache"
          refreshed=1
        fi
      fi
      if [ -s "$cache" ]; then
        while IFS='=' read -r k v; do
          case "$k" in
            ok) [ "$v" = false ] && ok=false ;;
            err) err=$v ;;
            serial) ;;
            *) set -- "$@" "$k=$v" ;;
          esac
        done < "$cache"
      fi
      # Error counters that grew within a day: a static count left from years ago is not news,
      # a growing one is the disk dying now. History is kept for two days.
      hist="$STATE/hist-$name"
      if [ "$refreshed" = 1 ] && [ "$ok" = true ] && [ "$err" != nosmart ]; then
        cv() { sed -n "s/^$1=//p" "$cache" | tail -1; }
        printf '%s|%s|%s|%s|%s|%s|%s\n' "$now" "$(clean "$serial")" "$(cv realloc)" "$(cv pending)" "$(cv uncorr)" "$(cv media)" "$(cv crc)" >> "$hist"
        awk -F'|' -v old=$((now - 2 * 86400)) '$1 >= old' "$hist" > "$hist.tmp" && mv -f "$hist.tmp" "$hist"
      fi
      if [ -s "$hist" ]; then
        grow=$(awk -F'|' -v since=$((now - 86400)) -v ser="$(clean "$serial")" '
          $2 == ser && $1 >= since {
            for (i = 3; i <= 7; i++) if ($i ~ /^[0-9]+$/) { if (!(i in first)) first[i] = $i + 0; last[i] = $i + 0 }
          }
          END {
            split("realloc pending uncorr media crc", nm, " ")
            for (i = 3; i <= 7; i++) if ((i in first) && last[i] > first[i]) printf "%s\"%s\":%.0f", (n++ ? "," : ""), nm[i - 2], last[i] - first[i]
          }' "$hist")
        set -- "$@" "grow=$grow"
      fi
    fi
    if [ "$ok" = true ] && [ -n "$serial" ]; then
      SEEN_NOW="$SEEN_NOW$(clean "$serial")|$(clean "$model")|$now|$name
"
    fi
    add_disk "$name" "$model" "$serial" "$tran" "$size" "$rota" "$ok" "$err" "$@"
  done <<EOF_LSBLK
$(lsblk -d -n -b -P -o NAME,TYPE,SIZE,TRAN,ROTA,RM,SERIAL,MODEL 2>/dev/null | awk '
    function val(k,   r) {
      if (match($0, "(^| )" k "=\"[^\"]*\"")) {
        r = substr($0, RSTART, RLENGTH); sub(/^ /, "", r); r = substr(r, length(k) + 3); sub(/"$/, "", r)
        gsub(/\|/, "/", r); return r
      }
      return ""
    }
    { printf "%s|%s|%s|%s|%s|%s|%s|%s\n", val("NAME"), val("TYPE"), val("SIZE"), val("TRAN"), val("ROTA"), val("RM"), val("SERIAL"), val("MODEL") }')
EOF_LSBLK

  # An NVMe controller whose namespace never came up has no block device at all: lsblk does not
  # list it, the kernel just keeps failing to identify it
  for c in /sys/class/nvme/nvme[0-9]*; do
    [ -d "$c" ] || continue
    ls -d "$c"/nvme*n[0-9]* >/dev/null 2>&1 && continue
    cn=${c##*/}
    st=$(cat "$c/state" 2>/dev/null || true)
    serial=$(sed 's/ *$//' "$c/serial" 2>/dev/null || true)
    model=$(sed 's/ *$//' "$c/model" 2>/dev/null || true)
    [ -n "$serial" ] && PRESENT="$PRESENT$(clean "$serial")
"
    add_disk "$cn" "$model" "$serial" nvme "" "" false "no_namespace${st:+_}$(printf '%s' "$st" | tr -cd 'A-Za-z0-9_-')"
  done
fi

# -- disks that vanished: seen within a week, not present now --
MISSING=""
if [ "$virt" = none ]; then
  touch "$STATE/seen"
  # A disk that showed up for the first time replaces one of the vanished: after a disk swap
  # the old serial must not hang as "vanished" for a week
  nnew=0
  while IFS='|' read -r serial rest; do
    [ -n "$serial" ] || continue
    cut -d'|' -f1 "$STATE/seen" | grep -qxF -- "$serial" || nnew=$((nnew + 1))
  done <<EOF_NEW
$SEEN_NOW
EOF_NEW
  forget=""
  while IFS='|' read -r serial model last name; do
    [ -n "$serial" ] || continue
    case "$last" in ''|*[!0-9]*) continue ;; esac
    printf '%s' "$PRESENT" | grep -qxF -- "$serial" && continue
    [ $((now - last)) -le $((GONE_DAYS * 86400)) ] || continue
    if [ "$nnew" -gt 0 ]; then
      nnew=$((nnew - 1)); forget="$forget$serial
"
      continue
    fi
    MISSING="$MISSING${MISSING:+,}{\"serial\":\"$(esc "$serial")\",\"model\":\"$(esc "$model")\",\"dev\":\"$(esc "$name")\",\"last\":$last}"
  done < "$STATE/seen"
  # remember: fresh sightings replace old ones, replaced disks and those gone for more than a
  # week are forgotten
  { printf '%s' "$SEEN_NOW"
    awk -F'|' -v now="$now" -v keep=$((GONE_DAYS * 86400)) '$3 ~ /^[0-9]+$/ && now - $3 <= keep' "$STATE/seen" \
      | while IFS='|' read -r serial rest; do
          printf '%s' "$SEEN_NOW" | cut -d'|' -f1 | grep -qxF -- "$serial" && continue
          printf '%s' "$forget" | grep -qxF -- "$serial" && continue
          printf '%s|%s\n' "$serial" "$rest"
        done
  } > "$STATE/seen.tmp"
  mv -f "$STATE/seen.tmp" "$STATE/seen"
fi

# -- I/O errors in the kernel log over the last 10 minutes --
IOC=0
IOL=""
if ! $container && command -v journalctl >/dev/null 2>&1; then
  journalctl -k --since "-10 min" -q -o cat 2>/dev/null \
    | grep -E -i 'I/O error|blk_update_request|critical medium error|medium error|nvme[0-9]+.*(timeout|failed|reset|abort)|ata[0-9.]+:.*(failed command|exception emask|hard resetting|link is slow)|EXT4-fs error|XFS .*(error|corrupt)|BTRFS (error|critical)|BTRFS warning.*csum failed|md/raid.*(failure|faulty)|SError' \
    | grep -E -v -i 'Shutdown timeout set|APST|\b(sr|fd|loop|nbd|rbd|zram)[0-9]+' \
    > "$STATE/io.tmp"
  if [ -n "$SKIPDEV" ]; then
    grep -E -v "\\b(${SKIPDEV#|})(p?[0-9]+)?\\b" "$STATE/io.tmp" > "$STATE/io.tmp2"
    mv -f "$STATE/io.tmp2" "$STATE/io.tmp"
  fi
  IOC=$(grep -c . "$STATE/io.tmp")
  while IFS= read -r line; do
    [ -n "$line" ] && IOL="$IOL${IOL:+,}\"$(esc "$(printf '%s' "$line" | cut -c1-200)")\""
  done <<EOF_IO
$(awk '!seen[$0]++' "$STATE/io.tmp" | tail -3)
EOF_IO
  rm -f "$STATE/io.tmp"
fi

printf '{"v":1,"ts":%s,"virt":"%s","container":%s,"smart":%s,"raid":[%s],"disks":[%s],"missing":[%s],"io":{"count":%s,"last":[%s]}}\n' \
  "$now" "$(esc "$virt")" "$container" "$have_smart" "$RAID" "$DISKS" "$MISSING" "$IOC" "$IOL" > "$TMP"
mv -f "$TMP" "$OUT"
chmod 0644 "$OUT"
HELPER_EOF
chmod 0755 "$HELPER"

cat > /etc/systemd/system/kervax-disk-health.service <<EOF
[Unit]
Description=Kervax: health of the physical disks (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
TimeoutStartSec=5min
EOF
cat > /etc/systemd/system/kervax-disk-health.timer <<'EOF'
[Unit]
Description=Kervax: check the health of the physical disks periodically
[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-disk-health.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-disk-health.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/diskhealth-setup.ver"
chmod 0644 "$STATE_DIR/versions/diskhealth-setup.ver"
echo "✓ diskhealth-setup: RAID, SMART and kernel I/O errors -> $OUT (every 2 minutes)."
