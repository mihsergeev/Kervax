#!/usr/bin/env bash
# Kervax: expiry dates of everything a Kubernetes cluster silently dies on - cluster PKI,
# kubeconfigs, TLS certificates and the credentials Flux uses to reach Git and registries.
#
# WHY. This is written after a real outage: a GitLab project access token in the flux-system
# secret expired, GitRepository went Ready=False with "HTTP Basic: Access denied", and for two
# days nothing was deployed anywhere in the cluster. Everything already running kept running,
# so no dashboard turned red - the only visible symptom was that new builds quietly stopped
# arriving. Both halves are covered here: the date before it happens, and the failed state
# after, in case a token is revoked rather than expired.
#
# WHAT IS READ, AND BY WHOM. Everything runs as ROOT ON THE NODE:
#  * PKI files of k0s/k3s/kubeadm and the kubeconfigs next to them - the notAfter of each cert;
#  * Flux sources and their secrets, through the node's own admin kubectl. The panel's
#    ServiceAccount has no access to secrets and gets none: the helper computes dates locally.
#  * A personal or project access token carries no expiry inside it - only the forge knows it.
#    So the helper asks the forge named in the source URL (self-hosted GitLab included) and
#    reads the date from the answer. The token is used for that single request, is never
#    written anywhere and never leaves the node.
#
# WHAT LEAVES THE NODE: a kind, a location (file path or namespace/name), a date, and for Flux
# resources their Ready state. No key material, no token values, not even their length.
#
# TWO PASSES, ONE FILE. Dates move over months, so collecting them hourly is plenty - and the
# forge is asked about token expiry only that often. Ready state moves in seconds: a broken
# delivery must be noticed quickly, and a delivery that has just been REPAIRED must stop being
# reported as broken just as quickly. Hourly it looked like the panel was lying - the cluster
# was already green while the alert stood for another forty minutes. So Ready state gets its own
# lightweight pass (--flux-only) every 5 minutes: it reads nothing but the Flux objects
# themselves, touches no secrets, calls no forge, and refreshes that one field.
#
# Result: /var/lib/kervax/kube-expiry.json, world-readable; the agent only reads it.
set -euo pipefail

KERVAX_SETUP_VERSION=0.2  # MAJOR.MINOR; compared component-wise
# Which nodes need this helper at all. Read by the ansible playbook on the
# CONTROL machine and evaluated as a shell condition ON THE NODE, so a new
# helper lands where it belongs without anyone editing the playbook.
# Only where a cluster actually runs - and regardless of whether the panel has
# access to it: this helper works through the node's own admin kubectl.
KERVAX_SETUP_WHEN="command -v k0s >/dev/null 2>&1 || command -v k3s >/dev/null 2>&1 || command -v microk8s >/dev/null 2>&1 || command -v kubelet >/dev/null 2>&1 || [ -e /etc/kubernetes/admin.conf ] || [ -d /var/lib/k0s ] || [ -d /var/lib/rancher/k3s ]"
# Deliberately NOT "always": on a node without Kubernetes there is nothing to collect, and
# asking a forge for token expiry is a network call - it belongs only where Flux actually runs.

HELPER_DIR=/lib65/kervax
HELPER="$HELPER_DIR/kervax-kube-expiry"
STATE_DIR=/var/lib/kervax
OUT="$STATE_DIR/kube-expiry.json"

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi

# The parent /var/lib/kervax is set to 0755 EXPLICITLY (under umask 077 the unprivileged agent
# could not enter it and would never read the file).
install -d -m 0755 "$HELPER_DIR" "$STATE_DIR" "$STATE_DIR/versions"

cat > "$HELPER" <<'HELPER_EOF'
#!/usr/bin/env bash
# Collects expiry dates and Flux state -> /var/lib/kervax/kube-expiry.json.
# Read only; no secret material is stored or transmitted.
set -u
OUT=/var/lib/kervax/kube-expiry.json
TMP="$OUT.tmp.$$"
TO=15   # per-command timeout: a hung API server must not hang the collection

# --flux-only: refresh just the Ready state of Flux objects, keep the dates already collected.
FLUX_ONLY=0
[ "${1:-}" = "--flux-only" ] && FLUX_ONLY=1

# Both passes write the same file and the slow one runs for a while: without a lock the hourly
# pass could land on top of a fresher Ready state with its own, older copy.
if command -v flock >/dev/null 2>&1; then
  exec 9>/var/lock/kervax-kube-expiry.lock
  flock -w 60 9 || exit 0
fi

have() { command -v "$1" >/dev/null 2>&1; }
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\000-\037'; }

