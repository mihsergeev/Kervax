#!/usr/bin/env bash
# Kervax: enable one-click clock synchronisation from the panel.
#
# Running as the unprivileged `kervax` with NoNewPrivileges, the agent cannot step the
# clock — that requires root. So the agent drops a request into the spool
# /var/lib/kervax/tsync-req, a root path unit runs a narrow helper (NTP sync with an HTTP
# fallback) and writes the answer to /var/lib/kervax/tsync-res. The agent stays isolated.
# There is exactly one action (sync), and the panel URL used for the fallback is
# validated. Run as root on the node. Without this helper the panel only shows a
# copy-paste command.
set -euo pipefail

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-timesync-helper"
STATE_DIR=/var/lib/kervax
REQ_DIR="$STATE_DIR/tsync-req"
RES_DIR="$STATE_DIR/tsync-res"
AGENT_USER=kervax

KERVAX_SETUP_VERSION=0.2  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node, so it is installed on the first pass too
if ! getent group "$AGENT_USER" >/dev/null 2>&1; then
  echo "No Kervax agent on this node (group '$AGENT_USER' is missing). Add the node in the panel first." >&2
  exit 2
fi
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" /var/lib/kervax/versions
echo "$KERVAX_SETUP_VERSION" > /var/lib/kervax/versions/timesync-setup.ver
chmod 0644 /var/lib/kervax/versions/timesync-setup.ver
# spool: the agent (kervax) drops requests (-wx) and reads/removes answers (r-x plus write)
install -d -o root -g "$AGENT_USER" -m 0730 "$REQ_DIR"
install -d -o root -g "$AGENT_USER" -m 0770 "$RES_DIR"
# the agent runs under ProtectSystem=strict — allow it to write the spool in /var/lib/kervax
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
# Kervax timesync helper (root): start or force clock synchronisation; when outbound NTP
# is blocked, fall back roughly to the panel's clock over HTTP. One action only: sync.
set -euo pipefail
REQ_DIR=/var/lib/kervax/tsync-req
RES_DIR=/var/lib/kervax/tsync-res

# the active time daemon ('' = none is running)
_active_timed() {
  local s
  for s in systemd-timesyncd chronyd chrony ntpd ntpsec openntpd; do
    [ "$(systemctl is-active "$s" 2>/dev/null)" = active ] && { echo "$s"; return; }
  done
}

cmd_sync() {
  local panel="$1" svc method note synced=no i httpdate now
  # the fallback panel URL is accepted only as https:// (it comes from the agent config)
  case "$panel" in https://*) ;; *) panel="" ;; esac
  # 1) is a daemon present? if not, start systemd-timesyncd (usually preinstalled)
  svc="$(_active_timed || true)"
  if [ -z "$svc" ]; then
    timedatectl set-ntp true >/dev/null 2>&1 || true
    systemctl start systemd-timesyncd >/dev/null 2>&1 || true
    svc="$(_active_timed || true)"
  fi
  # 2) force a sync (step the clock at once instead of a slow slew)
  case "$svc" in
    chrony*)          chronyc -a makestep >/dev/null 2>&1 || chronyc makestep >/dev/null 2>&1 || true; method="chrony makestep" ;;
    systemd-timesyncd*) timedatectl set-ntp true >/dev/null 2>&1 || true; systemctl restart systemd-timesyncd >/dev/null 2>&1 || true; method="timesyncd" ;;
    *)                method="" ;;
  esac
  # 3) confirm the sync USING THE DAEMON'S OWN CHECK. On chrony nodes timedatectl often
  #    reports NTPSynchronized=no while chrony is already in sync, so ask chrony itself
  #    (waitsync: up to 15 attempts, 1s apart, until the correction is below 0.5s; rc 0 = ok).
  case "$svc" in
    chrony*) chronyc waitsync 15 0.5 0 1 >/dev/null 2>&1 && synced=yes ;;
  esac
  if [ "$synced" != yes ]; then
    for i in $(seq 1 20); do
      [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ] && { synced=yes; break; }
      sleep 1
    done
  fi
  now="$(date '+%F %T %Z' 2>/dev/null || date)"
  if [ "$synced" = yes ]; then
    echo "OK clock synchronised (${method:-ntp}) - NTPSynchronized=yes, now $now"
    return 0
  fi
  # 4) NTP unreachable (outbound UDP 123 blocked?) -> HTTP fallback to the panel's clock
  if [ -n "$panel" ]; then
    httpdate="$(curl -sI --max-time 10 "$panel/" 2>/dev/null | grep -i '^date:' | head -1 | cut -d' ' -f2- | tr -d '\r')"
    if [ -n "$httpdate" ] && date -s "$httpdate" >/dev/null 2>&1; then
      echo "OK NTP unreachable - the clock was set from the panel (coarse, to the second): $(date '+%F %T %Z'). For accurate sync open outbound UDP 123 (e.g. ufw allow out 123/udp)"
      return 0
    fi
  fi
  echo "could not synchronise: daemon='${svc:-none}', NTPSynchronized=no. Outbound NTP (UDP 123) appears blocked and the HTTP fallback did not work - open the port or check access to the panel" >&2
  return 2
}

cmd_process_spool() {
  local req id panel line k v out ok
  for req in "$REQ_DIR"/*.req; do
    [ -f "$req" ] || continue
    id="$(basename "$req" .req)"
    panel=""
    while IFS= read -r line; do
      k="${line%%=*}"; v="${line#*=}"
      case "$k" in panel_url) panel="$v" ;; esac
    done < "$req"
    rm -f "$req"
    out=""; ok=false
    if out="$(cmd_sync "$panel" 2>&1)"; then ok=true; fi
    printf 'ok=%s\noutput=%s\n' "$ok" "$(printf '%s' "$out" | tr '\n' ' ')" > "$RES_DIR/$id.res.tmp"
    mv -f "$RES_DIR/$id.res.tmp" "$RES_DIR/$id.res"; chmod 0644 "$RES_DIR/$id.res"
  done
}

case "${1:-}" in
  sync)          shift; cmd_sync "${1:-}" ;;
  process-spool) cmd_process_spool ;;
  *) echo "usage: $0 {sync [https://panel]|process-spool}" >&2; exit 2 ;;
esac
HELPER_EOF
chmod 0755 "$HELPER"; chown root:root "$HELPER"

# path unit: as soon as the agent drops a request, root executes it immediately
cat > /etc/systemd/system/kervax-tsync-req.service <<UNIT_EOF
[Unit]
Description=Kervax timesync request processor
[Service]
Type=oneshot
ExecStart=$HELPER process-spool
UNIT_EOF
cat > /etc/systemd/system/kervax-tsync-req.path <<UNIT_EOF
[Unit]
Description=Kervax timesync request spool watch
[Path]
DirectoryNotEmpty=$REQ_DIR
Unit=kervax-tsync-req.service
[Install]
WantedBy=multi-user.target
UNIT_EOF
systemctl daemon-reload
systemctl enable --now kervax-tsync-req.path >/dev/null 2>&1 || true
"$HELPER" process-spool >/dev/null 2>&1 || true

echo "timesync-setup: done -> $HELPER; one-click sync through the spool $REQ_DIR."
