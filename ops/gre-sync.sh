#!/bin/sh
# Kervax: GRE tunnels from nodes to the panel host (outside docker).
#
# Some nodes reach the panel over a link whose DPI freezes a TCP connection after ~16 KB. For them
# "GRE to the panel" is switched on in the server settings: the node (helper gre-setup) and the
# panel host hold a bare GRE tunnel, and the agent dials the panel's inner address in it with TLS
# and SNI on the panel's name as before. The panel maintains <data>/gre_tunnels, a line per such
# server:
#     <iface> <node public IP> <panel host inner address>/30 <node inner address>
# and this script brings those interfaces up (ip tunnel mode gre, mtu 1476) and opens the firewall
# for them: GRE from the node's address, and 80/443 coming in on the tunnel interface. It touches
# ONLY its own interfaces (kvxg<N>) and the rules it added itself (kept in STATE); everything else
# on the host is invisible to it.
#
# Installation (example; scripts live in /lib65 because it is backed up, /usr is not):
#   install -D -m755 ops/gre-sync.sh /lib65/kervax/gre-sync.sh
#   install -m644 ops/kervax-gre-host.service ops/kervax-gre-host.path /etc/systemd/system/
#   systemctl daemon-reload && systemctl enable --now kervax-gre-host.path kervax-gre-host.service
#   echo '*/2 * * * * root KERVAX_GRE_LIST=/app/kervax/data/gre_tunnels /lib65/kervax/gre-sync.sh' \
#     > /etc/cron.d/kervax-gre && chmod 644 /etc/cron.d/kervax-gre
# (the units assume /app/kervax/data and /lib65/kervax - edit them if yours differ). The path unit
# runs the script the moment the panel rewrites the list, the service also at boot (the tunnels
# are not persistent), cron is the safety net.
#
# The panel's web server (caddy) has to listen on 443 on every address of the host, the inner ones
# included: a port published by docker as "443:443" is (docker DNATs it for any local address),
# "203.0.113.5:443:443" is not. With ufw-docker the "route allow in on <iface>" rule covers the
# FORWARD chain on the way to the container.
#
# What was done goes back to the panel in <data>/gre_tunnels.applied: a line per interface with the
# local address the host sends GRE from (the node needs it as the far end of its tunnel) and ok or
# the error. Written every run, so its age tells the panel whether this script runs at all.

set -eu

# cron provides a trimmed PATH (/usr/bin:/bin) while ip, ufw and firewall-cmd live in sbin.
# Appended, not prepended: a PATH that already has stubs first (tests) keeps them
PATH=$PATH:/usr/local/sbin:/usr/sbin:/sbin

LIST="${KERVAX_GRE_LIST:-/app/kervax/data/gre_tunnels}"
APPLIED="${KERVAX_GRE_APPLIED:-$LIST.applied}"
STATE="${KERVAX_GRE_STATE:-/var/lib/kervax/gre-opened}"
LOCK="${KERVAX_GRE_LOCK:-/run/kervax-gre.lock}"
MTU=1476
COMMENT=kervax-gre
[ -r "$LIST" ] || exit 0
# the panel always writes the header, even with no tunnels: without it this is a wrong or
# half-written file, and "no tunnels" from it would tear every tunnel down
if [ "$(head -n 1 "$LIST" | tr -d '\r')" != "# kervax-gre v1" ]; then
  logger -t kervax-gre "no header in $LIST - leaving the tunnels alone" 2>/dev/null || true
  exit 0
fi
# cron, boot and the path unit may fire together: one run at a time
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK" && flock -w 60 9 || exit 0
fi

have_ufw() { command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; }
have_fwd() { command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; }
if have_ufw; then FW=ufw; elif have_fwd; then FW=firewalld; else FW=none; fi

IP4='([0-9]{1,3}\.){3}[0-9]{1,3}'
# valid lines only: no shell surprises coming from the file
WANT=$(tr -d '\r' < "$LIST" | grep -E "^kvxg[0-9]{1,8} $IP4 $IP4/30 $IP4\$" || true)

# Firewall rules this script added, one per line: "<fw> gre <node IP>" or "<fw> in <iface> <node
# inner IP>". The firewall is part of the rule: switched from ufw to firewalld, the old ones are
# still removed from ufw.
mkdir -p "$(dirname "$STATE")" 2>/dev/null || true
[ -f "$STATE" ] || : > "$STATE"
RULES=""
CHANGED=0

fw_add() {
  # $1 = gre|in, then the rule's arguments; does nothing if this script already added it
  [ "$FW" = none ] && return 0
  rule="$FW $*"
  if ! grep -Fqx "$rule" "$STATE"; then
    case "$FW:$1" in
      ufw:gre) ufw allow proto gre from "$2" to any comment "$COMMENT" >/dev/null 2>&1 || return 1 ;;
      ufw:in)
        # INPUT - caddy listening on the host; FORWARD - caddy in docker (ufw-docker)
        ufw allow in on "$2" proto tcp to any port 80,443 comment "$COMMENT" >/dev/null 2>&1 || return 1
        ufw route allow in on "$2" proto tcp to any port 80,443 comment "$COMMENT" >/dev/null 2>&1 || return 1 ;;
      firewalld:gre)
        firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=$2 protocol value=gre accept" >/dev/null 2>&1 || return 1
        CHANGED=1 ;;
      firewalld:in)
        for port in 443 80; do
          firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=$3 port port=$port protocol=tcp accept" >/dev/null 2>&1 || return 1
        done
        CHANGED=1 ;;
    esac
  fi
  RULES="$RULES$rule
