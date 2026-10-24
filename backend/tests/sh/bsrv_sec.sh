#!/usr/bin/env bash
# Scenario test of backupserver-setup.sh (helper 0.36): the root side keeps its boundary. Through
# the real spool: answers cannot be redirected by a symlink, repopass, hpass and the repository name
# are checked before they reach the prune env and htpasswd, hpass stays out of argv, the weekly
# check state cannot be planted, the prune metrics do not follow a symlink, a bad DNS name stays
# out of the certificate. Runs the real helper in a scratch directory, as root, with fake htpasswd,
# curl, restic, docker and ufw. Usage: bsrv_sec.sh <repo root>. Prints PASS or the failures.
set -uo pipefail
ROOT="${1:?repo root}"
T="$(mktemp -d /tmp/kv-bsrvsec.XXXXXX)"
chmod 0755 "$T"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/cron.d" "$T/app/rest-server/data" "$T/app/rest-server/system/scripts" \
  "$T/app/rest-server/system/envs" "$T/var/bsrv-req" "$T/var/bsrv-res" "$T/ne" "$T/vartmp"
chmod 0750 "$T/var/bsrv-res"
: > "$T/etc-crontab"

# fakes: each logs its argv (and htpasswd/curl their stdin) so the test sees where the password went
cat > "$T/bin/htpasswd" <<EOF
#!/usr/bin/env bash
echo "argv: \$*" >> "$T/htpasswd.log"; echo "stdin: \$(cat)" >> "$T/htpasswd.log"
EOF
cat > "$T/bin/curl" <<EOF
#!/usr/bin/env bash
echo "argv: \$*" >> "$T/curl.log"
case "\$*" in *"-K -"*) echo "stdin: \$(cat)" >> "$T/curl.log" ;; esac
printf 200
EOF
cat > "$T/bin/restic" <<'EOF'
#!/usr/bin/env bash
case "$*" in version*) echo "restic 0.19.1 compiled with go1.24 on linux/amd64" ;; *--json*) echo "[]" ;; esac
exit 0
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$T/bin/docker"
printf '#!/usr/bin/env bash\necho "$*" >> %s/ufw.log\nexit 0\n' "$T" > "$T/bin/ufw"
printf '#!/usr/bin/env bash\nexit 1\n' > "$T/bin/crontab"
chmod 0755 "$T/bin/"*
export PATH="$T/bin:/usr/sbin:/usr/bin:/sbin:/bin"

sed -n '/^cat > "\$HELPER.new" <<.HELPER_EOF.$/,/^HELPER_EOF$/p' "$ROOT/agent/backupserver-setup.sh" | sed '1d;$d' \
  | sed -e "s#/app/rest-server#$T/app/rest-server#g" -e "s#/var/lib/kervax#$T/var#g" \
        -e "s#/var/lib/node_exporter/textfile_collector#$T/ne#g" -e "s#/etc/systemd-rest.conf#$T/systemd-rest.conf#g" \
        -e "s#/etc/cron.d#$T/cron.d#g" -e "s#/var/spool/cron/crontabs/root#$T/crontab#g" \
        -e "s#/etc/crontab#$T/etc-crontab#g" -e "s#/var/tmp/restic-check#$T/vartmp/restic-check#g" > "$T/helper"
chmod 0755 "$T/helper"
H="$T/helper"
D="$T/app/rest-server/data"
fail=0
check() { if eval "$2"; then :; else echo "FAIL: $1"; fail=1; fi; }
n=0
spool() {
  n=$((n+1)); printf '%s\n' "$@" > "$T/var/bsrv-req/r$n.req"
  "$H" process-spool >/dev/null 2>&1
  ok="$(sed -n 's/^ok=//p' "$T/var/bsrv-res/r$n.res" 2>/dev/null)"
  out="$(sed -n 's/^output=//p' "$T/var/bsrv-res/r$n.res" 2>/dev/null | tr '\r' ' ')"
}
prov() { spool action=provision_client "$@" client_ip=203.0.113.5 keep_last=3 keep_daily=7 keep_weekly=4 keep_monthly=6; }

# 1) bad names and secrets are refused before htpasswd, the env or the repository
for bad in "name=.." "name=.htpasswd" "name=-rf" "name=a/b"; do
  prov "$bad" hpass=fake-token-example repopass=fakepass
  check "refused $bad" '[ "$ok" = false ] && [ ! -e "$T/htpasswd.log" ]'
done
prov name=node-a hpass=fake:pass1 repopass=fakepass
check "an hpass with : is refused" '[ "$ok" = false ] && [ ! -e "$T/htpasswd.log" ]'
prov name=node-a hpass=fake-token-example "repopass=a\";touch $T/pwned;\""
check "a quote in repopass is refused" '[ "$ok" = false ] && [ ! -e "$T/app/rest-server/system/envs/node-a.env" ] && [ ! -e "$T/pwned" ]'

