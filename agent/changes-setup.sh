#!/usr/bin/env bash
# Kervax: what changed on the server. A root helper takes a snapshot every 10 minutes of who
# can log in and what runs on a schedule: SSH keys by fingerprint, users with a login shell,
# sudo rights, crontabs and enabled systemd units, and writes
# /var/lib/kervax/report.d/changes.json. The panel compares snapshots, keeps the feed and
# alerts on new keys, new users and sudo changes.
#
# Since 0.2 it also says which process listens on which TCP port: the agent runs unprivileged
# and sees only the socket owner, so the panel's "open to the internet" table showed
# "a process of user root" instead of sshd or kube-proxy.
#
# Since 0.3 it also reports how SSH lets people in: password and root login as sshd applies them
# to a connection from the internet (Match blocks included), failed and password logins over
# the last 10 minutes, and whether fail2ban, sshguard or crowdsec is running.
#
# Since 0.4: the version of its own sshd (the panel compares it with the banner it sees at the
# node's external address: on one node that address led to another server entirely), the
# addresses a brute force comes from, and the successful logins of the last 10 minutes with
# their method (the panel alerts on a password login from an address never seen before).
#
# Since 0.5: who still has access and does not use it. Key length (a DSA or short RSA key is
# weak), whether an account has a usable password, who gets sudo without a password, and the last
# SSH login of every account. lastlog sees only sessions with a terminal (ansible, CI and scp never
# show there), so the helper keeps its own record from the sshd log in /var/lib/kervax/ssh-lastseen:
# filled once from everything the journal still holds, then every run from the last minutes.
# "seen_since" is how far back that record reaches: the panel calls an account unused only when it
# has looked long enough.
#
# 0.6: 0.5 hung on nodes with a big journal and the unit was killed after two minutes, every run.
# systemd starts a service with SIGPIPE ignored, so "journalctl | head -1" (the oldest journal
# entry) did not stop journalctl at the first line: it read gigabytes of journal to the end. The
# unit now takes SIGPIPE back, every journalctl has a timeout, and the first fill reads the sshd
# log newest first: cut short, it still covers everything back to the oldest login it reached.
#
# 0.7: whom the host firewall lets in on which TCP port: the ACCEPT rules of iptables (ufw lives
# there too) or, without them, of nftables, as "family source ports", separately for the node itself
# and for the published ports of containers, plus whether the rest is refused. The panel checks the
# node's ports from outside through one address; when a neighbour put that address into an allow
# list (it also runs the monitors), a port closed to the internet looked open and "a new port is
# open to the internet" went out for SSH that nobody else could reach.
#
# Only fingerprints, names and hashes leave the node: no key material, no crontab or sudoers
# contents, no password hashes (only "set", "locked" or "empty"). The helper changes nothing.
set -euo pipefail

KERVAX_SETUP_VERSION=0.7  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: it only reads files and systemctl

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-changes"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/changes.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

# Written next to the old one and swapped in with mv: bash reads a running script as it goes,
# and rewriting it in place (cat >) made a run that was in progress continue from the same
# byte offset in the new text - a syntax error in the middle of a line and a failed unit.
cat > "$HELPER.new" <<'HELPER_EOF'
#!/usr/bin/env bash
# Snapshot of access and schedules -> /var/lib/kervax/report.d/changes.json. Read only.
set -u
export LC_ALL=C
OUT=/var/lib/kervax/report.d/changes.json
LASTSEEN=/var/lib/kervax/ssh-lastseen
MAX=200

