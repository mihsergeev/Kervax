#!/usr/bin/env bash
# Scenario test of kubeexpiry-setup.sh (helper 0.3): TLS secrets get a certificate fingerprint and
# a mark whether some Ingress points at them (spec.tls[].secretName); when the Ingress read fails
# the mark is left out ("unknown", not "unused"). Runs the real helper in a scratch directory with
# a fake kubectl. Usage: kube_expiry_ingress.sh <repo root>. Prints PASS or the failures.
set -uo pipefail
ROOT="${1:?repo root}"
T="$(mktemp -d /tmp/kv-kexp.XXXXXX)"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/var"

cert() { # name -> base64 of a fresh self-signed certificate
  openssl req -x509 -newkey rsa:2048 -nodes -days "$2" -subj "/CN=$1" \
    -keyout /dev/null -out "$T/$1.crt" >/dev/null 2>&1
  base64 -w0 < "$T/$1.crt"
}
A=$(cert used-ssl 30)
B=$(cert old-ssl 1)
cat > "$T/secrets.json" <<EOF
{"items":[
 {"type":"kubernetes.io/tls","metadata":{"namespace":"default","name":"used-ssl"},"data":{"tls.crt":"$A"}},
 {"type":"kubernetes.io/tls","metadata":{"namespace":"default","name":"old-ssl"},"data":{"tls.crt":"$B"}},
 {"type":"Opaque","metadata":{"namespace":"default","name":"other"},"data":{}}
]}
EOF
cat > "$T/ingresses.json" <<'EOF'
{"items":[
 {"metadata":{"namespace":"default","name":"web"},"spec":{"tls":[{"hosts":["a.example.com"],"secretName":"used-ssl"}]}},
 {"metadata":{"namespace":"other","name":"web"},"spec":{"tls":[{"secretName":"old-ssl"}]}}
]}
EOF
# fake kubectl: kubectl --kubeconfig <f> get <what> -A -o json
cat > "$T/bin/kubectl" <<EOF
#!/usr/bin/env bash
case "\$4" in
  secrets) cat "$T/secrets.json" ;;
  ingresses) [ -f "$T/ing-fail" ] && exit 1; cat "$T/ingresses.json" ;;
  *) echo '{"items":[]}' ;;
esac
EOF
chmod 0755 "$T/bin/kubectl"
: > "$T/admin.conf"
export PATH="$T/bin:/usr/sbin:/usr/bin:/sbin:/bin"

sed -n '/^cat > "\$HELPER.new" <<.HELPER_EOF.$/,/^HELPER_EOF$/p' "$ROOT/agent/kubeexpiry-setup.sh" | sed '1d;$d' \
  | sed -e "s#/var/lib/kervax#$T/var#g" -e "s#/var/lock/#$T/#g" \
        -e "s#/etc/kubernetes/admin.conf#$T/admin.conf#g" > "$T/helper"
chmod 0755 "$T/helper"
fail=0
check() { if eval "$2"; then :; else echo "FAIL: $1"; fail=1; fi; }
item() { # where -> the item as compact JSON
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(next((i for i in d["items"] if i["where"]==sys.argv[2]), None), sort_keys=True))' "$T/var/kube-expiry.json" "$1"
}

"$T/helper"
U=$(item default/used-ssl)
O=$(item default/old-ssl)
FP=$(openssl x509 -noout -fingerprint -sha256 -in "$T/used-ssl.crt" | cut -d= -f2 | tr -d : | tr A-F a-f | cut -c1-16)
check "used secret marked as used: $U" '[[ "$U" == *"\"ingress\": true"* ]]'
# an Ingress in another namespace does not count: secretName is namespace-local
check "unused secret marked as unused: $O" '[[ "$O" == *"\"ingress\": false"* ]]'
check "fingerprint is the short sha256: $U vs $FP" '[[ "$U" == *"\"fp\": \"$FP\""* ]]'
check "note kept" '[[ "$U" == *"\"note\": \"tls.crt\""* ]]'
check "only TLS secrets" '[ "$(item default/other)" = null ]'

touch "$T/ing-fail"
"$T/helper"
O=$(item default/old-ssl)
check "failed Ingress read leaves usage unknown: $O" '[[ "$O" != *ingress* && "$O" == *"\"fp\""* ]]'

[ "$fail" = 0 ] && echo PASS
exit "$fail"
