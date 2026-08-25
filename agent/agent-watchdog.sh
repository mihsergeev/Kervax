#!/usr/bin/env bash
# Kervax: enable the systemd watchdog for the agent (self-recovery when it hangs).
#
# WHY. The agent's report loop can hang (network or collection), and the in-process Go
# watchdog once failed to fire (incident: 23 minutes "offline"). The systemd watchdog is
# an independent backstop: agent 1.77+ sends WATCHDOG=1 every cycle, and if it goes quiet
# for longer than WatchdogSec systemd restarts it. All this script does is switch the UNIT
# to Type=notify (an OTA binary update does not touch the unit, hence a separate step).
# Installed LOCALLY by the ansible playbook, not from the panel.
#
# SAFE ON ANY NODE (KERVAX_SETUP_ALWAYS): if the agent is older than 1.77 and never sends
# READY=1, the start under Type=notify fails, the script notices and ROLLS BACK the drop-in
# (no marker is written, so the next run retries after an OTA update). Run as root.
set -euo pipefail

KERVAX_SETUP_VERSION=1.0  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # install on the first pass too (the rollback self-gate is safe anywhere)

UNIT=kervax-agent
DROPDIR=/etc/systemd/system/$UNIT.service.d
DROP=$DROPDIR/watchdog.conf
VERDIR=/var/lib/kervax/versions
MARK=$VERDIR/agent-watchdog.ver

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# no agent - exit quietly (on a node without kervax-agent the helper does not apply)
if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
  echo "· $UNIT is not installed - the watchdog does not apply, skipping."
  exit 0
fi

# already on Type=notify with the marker in place - do nothing (idempotent)
if [ -f "$MARK" ] && systemctl show "$UNIT" -p Type --value | grep -qx notify; then
  echo "· the systemd watchdog is already active ($UNIT)."
  exit 0
fi

install -d -m 0755 "$DROPDIR"
cat > "$DROP" <<'DROPIN'
[Service]
# systemd watchdog: agent 1.77+ sends READY=1 at startup and WATCHDOG=1 every cycle; if it
# goes quiet for longer than WatchdogSec, systemd restarts it (independently of the Go
# watchdog). TimeoutStartSec is kept short: an agent older than 1.77 never sends READY=1,
# so the start fails quickly and the installer rolls this drop-in back (see
# agent-watchdog.sh).
Type=notify
NotifyAccess=main
WatchdogSec=180
TimeoutStartSec=25
DROPIN

systemctl daemon-reload
if systemctl restart "$UNIT" && systemctl is-active --quiet "$UNIT"; then
  install -d -m 0755 /var/lib/kervax "$VERDIR"  # parent explicitly 0755: the unprivileged agent must enter it
  echo "$KERVAX_SETUP_VERSION" > "$MARK"
  chmod 0644 "$MARK"
  wd=$(systemctl show "$UNIT" -p WatchdogUSec --value)
  echo "✓ systemd watchdog enabled for $UNIT (WatchdogUSec=$wd)."
else
  # the agent did not confirm READY=1 (older than 1.77 or broken) -> roll back, no marker
  rm -f "$DROP"
  rmdir "$DROPDIR" 2>/dev/null || true
  systemctl daemon-reload
  systemctl restart "$UNIT" || true
  echo "· the agent does not support the watchdog (1.77+ required) - drop-in rolled back, skipping." >&2
  exit 0
fi
