#!/usr/bin/env bash
# Kervax: failed systemd units. A root helper lists the units in the "failed" state every
# minute - why they failed and the last lines of their log - and writes
# /var/lib/kervax/report.d/units.json. The agent hands report.d to the panel as is.
#
# The panel can restart a failed unit or clear its failed mark through a spool, the same
# scheme as the other helpers. Only a unit that is failed right now: the panel cannot touch
# a working service.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: it only reads systemctl and the journal

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-units"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/units.json"
AGENT_USER=kervax
REQ_DIR="$STATE_DIR/units-req"
RES_DIR="$STATE_DIR/units-res"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Failed systemd units -> /var/lib/kervax/report.d/units.json, and the panel's restart/reset
# requests from the spool. Reads systemctl and the journal; changes a unit only on request
# and only while that unit is failed.
set -u
export LC_ALL=C
OUT=/var/lib/kervax/report.d/units.json
REQ_DIR=/var/lib/kervax/units-req
RES_DIR=/var/lib/kervax/units-res
MAX_UNITS=30

# JSON string body: control characters dropped, backslash and quote escaped. One raw control
# character makes the agent drop the whole file.
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
num() { case "$1" in ''|*[!0-9]*) echo null ;; *) echo "$1" ;; esac; }

# A unit name systemd accepts (escaped names carry \x2d and the like) of a type worth
# watching. Transient units of systemd-run come and go by themselves and are left out.
unit_ok() {
  case "$1" in ''|-*|*[!A-Za-z0-9@._:\\-]*) return 1 ;; esac
  case "$1" in *.service|*.socket|*.timer|*.mount|*.automount|*.swap|*.path) ;; *) return 1 ;; esac
  case "$1" in run-u[0-9]*|run-r[0-9a-f]*|run-p[0-9]*) return 1 ;; esac
  return 0
}

# The last lines of the unit's log that say something: systemd's own "Main process exited",
# "Failed with result" and the like repeat what the panel already knows.
unit_log() {
  local raw
  raw=$(journalctl -u "$1" -n 40 --no-pager -o cat 2>/dev/null | tail -40)
  printf '%s\n' "$raw" \
    | grep -v -E '^[^ ]+\.(service|socket|timer|mount|automount|swap|path): (Main process exited|Failed with result|Consumed |Scheduled restart job|Deactivated successfully|Start request repeated|Control process exited|Killing process|Found left-over process|Unit process|Succeeded|Unit entered failed state|Triggering OnFailure)|^(Started|Stopped|Starting|Stopping|Failed to start|Finished|Dependency failed) ' \
    | grep -v '^$' | uniq | tail -5 > "$2"
  # nothing but systemd's lines - show those then, better than nothing
  [ -s "$2" ] || printf '%s\n' "$raw" | grep -v '^$' | uniq | tail -5 > "$2"
}

json_lines() {
  local out="" line
  while IFS= read -r line; do
    [ -n "$line" ] && out="$out${out:+,}\"$(esc "$(printf '%s' "$line" | cut -c1-200)")\""
  done < "$1"
  printf '%s' "$out"
}

scan() {
  local now up units="" n=0 unit props desc type result status mono restarts since tmp
  now=$(date +%s)
  up=$(awk '{printf "%.0f", $1 * 1000000}' /proc/uptime)
  tmp=$(mktemp)
  while read -r unit; do
    unit_ok "$unit" || continue
    [ "$n" -lt "$MAX_UNITS" ] || break
    n=$((n + 1))
    props=$(systemctl show "$unit" --no-pager -p Description -p Type -p Result -p ExecMainStatus \
      -p StateChangeTimestampMonotonic -p NRestarts 2>/dev/null)
    get() { printf '%s\n' "$props" | sed -n "s/^$1=//p" | head -1; }
    desc=$(get Description); type=$(get Type); result=$(get Result); status=$(get ExecMainStatus)
    mono=$(get StateChangeTimestampMonotonic); restarts=$(get NRestarts)
    # when it failed: the monotonic stamp is the same on every systemd, the wall-clock one
    # is printed in the local format and time zone
    since=0
    case "$mono" in ''|0|*[!0-9]*) ;; *) since=$(( now - (up - mono) / 1000000 )) ;; esac
    unit_log "$unit" "$tmp"
    units="$units${units:+,}{\"unit\":\"$(esc "$unit")\",\"desc\":\"$(esc "$desc")\",\"type\":\"$(esc "$type")\",\"result\":\"$(esc "$result")\",\"status\":$(num "$status"),\"since\":$since,\"restarts\":$(num "$restarts"),\"log\":[$(json_lines "$tmp")]}"
  done <<EOF_U