# 2) a good client: hpass on stdin only, the env holds the password, ufw gets the actual port
prov name=node-a hpass=fake-token-example repopass=fakepass
check "provisioning works ($out)" '[ "$ok" = true ]'
check "hpass is not in htpasswd argv" '! grep "^argv:" "$T/htpasswd.log" | grep -q fake-token-example && grep -q "^argv: -iB -C 10 .* node-a$" "$T/htpasswd.log"'
check "hpass reaches htpasswd on stdin" 'grep -qx "stdin: fake-token-example" "$T/htpasswd.log"'
check "hpass is not in curl argv" '! grep "^argv:" "$T/curl.log" | grep -q fake-token-example && grep -q "^stdin: user = \"node-a:fake-token-example\"" "$T/curl.log"'
check "the prune env holds the password" 'grep -qx "RESTIC_PASSWORD=\"fakepass\"" "$T/app/rest-server/system/envs/node-a.env"'
check "ufw opens the rest port" 'grep -q "port 64100" "$T/ufw.log"'
PS="$T/app/rest-server/system/scripts/restic-prune-node-a.sh"
check "the prune script keeps its check state out of /var/tmp" 'grep -q "^CHECK_STATE=\"$T/var/bsrv-check/" "$PS"'

# 3) the weekly check state: a file someone else planted is neither imported nor evaluated
mkdir -p "$D/node-a"; : > "$D/node-a/config"
printf 'a[$(touch${IFS}%s/pwned2)] 1\n' "$T" > "$T/vartmp/restic-check-node-a.state"; chown nobody "$T/vartmp/restic-check-node-a.state"
bash "$PS"
check "a planted /var/tmp state is not imported" '[ ! -e "$T/pwned2" ] && ! grep -q pwned2 "$T/var/bsrv-check/node-a.state"'
printf 'a[$(touch${IFS}%s/pwned3)] 1\n' "$T" > "$T/var/bsrv-check/node-a.state"
bash "$PS"
check "a state that is not numbers is ignored" '[ ! -e "$T/pwned3" ] && grep -qE "^[0-9]+ " "$T/var/bsrv-check/node-a.state"'
# a state root wrote before 0.36 moves over once
rm -f "$T/var/bsrv-check/node-b.state"
sed "s/node-a/node-b/g" "$PS" > "$T/prune-b.sh"
sed "s/node-a/node-b/g" "$T/app/rest-server/system/envs/node-a.env" > "$T/app/rest-server/system/envs/node-b.env"
printf '%s 1 3 8\n' "$(date +%s)" > "$T/vartmp/restic-check-node-b.state"
mkdir -p "$D/node-b"; : > "$D/node-b/config"
bash "$T/prune-b.sh"
check "root's old state is taken over" 'grep -q " 1 3 8$" "$T/var/bsrv-check/node-b.state"'

# 4) the prune metrics: a symlink left in the node_exporter directory is not followed
echo victim > "$T/victim"
ln -s "$T/victim" "$T/ne/restic_server_node-a.prom.partial"
ln -sf "$T/victim" "$T/ne/restic_server_node-a.prom"
bash "$PS"
check "the metrics do not follow a symlink" '[ "$(cat "$T/victim")" = victim ] && [ ! -L "$T/ne/restic_server_node-a.prom" ] && grep -q "^restic_server_prune_success" "$T/ne/restic_server_node-a.prom"'
check "the metrics are 0644" '[ "$(stat -c %a "$T/ne/restic_server_node-a.prom")" = 644 ]'
check "no temporary metrics are left" '[ -z "$(ls "$T/var/bsrv-check" | grep metrics)" ]'
# the node_exporter directory is not chmod'ed when it exists (backup-setup makes it 1775)
chmod 1775 "$T/ne"
spool action=provision_client name=node-c hpass=fake-token-example repopass=fakepass client_ip=203.0.113.6
check "provisioning leaves the metrics directory mode" '[ "$ok" = true ] && [ "$(stat -c %a "$T/ne")" = 1775 ]'

# 5) the spool: a planted symlink in place of the answer is replaced, a symlinked request dropped
n=$((n+1)); ln -s "$T/victim" "$T/var/bsrv-res/r$n.res"; printf 'action=nothing\n' > "$T/var/bsrv-req/r$n.req"
"$H" process-spool >/dev/null 2>&1
check "a symlink in place of the answer is replaced" '[ "$(cat "$T/victim")" = victim ] && [ ! -L "$T/var/bsrv-res/r$n.res" ] && [ "$(stat -c %a "$T/var/bsrv-res/r$n.res")" = 644 ]'
ln -s /etc/hostname "$T/var/bsrv-req/sym.req"
"$H" process-spool >/dev/null 2>&1
check "a symlinked request is dropped" '[ ! -L "$T/var/bsrv-req/sym.req" ] && [ ! -e "$T/var/bsrv-res/sym.res" ]'

# 6) the TLS front: a DNS name with a comma does not add SANs of its own
if command -v openssl >/dev/null 2>&1; then
  # (TLS_DIR /app/rest-server-tls is already moved into the scratch directory with /app/rest-server)
  spool action=deploy_tls_front san_ip=203.0.113.9 "san_dns=backup.example.com,IP:198.51.100.66"
  check "the bad DNS name is left out ($out)" '[ "$ok" = true ] && [[ "$out" == *"left out"* ]] && ! openssl x509 -in "$T/app/rest-server-tls/cert.pem" -noout -text | grep -q 198.51.100.66'
fi

[ "$fail" -eq 0 ] && echo PASS
exit "$fail"
