#!/usr/bin/env bash
# Scenario test of backup-setup.sh (helper 0.40): to-https moves the backup from rest:http to the
# TLS rest-server only after the repository is read over https, a failed check changes nothing.
# Runs the real helper in a scratch directory, as root (the helper chowns its files), with fake
# restic and systemctl. Usage: backup_to_https.sh <repo root>. Prints PASS or the failures.
set -uo pipefail
ROOT="${1:?repo root}"
T="$(mktemp -d /tmp/kv-tohttps.XXXXXX)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/restic" "$T/var" "$T/ne"

# fakes: restic logs its call and fails when the marker file is there; systemctl knows nothing
cat > "$T/restic/restic" <<EOF
#!/usr/bin/env bash
echo "\$RESTIC_REPOSITORY \$*" >> "$T/restic.log"
[ -f "$T/restic-fail" ] && { echo "Fatal: unable to open config file: Get \"https://node-a:fakepass@203.0.113.5:64101/node-a/config\": x509: certificate is valid for 198.51.100.9, not 203.0.113.5" >&2; exit 1; }
echo '[{"short_id":"aaaa1111"},{"short_id":"bbbb2222"}]'
EOF
printf '#!/usr/bin/env bash\nexit 0\n' > "$T/bin/systemctl"
chmod 0755 "$T/bin/"* "$T/restic/restic"
export PATH="$T/bin:/usr/sbin:/usr/bin:/sbin:/bin"

sed -n '/^cat > "\$HELPER.new" <<.HELPER_EOF.$/,/^HELPER_EOF$/p' "$ROOT/agent/backup-setup.sh" | sed '1d;$d' \
  | sed -e "s#/usr/local/lib/.restic#$T/restic#g" -e "s#/etc/systemd-resta.conf#$T/env#g" \
        -e "s#/etc/systemd-rest.conf#$T/runner#g" -e "s#/var/lib/kervax#$T/var#g" \
        -e "s#/var/lib/node_exporter/textfile_collector#$T/ne#g" > "$T/helper"
chmod 0755 "$T/helper"
H="$T/helper"
fail=0
check() { if eval "$2"; then :; else echo "FAIL: $1"; fail=1; fi; }

printf 'RESTIC_PASSWORD=repo-pass\nRESTIC_REPOSITORY=rest:http://node-a:fakepass@203.0.113.5:64100/node-a/\n' > "$T/env"
chmod 0600 "$T/env"
printf '#!/usr/bin/env bash\nset -euo pipefail\nRESTIC=%s/restic/restic\nENV_FILE=x\n' "$T" > "$T/runner"
CA="$(printf -- '-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n' | base64 -w0)"

# 0) bad input, nothing changes
check "bad port refused" '! "$H" to-https 64x "$CA" >/dev/null 2>&1'
check "bad certificate refused" '! "$H" to-https 64101 "$(printf notacert | base64 -w0)" >/dev/null 2>&1'
check "env untouched after bad input" 'grep -q "^RESTIC_REPOSITORY=rest:http://" "$T/env"'

# 1) the check fails: env, runner and certificate stay as they were, the error has no password
touch "$T/restic-fail"
out="$("$H" to-https 64101 "$CA" 2>&1)"; rc=$?
check "failed check returns an error" '[ $rc -ne 0 ]'
check "failed check says the backup stays on http" 'printf "%s" "$out" | grep -q "stays on http"'
check "the error carries no password" '! printf "%s" "$out" | grep -q fakepass'
check "env untouched after a failed check" 'grep -q "^RESTIC_REPOSITORY=rest:http://node-a:fakepass@203.0.113.5:64100/node-a/$" "$T/env"'
check "no certificate after a failed check" '[ ! -f "$T/restic/backup-ca.pem" ] && [ -z "$(ls "$T/restic" | grep backup-ca)" ]'
check "runner untouched after a failed check" '! grep -q -- --cacert "$T/runner"'

# 2) the check works: https address with the same login, host and repository, the TLS port
rm -f "$T/restic-fail" "$T/restic.log"
out="$("$H" to-https 64101 "$CA" 2>&1)"; rc=$?
check "switch succeeds ($out)" '[ $rc -eq 0 ]'
check "the check ran against the https address with --cacert" \
  'grep -q "^rest:https://node-a:fakepass@203.0.113.5:64101/node-a/ --cacert .* snapshots" "$T/restic.log"'
check "env switched" 'grep -q "^RESTIC_REPOSITORY=rest:https://node-a:fakepass@203.0.113.5:64101/node-a/$" "$T/env"'
check "password line kept" 'grep -q "^RESTIC_PASSWORD=repo-pass$" "$T/env"'
check "env stays 0600" '[ "$(stat -c %a "$T/env")" = 600 ]'
check "previous env kept" 'grep -q "rest:http://" "$T/env.http-bak"'
check "certificate in place" 'grep -q "BEGIN CERTIFICATE" "$T/restic/backup-ca.pem"'
check "runner uses --cacert" 'grep -q "^RESTIC=.*--cacert" "$T/runner"'
check "report counts snapshots" 'printf "%s" "$out" | grep -q "snapshots read over https: 2"'

# 3) already https: nothing to do
out="$("$H" to-https 64101 "$CA" 2>&1)"; rc=$?
check "second run is a no-op" '[ $rc -eq 0 ] && printf "%s" "$out" | grep -q "already https"'

[ "$fail" -eq 0 ] && echo PASS
exit "$fail"