$(systemctl list-units --state=failed --all --no-legend --plain --no-pager 2>/dev/null | awk '{print $1}')
EOF_U
  rm -f "$tmp"
  local fix=false
  [ -d "$REQ_DIR" ] && fix=true
  printf '{"v":1,"ts":%s,"units":[%s],"fix":%s}\n' "$now" "$units" "$fix" > "$OUT.tmp.$$"
  mv -f "$OUT.tmp.$$" "$OUT"
  chmod 0644 "$OUT"
}

process_spool() {
  local req id line k v op unit ok out rc state result tmp
  for req in "$REQ_DIR"/*.req; do
    [ -f "$req" ] || continue
    id=$(basename "$req" .req)
    op=""; unit=""
    while IFS= read -r line; do
      k=${line%%=*}; v=${line#*=}
      case "$k" in op) op=$v ;; unit) unit=$v ;; esac
    done < "$req"
    rm -f "$req"
    ok=false
    if ! unit_ok "$unit" || { [ "$op" != restart ] && [ "$op" != reset ]; }; then
      out="bad request"
    elif ! systemctl is-failed --quiet -- "$unit"; then
      out="$(esc "$unit") is not failed now: the panel restarts and resets only failed units"
    else
      tmp=$(mktemp)
      if [ "$op" = restart ]; then
        # a oneshot unit runs to the end inside restart: certbot and the like take minutes
        timeout 300 systemctl restart -- "$unit" > "$tmp" 2>&1; rc=$?
      else
        systemctl reset-failed -- "$unit" > "$tmp" 2>&1; rc=$?
      fi
      state=$(systemctl is-active -- "$unit" 2>/dev/null)
      result=$(systemctl show -p Result --value -- "$unit" 2>/dev/null)
      [ "$rc" -eq 0 ] && [ "$state" != failed ] && ok=true
      unit_log "$unit" "$tmp"
      out="{\"op\":\"$op\",\"unit\":\"$(esc "$unit")\",\"state\":\"$(esc "$state")\",\"result\":\"$(esc "$result")\",\"rc\":$rc,\"log\":[$(json_lines "$tmp")]}"
      rm -f "$tmp"
    fi
    printf 'ok=%s\noutput=%s\n' "$ok" "$out" > "$RES_DIR/$id.res.tmp"
    mv -f "$RES_DIR/$id.res.tmp" "$RES_DIR/$id.res"
    chmod 0644 "$RES_DIR/$id.res"
  done
  # the panel should see the new state at once, not in a minute
  scan
}

case "${1:-scan}" in
  scan) scan ;;
  process-spool) process_spool ;;
  *) echo "usage: $0 [scan|process-spool]" >&2; exit 2 ;;
esac
HELPER_EOF
chmod 0755 "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-units.service <<EOF
[Unit]
Description=Kervax: failed systemd units (read only)
[Service]
Type=oneshot
ExecStart=$HELPER scan
Nice=10
TimeoutStartSec=2min
EOF
cat > /etc/systemd/system/kervax-units.timer <<'EOF'
[Unit]
Description=Kervax: check for failed systemd units every minute
[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
[Install]
WantedBy=timers.target
EOF

# -- restart/reset requests from the panel through the spool --
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
  cat > /etc/systemd/system/kervax-units-req.service <<EOF
[Unit]
Description=Kervax: restart/reset requests for failed units from the panel
[Service]
Type=oneshot
ExecStart=$HELPER process-spool
TimeoutStartSec=10min
EOF
  cat > /etc/systemd/system/kervax-units-req.path <<EOF
[Unit]
Description=Kervax: watch the failed units request spool
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-units-req.service
[Install]
WantedBy=multi-user.target
EOF
else
  echo "· no Kervax agent group on this node: restart from the panel is not installed." >&2
fi

systemctl daemon-reload
systemctl enable --now kervax-units.timer >/dev/null 2>&1 || true
[ -d "$REQ_DIR" ] && systemctl enable --now kervax-units-req.path >/dev/null 2>&1 || true
systemctl start --no-block kervax-units.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/units-setup.ver"
chmod 0644 "$STATE_DIR/versions/units-setup.ver"
echo "✓ units-setup: failed systemd units -> $OUT (every minute); restart/reset through $REQ_DIR."
