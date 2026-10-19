#!/usr/bin/env bash
# Scenario test of backupserver-setup.sh (helper 0.34): a cleanup turned off by hand stays off.
# Runs the real helper in a scratch directory, as root (the helper chowns its files), with fake
# crontab, restic and docker. Usage: bsrv_prune_off.sh <repo root>. Prints PASS or the failures.
set -uo pipefail
ROOT="${1:?repo root}"
T="$(mktemp -d /tmp/kv-pruneoff.XXXXXX)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/cron.d" "$T/app/rest-server/data" "$T/app/rest-server/system/scripts" \
  "$T/app/rest-server/system/envs" "$T/var" "$T/ne"
: > "$T/etc-crontab"

# fakes: root crontab in a file, restic that knows nothing, docker that sees nothing
cat > "$T/bin/crontab" <<EOF
#!/usr/bin/env bash
case "\$*" in *-l*) [ -f "$T/crontab" ] && cat "$T/crontab" || exit 1 ;; *) cat > "$T/crontab" ;; esac
EOF
cat > "$T/bin/restic" <<'EOF'
#!/usr/bin/env bash
case "$*" in version*) echo "restic 0.19.1 compiled with go1.24 on linux/amd64" ;; *--json*) echo "[]" ;; esac
exit 0
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$T/bin/docker"
chmod 0755 "$T/bin/"*
export PATH="$T/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# the helper from the installer, with its paths moved into the scratch directory
sed -n '/^cat > "\$HELPER.new" <<.HELPER_EOF.$/,/^HELPER_EOF$/p' "$ROOT/agent/backupserver-setup.sh" | sed '1d;$d' \
  | sed -e "s#/app/rest-server#$T/app/rest-server#g" -e "s#/var/lib/kervax#$T/var#g" \
        -e "s#/var/lib/node_exporter/textfile_collector#$T/ne#g" -e "s#/etc/systemd-rest.conf#$T/systemd-rest.conf#g" \
        -e "s#/etc/cron.d#$T/cron.d#g" -e "s#/var/spool/cron/crontabs/root#$T/crontab#g" \
        -e "s#/etc/crontab#$T/etc-crontab#g" > "$T/helper"
chmod 0755 "$T/helper"
H="$T/helper"
D="$T/app/rest-server/data"
S="$T/app/rest-server/system/scripts"
fail=0
check() { if eval "$2"; then :; else echo "FAIL: $1"; fail=1; fi; }
repo() { mkdir -p "$D/$1/snapshots"; : > "$D/$1/config"; }
script() { repo "$1"; printf '#!/usr/bin/env bash\nKEEP=(--keep-last 3 --keep-daily 7 --keep-weekly 4 --keep-monthly 6)\n' > "$S/restic-prune-$1.sh"; chmod 0755 "$S/restic-prune-$1.sh"; }
active() { grep -vE '^[[:space:]]*(#|$)' "$T/cron.d/kervax-prune-$1" 2>/dev/null | grep -q "restic-prune-$1.sh"; }

# a: root crontab line commented out by hand, no cron.d (the frozen repository)
# b: active root crontab line from the ansible role
# c: cron.d from an older helper, active
# d: turned off with prune-off by an earlier run
# e: cron.d line commented out by hand, without the header
# f: a script nothing runs
# g: cron.d that an older helper turned on while the root crontab line is commented out by hand
for n in repo-a repo-b repo-c repo-d repo-e repo-f repo-g; do script "$n"; done
cat > "$T/crontab" <<EOF
#Ansible: restic-prune repo-a
#DISABLED 0 3 * * * $S/restic-prune-repo-a.sh
#Ansible: restic-prune repo-b
0 22 * * * $S/restic-prune-repo-b.sh
#DISABLED 0 4 * * * $S/restic-prune-repo-g.sh
EOF
echo "5 13 * * * root $S/restic-prune-repo-c.sh >/dev/null 2>&1" > "$T/cron.d/kervax-prune-repo-c"
printf '# kervax-prune-off: frozen for the audit\n# 5 13 * * * root %s >/dev/null 2>&1\n' "$S/restic-prune-repo-d.sh" > "$T/cron.d/kervax-prune-repo-d"
echo "#5 13 * * * root $S/restic-prune-repo-e.sh >/dev/null 2>&1" > "$T/cron.d/kervax-prune-repo-e"
echo "7 18 * * * root $S/restic-prune-repo-g.sh >/dev/null 2>&1" > "$T/cron.d/kervax-prune-repo-g"

