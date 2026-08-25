#!/bin/sh
# Kervax: sync agent IPs into the host firewall (outside docker).
#
# The panel maintains <data>/agent_allow_ips - the addresses from which the agents of added
# servers reach INTO the panel (the "server IP" field in the form). If the panel host is
# firewalled with ufw or firewalld, this script allows those addresses on 80/443.
# With neither ufw nor firewalld present it exits quietly (a panel behind a Caddy allow-list
# does not need it: /api/agent/* is open there and protected by agent tokens).
#
# Installation (example; scripts live in /lib65 because it is backed up, /usr is not):
#   install -D -m755 ops/agent-firewall-sync.sh /lib65/kervax/agent-firewall-sync.sh
#   echo '*/2 * * * * root KERVAX_AGENT_IPS=/app/kervax/data/agent_allow_ips /lib65/kervax/agent-firewall-sync.sh' \
#     > /etc/cron.d/kervax-agent-fw
#   chmod 644 /etc/cron.d/kervax-agent-fw
#
# SYNC BOTH WAYS: adding a server opens access, removing it closes access again.
# To make automatic removal safe, the script touches ONLY the addresses it opened itself: it
# keeps that list in its own state file (STATE). Rules created by hand or by anything else are
# invisible to it and never removed, even if the address matches.
# To disable removal (previous behaviour, additions only): KERVAX_FW_PRUNE=0.

set -eu

# cron provides a trimmed PATH (/usr/bin:/bin) while ufw and firewall-cmd live in sbin;
# without this the script silently concluded "no firewall" and opened nothing
PATH=/usr/local/sbin:/usr/sbin:/sbin:$PATH

IPS_FILE="${KERVAX_AGENT_IPS:-/app/kervax/data/agent_allow_ips}"
STATE="${KERVAX_FW_STATE:-/var/lib/kervax/agent-fw-opened}"
PRUNE="${KERVAX_FW_PRUNE:-1}"
[ -r "$IPS_FILE" ] || exit 0

CHANGED=0

have_ufw() { command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; }
have_fwd() { command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; }

add_ufw() {
  ip="$1"
  # INPUT - when the panel listens on a host port directly
  ufw status | grep -q "ALLOW IN[[:space:]]*$ip\b.*kervax-agent" || \
    ufw allow proto tcp from "$ip" to any port 80,443 comment kervax-agent >/dev/null 2>&1 || true
  # FORWARD - when the panel is behind docker-publish (ufw-docker): traffic to the container
  # goes through FORWARD, where a plain "ufw allow" does not apply - a route allow is needed
  ufw status | grep -q "ALLOW FWD[[:space:]]*$ip\b.*kervax-agent" || \
    ufw route allow proto tcp from "$ip" to any port 80,443 comment kervax-agent >/dev/null 2>&1 || true
}

del_ufw() {
  ip="$1"
  ufw delete allow proto tcp from "$ip" to any port 80,443 >/dev/null 2>&1 || true
  ufw route delete allow proto tcp from "$ip" to any port 80,443 >/dev/null 2>&1 || true
}

add_firewalld() {
  ip="$1"
  for port in 80 443; do
    rule="rule family=ipv4 source address=$ip port port=$port protocol=tcp accept"
    firewall-cmd --query-rich-rule="$rule" >/dev/null 2>&1 && continue
    firewall-cmd --permanent --add-rich-rule="$rule" >/dev/null 2>&1 || true
    CHANGED=1
  done
}

del_firewalld() {
  ip="$1"
  for port in 80 443; do
    rule="rule family=ipv4 source address=$ip port port=$port protocol=tcp accept"
    firewall-cmd --permanent --remove-rich-rule="$rule" >/dev/null 2>&1 || true
    CHANGED=1
  done
}

