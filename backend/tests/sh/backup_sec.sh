#!/usr/bin/env bash
# Scenario test of backup-setup.sh (helper 0.43): the root side keeps its boundary against the agent
# and against the backup user. Through the real spool: answers cannot be redirected by a symlink,
# repopass and repo_url are checked before they reach the env, provisioning goes only to the backup
# server the node already uses or one in backup-allow, the restic directory goes back to root, the
# backup user keeps writing its own metrics, dumps stay out of system and foreign directories.
# Runs the real helper in a scratch directory under /srv (dump directories under /tmp are refused
# by design), as root, with a throwaway system user in place of bx231 and fake restic and systemctl.
# Usage: backup_sec.sh <repo root>. Prints PASS or the failures.
set -uo pipefail
ROOT="${1:?repo root}"
U=kvtst231
T="$(mktemp -d /srv/kv-sec.XXXXXX)"
chmod 0755 "$T"
# the system paths the test hands the helper do not exist: a regression creates (and the trap
# removes) a directory of its own there instead of chmod'ing a real one
SYS1="/var/lib/kv-sec-test-$$"
SYS2="/etc/kv-sec-test-$$"
cleanup() { userdel "$U" >/dev/null 2>&1; rm -rf "$T" "$SYS1" "$SYS2"; }
trap cleanup EXIT
mkdir -p "$T/bin" "$T/restic" "$T/var/backup-req" "$T/var/backup-res" "$T/ne" "$T/systemd" "$T/lib65"
chmod 0750 "$T/var/backup-res"
id -u "$U" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir "$T/home" --shell /usr/sbin/nologin "$U"

cat > "$T/restic/restic" <<EOF
#!/usr/bin/env bash
echo "\$(id -un) \$RESTIC_REPOSITORY pass=\$RESTIC_PASSWORD \$*" >> "$T/restic.log"
case "\$*" in *--help*) echo "  --retry-lock duration" ;; *) echo "restic 0.19.1 compiled with go1.24 on linux/amd64" ;; esac
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$T/bin/systemctl"
chmod 0755 "$T/bin/systemctl" "$T/restic/restic"

# the helper from the installer, paths moved into the scratch directory; the dump probe always
# passes (no database here) and systemctl is the fake (the helper puts the system PATH first)
sed -n '/^cat > "\$HELPER.new" <<.HELPER_EOF.$/,/^HELPER_EOF$/p' "$ROOT/agent/backup-setup.sh" | sed '1d;$d' \
  | sed -e "s#/usr/local/lib/.restic#$T/restic#g" -e "s#/etc/systemd-resta.conf#$T/env#g" \
        -e "s#/etc/systemd-rest.conf#$T/runner#g" -e "s#/var/lib/kervax#$T/var#g" \
        -e "s#/var/lib/node_exporter/textfile_collector#$T/ne#g" -e "s#/etc/systemd/system#$T/systemd#g" \
        -e "s#/etc/kervax#$T/etckv#g" -e "s#/lib65/kervax#$T/lib65#g" -e "s#^DUMP_DIR_BASE=/backup\$#DUMP_DIR_BASE=$T/backup#" \
        -e "s#/var/cache/bx231#$T/home#g" -e "s#bx231#$U#g" -e "s#\\bsystemctl #$T/bin/systemctl #g" \
  | sed "/^case \"\${1:-}\" in\$/i dump_probe() { return 0; }" > "$T/helper"
chmod 0755 "$T/helper"
H="$T/helper"
fail=0
check() { if eval "$2"; then :; else echo "FAIL: $1"; fail=1; fi; }
n=0
# one request through the spool; the answer's output goes to $out, ok to $ok
spool() {
  n=$((n+1)); printf '%s\n' "$@" > "$T/var/backup-req/r$n.req"
  "$H" process-spool >/dev/null 2>&1
  ok="$(sed -n 's/^ok=//p' "$T/var/backup-res/r$n.res" 2>/dev/null)"
  out="$(sed -n 's/^output=//p' "$T/var/backup-res/r$n.res" 2>/dev/null)"
}
URL_A="rest:http://node-a:fake-token-example@203.0.113.5:64100/node-a"
URL_B="rest:https://node-a:fake-token-example@198.51.100.9:64101/node-a"