ITEMS=""
add() { # kind where expires_ts note
  local k="$1" w="$2" ts="$3" n="${4:-}"
  [ -n "$ts" ] && [ "$ts" -gt 0 ] 2>/dev/null || return 0
  ITEMS="$ITEMS${ITEMS:+,}{\"kind\":\"$(esc "$k")\",\"where\":\"$(esc "$w")\",\"expires\":$ts,\"note\":\"$(esc "$n")\"}"
}

cert_ts() { # PEM on stdin -> unix timestamp of notAfter
  local d
  d=$(timeout $TO openssl x509 -noout -enddate 2>/dev/null | sed 's/^notAfter=//') || return 0
  [ -n "$d" ] || return 0
  date -d "$d" +%s 2>/dev/null || true
}

# ── 1. cluster PKI: the files the control plane itself runs on ────────────────
if [ "$FLUX_ONLY" = 0 ]; then
for dir in /var/lib/k0s/pki /var/lib/k0s/pki/etcd /etc/kubernetes/pki /etc/kubernetes/pki/etcd \
           /var/lib/rancher/k3s/server/tls; do
  [ -d "$dir" ] || continue
  for crt in "$dir"/*.crt; do
    [ -f "$crt" ] || continue
    case "$crt" in *ca.crt) continue;; esac   # a CA lives ~10 years: noise, not a signal
    add cluster-cert "$crt" "$(cert_ts < "$crt")" "$(basename "$crt" .crt)"
  done
done

# ── 2. kubeconfigs: the client certificates inside them ──────────────────────
# They expire on the same schedule as the PKI but break differently: the cluster keeps running
# while a human suddenly cannot reach it.
for kc in /etc/kubernetes/admin.conf /etc/kubernetes/super-admin.conf \
          /var/lib/k0s/pki/admin.conf /var/lib/rancher/k3s/server/cred/admin.kubeconfig \
          /etc/rancher/k3s/k3s.yaml /root/.kube/config; do
  [ -f "$kc" ] || continue
  data=$(sed -n 's/.*client-certificate-data:[[:space:]]*//p' "$kc" | head -1)
  [ -n "$data" ] || continue
  add kubeconfig "$kc" "$(printf '%s' "$data" | base64 -d 2>/dev/null | cert_ts)" "$(basename "$kc")"
done

# ── 2b. kubelet client certificate ───────────────────────────────────────────
# This one is supposed to rotate itself, and that is exactly why it is worth watching: when
# rotation silently stops working (a rejected CSR, a clock skew, a full disk), nothing complains
# until the certificate runs out and the node drops out of the cluster in one step.
for kp in /var/lib/kubelet/pki/kubelet-client-current.pem /var/lib/k0s/kubelet/pki/kubelet-client-current.pem; do
  [ -f "$kp" ] || continue
  add kubelet-cert "$kp" "$(cert_ts < "$kp")" ""   # the kind already says what it is
done
fi   # /FLUX_ONLY = 0: dates read from files on disk

# ── 3. inside the cluster: Flux credentials, Flux state, TLS secrets ─────────
# The node's own admin kubectl: k0s/k3s ship one, kubeadm leaves admin.conf behind.
KC=""
if have k0s && timeout $TO k0s status >/dev/null 2>&1; then KC="k0s kubectl"
elif have k3s && [ -e /etc/rancher/k3s/k3s.yaml ]; then KC="k3s kubectl"
elif have kubectl && [ -e /etc/kubernetes/admin.conf ]; then KC="kubectl --kubeconfig /etc/kubernetes/admin.conf"
elif have microk8s; then KC="microk8s kubectl"
fi

FLUX_STATE="[]"
ROWS=/tmp/kv-kube-rows.$$
: > "$ROWS"

