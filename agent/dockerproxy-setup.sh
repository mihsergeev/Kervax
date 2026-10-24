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
#
# 0.3: 127.0.0.1:2375 was open to every user of the node, and to the agent's own "check from
# inside" probes a panel account can point at any local port: anybody could read the env of
# every container (database passwords) and stop any of them. The proxy listens on a unix socket
# now, /var/lib/kervax/docker/docker.sock, in a directory only root and the kervax group can
# enter, with no network at all (--network none); docker_host in the agent's config is switched
# to it and the agent restarted. The image is pinned by digest, the allowlist is anchored at the
# end and /info is gone (the agent never asks it).
set -euo pipefail

KERVAX_SETUP_VERSION=0.3  # MAJOR.MINOR; compared component-wise
KERVAX_SETUP_WHEN="docker inspect kervax-docker-proxy || docker inspect kervax-docker-proxy-old"

NAME=kervax-docker-proxy
OLD=$NAME-old
IMAGE=wollomatic/socket-proxy:1.13.2@sha256:ff95d6ff6c27853005ef85eb8f4ab5923dc3d2f1e055f98fb6aeeaceec3fe848
ALLOW_GET='^/(v[0-9.]+/)?(version|_ping|containers/json|images/json|containers/[a-zA-Z0-9_.-]+/(json|logs))$'
ALLOW_POST='^/(v[0-9.]+/)?containers/[a-zA-Z0-9_.-]+/(restart|stop|start)$'
SOCK_DIR=/var/lib/kervax/docker
SOCK=$SOCK_DIR/docker.sock
OLD_HOST=tcp://127.0.0.1:2375
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

# the agent's group: the socket is its, and only root and this group enter the directory
KGID=$(getent group kervax 2>/dev/null | cut -d: -f3)
if [ -z "$KGID" ]; then
  echo "✗ no kervax group on this node (the agent is not installed) - leaving the proxy as it is." >&2
  exit 1
fi

# the proxy answers on its socket: curl where it knows unix sockets (7.40), else the container runs
probe() {
  [ -S "$SOCK" ] || return 1
  if curl -q --help all 2>/dev/null | grep -q -- --unix-socket; then
    [ "$(timeout 5 curl -q -s --unix-socket "$SOCK" http://docker/_ping 2>/dev/null)" = OK ]
  else
    [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = true ]
  fi
}

# the allowlist is part of the check: a proxy from 0.1 is hardened, but answers 403 to the image list
hardened() {
  [ "$(docker inspect -f '{{.Config.Image}} {{.HostConfig.NetworkMode}} {{.HostConfig.ReadonlyRootfs}}' "$NAME" 2>/dev/null)" = "$IMAGE none true" ] \
    && docker inspect -f '{{json .Args}}' "$NAME" | grep -qF '"-proxysocketendpoint","/run/kervax/docker.sock"' \
    && docker inspect -f '{{json .Args}}' "$NAME" | grep -qF -- "\"$ALLOW_GET\""
}

# docker_host of every agent on the node that read the proxy over TCP -> the socket; the agent
# reads its config at start
switch_agents() {
  local c u
  for c in /etc/kervax-agent.conf /etc/kervax-agent-*.conf; do
    [ -f "$c" ] && grep -qxF "docker_host=$OLD_HOST" "$c" || continue
    sed -i "s#^docker_host=$OLD_HOST\$#docker_host=$SOCK#" "$c"
    chown kervax "$c" 2>/dev/null || true
    chmod 0600 "$c"
    case "$c" in /etc/kervax-agent.conf) u=kervax-agent ;; *) u=${c#/etc/kervax-agent-}; u=kervax-agent@${u%.conf} ;; esac
    systemctl try-restart "$u" 2>/dev/null || true
    echo "· $c: docker_host=$SOCK, $u restarted."
  done
}

install -d -o root -g kervax -m 2770 "$SOCK_DIR"

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
  docker stop "$OLD" >/dev/null
  # the socket is created by the proxy (nobody, docker's group, kervax's group added) in the
  # setgid directory: it gets the kervax group and 0660, nobody else gets to it
  if docker run -d --name "$NAME" --restart unless-stopped \
       --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
       --user "65534:$DGID" --group-add "$KGID" \
       -v /var/run/docker.sock:/var/run/docker.sock:ro -v "$SOCK_DIR:/run/kervax" \
       "$IMAGE" -loglevel warn -proxysocketendpoint /run/kervax/docker.sock -proxysocketendpointfilemode 0660 \
       -shutdowngracetime 1 -allowGET "$ALLOW_GET" -allowPOST "$ALLOW_POST" >/dev/null \
     && { for _ in 1 2 3 4 5 6 7 8; do probe && break; sleep 1; done; probe; }; then
    docker rm -f "$OLD" >/dev/null
    echo "✓ $NAME hardened: a unix socket only the agent's group reaches, no network, no capabilities, read-only root."
  else
    echo "✗ the hardened proxy did not come up - putting the old one back." >&2
    docker logs --tail 5 "$NAME" 2>&1 | sed 's/^/  /' >&2 || true
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker rename "$OLD" "$NAME"
    docker start "$NAME" >/dev/null
    exit 1
  fi
fi
switch_agents
install -d -m 0755 "$VERDIR"
echo "$KERVAX_SETUP_VERSION" > "$MARK"
chmod 0644 "$MARK"  # explicit: the agent (kervax) must read it; the installer runs helpers under umask 077
