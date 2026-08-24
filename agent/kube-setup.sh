#!/usr/bin/env bash
# Kervax: включить read-only мониторинг + узкое управление Kubernetes для агента.
#
# Агент бежит под непривилегированным пользователем `kervax` (NoNewPrivileges,
# ProtectSystem=strict) — root-only admin.conf ему недоступен. Скрипт создаёт
# выделенный ServiceAccount с УЗКИМ RBAC (read по всему + точечный write: rollout
# restart воркоадов и удаление подов) и кладёт токен в /etc/kervax/kube.json,
# читаемый только root:kervax. Даже полностью скомпрометированный агент ограничен
# этим RBAC — не cluster-admin. Запускать от root на control-plane ноде.
#
# Использование:  sudo bash kube-setup.sh            # автодетект дистрибутива
#                 sudo KUBECTL="k3s kubectl" bash kube-setup.sh   # явный kubectl
set -euo pipefail

# v2: в RBAC добавлено ЧТЕНИЕ batch/cronjobs+jobs — панель видит уже настроенные дампы СУБД
# v3: + чтение persistentvolumes/claims — аудит сверяет тома кластера с покрытием бэкапа
KERVAX_SETUP_VERSION=0.14  # МАЖОР.МИНОР; сравнивается покомпонентно (0.13 > 0.2!)
NS=kube-system
SA=kervax-agent
OUT=/etc/kervax/kube.json

# --- автодетект инструмента kubectl по дистрибутиву ---
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
[ -z "$KC" ] && { echo "kube-setup: не нашёл kubectl/дистрибутив кластера" >&2; exit 1; }
echo "kube-setup: kubectl = $KC"

# --- SA + узкий ClusterRole + binding (идемпотентно, apply) ---
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
  # read по кластеру — для дашборда (ноды/поды/воркоады/события)
  - apiGroups: [""]
    resources: [nodes, namespaces, pods, pods/log, events, services]
    verbs: [get, list, watch]
  - apiGroups: ["apps"]
    resources: [deployments, statefulsets, daemonsets, replicasets]
    verbs: [get, list, watch, patch]     # patch = rollout restart (аннотация в шаблоне)
  # ТОЛЬКО ЧТЕНИЕ расписаний: панель по ним понимает, что дамп СУБД уже настроен, и не
  # ноет про «нужен отдельный бэкап». Содержимое секретов это не даёт — только имена/спеки.
  - apiGroups: ["batch"]
    resources: [cronjobs, jobs]
    verbs: [get, list, watch]
  # тома: аудит бэкапа сверяет, попадают ли каталоги hostPath/local в restic. Это СПЕКА
  # тома (путь, класс, размер), а не его содержимое — читать данные это права не дают.
  - apiGroups: [""]
    resources: [persistentvolumes, persistentvolumeclaims]
    verbs: [get, list, watch]
  # точечный write: перезапуск пода = его удаление (контроллер пересоздаст)
  - apiGroups: [""]
    resources: [pods]
    verbs: [delete]
  # метрики, если стоит metrics-server (иначе просто не будет данных)
  - apiGroups: ["metrics.k8s.io"]
    resources: [pods, nodes]
    verbs: [get, list]
  # ingress-хосты: панель показывает, какие домены/сайты обслуживает ingress-контроллер.
  # Это СПЕКА маршрутизации (host/path/backend), без секретов и без содержимого TLS.
  - apiGroups: ["networking.k8s.io"]
    resources: [ingresses]
    verbs: [get, list, watch]
  # Gateway API (Envoy Gateway/Traefik/…): домены в HTTPRoute.spec.hostnames, а не в Ingress.
  # Тоже только спека маршрутов, без секретов. Нет CRD → запрос 404 → просто пусто.
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

# --- ждём, пока контроллер наполнит Secret токеном ---
echo -n "kube-setup: жду токен"
for _ in $(seq 1 30); do
  TOKEN=$($KC get secret ${SA}-token -n ${NS} -o jsonpath='{.data.token}' 2>/dev/null | base64 -d 2>/dev/null || true)
  [ -n "$TOKEN" ] && break
  echo -n "."; sleep 1
done
echo
[ -z "${TOKEN:-}" ] && { echo "kube-setup: токен не появился" >&2; exit 1; }
CA=$($KC get secret ${SA}-token -n ${NS} -o jsonpath='{.data.ca\.crt}')  # уже base64

# server: kube-api на ноде слушает локально — ходим на 127.0.0.1 (наружу не светим)
PORT=$(ss -ltn 2>/dev/null | grep -oE ':6443' | head -1 | tr -d ':'); PORT=${PORT:-6443}
SERVER="https://127.0.0.1:${PORT}"

# --- пишем kube.json (root:kervax, 0640) ---
install -d -m 0755 "$(dirname "$OUT")"
umask 077
cat > "$OUT" <<JSON
{"server":"${SERVER}","ca":"${CA}","token":"${TOKEN}"}
JSON
# читать должен только агент (kervax) и root
if getent group kervax >/dev/null 2>&1; then chgrp kervax "$OUT"; fi
chmod 0640 "$OUT"

# версия для панели (сверяет с раздаваемой, флагует устаревшие).
# ВАЖНО: перечисляем РОДИТЕЛЯ /var/lib/kervax явно — иначе под активным `umask 077` (стоит
# выше для kube.json) install создаёт промежуточный /var/lib/kervax под 0700, и непривил.
# агент (kervax) НЕ может в него зайти → маркер не читается, панель вечно флагует «? → устарел».
# (На kube-only ноде именно kube-setup первым создаёт /var/lib/kervax — тут и всплывало.)
# chmod .ver тоже явный: без него файл ляжет 0600 root и его снова не прочитать.
install -d -m 0755 /var/lib/kervax /var/lib/kervax/versions
echo "$KERVAX_SETUP_VERSION" > /var/lib/kervax/versions/kube-setup.ver
chmod 0644 /var/lib/kervax/versions/kube-setup.ver

echo "kube-setup: готово → $OUT (server=$SERVER)"
echo "kube-setup: проверка read-доступа…"
if command -v curl >/dev/null 2>&1; then
  code=$(curl -sS -o /dev/null -w '%{http_code}' --cacert <(echo "$CA" | base64 -d) \
        -H "Authorization: Bearer $TOKEN" "$SERVER/api/v1/nodes" || echo 000)
  echo "kube-setup: GET /api/v1/nodes → HTTP $code (200 = ок)"
fi