if [ -n "$KC" ] && have python3; then
  # Sources carry the URL and the name of the secret with the credentials; the secrets hold the
  # token itself; the resources hold the Ready state. Three cheap reads, one pass.
  # The quick pass takes only the last of the three: reading every secret in the cluster and
  # asking the forge about token expiry has no business running every five minutes.
  SRC='{}'
  SEC='{}'
  RES=$(timeout $TO $KC get gitrepositories,ocirepositories,helmrepositories,kustomizations,helmreleases,imagerepositories,imageupdateautomations \
        -A -o json 2>/dev/null || echo '{}')
  if [ "$FLUX_ONLY" = 0 ]; then
    SRC=$(timeout $TO $KC get gitrepositories,ocirepositories,helmrepositories,imagerepositories \
          -A -o json 2>/dev/null || echo '{}')
    SEC=$(timeout $TO $KC get secrets -A -o json 2>/dev/null || echo '{}')
  fi

  FLUX_STATE=$(printf '%s' "$RES" | timeout $TO python3 -c '
import json, sys
out = []
for i in (json.load(sys.stdin) or {}).get("items", []):
    m = i.get("metadata") or {}
    conds = (i.get("status") or {}).get("conditions") or []
    ready = next((c for c in conds if c.get("type") == "Ready"), None)
    if not ready:
        continue
    ns = m.get("namespace", "")
    nm = m.get("name", "")
    out.append({
        "kind": i.get("kind", ""),
        "where": f"{ns}/{nm}",
        "ready": ready.get("status") == "True",
        # the reason is what an engineer reads first: GitOperationFailed says "credentials",
        # BuildFailed says "manifests" - very different mornings
        "reason": str(ready.get("reason") or "")[:60],
        "message": str(ready.get("message") or "")[:200],
    })
print(json.dumps(out, ensure_ascii=False))
' 2>/dev/null || echo '[]')
  [ -n "$FLUX_STATE" ] || FLUX_STATE="[]"

  if [ "$FLUX_ONLY" = 0 ]; then
  printf '%s\n---\n%s\n' "$SRC" "$SEC" | timeout $TO python3 -c '
import base64, json, subprocess, sys
from urllib.parse import urlsplit

raw = sys.stdin.read().split("\n---\n")
sources = json.loads(raw[0] or "{}") or {}
secrets = json.loads(raw[1] or "{}") or {}

# secrets by namespace/name, so a source can find its own without a second API call
by_name = {}
for s in secrets.get("items", []):
    m = s.get("metadata") or {}
    by_name[(m.get("namespace"), m.get("name"))] = s


def run(cmd, timeout=12):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout)
    except Exception:
        return None


def to_ts(stamp):
    p = run(["date", "-d", stamp, "+%s"], 10)
    try:
        return int((p.stdout or b"").strip() or 0) or None
    except Exception:
        return None


def forge_expiry(token, url):
    """Ask the forge that hosts this source when its token expires.

    The host comes from the source URL, so a self-hosted GitLab is asked about its own token
    rather than gitlab.com. The token is used for this one request only.
    """
    host = urlsplit(url).netloc.split("@")[-1]
    if not host or not token:
        return None
    base = f"https://{host}"
    if token.startswith(("glpat-", "gldt-")):
        # project access tokens answer here too: they belong to a project bot user
        p = run(["curl", "-sS", "-m", "8", "-H", "PRIVATE-TOKEN: " + token,
                 f"{base}/api/v4/personal_access_tokens/self"])
        if p:
            try:
                exp = (json.loads((p.stdout or b"{}").decode() or "{}") or {}).get("expires_at")
            except Exception:
                exp = None
            if exp:
                return to_ts(exp)
    elif token.startswith(("ghp_", "github_pat_", "gho_")):
        api = "https://api.github.com" if host.endswith("github.com") else f"{base}/api/v3"
        p = run(["curl", "-sS", "-o", "/dev/null", "-D", "-", "-m", "8",
                 "-H", "Authorization: Bearer " + token, f"{api}/user"])
        if p:
            for line in (p.stdout or b"").decode(errors="replace").splitlines():
                if line.lower().startswith("github-authentication-token-expiration:"):
                    return to_ts(line.split(":", 1)[1].strip())
    return None


rows = []
seen = set()
for src in sources.get("items", []):
    meta = src.get("metadata") or {}
    spec = src.get("spec") or {}
    ns = meta.get("namespace", "")
    ref = (spec.get("secretRef") or {}).get("name")
    url = spec.get("url") or ""
    if not ref:
        continue
    sec = by_name.get((ns, ref))
    if not sec:
        continue
    key = (ns, ref, url)
    if key in seen:
        continue
    seen.add(key)
    data = sec.get("data") or {}
    for field in ("password", "token", "bearerToken"):
        raw_val = data.get(field)
        if not raw_val:
            continue
        try:
            token = base64.b64decode(raw_val).decode("utf-8", "replace").strip()
        except Exception:
            continue
        ts = forge_expiry(token, url)
        if ts:
            kind = src.get("kind", "source")
            name = meta.get("name", "")
            rows.append(("flux-token", f"{ns}/{ref}", ts, f"{kind} {name}"))
        break

print("\n".join(f"{k}\t{w}\t{t}\t{n}" for k, w, t, n in rows))
' >> "$ROWS" 2>/dev/null || true

  # TLS certificates in secrets (cert-manager, ingress, Flux mTLS): the date is inside the
  # certificate, no network call needed.
  printf '%s' "$SEC" | timeout $TO python3 -c '