# The list of addresses opened BY THIS script. On the first run after an update it does not
# exist yet, so it is reconstructed from ufw by the kervax-agent comment; otherwise previously
# opened rules would become ownerless and never be removed.
mkdir -p "$(dirname "$STATE")" 2>/dev/null || true
if [ ! -f "$STATE" ]; then
  : > "$STATE" 2>/dev/null || true
  if have_ufw; then
    # the address is the THIRD field of "80,443/tcp  ALLOW  <ip>  # kervax-agent".
    # $(NF-1) cannot be used: it lands on "#", and the seeding silently produced nothing
    ufw status 2>/dev/null | grep "kervax-agent" | awk '{print $3}' \
      | grep -E '^[0-9a-fA-F.:/]+$' | sort -u > "$STATE" 2>/dev/null || : > "$STATE"
  fi
fi

# normalise: valid addresses only, no shell surprises coming from the file
DESIRED=$(tr -d '\r' < "$IPS_FILE" | tr -s '[:space:]' '\n' \
          | grep -E '^[0-9a-fA-F.:/]+$' | sort -u || true)
OPENED=$(tr -d '\r' < "$STATE" | tr -s '[:space:]' '\n' \
         | grep -E '^[0-9a-fA-F.:/]+$' | sort -u || true)

# 1) open what is not open yet
for ip in $DESIRED; do
  if have_ufw; then add_ufw "$ip"
  elif have_fwd; then add_firewalld "$ip"
  fi
done

# 2) close what the panel no longer asks for (and only if we opened it ourselves).
#
# SAFETY CATCHES - learned the hard way: a run against the WRONG list file wiped every real
# rule at once and left the panel closed to the entire fleet. Hence:
#  * an empty list means "wrong file / the panel returned nothing", not "there are no servers"
#    - nothing is closed on that basis, it only goes into the log;
#  * at most MAXPRUNE addresses are removed per run: deleting a server is one or two entries,
#    while "minus ten" is almost always an accident - better noticed than locked out.
DESIRED_N=$(printf '%s\n' $DESIRED | grep -c . || true)
OPENED_N=$(printf '%s\n' $OPENED | grep -c . || true)
MAXPRUNE="${KERVAX_FW_MAXPRUNE:-3}"
if [ "$PRUNE" = "1" ] && [ "$DESIRED_N" = "0" ] && [ "$OPENED_N" != "0" ]; then
  logger -t kervax-agent-fw "the agent IP list is empty ($IPS_FILE) - leaving the rules alone" 2>/dev/null || true
  PRUNE=0
fi
if [ "$PRUNE" = "1" ]; then
  pruned=0
  for ip in $OPENED; do
    printf '%s\n' $DESIRED | grep -qx "$ip" && continue
    if [ "$pruned" -ge "$MAXPRUNE" ]; then
      logger -t kervax-agent-fw "more than $MAXPRUNE stale addresses - kept the rest, check the list" 2>/dev/null || true
      break
    fi
    if have_ufw; then del_ufw "$ip"
    elif have_fwd; then del_firewalld "$ip"
    fi
    pruned=$((pruned + 1))
    logger -t kervax-agent-fw "closed access for $ip (the server is gone from the panel)" 2>/dev/null || true
  done
fi

# State = what is ACTUALLY open (for ufw the firewall itself is queried), not what we
# intended: addresses kept by the safety catches would otherwise become ownerless forever.
if have_ufw; then
  ufw status 2>/dev/null | grep "kervax-agent" | awk '{print $3}' \
    | grep -E '^[0-9a-fA-F.:/]+$' | sort -u > "$STATE.tmp" 2>/dev/null \
    || printf '%s\n' $DESIRED > "$STATE.tmp"
else
  printf '%s\n' $DESIRED > "$STATE.tmp" 2>/dev/null || true
fi
mv -f "$STATE.tmp" "$STATE" 2>/dev/null || true

# firewalld applies permanent rules only after a reload
[ "$CHANGED" = "1" ] && have_fwd && firewall-cmd --reload >/dev/null 2>&1 || true
exit 0
