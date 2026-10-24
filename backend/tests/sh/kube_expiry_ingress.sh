#!/usr/bin/env bash
# Scenario test of kubeexpiry-setup.sh (helper 0.4): TLS secrets get a certificate fingerprint and
# a mark whether something points at them - Ingress spec.tls[].secretName, Gateway certificateRefs,
# Traefik IngressRoute tls.secretName, pod volumes and env. A cluster without Gateway API or Traefik
# is not a failed read; when any read fails the mark is left out ("unknown", not "unused"). Runs
# the real helper in a scratch directory with a fake kubectl. Usage: kube_expiry_ingress.sh <repo
# root>. Prints PASS or the failures.
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
C=$(cert gw-ssl 30)
D=$(cert route-ssl 30)
E=$(cert mounted-certs 30)
F=$(cert env-ssl 30)
sec() { # namespace name base64
  printf '{"type":"kubernetes.io/tls","metadata":{"namespace":"%s","name":"%s"},"data":{"tls.crt":"%s"}},' "$1" "$2" "$3"
}
{
  printf '{"items":['
  sec default used-ssl "$A"; sec default old-ssl "$B"; sec edge gw-ssl "$C"; sec default route-ssl "$D"
  sec kube-system mounted-certs "$E"; sec default env-ssl "$F"
  printf '{"type":"Opaque","metadata":{"namespace":"default","name":"other"},"data":{}}]}\n'
} > "$T/secrets.json"
cat > "$T/ingresses.json" <<'EOF'
{"items":[
 {"metadata":{"namespace":"default","name":"web"},"spec":{"tls":[{"hosts":["a.example.com"],"secretName":"used-ssl"}]}},
 {"metadata":{"namespace":"other","name":"web"},"spec":{"tls":[{"secretName":"old-ssl"}]}}
]}
EOF
# the Gateway lives in its own namespace and points at a secret in another one
cat > "$T/gateways.json" <<'EOF'
{"items":[{"metadata":{"namespace":"gw","name":"main"},"spec":{"listeners":[
 {"name":"https","tls":{"certificateRefs":[{"kind":"Secret","name":"gw-ssl","namespace":"edge"}]}},
 {"name":"http"}]}}]}
EOF
cat > "$T/routes.json" <<'EOF'
{"items":[{"metadata":{"namespace":"default","name":"app"},"spec":{"tls":{"secretName":"route-ssl"}}}]}
EOF
cat > "$T/pods.json" <<'EOF'
{"items":[
 {"metadata":{"namespace":"kube-system","name":"relay-5d9c7b8f6-abcde"},"spec":{"volumes":[
   {"name":"tls","projected":{"sources":[{"secret":{"name":"mounted-certs"}}]}}],"containers":[{"name":"relay"}]}},
 {"metadata":{"namespace":"default","name":"worker-0"},"spec":{"containers":[{"name":"w",
   "env":[{"name":"CERT","valueFrom":{"secretKeyRef":{"name":"env-ssl","key":"tls.crt"}}}]}]}}
]}
EOF
# fake kubectl: kubectl --kubeconfig <f> get <what> -A -o json. No Traefik in the old group, like
# a cluster that never had it
cat > "$T/bin/kubectl" <<EOF
#!/usr/bin/env bash
case "\$4" in
  secrets) cat "$T/secrets.json" ;;
  ingresses) [ -f "$T/ing-fail" ] && exit 1; cat "$T/ingresses.json" ;;
  gateways.gateway.networking.k8s.io) cat "$T/gateways.json" ;;
  ingressroutes.traefik.io) cat "$T/routes.json" ;;
  ingressroutetcps.traefik.io) echo '{"items":[]}' ;;
  pods) [ -f "$T/pods-fail" ] && { echo "Unable to connect to the server" >&2; exit 1; }; cat "$T/pods.json" ;;
  *traefik.containo.us) echo "error: the server doesn't have a resource type \"\$4\"" >&2; exit 1 ;;
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
for w in edge/gw-ssl default/route-ssl kube-system/mounted-certs default/env-ssl; do
  X=$(item "$w")
  check "$w is used (Gateway, IngressRoute, pod volume, pod env): $X" '[[ "$X" == *"\"ingress\": true"* ]]'
done
check "no leftovers" '[ -z "$(ls /tmp/kv-kube-rows.* 2>/dev/null)" ]'

touch "$T/pods-fail"
"$T/helper"
O=$(item default/old-ssl)
check "failed pod read leaves usage unknown: $O" '[[ "$O" != *ingress* && "$O" == *"\"fp\""* ]]'
rm -f "$T/pods-fail"

touch "$T/ing-fail"
"$T/helper"
O=$(item default/old-ssl)
check "failed Ingress read leaves usage unknown: $O" '[[ "$O" != *ingress* && "$O" == *"\"fp\""* ]]'

[ "$fail" = 0 ] && echo PASS
exit "$fail"