# JSON string body: control characters dropped, backslash and quote escaped. One raw control
# character makes the agent drop the whole file.
esc() { printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'; }
h16() { sha256sum -- "$1" 2>/dev/null | cut -c1-16; }

# Where sshd looks for keys (AuthorizedKeysFile): %h - home, %u - user, %% - percent;
# a relative path is relative to the home directory.
key_patterns() {
  local p
  p=$(sshd -T 2>/dev/null | awk '$1 == "authorizedkeysfile" { $1 = ""; print; exit }')
  [ -n "$p" ] || p=".ssh/authorized_keys .ssh/authorized_keys2"
  printf '%s\n' $p
}

keys() {
  local out="" n=0 user uid home shell pat f line bits fp rest type comment seen=" "
  local pats
  pats=$(key_patterns)
  while IFS=: read -r user _ uid _ _ home shell; do
    [ -n "$user" ] || continue
    for pat in $pats; do
      [ "$pat" = none ] && continue
      f=${pat//%%/$'\001'}
      f=${f//%h/$home}
      f=${f//%u/$user}
      f=${f//$'\001'/%}
      case "$f" in /*) ;; *) f="$home/$f" ;; esac
      [ -f "$f" ] || continue
      case "$seen" in *" $user:$f "*) continue ;; esac
      seen="$seen$user:$f "
      while read -r bits fp rest; do
        case "$fp" in SHA256:*) ;; *) continue ;; esac
        type=${rest##*(}
        type=${type%)}
        comment=${rest% (*}
        [ "$comment" = "no comment" ] && comment=""
        out="$out${out:+,}{\"u\":\"$(esc "$user")\",\"t\":\"$(esc "$type")\",\"b\":${bits//[!0-9]/},\"fp\":\"$(esc "$fp")\",\"c\":\"$(esc "$(printf '%s' "$comment" | cut -c1-60)")\",\"f\":\"$(esc "$f")\"}"
        n=$((n + 1))
        [ "$n" -ge "$MAX" ] && break 3
      done < <(ssh-keygen -lf "$f" 2>/dev/null)
    done
  done < /etc/passwd
  printf '%s' "$out"
}

# Last SSH login per user from the sshd log, interactive or not: "user unixtime" lines, the first
# line "#since unixtime" - how far back the record reaches. The first fill goes newest first, so
# when it is cut short (or journalctl fails) the record reaches back only to the oldest login it
# read, never further than what was actually looked at.
lastseen_update() {
  local now=$1 since raw="$LASTSEEN.raw"
  command -v journalctl >/dev/null 2>&1 || return 0
  if [ -s "$LASTSEEN" ]; then
    since=$(sed -n 's/^#since //p' "$LASTSEEN" | head -1)
    { grep -v '^#' "$LASTSEEN"
      timeout 30 journalctl -u ssh -u sshd --since "-15min" -o short-unix --no-pager 2>/dev/null | lastseen_parse
    } | lastseen_max > "$raw"
  else
    since=$(timeout 20 journalctl -o short-unix --no-pager 2>/dev/null | head -1 | cut -d. -f1)
    timeout 60 journalctl -r -u ssh -u sshd -o short-unix --no-pager 2>/dev/null | lastseen_parse > "$raw"
    [ "${PIPESTATUS[0]}" = 0 ] || since=$(tail -n 1 "$raw" | cut -d' ' -f2)
    lastseen_max < "$raw" > "$raw.max" && mv -f "$raw.max" "$raw"
  fi
  case "$since" in ''|*[!0-9]*) since=$now ;; esac
  { echo "#since $since"; cat "$raw"; } > "$LASTSEEN.tmp" && chmod 0600 "$LASTSEEN.tmp" && mv -f "$LASTSEEN.tmp" "$LASTSEEN"
  rm -f "$raw"
}
lastseen_parse() {
  grep -F 'Accepted ' | sed -nE 's/^([0-9]+)(\.[0-9]+)? .*Accepted [a-z/-]+ for ([A-Za-z0-9._@-]+) from .*/\3 \1/p'
}
lastseen_max() {
  awk 'NF == 2 && $2 ~ /^[0-9]+$/ { if (!($1 in m) || $2 + 0 > m[$1] + 0) m[$1] = $2 } END { for (u in m) print u, m[u] }'
}

# Last interactive login from lastlog (0 - never or no lastlog)
lastlog_ts() {
  local d
  command -v lastlog >/dev/null 2>&1 || { echo 0; return; }
  d=$(lastlog -u "$1" 2>/dev/null | awk 'NR == 2 && !/Never logged in/ { print $(NF-5), $(NF-4), $(NF-3), $(NF-2), $(NF-1), $NF }')
  [ -n "$d" ] && date -d "$d" +%s 2>/dev/null || echo 0
}

# Users who can log in (a real shell) and every uid 0 account whatever its shell: password state
# from /etc/shadow (P - set, L - locked, NP - empty; never the hash) and the last SSH login
users() {
  local out="" n=0 user uid shell pw last seen
  while IFS=: read -r user _ uid _ _ _ shell; do
    case "$shell" in */nologin|*/false|*/sync|*/halt|*/shutdown|'') [ "$uid" = 0 ] || continue ;; esac
    pw=$(awk -F: -v u="$user" '$1 == u { h = $2; print (h == "" ? "NP" : (h ~ /^[!*]/ ? "L" : "P")); exit }' /etc/shadow 2>/dev/null)
    last=$(lastlog_ts "$user")
    seen=$(awk -v u="$user" '$1 == u { print $2; exit }' "$LASTSEEN" 2>/dev/null)
    case "$seen" in ''|*[!0-9]*) seen=0 ;; esac
    [ "$seen" -gt "${last:-0}" ] && last=$seen
    out="$out${out:+,}{\"n\":\"$(esc "$user")\",\"uid\":${uid//[!0-9]/},\"sh\":\"$(esc "$shell")\",\"pw\":\"${pw:-}\",\"last\":${last:-0}}"
    n=$((n + 1))
    [ "$n" -ge "$MAX" ] && break
  done < /etc/passwd
  printf '%s' "$out"
}

# Members of the groups that give sudo by default, and hashes of the sudoers files
sudo_info() {
  local groups="" g members files="" f
  for g in sudo wheel admin; do
    members=$(getent group "$g" 2>/dev/null | cut -d: -f4)
    getent group "$g" >/dev/null 2>&1 || continue
    groups="$groups${groups:+,}\"$g\":[$(printf '%s' "$members" | tr ',' '\n' | grep -v '^$' | sort -u \
      | while read -r m; do printf '"%s",' "$(esc "$m")"; done | sed 's/,$//')]"
  done
  for f in /etc/sudoers /etc/sudoers.d/*; do
    [ -f "$f" ] || continue
    files="$files${files:+,}\"$(esc "$f")\":\"$(h16 "$f")\""
  done
  # who gets sudo without a password: the first word of every NOPASSWD rule (user or %group)
  local nopw
  nopw=$(cat /etc/sudoers /etc/sudoers.d/* 2>/dev/null | grep -E '^[^#]*NOPASSWD' | grep -vE '^[[:space:]]*Defaults' \
    | awk '{ print $1 }' | sort -u | head -50 | while read -r m; do printf '"%s",' "$(esc "$m")"; done | sed 's/,$//')
  printf '{"groups":{%s},"files":{%s},"nopasswd":[%s]}' "$groups" "$files" "$nopw"
}

# Crontabs: system, cron.d, per-user spools and the run-parts directories - hashes only
cron_info() {
  local out="" n=0 f
  for f in /etc/crontab /etc/cron.d/* /var/spool/cron/crontabs/* /var/spool/cron/* \
           /etc/cron.hourly/* /etc/cron.daily/* /etc/cron.weekly/* /etc/cron.monthly/*; do
    [ -f "$f" ] || continue
    case "$f" in */.placeholder) continue ;; esac
    out="$out${out:+,}\"$(esc "$f")\":\"$(h16 "$f")\""
    n=$((n + 1))
    [ "$n" -ge "$MAX" ] && break
  done
  printf '{%s}' "$out"
}

# Enabled services and timers: a new one starts at every boot
units() {
  command -v systemctl >/dev/null 2>&1 || return 0
  systemctl list-unit-files --state=enabled --type=service,timer --no-legend --no-pager 2>/dev/null \
    | awk '{print $1}' | grep -E '^[A-Za-z0-9@._:\\-]+\.(service|timer)$' | sort -u | head -400 \
    | while read -r u; do printf '"%s",' "$(esc "$u")"; done | sed 's/,$//'
}

# Which process listens on each TCP port, loopback skipped: {"2221":"sshd","10250":"kubelet"}
listen_procs() {
  command -v ss >/dev/null 2>&1 || return 0
  ss -ltnp 2>/dev/null | awk 'NR > 1 && c < 300 {
      addr = $4
      if (addr ~ /^127\./ || addr ~ /^\[::1\]/ || addr ~ /^\[::ffff:127\./) next
      n = split(addr, a, ":"); port = a[n]
      if (port !~ /^[0-9]+$/ || (port in seen)) next
      if (!match($0, /users:\(\("[^"]+"/)) next
      name = substr($0, RSTART + 9, RLENGTH - 10)
      gsub(/[\\"]/, "", name)
      seen[port] = 1
      printf "%s\"%s\":\"%s\"", (c++ ? "," : ""), port, name
    }' | tr -d '\000-\037'
}

# Whom the host firewall lets in (0.7). "in" - ACCEPT rules for TCP on the way to the node itself,
# "fwd" - on the way through it into containers (DOCKER-USER, ufw-docker), as "family source ports":
# ["4 198.51.100.7/32 2221", "46 * 22", "4 * 80,443", "4 192.0.2.0/24 *"], "*" for any source and
# for all ports, family 4, 6 or 46. Whether a chain counts is decided by where the jumps from INPUT
# and FORWARD lead, not by its name. Rules for loopback and the inner interfaces of docker,
# kubernetes and VPNs, for established connections and in outgoing chains are skipped: they do not
# let the internet in. A jump to ufw's rate limit is an allow, the body of that chain is not (it
# would read as "everything for everybody"). Docker's own per-port allows (chain DOCKER) are not
# listed: what decides is DOCKER-USER before them. "idrop"/"fdrop" - families where a plain
# "drop the rest" stands on the way in or in DOCKER-USER: the default is to refuse even with the
# policy ACCEPT
fw_info() {
  local src="" rules="" ipt=""
  if command -v iptables-save >/dev/null 2>&1; then
    ipt=$({ timeout 10 iptables-save -t filter 2>/dev/null | sed 's/^/4 /'
              timeout 10 ip6tables-save -t filter 2>/dev/null | sed 's/^/6 /'; } | awk '
      $2 ~ /^:/ { if ($2 == ":INPUT") print "policy" $1 " " $3; next }
      $2 != "-A" { next }
      {
        n++; fam[n] = $1; ch[n] = $3; line = $0; sub(/^[46] -A [^ ]+/, "", line); body[n] = line " "
        tgt[n] = ""
        if (match(line, / -[jg] [^ ]+/)) { tgt[n] = substr(line, RSTART + 4, RLENGTH - 4); edge[$1, $3, tgt[n]] = 1 }
      }
      END {
        for (f = 4; f <= 6; f += 2) { rin[f, "INPUT"] = 1; rfw[f, "FORWARD"] = 1; rdu[f, "DOCKER-USER"] = 1 }
        do {
          more = 0
          for (k in edge) {
            split(k, e, SUBSEP)
            if (rin[e[1], e[2]] && !rin[e[1], e[3]]) { rin[e[1], e[3]] = 1; more = 1 }
            if (rfw[e[1], e[2]] && !rfw[e[1], e[3]]) { rfw[e[1], e[3]] = 1; more = 1 }
            if (rdu[e[1], e[2]] && !rdu[e[1], e[3]]) { rdu[e[1], e[3]] = 1; more = 1 }
          }
        } while (more)
        for (i = 1; i <= n; i++) {
          f = fam[i]; c = ch[i]; t = tgt[i]; line = body[i]
          if (t == "DROP" || t == "REJECT") {
            # "drop the rest": nothing but the interface, tcp and a comment around the drop
            pure = line
            gsub(/ -m comment --comment ("[^"]*"|[^ ]+)/, "", pure)
            gsub(/ -p tcp| -m tcp| --reject-with [^ ]+| -j (DROP|REJECT)/, "", pure)
            if (match(pure, / -i [^ ]+/) && substr(pure, RSTART + 4, RLENGTH - 4) !~ /^(lo|docker|br-|cni|cali|flannel|veth|vxlan|kube|tailscale|wg|tun|virbr|lxc|podman|nebula|zt)/) sub(/ -i [^ ]+/, "", pure)
            if (pure ~ /^ *$/ && c !~ /^(cali|KUBE|CILIUM|cilium|FLANNEL|flannel|DOCKER$|DOCKER-ISOLATION)|limit/) {
              if (rin[f, c]) idrop[f] = 1
              if (rdu[f, c]) fdrop[f] = 1
            }
            continue
          }
          if (c ~ /[Oo][Uu][Tt][Pp][Uu][Tt]|skip-to-policy|limit-accept|logging|reject|track/) continue
          if (t != "ACCEPT" && t !~ /^ufw6?-user-limit-accept$/) continue
          if (line ~ / -p (udp|icmp|ipv6-icmp|icmpv6|58|1|17) /) continue
          if (line ~ /--ctstate|--state |-m (conntrack|state) /) continue
          gsub(/ ! -[io] [^ ]+/, "", line)
          if (line ~ / -i (lo|docker|br-|cni|cali|flannel|veth|vxlan|kube|tailscale|wg|tun|virbr|lxc|podman|nebula|zt)/) continue
          if (line ~ / -o lo / || line ~ / -d (224\.|ff0)/) continue
          # kubernetes plumbing (marks, traffic from the node itself) and forwarding out of an
          # interface without a port (outbound, into the pod bridge): not a door from the internet
          if (line ~ /--src-type LOCAL| -m mark | -m physdev /) continue
          if (line ~ / -o [^ ]+/ && line !~ / --dports? /) continue
          s = "*"
          if (match(line, / -s [^ ]+/)) { s = substr(line, RSTART + 4, RLENGTH - 4); if (s == "0.0.0.0/0" || s == "::/0") s = "*" }
          p = "*"
          if (match(line, / --dports [0-9,:]+/)) p = substr(line, RSTART + 9, RLENGTH - 9)
          else if (match(line, / --dport [0-9:]+/)) p = substr(line, RSTART + 8, RLENGTH - 8)
          if (rin[f, c]) print "in " f " " s " " p
          if (rfw[f, c] && c != "DOCKER") print "fwd " f " " s " " p
        }
        for (f in idrop) print "idrop " f
        for (f in fdrop) print "fdrop " f
      }' | sort -u)
    printf '%s\n' "$ipt" | grep -q '^in \|^policy[46] DROP' && { src=iptables; rules=$ipt; }
  fi
  if [ -z "$src" ] && command -v nft >/dev/null 2>&1; then
    rules=$(timeout 10 nft list ruleset 2>/dev/null | awk '
      /^table / { nat = ($0 ~ / nat /); tf = ($2 == "ip") ? "4" : ($2 == "ip6") ? "6" : ($2 == "inet") ? "46" : ""; next }
      /^[ \t]*chain / { ch = $2; hook = ""; next }
      /hook / { hook = $0; sub(/.*hook /, "", hook); sub(/ .*/, "", hook)
                if (hook == "input" && / policy drop/) { if (tf ~ /4/) print "policy4 DROP"; if (tf ~ /6/) print "policy6 DROP" }
                next }
      tf == "" || nat || hook ~ /output|postrouting|prerouting|forward/ { next }
      ch ~ /^DOCKER(-ISOLATION.*)?$|[Oo][Uu][Tt][Pp][Uu][Tt]|[Ff][Ww][Dd]|[Ff][Oo][Rr][Ww][Aa][Rr][Dd]|skip-to-policy|limit-accept|logging|reject|track/ { next }
      / accept/ {
        line = $0
        if (line ~ /ct state|iifname "?(lo|docker|br-|cni|cali|flannel|veth|vxlan|kube|tailscale|wg|tun|virbr|lxc|podman)/) next
        if (line ~ /oifname "?lo"?( |$)/) next
        if (line ~ /udp |icmp/) next
        f = tf; s = "*"
        if (match(line, /ip6? saddr (\{[^}]*\}|[^ ]+)/)) {
          s = substr(line, RSTART, RLENGTH); f = (s ~ /^ip6/) ? "6" : "4"; sub(/ip6? saddr /, "", s); gsub(/[{} ]/, "", s)
        }
        p = "*"
        if (match(line, /tcp dport (\{[^}]*\}|[0-9-]+)/)) { p = substr(line, RSTART + 10, RLENGTH - 10); gsub(/[{} ]/, "", p); gsub(/-/, ":", p) }
        else if (line ~ /udp dport|sport/) next
        k = split(s, ss, ",")
        for (i = 1; i <= k; i++) print "in " f " " ss[i] " " p
      }' | sort -u)
    printf '%s\n' "$rules" | grep -q '^in \|^policy' && src=nft
  fi
  # iptables without a single allow and with the policy ACCEPT: everything is open, say so
  [ -z "$src" ] && printf '%s\n' "$ipt" | grep -q '^policy' && { src=iptables; rules=$ipt; }
  [ -n "$src" ] || { printf 'null'; return 0; }
  printf '%s\n' "$rules" | awk -v src="$src" '
    /^policy4 / { p4 = $2; next }
    /^policy6 / { p6 = $2; next }
    /^idrop / { idrop = idrop $2; next }
    /^fdrop / { fdrop = fdrop $2; next }
    NF == 4 && $2 ~ /^[46]+$/ && $3 ~ /^[*0-9A-Fa-f.:\/]+$/ && $4 ~ /^[*0-9,:]+$/ {
      e = "\"" $2 " " $3 " " $4 "\""
      if ($1 == "in" && ni < 500) a = a (ni++ ? "," : "") e
      if ($1 == "fwd" && nf < 500) w = w (nf++ ? "," : "") e
    }
    END { printf "{\"src\":\"%s\",\"p4\":\"%s\",\"p6\":\"%s\",\"idrop\":\"%s\",\"fdrop\":\"%s\",\"in\":[%s],\"fwd\":[%s]}", src, p4, p6, idrop, fdrop, a, w }'
}

# How SSH lets people in. sshd -T -C evaluates the config for a given connection: an address from
# the documentation range stands for "somebody on the internet", so Match blocks that open
# passwords to the office network do not count. Failed and password logins - the last 10 minutes
# of the journal.
ssh_info() {
  command -v sshd >/dev/null 2>&1 || return 0
  local t r ports fail accp guard g j banner top logins
  t=$(sshd -T -C user=kervax-probe,host=probe.invalid,addr=203.0.113.1 2>/dev/null) || t=$(sshd -T 2>/dev/null) || return 0
  r=$(sshd -T -C user=root,host=probe.invalid,addr=203.0.113.1 2>/dev/null) || r="$t"
  sv() { printf '%s\n' "$1" | awk -v k="$2" '$1 == k { print $2; exit }'; }
  ports=$(printf '%s\n' "$t" | awk '$1 == "port" && $2 ~ /^[0-9]+$/ { print $2 }' | sort -un | head -5 | paste -sd, -)
  j=$(timeout 30 journalctl -u ssh -u sshd --since "-10min" -o cat 2>/dev/null | tail -n 20000)
  fail=$(printf '%s\n' "$j" | grep -cE "Failed (password|keyboard-interactive)|Invalid user|authentication failure" || true)
  accp=$(printf '%s\n' "$j" | grep -cE "Accepted (password|keyboard-interactive)" || true)
  # where the brute force comes from: the five busiest addresses
  top=$(printf '%s\n' "$j" | grep -E "Failed (password|keyboard-interactive)|Invalid user" \
    | grep -oE "from [0-9A-Fa-f.:]+" | awk '{ print $2 }' | sort | uniq -c | sort -rn | head -5 \
    | awk '{ printf "%s{\"ip\":\"%s\",\"n\":%s}", (NR > 1 ? "," : ""), $2, $1 }')
  # successful logins: method, user, address
  logins=$(printf '%s\n' "$j" | sed -nE 's/^Accepted ([a-z/-]+) for ([A-Za-z0-9._-]+) from ([0-9A-Fa-f.:]+) port .*/\1 \2 \3/p' \
    | sort -u | head -20 | awk '{ printf "%s{\"m\":\"%s\",\"u\":\"%s\",\"ip\":\"%s\"}", (NR > 1 ? "," : ""), $1, $2, $3 }')
  # the version of its own sshd: the panel compares it with the banner at the external address.
  # Not by connecting to itself - tcp wrappers refuse localhost on some nodes and log every try
  banner=$(sshd -V 2>&1 | grep -o 'OpenSSH_[0-9][0-9A-Za-z.]*' | head -1 || true)
  guard=""
  for g in fail2ban sshguard crowdsec; do
    systemctl is-active --quiet "$g" 2>/dev/null && { guard=$g; break; }
  done
  printf '{"pass":"%s","kbd":"%s","pam":"%s","methods":"%s","root":"%s","rootpass":"%s","rootkbd":"%s","ports":[%s],"fail":%d,"accp":%d,"guard":"%s","banner":"%s","fail_top":[%s],"logins":[%s]}' \
    "$(esc "$(sv "$t" passwordauthentication)")" "$(esc "$(sv "$t" kbdinteractiveauthentication)")" \
    "$(esc "$(sv "$t" usepam)")" "$(esc "$(sv "$t" authenticationmethods)")" \
    "$(esc "$(sv "$r" permitrootlogin)")" "$(esc "$(sv "$r" passwordauthentication)")" \
    "$(esc "$(sv "$r" kbdinteractiveauthentication)")" "$ports" "${fail:-0}" "${accp:-0}" "$guard" \
    "$(esc "$banner")" "$top" "$logins"
}

now=$(date +%s)
# A run killed half way (0.5 on a big journal) left its temp file behind
find "${OUT%/*}" -maxdepth 1 -name "${OUT##*/}.??????" -mmin +20 -delete 2>/dev/null
rm -f "$LASTSEEN.raw" "$LASTSEEN.raw.max" "$LASTSEEN.tmp"
ssh=$(ssh_info)
lastseen_update "$now"
seen_since=$(sed -n 's/^#since //p' "$LASTSEEN" 2>/dev/null | head -1)
case "$seen_since" in ''|*[!0-9]*) seen_since=0 ;; esac
json=$(printf '{"v":1,"ts":%s,"keys":[%s],"users":[%s],"sudo":%s,"cron":%s,"units":[%s],"listen":{%s},"ssh":%s,"seen_since":%s,"fw":%s}' \
  "$now" "$(keys)" "$(users)" "$(sudo_info)" "$(cron_info)" "$(units)" "$(listen_procs)" "${ssh:-null}" "$seen_since" "$(fw_info)")
tmp=$(mktemp "${OUT}.XXXXXX")
printf '%s\n' "$json" > "$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" "$OUT"
HELPER_EOF
chmod 0755 "$HELPER.new" && mv -f "$HELPER.new" "$HELPER"
chmod 0755 "$HELPER"
chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-changes.service <<EOF
[Unit]
Description=Kervax: snapshot of SSH keys, users, sudo, crontabs and enabled units (read only)
[Service]
Type=oneshot
ExecStart=$HELPER
Nice=10
TimeoutStartSec=2min
# "journalctl | head -1" stops journalctl only when SIGPIPE kills it; systemd ignores SIGPIPE by default
IgnoreSIGPIPE=no
EOF
cat > /etc/systemd/system/kervax-changes.timer <<'EOF'
[Unit]
Description=Kervax: snapshot of access and schedules every 10 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-changes.timer >/dev/null 2>&1 || true
systemctl start --no-block kervax-changes.service >/dev/null 2>&1 || true

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/changes-setup.ver"
chmod 0644 "$STATE_DIR/versions/changes-setup.ver"
echo "changes-setup: SSH keys and login settings, users, sudo, crontabs, enabled units, listening processes, firewall allows -> $OUT (every 10 minutes)."