# the layout 0.42 left: bx231 owns the restic directory, the binary and the metrics directory,
# and root wrote the metrics file once
chown -R "$U:$U" "$T/restic" "$T/ne"; chmod 0755 "$T/ne"
echo 'restic_last_backup_success{host="x"} 1' > "$T/ne/restic-backup.prom"
echo 'other 1' > "$T/ne/other.prom"

# 1) values the env file cannot hold are refused before anything is written
spool action=provision "repo_url=$URL_A" "repopass=a\";touch $T/pwned;\"" mode=exclude schedule=03:00 path=/proc
check "a quote in repopass is refused" '[ "$ok" = false ] && [ ! -e "$T/env" ] && [ ! -e "$T/pwned" ]'
spool action=provision "repo_url=$URL_A;id" repopass=fakepass mode=exclude schedule=03:00 path=/proc
check "a ; in repo_url is refused" '[ "$ok" = false ] && [ ! -e "$T/env" ]'
spool action=provision "repo_url=$URL_A" 'repopass=fake$(id)pass' mode=exclude schedule=03:00 path=/proc
check "a \$ in repopass is refused" '[ "$ok" = false ] && [ ! -e "$T/env" ]'

# 2) a good provisioning: env as is, the server recorded, root owns restic, bx231 keeps its metrics
spool action=provision "repo_url=$URL_A" repopass=fakepass mode=exclude schedule=03:00 path=/proc
check "provisioning works ($out)" '[ "$ok" = true ]'
check "env holds the values" 'grep -qx "RESTIC_PASSWORD=fakepass" "$T/env" && grep -qx "RESTIC_REPOSITORY=$URL_A" "$T/env"'
check "the backup server is recorded" 'grep -qx "203.0.113.5" "$T/etckv/backup-allow" && [ "$(stat -c %a "$T/etckv/backup-allow")" = 644 ]'
check "the restic directory and binary are root's" '[ "$(stat -c %U "$T/restic")" = root ] && [ "$(stat -c %U "$T/restic/restic")" = root ]'
check "bx231 cannot write the restic directory" '! runuser -u "$U" -- sh -c "echo x > $T/restic/evil" 2>/dev/null'
check "the metrics directory is root:bx231 1775" '[ "$(stat -c "%U:%G %a" "$T/ne")" = "root:$U 1775" ]'
check "root's old metrics file went to bx231" '[ "$(stat -c %U "$T/ne/restic-backup.prom")" = "$U" ]'
# the runner writes its metrics through a temporary file and mv (panel and ansible runners alike)
check "bx231 still writes its metrics" 'runuser -u "$U" -- sh -c "echo ok > $T/ne/restic-backup.prom.1.tmp && mv -f $T/ne/restic-backup.prom.1.tmp $T/ne/restic-backup.prom"'
check "bx231 cannot replace a root file there" '! runuser -u "$U" -- sh -c "echo x > $T/ne/t.tmp && mv -f $T/ne/t.tmp $T/ne/other.prom" 2>/dev/null && grep -q "other 1" "$T/ne/other.prom"'
# the backup itself, the way the service runs it: its user with CAP_DAC_READ_SEARCH, the env file
# read by systemd and sourced by the runner again
install -d -o "$U" -g "$U" -m 0755 "$T/home"
run_backup() {
  : > "$T/restic.log"; chmod 0666 "$T/restic.log"   # the fake restic logs as the backup user
  timeout 120 systemd-run --wait --pipe --collect -q -p "User=$U" -p AmbientCapabilities=CAP_DAC_READ_SEARCH \
    -p "EnvironmentFile=$T/env" -- "$T/runner" >/dev/null 2>&1
}
run_backup; rc=$?
check "http: the backup runs as the backup user (rc=$rc)" '[ $rc -eq 0 ] && grep -q "^$U rest:http://.* pass=fakepass backup --retry-lock 2h /" "$T/restic.log"'
check "http: the run writes its metrics" 'grep -q "^restic_last_backup_success{host=\".*\"} 1$" "$T/ne/restic-backup.prom" && [ "$(stat -c %U "$T/ne/restic-backup.prom")" = "$U" ]'

