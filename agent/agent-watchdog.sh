#!/usr/bin/env bash
# Kervax: enable the systemd watchdog for the agent (self-recovery when it hangs).
#
# WHY. The agent's report loop can hang (network or collection), and the in-process Go
# watchdog once failed to fire (incident: 23 minutes "offline"). The systemd watchdog is
# an independent backstop: agent 1.77+ sends WATCHDOG=1 every cycle, and if it goes quiet
# for longer than WatchdogSec systemd restarts it. All this script does is switch the UNIT
# to Type=notify (an OTA binary update does not touch the unit, hence a separate step).
# Since 1.1 it also gives the agent and its instances their own /tmp (PrivateTmp=yes, as new
# installs have it). If another drop-in overrides that or the agent does not start with it,
# the agent is left without it, the reason goes to the output, the journal and $NOTE, and the
# marker is written anyway: the next run does not restart the agent for the same thing again.
# Installed LOCALLY by the ansible playbook, not from the panel.
#
# SAFE ON ANY NODE (KERVAX_SETUP_ALWAYS): if the agent is older than 1.77 and never sends
# READY=1, the start under Type=notify fails, the script notices and ROLLS BACK the drop-in
# (no marker is written, so the next run retries after an OTA update). Run as root.
set -euo pipefail

KERVAX_SETUP_VERSION=1.1  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # install on the first pass too (the rollback self-gate is safe anywhere)

UNIT=kervax-agent
DROPDIR=/etc/systemd/system/$UNIT.service.d
DROP=$DROPDIR/watchdog.conf
PTDROP=$DROPDIR/private-tmp.conf
# instances (kervax-agent@<name>, an agent of a second panel) take it from the template
TPLDIR=/etc/systemd/system/kervax-agent@.service.d
TPLDROP=$TPLDIR/private-tmp.conf
VERDIR=/var/lib/kervax/versions
MARK=$VERDIR/agent-watchdog.ver
# why the private /tmp is not in place. The marker stays a plain version (the panel and the
# playbook compare it as is); with this note a rerun of the same version leaves the agent
# alone. Delete the file to try again.
NOTE=$VERDIR/agent-watchdog.private-tmp

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# no agent - exit quietly (on a node without kervax-agent the helper does not apply)
if ! systemctl cat "$UNIT" >/dev/null 2>&1; then
  echo "· $UNIT is not installed - the watchdog does not apply, skipping."
  exit 0
fi

instances() { systemctl list-units --plain --no-legend --state=active 'kervax-agent@*.service' 2>/dev/null | awk '{print $1}'; }
# PrivateTmp is in effect: the main process sees a /tmp other than the host one
private_tmp() {
  local pid t
  [ "$(systemctl show "$1" -p PrivateTmp --value)" = yes ] || return 1
  pid=$(systemctl show "$1" -p MainPID --value)
  [ -n "$pid" ] && [ "$pid" != 0 ] || return 1
  t=$(stat -Lc %d:%i "/proc/$pid/root/tmp" 2>/dev/null) && [ "$t" != "$(stat -Lc %d:%i /tmp)" ]
}
all_private() {
  local u
  for u in "$UNIT" $(instances); do private_tmp "$u" || return 1; done
}
started() { systemctl daemon-reload; systemctl restart "$1" && systemctl is-active --quiet "$1"; }
note() {
  install -d -m 0755 /var/lib/kervax "$VERDIR"  # parent explicitly 0755: the unprivileged agent must enter it
  echo "$*" >> "$NOTE"
  chmod 0644 "$NOTE"
  echo "· $*" >&2
  logger -t kervax-agent-watchdog -- "$*" 2>/dev/null || true
}

# already on Type=notify, the marker of this version and the private /tmp in effect (or the
# reason why not) - do nothing (idempotent)
if [ "$(cat "$MARK" 2>/dev/null)" = "$KERVAX_SETUP_VERSION" ] \
   && systemctl show "$UNIT" -p Type --value | grep -qx notify; then
  if [ -f "$NOTE" ]; then
    echo "· the systemd watchdog is active ($UNIT), the private /tmp is not: $(tr '\n' ' ' < "$NOTE")(delete $NOTE to retry)."
    exit 0
  fi
  if all_private; then
    echo "· the systemd watchdog and the private /tmp are already active ($UNIT)."
    exit 0
  fi
fi
rm -f "$NOTE"

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
# Its own /tmp and /var/tmp: what other users leave in /tmp (docker CLI plugins and config
# were found there with HOME=/tmp before agent 2.73) does not reach the agent and the
# programs it runs. Covers nodes without self-update too.
cat > "$PTDROP" <<'DROPIN'
[Service]
PrivateTmp=yes
DROPIN

if started "$UNIT"; then
  if ! private_tmp "$UNIT"; then
    # another drop-in or the unit sets PrivateTmp itself: that is someone's decision, ours goes
    why="PrivateTmp=$(systemctl show "$UNIT" -p PrivateTmp --value), drop-ins: $(systemctl show "$UNIT" -p DropInPaths --value)"
    rm -f "$PTDROP"
    systemctl daemon-reload
    note "$UNIT runs without a private /tmp ($why) - left as is."
  fi
elif rm -f "$PTDROP" && started "$UNIT"; then
  # the watchdog may have worked before this version: keep it, without the private /tmp
  note "$UNIT did not start with PrivateTmp=yes - left without it."
else
  # the agent did not confirm READY=1 (older than 1.77 or broken) -> roll back, no marker
  rm -f "$DROP"
  rmdir "$DROPDIR" 2>/dev/null || true
  systemctl daemon-reload
  systemctl restart "$UNIT" || true
  echo "· the agent does not support the watchdog (1.77+ required) - drop-in rolled back, skipping." >&2
  exit 0
fi

# instances: the template gets the same drop-in, the running ones restart with it. One that
# does not come up with its own /tmp rolls the template back, and all of them restart again.
if [ -n "$(systemctl list-units --all --plain --no-legend 'kervax-agent@*.service' 2>/dev/null)" ]; then
  inst=$(instances)
  install -d -m 0755 "$TPLDIR"
  printf '[Service]\nPrivateTmp=yes\n' > "$TPLDROP"
  systemctl daemon-reload
  bad=""
  for u in $inst; do
    { systemctl restart "$u" && systemctl is-active --quiet "$u" && private_tmp "$u"; } || bad="$bad $u"
  done
  if [ -n "$bad" ]; then
    rm -f "$TPLDROP"
    rmdir "$TPLDIR" 2>/dev/null || true
    systemctl daemon-reload
    for u in $inst; do systemctl restart "$u" || true; done
    note "instances$bad did not come up with PrivateTmp=yes - instances are left without it."
  fi
fi

install -d -m 0755 /var/lib/kervax "$VERDIR"  # parent explicitly 0755: the unprivileged agent must enter it
echo "$KERVAX_SETUP_VERSION" > "$MARK"
chmod 0644 "$MARK"
wd=$(systemctl show "$UNIT" -p WatchdogUSec --value)
pt=yes; [ -f "$NOTE" ] && pt="no, see $NOTE"
echo "✓ systemd watchdog enabled for $UNIT (WatchdogUSec=$wd), private /tmp: $pt."
