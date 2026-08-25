#!/usr/bin/env bash
# Kervax: enable read-only Kubernetes monitoring plus narrow control for the agent.
#
# The agent runs as the unprivileged user `kervax` (NoNewPrivileges,
# ProtectSystem=strict), so a root-only admin.conf is out of its reach. This script
# creates a dedicated ServiceAccount with NARROW RBAC (read everywhere plus a precise
# write: rollout restart of workloads and pod deletion) and writes the token into
# /etc/kervax/kube.json, readable only by root:kervax. Even a fully compromised agent
# stays bounded by this RBAC — it is not cluster-admin. Run as root on a control-plane
# node.
#
# Usage:  sudo bash kube-setup.sh                          # auto-detect the distribution
#         sudo KUBECTL="k3s kubectl" bash kube-setup.sh    # explicit kubectl
set -euo pipefail

# v2: RBAC gained READ on batch/cronjobs+jobs — the panel sees database dumps already set up
# v3: plus read on persistentvolumes/claims — the audit matches cluster volumes against backups
KERVAX_SETUP_VERSION=0.14  # MAJOR.MINOR; compared component-wise (0.13 > 0.2!)
NS=kube-system
SA=kervax-agent
OUT=/etc/kervax/kube.json

# --- auto-detect the kubectl entry point for this distribution ---
detect_kubectl() {
  if [ -n "${KUBECTL:-}" ]; then echo "$KUBECTL"; return; fi
  if command -v k0s >/dev/null 2>&1 && k0s status >/dev/null 2>&1; then echo "k0s kubectl"; return; fi
  if command -v k3s >/dev/null 2>&1 && [ -e /etc/rancher/k3s/k3s.yaml ]; then echo "k3s kubectl"; return; fi
  if command -v microk8s >/dev/null 2>&1; then echo "microk8s kubectl"; return; fi
  for cfg in /etc/kubernetes/admin.conf /root/.kube/config; do
    if command -v kubectl >/dev/null 2>&1 && [ -e "$cfg" ]; then echo "kubectl --kubeconfig $cfg"; return; fi
  done
  echo "" ; return
}
KC=$(detect_kubectl)
[ -z "$KC" ] && { echo "kube-setup: no kubectl or cluster distribution found" >&2; exit 1; }
echo "kube-setup: kubectl = $KC"

# --- SA plus a narrow ClusterRole and binding (idempotent, apply) ---
$KC apply -f - <<YAML
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${SA}
  namespace: ${NS}
  labels: { app.kubernetes.io/managed-by: kervax }
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: ${SA}
  labels: { app.kubernetes.io/managed-by: kervax }
rules:
  # cluster-wide read for the dashboard (nodes/pods/workloads/events)
  - apiGroups: [""]
    resources: [nodes, namespaces, pods, pods/log, events, services]
    verbs: [get, list, watch]
  - apiGroups: ["apps"]
    resources: [deployments, statefulsets, daemonsets, replicasets]
    verbs: [get, list, watch, patch]     # patch = rollout restart (annotation in the template)
  # READ ONLY on schedules: from them the panel understands a database dump is already
  # configured and stops asking for a separate backup. This grants no secret contents —
  # only names and specs.
  - apiGroups: ["batch"]
    resources: [cronjobs, jobs]
    verbs: [get, list, watch]
  # volumes: the backup audit checks whether hostPath/local directories are covered by
  # restic. This is the volume SPEC (path, class, size), not its contents — the
  # permission does not allow reading data.
  - apiGroups: [""]
    resources: [persistentvolumes, persistentvolumeclaims]
    verbs: [get, list, watch]
  # precise write: restarting a pod means deleting it (the controller recreates it)
  - apiGroups: [""]
    resources: [pods]
    verbs: [delete]
  # metrics, if metrics-server is installed (otherwise there is simply no data)
  - apiGroups: ["metrics.k8s.io"]
    resources: [pods, nodes]
    verbs: [get, list]
  # ingress hosts: the panel shows which domains the ingress controller serves. This is
  # the routing SPEC (host/path/backend), without secrets or TLS contents.
  - apiGroups: ["networking.k8s.io"]
    resources: [ingresses]
    verbs: [get, list, watch]
  # Gateway API (Envoy Gateway/Traefik/...): domains live in HTTPRoute.spec.hostnames
  # rather than Ingress. Route spec only, no secrets. No CRD means a 404 and an empty
  # result.
  - apiGroups: ["gateway.networking.k8s.io"]
    resources: [httproutes]
    verbs: [get, list, watch]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: ${SA}
  labels: { app.kubernetes.io/managed-by: kervax }
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: ${SA}
subjects:
  - kind: ServiceAccount
    name: ${SA}
    namespace: ${NS}
---
apiVersion: v1
kind: Secret
metadata:
  name: ${SA}-token
  namespace: ${NS}
  annotations: { kubernetes.io/service-account.name: ${SA} }
  labels: { app.kubernetes.io/managed-by: kervax }
type: kubernetes.io/service-account-token
YAML

# --- wait for the controller to fill the Secret with a token ---
echo -n "kube-setup: waiting for the token"
for _ in $(seq 1 30); do
  TOKEN=$($KC get secret ${SA}-token -n ${NS} -o jsonpath='{.data.token}' 2>/dev/null | base64 -d 2>/dev/null || true)
  [ -n "$TOKEN" ] && break
  echo -n "."; sleep 1
done
echo
[ -z "${TOKEN:-}" ] && { echo "kube-setup: the token never appeared" >&2; exit 1; }
CA=$($KC get secret ${SA}-token -n ${NS} -o jsonpath='{.data.ca\.crt}')  # already base64

# server: the kube-api on this node listens locally, so we use 127.0.0.1 and expose nothing
PORT=$(ss -ltn 2>/dev/null | grep -oE ':6443' | head -1 | tr -d ':'); PORT=${PORT:-6443}
SERVER="https://127.0.0.1:${PORT}"

# --- write kube.json (root:kervax, 0640) ---
install -d -m 0755 "$(dirname "$OUT")"
umask 077
cat > "$OUT" <<JSON
{"server":"${SERVER}","ca":"${CA}","token":"${TOKEN}"}
JSON
# only the agent (kervax) and root may read it
if getent group kervax >/dev/null 2>&1; then chgrp kervax "$OUT"; fi
chmod 0640 "$OUT"

# version marker for the panel (it compares with the served one and flags outdated ones).
# IMPORTANT: the PARENT /var/lib/kervax is listed explicitly — otherwise, under the active
# `umask 077` (set above for kube.json), install creates it as 0700 and the unprivileged
# agent cannot enter it, so the marker is unreadable and the panel flags "? -> outdated"
# forever. (On a kube-only node it is kube-setup that creates /var/lib/kervax first, which
# is where this surfaced.) The chmod on .ver is explicit too: without it the file lands as
# 0600 root and is again unreadable.
install -d -m 0755 /var/lib/kervax /var/lib/kervax/versions
echo "$KERVAX_SETUP_VERSION" > /var/lib/kervax/versions/kube-setup.ver
chmod 0644 /var/lib/kervax/versions/kube-setup.ver

echo "kube-setup: done -> $OUT (server=$SERVER)"
echo "kube-setup: verifying read access..."
if command -v curl >/dev/null 2>&1; then
  code=$(curl -sS -o /dev/null -w '%{http_code}' --cacert <(echo "$CA" | base64 -d) \
        -H "Authorization: Bearer $TOKEN" "$SERVER/api/v1/nodes" || echo 000)
  echo "kube-setup: GET /api/v1/nodes -> HTTP $code (200 = ok)"
fi