# 3) another backup server: refused while it is not in backup-allow, accepted once root adds it
spool action=provision "repo_url=$URL_B" repopass=fakepass mode=exclude schedule=03:00 path=/proc
check "an unlisted server is refused ($out)" '[ "$ok" = false ] && grep -qx "RESTIC_REPOSITORY=$URL_A" "$T/env" && [[ "$out" == *"198.51.100.9"* ]]'
echo 198.51.100.9 >> "$T/etckv/backup-allow"
CA="$(printf -- '-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n' | base64 -w0)"
spool action=provision "repo_url=$URL_B" repopass=fakepass mode=exclude schedule=03:00 path=/proc "cacert_b64=$CA"
check "a listed server is accepted ($out)" '[ "$ok" = true ] && grep -qx "RESTIC_REPOSITORY=$URL_B" "$T/env"'
check "https: the certificate is root's and readable" '[ "$(stat -c "%U %a" "$T/restic/backup-ca.pem")" = "root 644" ]'
run_backup; rc=$?
check "https: the backup runs with the certificate (rc=$rc)" '[ $rc -eq 0 ] && grep -q "^$U rest:https://.* --cacert $T/restic/backup-ca.pem backup " "$T/restic.log"'
# RESTIC_CACERT in the env (to-https since 0.42) works for the run as well
echo "RESTIC_CACERT=$T/restic/backup-ca.pem" >> "$T/env"
run_backup; rc=$?
check "https: an env with RESTIC_CACERT still runs (rc=$rc)" '[ $rc -eq 0 ]'
# without the file the node stays on the server it backs up to
rm -f "$T/etckv/backup-allow"
spool action=provision "repo_url=$URL_A" repopass=fakepass mode=exclude schedule=03:00 path=/proc
check "no file: moving to another server is refused ($out)" '[ "$ok" = false ] && grep -qx "RESTIC_REPOSITORY=$URL_B" "$T/env" && [ ! -e "$T/etckv/backup-allow" ]'
spool action=provision "repo_url=$URL_B" repopass=fakepass mode=include schedule=04:00 path=/etc
check "no file: the same server is fine and gets recorded" '[ "$ok" = true ] && grep -qx "198.51.100.9" "$T/etckv/backup-allow"'

# 4) the spool: answers through mktemp, a planted symlink is replaced, not followed
echo victim > "$T/victim"
n=$((n+1)); ln -s "$T/victim" "$T/var/backup-res/r$n.res"; printf 'action=nothing\n' > "$T/var/backup-req/r$n.req"
"$H" process-spool >/dev/null 2>&1
check "a symlink in place of the answer is replaced" '[ "$(cat "$T/victim")" = victim ] && [ ! -L "$T/var/backup-res/r$n.res" ] && grep -q "^ok=false" "$T/var/backup-res/r$n.res"'
check "the answer is 0644" '[ "$(stat -c %a "$T/var/backup-res/r$n.res")" = 644 ]'
ln -s /etc/hostname "$T/var/backup-req/sym.req"
"$H" process-spool >/dev/null 2>&1
check "a symlinked request is dropped unanswered" '[ ! -e "$T/var/backup-req/sym.req" ] && [ ! -L "$T/var/backup-req/sym.req" ] && [ ! -e "$T/var/backup-res/sym.res" ]'
touch -d '20 minutes ago' "$T/var/backup-res/r1.res"
"$H" process-spool >/dev/null 2>&1
check "an answer nobody took is removed after 10 minutes" '[ ! -e "$T/var/backup-res/r1.res" ] && [ -e "$T/var/backup-res/r$n.res" ]'

