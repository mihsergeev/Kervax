#!/usr/bin/env bash
# Kervax: what changed on the server. A root helper takes a snapshot every 10 minutes of who
# can log in and what runs on a schedule: SSH keys by fingerprint, users with a login shell,
# sudo rights, crontabs and enabled systemd units, and writes
# /var/lib/kervax/report.d/changes.json. The panel compares snapshots, keeps the feed and
# alerts on new keys, new users and sudo changes.
#
# Only fingerprints, names and hashes leave the node: no key material, no crontab or sudoers
# contents. The helper changes nothing.
set -euo pipefail

KERVAX_SETUP_VERSION=0.1  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_ALWAYS=1     # safe on any node: it only reads files and systemctl

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-changes"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/report.d/changes.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent could not enter).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions" "$STATE_DIR/report.d"

cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Snapshot of access and schedules -> /var/lib/kervax/report.d/changes.json. Read only.
set -u
export LC_ALL=C
OUT=/var/lib/kervax/report.d/changes.json
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
        out="$out${out:+,}{\"u\":\"$(esc "$user")\",\"t\":\"$(esc "$type")\",\"fp\":\"$(esc "$fp")\",\"c\":\"$(esc "$(printf '%s' "$comment" | cut -c1-60)")\",\"f\":\"$(esc "$f")\"}"
        n=$((n + 1))
        [ "$n" -ge "$MAX" ] && break 3
      done < <(ssh-keygen -lf "$f" 2>/dev/null)
    done
  done < /etc/passwd
  printf '%s' "$out"
}

# Users who can log in (a real shell) and every uid 0 account whatever its shell
users() {
  local out="" n=0 user uid shell
  while IFS=: read -r user _ uid _ _ _ shell; do
    case "$shell" in */nologin|*/false|*/sync|*/halt|*/shutdown|'') [ "$uid" = 0 ] || continue ;; esac
    out="$out${out:+,}{\"n\":\"$(esc "$user")\",\"uid\":${uid//[!0-9]/},\"sh\":\"$(esc "$shell")\"}"
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
  printf '{"groups":{%s},"files":{%s}}' "$groups" "$files"
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

now=$(date +%s)
tmp=$(mktemp "${OUT}.XXXXXX")
printf '{"v":1,"ts":%s,"keys":[%s],"users":[%s],"sudo":%s,"cron":%s,"units":[%s]}\n' \
  "$now" "$(keys)" "$(users)" "$(sudo_info)" "$(cron_info)" "$(units)" > "$tmp"
chmod 0644 "$tmp"
mv -f "$tmp" "$OUT"
HELPER_EOF
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
echo "changes-setup: SSH keys, users, sudo, crontabs, enabled units -> $OUT (every 10 minutes)."