import base64, json, subprocess, sys
data = json.load(sys.stdin) or {}
rows = []
for s in data.get("items", []):
    if s.get("type") != "kubernetes.io/tls":
        continue
    m = s.get("metadata") or {}
    blob = (s.get("data") or {}).get("tls.crt")
    if not blob:
        continue
    try:
        pem = base64.b64decode(blob)
        p = subprocess.run(["openssl", "x509", "-noout", "-enddate"], input=pem,
                           capture_output=True, timeout=10)
        line = p.stdout.decode().strip()
        if not line.startswith("notAfter="):
            continue
        d = subprocess.run(["date", "-d", line[9:], "+%s"], capture_output=True, timeout=10)
        ts = int((d.stdout or b"").strip() or 0)
    except Exception:
        continue
    if ts:
        ns = m.get("namespace", "")
        nm = m.get("name", "")
        rows.append(("secret-cert", f"{ns}/{nm}", ts, "tls.crt"))
print("\n".join(f"{k}\t{w}\t{t}\t{n}" for k, w, t, n in rows))
' >> "$ROWS" 2>/dev/null || true
  fi   # /FLUX_ONLY = 0: secrets and the forge
fi

if [ -s "$ROWS" ]; then
  while IFS=$'\t' read -r k w ts n; do
    [ -n "${ts:-}" ] && add "$k" "$w" "$ts" "$n"
  done < "$ROWS"
fi
rm -f "$ROWS"

if [ "$FLUX_ONLY" = 1 ]; then
  # Keep the dates from the last full pass, replace only the Ready state. If the API server was
  # unreachable there is no state to write: changing nothing beats erasing a real report of a
  # broken delivery because of one failed read.
  [ -n "$KC" ] || exit 0
  FLUX_STATE="$FLUX_STATE" python3 - "$OUT" "$TMP" <<'MERGE_EOF' || exit 0
import json, os, sys, time
out, tmp = sys.argv[1], sys.argv[2]
try:
    doc = json.load(open(out))
except Exception:
    doc = {}
doc.setdefault("ts", int(time.time()))
doc.setdefault("items", [])
doc["flux"] = json.loads(os.environ.get("FLUX_STATE") or "[]")
doc["flux_ts"] = int(time.time())
json.dump(doc, open(tmp, "w"), ensure_ascii=False)
MERGE_EOF
else
  printf '{"ts":%s,"items":[%s],"flux":%s,"flux_ts":%s}\n' \
    "$(date +%s)" "$ITEMS" "$FLUX_STATE" "$(date +%s)" > "$TMP"
fi
mv -f "$TMP" "$OUT"
chmod 0644 "$OUT"
HELPER_EOF
chmod 0755 "$HELPER"; chown root:root "$HELPER"

cat > /etc/systemd/system/kervax-kube-expiry.service <<EOF
[Unit]
Description=Kervax: collect Kubernetes and Flux expiry dates
[Service]
Type=oneshot
ExecStart=$HELPER
EOF
cat > /etc/systemd/system/kervax-kube-expiry.timer <<'EOF'
[Unit]
Description=Kervax: refresh Kubernetes and Flux expiry dates
[Timer]
OnBootSec=5min
# Certificates and tokens move slowly, so hourly is frequent enough - and it keeps the
# requests to the forge down to a couple of dozen a day.
OnUnitActiveSec=1h
Persistent=true
[Install]
WantedBy=timers.target
EOF

cat > /etc/systemd/system/kervax-kube-flux.service <<EOF
[Unit]
Description=Kervax: refresh Flux Ready state (quick pass)
[Service]
Type=oneshot
ExecStart=$HELPER --flux-only
EOF
cat > /etc/systemd/system/kervax-kube-flux.timer <<'EOF'
[Unit]
Description=Kervax: refresh Flux Ready state every few minutes
[Timer]
OnBootSec=6min
# Ready state is the half that moves fast: a stopped delivery should be seen within minutes,
# and a repaired one should stop being reported just as fast. This pass touches no secrets and
# makes no outside calls, so it is cheap enough to run often.
OnUnitActiveSec=5min
Persistent=true
[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now kervax-kube-expiry.timer >/dev/null 2>&1 || true
systemctl enable --now kervax-kube-flux.timer >/dev/null 2>&1 || true
"$HELPER" || true   # run once immediately so the data appears without waiting for the timer

echo "$KERVAX_SETUP_VERSION" > "$STATE_DIR/versions/kubeexpiry-setup.ver"
chmod 0644 "$STATE_DIR/versions/kubeexpiry-setup.ver"
echo "✓ kubeexpiry-setup: $OUT (dates hourly, Flux state every 5 min)."