# 5) dumps: no system directory, no directory someone else controls, never chmod of the chosen one
spool action=dump_setup engine=redis container=c1 "dump_dir=$SYS1" dump_minfree=10
check "a system directory is refused ($out)" '[ "$ok" = false ] && [[ "$out" == *"system directory"* ]] && [ ! -e "$T/lib65/dumps.d/redis@c1.sh" ] && [ ! -e "$SYS1" ]'
spool action=dump_setup engine=redis container=c1 "dump_dir=$SYS2" dump_minfree=10
check "a path under /etc is refused" '[ "$ok" = false ] && [ ! -e "$SYS2" ]'
spool action=dump_setup engine=redis container=c1 "dump_dir=$T/data" dump_minfree=10
check "a new directory gets its kervax-dumps subdirectory ($out)" '[ "$ok" = true ] && [ -d "$T/data/kervax-dumps/redis/c1" ] && grep -qx "dir=$T/data/kervax-dumps/redis/c1" "$T/lib65/dumps.d/redis@c1.meta"'
check "the chosen directory is not 0700" '[ "$(stat -c %a "$T/data")" = 755 ]'
mkdir -p "$T/shared"; chmod 0751 "$T/shared"; echo keep > "$T/shared/f"
spool action=dump_setup engine=redis container=c2 "dump_dir=$T/shared" dump_minfree=10
check "an existing directory keeps its mode and files" '[ "$ok" = true ] && [ "$(stat -c %a "$T/shared")" = 751 ] && [ "$(cat "$T/shared/f")" = keep ] && [ -d "$T/shared/kervax-dumps/redis/c2" ]'
mkdir -p "$T/user"; chown nobody "$T/user"
spool action=dump_setup engine=redis container=c3 "dump_dir=$T/user/dumps" dump_minfree=10
check "a path through someone else's directory is refused ($out)" '[ "$ok" = false ] && [ ! -e "$T/user/dumps" ]'
mkdir -p "$T/open"; chmod 0777 "$T/open"
spool action=dump_setup engine=redis container=c3 "dump_dir=$T/open/dumps" dump_minfree=10
check "a path through a world-writable directory is refused" '[ "$ok" = false ]'
# KV_DUMP_* of one request do not reach the next one of the same pass
printf 'action=dump_setup\nengine=redis\ncontainer=c4\ndump_dir=%s/data\ndump_minfree=10\n' "$T" > "$T/var/backup-req/x1.req"
printf 'action=dump_setup\nengine=redis\ncontainer=c5\ndump_minfree=10\n' > "$T/var/backup-req/x2.req"
"$H" process-spool >/dev/null 2>&1
check "the second request of a pass gets the default directory" 'grep -qx "dir=$T/backup/redis/c5" "$T/lib65/dumps.d/redis@c5.meta" && grep -qx "dir=$T/data/kervax-dumps/redis/c4" "$T/lib65/dumps.d/redis@c4.meta"'
# a slot already in the old layout <dir>/<engine>/<container> stays where it is
mkdir -p "$T/old/redis/c6"; printf '#!/bin/sh\n' > "$T/lib65/dumps.d/redis@c6.sh"
printf 'dir=%s/old/redis/c6\nkeep=2\nminfree=10\n' "$T" > "$T/lib65/dumps.d/redis@c6.meta"
spool action=dump_setup engine=redis container=c6 "dump_dir=$T/old" dump_minfree=10
check "an old-layout slot keeps its directory" '[ "$ok" = true ] && grep -qx "dir=$T/old/redis/c6" "$T/lib65/dumps.d/redis@c6.meta"'

# 6) dump-remove removes only what the helper laid out
spool action=dump_remove engine=redis container=c1
check "dump-remove removes the slot directory only" '[ "$ok" = true ] && [ ! -e "$T/data/kervax-dumps/redis/c1" ] && [ -d "$T/data" ]'
mkdir -p "$T/victim2"; echo keep > "$T/victim2/f"; printf '#!/bin/sh\n' > "$T/lib65/dumps.d/redis@c9.sh"
printf 'dir=%s/victim2\n' "$T" > "$T/lib65/dumps.d/redis@c9.meta"
spool action=dump_remove engine=redis container=c9
check "a .meta pointing elsewhere removes nothing ($out)" '[ "$ok" = true ] && [ "$(cat "$T/victim2/f")" = keep ] && [ ! -e "$T/lib65/dumps.d/redis@c9.sh" ]'
printf '#!/bin/sh\n' > "$T/lib65/dumps.d/mysql@c9.sh"; mkdir -p "$SYS1/mysql/c9"; echo keep > "$SYS1/mysql/c9/f"
printf 'dir=%s/mysql/c9\n' "$SYS1" > "$T/lib65/dumps.d/mysql@c9.meta"
spool action=dump_remove engine=mysql container=c9
check "a .meta under /var/lib removes nothing" '[ "$ok" = true ] && [[ "$out" == *"left in place"* ]] && [ -f "$SYS1/mysql/c9/f" ]'

[ "$fail" -eq 0 ] && echo PASS
exit "$fail"