out="$("$H" regen-prune 2>&1)"
"$H" dedup-cron >/dev/null 2>&1
check "regen says which stay off: $out" '[[ "$out" == *"cleanup left off: repo-a repo-d repo-e repo-f"* ]]'
check "a stays off" '! active repo-a && grep -q "^# kervax-prune-off: its cron line was commented out by hand" "$T/cron.d/kervax-prune-repo-a"'
check "b is moved and on" 'active repo-b && grep -q "^# kervax-moved to .*restic-prune-repo-b.sh" "$T/crontab"'
check "c stays on" 'active repo-c'
check "d keeps its reason" '! active repo-d && grep -q "^# kervax-prune-off: frozen for the audit" "$T/cron.d/kervax-prune-repo-d"'
check "e stays off" '! active repo-e && grep -q "^# kervax-prune-off: its cron.d line was commented out by hand" "$T/cron.d/kervax-prune-repo-e"'
check "f stays off" '! active repo-f && grep -q "^# kervax-prune-off: nothing ran this script" "$T/cron.d/kervax-prune-repo-f"'
check "g is left as it is" 'active repo-g'
check "hand-commented lines are kept" 'grep -q "^#DISABLED 0 3 .*repo-a.sh" "$T/crontab" && grep -q "^#DISABLED 0 4 .*repo-g.sh" "$T/crontab"'
check "the off header says how to turn it on" 'grep -q "^# to turn it on: .* prune-on repo-a" "$T/cron.d/kervax-prune-repo-a"'

field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["repos"][sys.argv[2]][sys.argv[3]])' "$T/var/report.d/backup-server.json" "$1" "$2"; }
"$H" stats > /dev/null
check "stats: a off" '[ "$(field repo-a prune_off)" = 1 ] && [ "$(field repo-a prune_off_ts)" -gt 0 ]'
check "stats: d off with its reason" '[ "$(field repo-d prune_off)" = 1 ] && [ "$(field repo-d prune_off_why)" = "frozen for the audit" ]'
check "stats: b, c on" '[ "$(field repo-b prune_off)" = 0 ] && [ "$(field repo-c prune_off)" = 0 ] && [ "$(field repo-c prune_hand_off)" = 0 ]'
check "stats: g on while turned off by hand" '[ "$(field repo-g prune_off)" = 0 ] && [ "$(field repo-g prune_hand_off)" = 1 ]'

# prune-off / prune-on
"$H" prune-off repo-g > /dev/null
check "prune-off g" '! active repo-g && grep -q "^# kervax-prune-off: turned off by hand .* with prune-off" "$T/cron.d/kervax-prune-repo-g"'
check "stats: g off, no longer a question" '[ "$(field repo-g prune_off)" = 1 ] && [ "$(field repo-g prune_hand_off)" = 0 ]'
"$H" prune-off repo-b > /dev/null
check "prune-off b" '! active repo-b'
on="$("$H" prune-on repo-a)"
check "prune-on a: $on" 'active repo-a && grep -q "^# kervax-prune-on" "$T/cron.d/kervax-prune-repo-a" && [[ "$on" == *"every night"* ]]'
"$H" regen-prune > /dev/null
check "after regen a stays on, g and b stay off" 'active repo-a && ! active repo-g && ! active repo-b'
"$H" stats > /dev/null
check "stats: a confirmed by prune-on" '[ "$(field repo-a prune_off)" = 0 ] && [ "$(field repo-a prune_hand_off)" = 0 ]'
check "prune-on refuses a name without a script" '! "$H" prune-on no-such-repo 2>/dev/null'
check "prune-off refuses a bad name" '! "$H" prune-off "../x" 2>/dev/null'

# adopt-legacy: the shared script does not run, so the moved repository keeps its cleanup off
legacy_block() {
  printf '############################################%s\nset -a\nRESTIC_PASSWORD=secret-%s\nRESTIC_REPOSITORY=%s/%s\nrestic check\nrestic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6\nrestic prune\n' "$1" "$1" "$D" "$1"
}
repo repo-m
legacy_block repo-m > "$T/systemd-rest.conf"
echo "#DISABLED 30 16 * * * /etc/systemd-rest.conf 2>&1 | tee /tmp/clean.txt" >> "$T/crontab"
out="$("$H" adopt-legacy --apply repo-m 2>&1)"
check "adopt with the shared script off: $out" '[ -f "$S/restic-prune-repo-m.sh" ] && ! active repo-m && grep -q "^# kervax-prune-off: moved off" "$T/cron.d/kervax-prune-repo-m"'
check "adopt says the cleanup stays off" '[[ "$out" == *"cleanup off as before"* ]]'
check "the env holds the password" 'grep -q "secret-repo-m" "$T/app/rest-server/system/envs/repo-m.env"'
# and with the shared script running from cron, the moved one is on
repo repo-n
legacy_block repo-n > "$T/systemd-rest.conf"
echo "30 16 * * * /etc/systemd-rest.conf 2>&1 | tee /tmp/clean.txt" >> "$T/crontab"
out="$("$H" adopt-legacy --apply repo-n 2>&1)"
check "adopt with the shared script on: $out" 'active repo-n && [[ "$out" == *"every night"* ]]'

[ "$fail" -eq 0 ] && echo PASS
exit "$fail"
