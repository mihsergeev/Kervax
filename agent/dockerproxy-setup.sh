#!/usr/bin/env bash
# Kervax: harden the agent's bounded Docker access (the kervax-docker-proxy container).
#
# WHY. The agent never gets the docker socket itself: a tiny socket-proxy (wollomatic) lets
# through only a per-method allowlist - GET version/list/inspect/logs and the image list, POST
# restart/stop/start of a container; exec, create, image inspect/pull, build and volumes get 403.
# The image list (names, labels, build dates, no env) tells the panel how old the image of an
# internet-facing proxy is: a traefik:latest tag turned out to be a 2021 build. The proxy used to be
# published as -p 127.0.0.1:2375 from the default bridge network, so any other container on
# that network could reach it by its address, read the env (secrets) and logs of every
# container and stop them. Now it runs in the host network and listens on 127.0.0.1 only:
# containers cannot reach it at all. Capabilities are dropped, the root fs is read-only,
# no-new-privileges is set. A read-only tecnativa proxy (restart answered 403) is replaced too.
#
# Touches ONLY an existing kervax-docker-proxy: where docker access was never enabled the
# helper does nothing (enabling it is a decision for a person - install.sh --docker or the
# command in the Docker section of the panel). If the new proxy does not come up, the old one
# is put back. Run as root.
set -euo pipefail

KERVAX_SETUP_VERSION=0.2  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_WHEN="docker inspect kervax-docker-proxy || docker inspect kervax-docker-proxy-old"

NAME=kervax-docker-proxy
OLD=$NAME-old
IMAGE=wollomatic/socket-proxy:1
ALLOW_GET='^/(v[0-9.]+/)?(version|info|_ping|containers/json|images/json|containers/[a-zA-Z0-9_.-]+/(json|logs))'
ALLOW_POST='^/(v[0-9.]+/)?containers/[a-zA-Z0-9_.-]+/(restart|stop|start)$'
VERDIR=/var/lib/kervax/versions
MARK=$VERDIR/dockerproxy-setup.ver

if [ "$(id -u)" != 0 ]; then echo "Root required." >&2; exit 1; fi
if ! command -v docker >/dev/null 2>&1; then
  echo "· docker is not installed - skipping."
  exit 0
fi

# A previous run was cut off between stopping the old proxy and starting the new one: put the
# old one back first, otherwise the node would stay without docker access and the check below
# would not even see the proxy.
if ! docker inspect "$NAME" >/dev/null 2>&1 && docker inspect "$OLD" >/dev/null 2>&1; then
  docker rename "$OLD" "$NAME"
  docker start "$NAME" >/dev/null
fi
if ! docker inspect "$NAME" >/dev/null 2>&1; then
  echo "· no $NAME on this node (docker access is not enabled) - skipping."
  exit 0
fi

# the proxy answers on 127.0.0.1:2375 (bash /dev/tcp: curl is not installed everywhere)
probe() {
  local line
  line=$(timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/2375 && printf "GET /_ping HTTP/1.0\r\nHost: docker\r\n\r\n" >&3 && head -n1 <&3' 2>/dev/null) || return 1
  case "$line" in *" 200 "*) return 0 ;; esac
  return 1
}

# the allowlist is part of the check: a proxy from 0.1 is hardened, but answers 403 to the image list
hardened() {
  [ "$(docker inspect -f '{{.Config.Image}} {{.HostConfig.NetworkMode}} {{.HostConfig.ReadonlyRootfs}}' "$NAME" 2>/dev/null)" = "$IMAGE host true" ] \
    && docker inspect -f '{{json .Args}}' "$NAME" | grep -q '"-listenip","127.0.0.1"' \
    && docker inspect -f '{{json .Args}}' "$NAME" | grep -qF -- "\"$ALLOW_GET\""
}

if hardened; then
  echo "· $NAME is already hardened, the allowlist is current."
else
  # the group that may use the socket, taken from the socket itself: a node without a group
  # named docker still works, the same as the proxy that is running there now
  DGID=$(stat -c %g /var/run/docker.sock 2>/dev/null || getent group docker | cut -d: -f3)
  if [ -z "$DGID" ]; then
    echo "✗ cannot tell the group of /var/run/docker.sock - leaving the proxy as it is." >&2
    exit 1
  fi
  # Pull first: the agent goes without docker data only while the proxy is being swapped. A
  # node that cannot reach Docker Hub still has the image if it already runs this proxy.
  if ! docker pull -q "$IMAGE" >/dev/null 2>&1 && ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "✗ cannot pull $IMAGE - leaving the proxy as it is." >&2
    exit 1
  fi
  docker rm -f "$OLD" >/dev/null 2>&1 || true
  docker rename "$NAME" "$OLD"
  docker stop "$OLD" >/dev/null  # frees 127.0.0.1:2375
  if docker run -d --name "$NAME" --restart unless-stopped \
       --network host --read-only --cap-drop ALL --security-opt no-new-privileges \
       --user "65534:$DGID" -v /var/run/docker.sock:/var/run/docker.sock:ro \
       "$IMAGE" -loglevel warn -listenip 127.0.0.1 -allowfrom 127.0.0.1/32 -shutdowngracetime 1 \
       -allowGET "$ALLOW_GET" -allowPOST "$ALLOW_POST" >/dev/null \
     && { for _ in 1 2 3 4 5; do probe && break; sleep 1; done; probe; }; then
    docker rm -f "$OLD" >/dev/null
    echo "✓ $NAME hardened: host network, 127.0.0.1 only, no capabilities, read-only root."
  else
    echo "✗ the hardened proxy did not come up - putting the old one back." >&2
    docker logs --tail 5 "$NAME" 2>&1 | sed 's/^/  /' >&2 || true
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker rename "$OLD" "$NAME"
    docker start "$NAME" >/dev/null
    exit 1
  fi
fi
install -d -m 0755 "$VERDIR"
echo "$KERVAX_SETUP_VERSION" > "$MARK"
chmod 0644 "$MARK"  # explicit: the agent (kervax) must read it; the installer runs helpers under umask 077