"
}

fw_del() {
  case "$1:$2" in
    ufw:gre) ufw delete allow proto gre from "$3" to any >/dev/null 2>&1 || true ;;
    ufw:in)
      ufw delete allow in on "$3" proto tcp to any port 80,443 >/dev/null 2>&1 || true
      ufw route delete allow in on "$3" proto tcp to any port 80,443 >/dev/null 2>&1 || true ;;
    firewalld:gre)
      firewall-cmd --permanent --remove-rich-rule="rule family=ipv4 source address=$3 protocol value=gre accept" >/dev/null 2>&1 || true
      CHANGED=1 ;;
    firewalld:in)
      for port in 443 80; do
        firewall-cmd --permanent --remove-rich-rule="rule family=ipv4 source address=$4 port port=$port protocol=tcp accept" >/dev/null 2>&1 || true
      done
      CHANGED=1 ;;
  esac
}

# a tunnel that failed this time keeps the rules it already had: the next run will likely fix it,
# and a rule removed and added back every two minutes is only noise
keep_rules() {
  RULES="$RULES$(grep -E -e "^[a-z]+ gre $1\$" -e "^[a-z]+ in $2 " "$STATE" || true)
"
}

OUT=""
WANT_IF=""
printf '%s\n' "$WANT" | while read -r _; do :; done   # (keeps set -e quiet on an empty list)
for line in $(printf '%s\n' "$WANT" | tr ' ' '|'); do
  iface=$(echo "$line" | cut -d'|' -f1)
  peer=$(echo "$line" | cut -d'|' -f2)
  cidr=$(echo "$line" | cut -d'|' -f3)
  node=$(echo "$line" | cut -d'|' -f4)
  WANT_IF="$WANT_IF $iface"
  # the address this host sends from towards the node: the near end of the tunnel
  src=$(ip -o route get "$peer" 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n 1)
  if [ -z "$src" ]; then
    OUT="$OUT$iface $peer - err no route to the node
"
    keep_rules "$peer" "$iface"
    continue
  fi
  cur=$(ip -o tunnel show "$iface" 2>/dev/null || true)
  if ! printf '%s\n' "$cur" | grep -Eq "remote $peer local $src( |\$)"; then
    ip link del "$iface" 2>/dev/null || true
    if ! err=$(ip tunnel add "$iface" mode gre local "$src" remote "$peer" ttl 64 2>&1); then
      OUT="$OUT$iface $peer $src err $(printf '%s' "$err" | tr '\n' ' ' | cut -c1-150)
"
      keep_rules "$peer" "$iface"
      continue
    fi
  fi
  if ! ip -o addr show dev "$iface" 2>/dev/null | grep -q " inet $cidr "; then
    ip addr flush dev "$iface" 2>/dev/null || true
    ip addr add "$cidr" dev "$iface" 2>/dev/null || true
  fi
  if ! err=$(ip link set "$iface" mtu "$MTU" up 2>&1); then
    OUT="$OUT$iface $peer $src err $(printf '%s' "$err" | tr '\n' ' ' | cut -c1-150)
"
    keep_rules "$peer" "$iface"
    continue
  fi
  if ! fw_add gre "$peer" || ! fw_add in "$iface" "$node"; then
    OUT="$OUT$iface $peer $src err $FW did not take the rules for the tunnel
"
    keep_rules "$peer" "$iface"
    continue
  fi
  OUT="$OUT$iface $peer $src ok
"
done

# own interfaces the panel no longer asks for go away
for iface in $(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | cut -d@ -f1 | grep -E '^kvxg[0-9]+$' || true); do
  case " $WANT_IF " in
    *" $iface "*) ;;
    *) ip link del "$iface" 2>/dev/null || true
       logger -t kervax-gre "removed $iface (GRE switched off in the panel)" 2>/dev/null || true ;;
  esac
done

# rules this script added that are not wanted any more (the same rule for two servers behind one
# address is kept while either wants it)
while IFS= read -r rule; do
  [ -n "$rule" ] || continue
  printf '%s' "$RULES" | grep -Fqx "$rule" && continue
  # shellcheck disable=SC2086
  fw_del $rule
done < "$STATE"
printf '%s' "$RULES" | sort -u > "$STATE.tmp" && mv -f "$STATE.tmp" "$STATE"

# firewalld applies permanent rules only after a reload
if [ "$CHANGED" = "1" ] && [ "$FW" = firewalld ]; then firewall-cmd --reload >/dev/null 2>&1 || true; fi

{
  echo "# kervax-gre fw=$FW ts=$(date +%s)"
  printf '%s' "$OUT"
} > "$APPLIED.tmp" 2>/dev/null && chmod 0644 "$APPLIED.tmp" 2>/dev/null && mv -f "$APPLIED.tmp" "$APPLIED" 2>/dev/null || true
exit 0
