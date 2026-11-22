#!/bin/sh
# Kervax: deploy the panel on a clean server with one command.
#
#   curl -fsSL https://raw.githubusercontent.com/mihsergeev/Kervax/main/ops/quickstart.sh | sudo sh
#
# What it does: installs Docker (if missing), brings up caddy-docker-proxy for TLS,
# clones the repository, generates the secrets, starts the panel and prints its
# address together with the admin password.
#
# Only this script comes from main. The repository is checked out at the latest release
# tag and the images are pinned to it (KERVAX_VERSION in .env): the files and the images
# are one released version, and a commit in main reaches an installation only through a
# release. A repeated run upgrades to the newest release.
#
# The default domain is <public-IP>.sslip.io. sslip.io is a public DNS service that
# answers with the address written in the name itself: 203.0.113.10.sslip.io →
# 203.0.113.10. Such a name needs no registration, so Let's Encrypt issues an
# ordinary certificate for it and the panel opens over https right away. For a
# permanent installation prefer your own domain:
#   ... | sudo sh -s -- --domain kervax.example.com
#
# A server in a local network (a lab VM, a home LAN) is out of Let's Encrypt's reach,
# and a home router often refuses to resolve <private-IP>.sslip.io at all (DNS
# rebinding protection). For that case:
#   ... | sudo sh -s -- --local-ca
# The panel opens at https://<LAN-IP>, its certificate comes from caddy's internal CA,
# and the agent install command in the panel carries that CA's root.
#
# Main options:
#   --domain <name>      domain instead of <IP>.sslip.io
#   --local-ca           local network: caddy's internal CA instead of Let's Encrypt,
#                        the LAN address instead of sslip.io (docs/install.md)
#   --allow-ips "A B"    let only these addresses reach the panel (see below)
#   --build              build images from source instead of pulling from GHCR
#   --dir <path>         install directory (default /opt/kervax)
#   --version <vX.Y.Z>   this release instead of the latest one
#   --allow-downgrade    let a repeated run go to an older release (see step 5)
#
# Russian version of this script: ops/quickstart-ru.sh
set -eu

DOMAIN=""
ALLOW_IPS=""
BUILD=0
LOCAL_CA=0
DIR=/opt/kervax
VERSION=""
ALLOW_DOWNGRADE=0
REPO=https://github.com/mihsergeev/Kervax.git

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --allow-ips) ALLOW_IPS="$2"; shift 2 ;;
        --build) BUILD=1; shift ;;
        --local-ca) LOCAL_CA=1; shift ;;
        --dir) DIR="$2"; shift 2 ;;
        --version) VERSION="$2"; shift 2 ;;
        --allow-downgrade) ALLOW_DOWNGRADE=1; shift ;;
        -h|--help) sed -n '2,39p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ "$(id -u)" = "0" ] || { echo "Run as root (sudo)." >&2; exit 1; }

say() { printf '\n\033[1m→ %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# The script is often run right after removing the panel — from a directory that no
# longer exists. git and docker then fail with an opaque "Unable to read current
# working directory", and the script blames a failed clone: the wrong cause, sending
# you to look in the wrong place. Move to the root, expanding a relative --dir first,
# while the current directory can still be read.
case "$DIR" in
    /*) ;;
    *) DIR="$(pwd 2>/dev/null || echo /root)/$DIR" ;;
esac
cd / || die "could not change to /"

# ── 1. Ports ─────────────────────────────────────────────────────────────────
# Caddy needs ports 80/443 to obtain a certificate. A busy port is the most common
# reason for "the certificate was not issued". Checked BEFORE installing Docker:
# otherwise a machine that cannot host the panel would still be left with Docker on it.
for port in 80 443; do
    if ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$port\$"; then
        # our own caddy on that port is fine, but docker may not be installed yet —
        # in which case the port is definitely held by something else
        { command -v docker >/dev/null 2>&1 &&
          docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ":$port->"; } ||
            die "port $port is taken by another process — free it and try again"
    fi
done

# ── 2. Docker ────────────────────────────────────────────────────────────────
if command -v docker >/dev/null 2>&1; then
    say "Docker is already installed: $(docker --version)"
else
    say "Installing Docker"
    curl -fsSL https://get.docker.com | sh >/dev/null || die "could not install Docker"
    echo "  $(docker --version)"
fi
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the compose plugin)"

# ── 3. Domain ────────────────────────────────────────────────────────────────
# A repeated run (an upgrade: quickstart without arguments) keeps what the first one
# chose. Otherwise a panel installed with --local-ca was switched back to Let's Encrypt
# and to <public-IP>.sslip.io, and the address everyone uses stopped answering.
if [ -f "$DIR/.env" ]; then
    grep -q '^COMPOSE_FILE=.*compose\.localca\.yml' "$DIR/.env" && LOCAL_CA=1
    [ -n "$DOMAIN" ] || DOMAIN=$(grep '^KERVAX_DOMAIN=' "$DIR/.env" | cut -d= -f2-)
fi
if [ -z "$DOMAIN" ] && [ "$LOCAL_CA" = "1" ]; then
    # The panel is addressed by its LAN IP itself: no DNS name means nothing to put into
    # /etc/hosts on every machine, and nothing for a router to block. caddy's internal
    # CA issues certificates for an IP address.
    IP=$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)
    [ -n "$IP" ] || IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -n "$IP" ] || die "could not determine the LAN address, pass --domain"
    DOMAIN="$IP"
    say "Local network, the panel is addressed by IP: $DOMAIN"
    echo "  (the certificate comes from caddy's internal CA, see the end of the output)"
elif [ -z "$DOMAIN" ]; then
    IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
    [ -n "$IP" ] || IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
    [ -n "$IP" ] || die "could not determine the public IP — pass --domain"
    DOMAIN="$IP.sslip.io"
    say "No domain given, using $DOMAIN"
    echo "  (sslip.io resolves to $IP — Let's Encrypt will issue a certificate for that name)"
else
    say "Domain: $DOMAIN"
    if [ "$LOCAL_CA" = "1" ]; then
        echo "  (caddy's internal CA. Every agent machine and your computer must resolve"
        echo "  this name to the panel: local DNS or /etc/hosts, see docs/install.md)"
    fi
fi

# ── 4. Caddy ─────────────────────────────────────────────────────────────────
# --ipv6 here is not cosmetic. In an IPv4-only network docker forwards IPv6
# connections through its own userland proxy, and caddy then sees the bridge gateway
# instead of the client: an IP allow-list cannot tell anyone apart and lets every
# IPv6 client in. With IPv6 on the network the port is forwarded by ip6tables and
# the real address arrives. An older docker that cannot do it falls back to IPv4,
# and then the ports below are bound to IPv4 so that no IPv6 client reaches caddy.
if ! docker network inspect caddy >/dev/null 2>&1; then
    docker network create --ipv6 caddy >/dev/null 2>&1 || docker network create caddy >/dev/null
fi
NET_IPV6=$(docker network inspect caddy -f '{{.EnableIPv6}}' 2>/dev/null)
if [ "$NET_IPV6" = "true" ]; then
    CADDY_PORTS='["80:80", "443:443"]'
else
    CADDY_PORTS='["0.0.0.0:80:80", "0.0.0.0:443:443"]'
fi
if docker ps --format '{{.Image}}' | grep -q caddy-docker-proxy; then
    say "caddy-docker-proxy is already running"
    if [ "$NET_IPV6" != "true" ] && ip -6 addr show scope global 2>/dev/null | grep -q inet6; then
        say "NOTE: the caddy network has no IPv6 and this host has a public one"
        echo "  An IPv6 client then reaches caddy from the bridge gateway, and the IP"
        echo "  allow-list cannot stop it. Either publish caddy's ports on 0.0.0.0 only,"
        echo "  or recreate the network with IPv6 (stop every stack in it, then"
        echo "  docker network rm caddy && docker network create --ipv6 caddy)."
    fi
else
    say "Starting caddy-docker-proxy (TLS and certificates)"
    mkdir -p /srv/caddy
    # caddy faces the internet, so it does not get the docker socket (a hole in caddy would
    # be root on the host): it reads the container list, networks and events through a
    # read-only proxy that only caddy may connect to
    DGID=$(stat -c %g /var/run/docker.sock)
    cat > /srv/caddy/compose.yml <<'YML'
services:
  caddy:
    image: local/kervax-caddy-docker-proxy:2.11.9
    pull_policy: build
    build:
      context: .
      dockerfile_inline: |
        FROM caddy:2.11.9-builder-alpine@sha256:646fee578e680989ca5c7fd1ce6f1de574d19fd71ef2221810cc0510a9f40c86 AS xcaddy
        FROM golang:1.27.4-alpine@sha256:70bd7ddea8b6cbe5d6943cde279d6b67feb10cc924fb9100d1d8757c1575b280 AS build
        COPY --from=xcaddy /usr/bin/xcaddy /usr/bin/xcaddy
        RUN GOTOOLCHAIN=local xcaddy build v2.11.9 --output /out/caddy \
              --with github.com/lucaslorentz/caddy-docker-proxy/v2@v2.13.1 \
              --replace golang.org/x/net=golang.org/x/net@v0.62.0
        FROM caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
        RUN apk upgrade --no-cache
        COPY --from=build /out/caddy /usr/bin/caddy
        CMD ["caddy", "docker-proxy"]
    restart: unless-stopped
    ports: __PORTS__
    environment:
      CADDY_INGRESS_NETWORKS: caddy
      DOCKER_HOST: tcp://docker-api:2375
    volumes:
      - ./data:/data
    networks: [caddy, docker-api]
    depends_on: [docker-api]
  docker-api:
    image: wollomatic/socket-proxy:1.13.2@sha256:ff95d6ff6c27853005ef85eb8f4ab5923dc3d2f1e055f98fb6aeeaceec3fe848
    restart: unless-stopped
    user: "65534:__DGID__"
    read_only: true
    mem_limit: 64M
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks: [docker-api]
    command:
      - -loglevel=warn
      - -listenip=0.0.0.0
      - -allowfrom=caddy
      - -shutdowngracetime=5
      - -watchdoginterval=600
      - -stoponwatchdog
      - -allowHEAD=/_ping
      - -allowGET=/(v[0-9]+\.[0-9]+/)?(_ping|version|info|events|containers/json|networks(/[a-zA-Z0-9][a-zA-Z0-9_.-]*)?)
networks:
  caddy:
    external: true
  docker-api:
    internal: true
YML
    sed -i "s|__PORTS__|$CADDY_PORTS|; s|__DGID__|$DGID|" /srv/caddy/compose.yml
    (cd /srv/caddy && docker compose up -d >/dev/null) || die "caddy failed to start"
fi

# ── 5. Repository ────────────────────────────────────────────────────────────
# A release, never main: the scripts below run as root and the images have to match them.
# The latest release is the highest stable vX.Y.Z tag whose images are already in the registry -
# the order ops/release-latest.sh moves the latest images in. GitHub's "latest release" is not
# used: a backport published after a newer version becomes it and would take the panel back. A
# tag comes minutes before its images (the Release workflow), and a failed workflow leaves a tag
# without them for good: such a tag is passed over, before anything in $DIR or .env changes.
# --build builds the images itself and takes the highest tag.
command -v git >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq git) >/dev/null 2>&1
IMAGES=ghcr.io/mihsergeev      # the owner in lower case, as release.yml publishes it
stable_tags() {                # stable tags of the repository, highest first
    git ls-remote --tags --refs "$REPO" 'refs/tags/v*' 2>/dev/null | cut -f2 | sed -n 's#^refs/tags/##p' \
        | grep -E '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
        | sort -t . -k 1.2,1nr -k 2,2nr -k 3,3nr
}
published() {                  # both images of release $1 are in the registry
    docker manifest inspect "$IMAGES/kervax-backend:${1#v}" >/dev/null 2>&1 &&
        docker manifest inspect "$IMAGES/kervax-frontend:${1#v}" >/dev/null 2>&1
}
# ver_lt A B: version A (without v) is lower than B, by numbers; pre-release suffixes are ignored
ver_lt() {
    [ "${1%%-*}" != "${2%%-*}" ] &&
        [ "$(printf '%s\n%s\n' "${1%%-*}" "${2%%-*}" | sort -t . -k 1,1n -k 2,2n -k 3,3n | head -n 1)" = "${1%%-*}" ]
}
if [ -n "$VERSION" ]; then
    TAG="v${VERSION#v}"
    # the whole value, not line by line: a newline would smuggle anything past grep
    case "$TAG" in *[!0-9A-Za-z.-]*) TAG="" ;; esac
    printf '%s\n' "$TAG" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?' \
        || die "--version must look like vX.Y.Z"
    [ "$BUILD" = 1 ] || published "$TAG" || die "the images of $TAG are not in $IMAGES (not published yet, or is ghcr.io out of reach?).
  Pick another release, wait a few minutes after a fresh tag, or build from source: --build"
else
    TAG=""
    n=0
    for t in $(stable_tags); do
        if [ "$BUILD" = 1 ] || published "$t"; then TAG=$t; break; fi
        echo "  $t: its images are not in $IMAGES (yet), looking at the previous release"
        n=$((n + 1))
        [ "$n" -lt 5 ] || break
    done
    [ -n "$TAG" ] || [ "$n" = 0 ] || die "none of the latest $n releases has its images in $IMAGES: is ghcr.io reachable?
  Or build from source: --build"
    [ -n "$TAG" ] || die "could not get the releases from github.com: is it reachable? Or pass --version vX.Y.Z"
fi
if [ -d "$DIR/.git" ]; then
    say "Updating $DIR to $TAG"
    # only that tag: a tag that changed on the server is refused, not taken over
    git -C "$DIR" fetch -q origin "refs/tags/$TAG:refs/tags/$TAG" >/dev/null || die "could not fetch $TAG into $DIR"
    # Never backwards on its own: the database was migrated by the newer code, and the older
    # backend fails at start on a revision it does not know (alembic) and restarts in a loop.
    CUR=$(grep '^KERVAX_VERSION=' "$DIR/.env" 2>/dev/null | tail -n 1 | cut -d= -f2-)
    printf '%s\n' "$CUR" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?' || CUR=""
    BACK=""
    if [ -n "$CUR" ]; then
        # the installed release is known: only its number matters (an upgrade from a backport
        # release is not a descendant of the newer tag, and that is fine)
        ver_lt "${TAG#v}" "$CUR" && BACK="$TAG is older than the installed $CUR.
  The database may already have a newer schema, and the older panel would not start on it.
  Pass --allow-downgrade only together with a backup of the $TAG database to restore"
    elif grep '^COMPOSE_FILE=' "$DIR/.env" 2>/dev/null | grep -q 'compose\.ghcr\.yml'; then
        # Installed from main before the panel was pinned to releases, on the latest images: the
        # database follows the images (the highest release), not the files in $DIR. The latest
        # release with images is that one; an explicitly older one could be below it.
        TOP=$(stable_tags | head -n 1)
        if [ -n "$VERSION" ] && [ -n "$TOP" ] && ver_lt "${TAG#v}" "${TOP#v}"; then
            BACK="$TAG is older than $TOP, the latest images the panel may run on.
  Pass --allow-downgrade only together with a backup of the $TAG database to restore"
        else
            echo "  The panel ran the latest images: from now on it is pinned to $TAG."
        fi
    elif ! git -C "$DIR" merge-base --is-ancestor HEAD "refs/tags/$TAG" 2>/dev/null; then
        # built from source of a commit that $TAG does not contain: its migrations may be in the database
        BACK="$DIR is built from commit $(git -C "$DIR" rev-parse --short HEAD 2>/dev/null), which $TAG does not contain,
  and the database may already have migrations newer than $TAG. Wait for a release that contains
  that commit and run this again. If it never comes (the commit left main), restore a backup made
  on $TAG and pass --allow-downgrade"
    fi
    if [ -n "$BACK" ]; then
        [ "$ALLOW_DOWNGRADE" = 1 ] || die "not going back: $BACK."
        say "WARNING: going back on --allow-downgrade: $BACK"
        echo "  A database schema newer than $TAG stops the backend at start: restore a matching backup."
    fi
    git -C "$DIR" checkout -q --detach "refs/tags/$TAG" >/dev/null || die "could not switch $DIR to $TAG"
elif [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    die "directory $DIR is not empty and is not a Kervax repository.
  Remove it (rm -rf $DIR) or install the panel elsewhere: --dir /path"
else
    say "Cloning $TAG into $DIR"
    # Clone next to the target and move the finished copy into place: a clone
    # interrupted halfway otherwise leaves a non-empty directory without .git, after
    # which a retry is impossible — it can neither clone (occupied) nor pull (not a
    # repository).
    rm -rf "$DIR.tmp"
    git clone -q --branch "$TAG" "$REPO" "$DIR.tmp" || { rm -rf "$DIR.tmp"; die "could not clone $TAG of $REPO"; }
    rm -rf "$DIR"
    mv "$DIR.tmp" "$DIR"
fi
cd "$DIR"

# ── 6. Configuration ─────────────────────────────────────────────────────────
# The set of overlays is decided HERE because it has to be written into .env (below).
if [ "$BUILD" = "1" ]; then
    COMPOSE_LIST="compose.yml:compose.caddy.yml"
else
    COMPOSE_LIST="compose.yml:compose.ghcr.yml:compose.caddy.yml"
fi
[ "$LOCAL_CA" = "1" ] && COMPOSE_LIST="$COMPOSE_LIST:compose.localca.yml"
FILES=$(printf -- '-f %s ' $(echo "$COMPOSE_LIST" | tr ':' ' '))

if [ -f .env ]; then
    say ".env already exists — leaving it as is"
    ADMIN_PW=$(grep '^KERVAX_ADMIN_PASSWORD=' .env | cut -d= -f2-)
else
    say "Writing .env with random secrets"
    # 0600 from the start: with the default umask the copy was readable by everyone until
    # the chmod below, and the secrets go into it in between
    (umask 077 && cp .env.example .env)
    ADMIN_PW=$(openssl rand -base64 18)
    set_env() { sed -i "s|^$1=.*|$1=$2|" .env; }
    set_env KERVAX_ADMIN_PASSWORD "$ADMIN_PW"
    set_env KERVAX_JWT_SECRET "$(openssl rand -hex 32)"
    set_env KERVAX_DB_PASSWORD "$(openssl rand -hex 24)"
    set_env KERVAX_DOMAIN "$DOMAIN"
    set_env KERVAX_PANEL_URL "https://$DOMAIN"
    set_env KERVAX_ALLOW_IPS "$ALLOW_IPS"
    chmod 600 .env
fi

# COMPOSE_FILE is written ALWAYS, including into an existing .env. Without it,
# "docker compose up -d" in this directory sees only the base compose.yml: it builds
# from source instead of pulling the released images and recreates the containers
# without the caddy network and its labels — the panel silently disappears from its
# domain. That is exactly what the documented upgrade command does, since it carries
# no -f flags.
if grep -q '^COMPOSE_FILE=' .env; then
    sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$COMPOSE_LIST|" .env
else
    printf 'COMPOSE_FILE=%s\n' "$COMPOSE_LIST" >> .env
fi

# KERVAX_VERSION is written ALWAYS as well: it pins the images (compose.ghcr.yml) to the
# release checked out above. Without it they follow latest, and the panel's files and
# images drift apart.
if grep -q '^KERVAX_VERSION=' .env; then
    sed -i "s|^KERVAX_VERSION=.*|KERVAX_VERSION=${TAG#v}|" .env
else
    printf 'KERVAX_VERSION=%s\n' "${TAG#v}" >> .env
fi

# KERVAX_PROXY_GATEWAY is written ALWAYS too. The caddy overlay allows that one
# address so a local probe works: the agent checks a whitelisted site from inside
# its own server, docker publishes the port through its proxy, and caddy sees the
# gateway of its network rather than the loopback. One address, never the whole
# 172.16.0.0/12 - a range there would also let in every neighbouring container and,
# on an IPv4-only network, every IPv6 client.
PROXY_GW=$(docker network inspect caddy \
    -f '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}} {{end}}{{end}}' 2>/dev/null \
    | sed 's/ *$//')
if grep -q '^KERVAX_PROXY_GATEWAY=' .env; then
    sed -i "s|^KERVAX_PROXY_GATEWAY=.*|KERVAX_PROXY_GATEWAY=$PROXY_GW|" .env
else
    printf 'KERVAX_PROXY_GATEWAY=%s\n' "$PROXY_GW" >> .env
fi

# ── 7. Start ─────────────────────────────────────────────────────────────────
sh "$DIR/ops/agent-proxy-peers-setup.sh" "$DIR" $FILES || die "proxy peer snapshot could not be prepared"
if [ "$BUILD" = "1" ]; then
    say "Building images from source (a few minutes, needs about 2 GB of RAM)"
    docker compose $FILES up -d --build || die "the build failed"
else
    say "Pulling released images from GHCR"
    # --pull always is required: without it a repeated run starts the image already
    # on disk, and the "upgrade" only updates the repository files. The panel stays
    # on the old version — silently.
    docker compose $FILES up -d --pull always || die "startup failed (the images of a release appear a few minutes after its tag)"
fi

sh "$DIR/ops/agent-proxy-peers-setup.sh" "$DIR" $FILES || die "proxy peer snapshot could not be refreshed"
say "Waiting for the panel to answer"
# Ask the panel from the INSIDE rather than over its public address. From outside we
# would be connecting from the server's own address, which the allow-list
# (--allow-ips) does not contain — caddy aborts such a connection, so the check would
# always report "no answer" while the panel works fine. The internal health endpoint
# tells exactly what is needed here: the application is up and the database answers.
ready=0
i=0
while [ $i -lt 90 ]; do
    # </dev/null is MANDATORY here. The script is run as "curl … | sh": its own text
    # arrives on stdin and is read as execution proceeds. docker compose exec reads
    # standard input (and -T does not change that), so it swallows the unread
    # remainder — sh then dies mid-line with "Unterminated quoted string". From a
    # file everything works; the main scenario is the one that breaks.
    if docker compose $FILES exec -T backend \
        python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=3)" \
        </dev/null >/dev/null 2>&1; then
        ready=1
        break
    fi
    i=$((i + 1))
    sleep 2
done

# The public address is only checked when the panel is open to everyone: otherwise a
# request from the server is doomed (see above). It doubles as a certificate check.
CADDY_CT=$(docker ps --format '{{.Names}} {{.Image}}' | grep caddy-docker-proxy | head -1 | cut -d' ' -f1)

# Local network: the root of caddy's internal CA goes to data/panel-ca.crt. The panel
# puts it into the agent install command (install.sh --ca-file trusts it on the node),
# and the https check below verifies the panel with it instead of skipping the check.
# caddy creates the CA when it loads a site with `tls internal`, a few seconds after the
# panel's container appears. Read through docker: caddy may be someone else's, with its
# data not in /srv/caddy.
CA_FILE="$DIR/data/panel-ca.crt"
CURL_CA=""
if [ "$LOCAL_CA" = "1" ]; then
    say "Taking the root of caddy's internal CA"
    i=0
    root=""
    while [ $i -lt 30 ] && [ -n "$CADDY_CT" ]; do
        # </dev/null: see the note on docker compose exec above
        root=$(docker exec "$CADDY_CT" cat /data/caddy/pki/authorities/local/root.crt </dev/null 2>/dev/null || true)
        case "$root" in *"BEGIN CERTIFICATE"*) break ;; esac
        root=""
        i=$((i + 1))
        sleep 2
    done
    if [ -n "$root" ]; then
        mkdir -p "$DIR/data"
        # data/ is writable by the panel's container, already running: a random name created with
        # O_EXCL (noclobber) and 0644 from umask, no fixed .tmp a planted symlink could redirect
        ca_tmp=$(mktemp -u "$CA_FILE.XXXXXXXXXX") && (umask 022 && set -C && printf '%s\n' "$root" > "$ca_tmp") \
            && mv -fT "$ca_tmp" "$CA_FILE"
        CURL_CA="--cacert $CA_FILE"
        # a name is checked against this server itself: it may well have no DNS record
        case "$DOMAIN" in *[!0-9.]*) CURL_CA="$CURL_CA --resolve $DOMAIN:443:127.0.0.1" ;; esac
        echo "  $CA_FILE"
    else
        echo "  caddy has not created it yet: docker logs ${CADDY_CT:-<caddy container>} --tail=50"
    fi
fi

code=""
if [ -z "$ALLOW_IPS" ]; then
    i=0
    while [ $i -lt 30 ]; do
        code=$(curl -s -o /dev/null -w '%{http_code}' $CURL_CA "https://$DOMAIN/api/health" --max-time 5 || true)
        [ "$code" = "200" ] && break
        i=$((i + 1))
        sleep 2
    done
fi

# Let's Encrypt counts issuances per SET of names: five for the same name per 168
# hours. Reinstalling the panel while deleting the caddy directory throws away the
# certificate already issued and asks for a new one — on the sixth attempt issuance is
# refused and the panel goes quiet without a word of explanation. Saying so outright
# is cheaper than guessing.
RATE_LIMITED=0
if [ "$LOCAL_CA" != "1" ] && [ -n "$CADDY_CT" ] && docker logs "$CADDY_CT" --since 10m 2>&1 | grep -q rateLimited; then
    RATE_LIMITED=1
fi

printf '\n\033[32m════════════════════════════════════════════════════════\033[0m\n'
if [ "$RATE_LIMITED" = "1" ]; then
    printf '  The panel is running, but the certificate for %s\n' "$DOMAIN"
    printf '  \033[33mwas not issued: Let'"'"'s Encrypt refused on a rate limit\033[0m (5 certificates\n'
    printf '  per name per 168 hours — usually the result of repeated reinstalls).\n'
    printf '  What to do: use a different name (--domain other.%s)\n' "$DOMAIN"
    printf '  or wait for the limit to lift. The exact time is in the log:\n'
    printf '    docker logs %s | grep rateLimited\n' "$CADDY_CT"
    printf '  For the future: do not delete /srv/caddy/data — the certificates live there.\n'
elif [ "$ready" = "1" ] && { [ -z "$ALLOW_IPS" ] && [ "$code" = "200" ] || [ -n "$ALLOW_IPS" ]; }; then
    printf '  The panel is ready:  \033[1mhttps://%s\033[0m\n' "$DOMAIN"
    if [ -n "$ALLOW_IPS" ]; then
        printf '  Open it from the allowed addresses: %s\n' "$ALLOW_IPS"
        printf '  From the server itself it deliberately does not answer — it is not on the list.\n'
    fi
elif [ "$ready" = "1" ]; then
    printf '  The panel works, but https has not answered yet (the first certificate\n'
    printf '  takes up to a minute). Address: https://%s\n' "$DOMAIN"
    printf '  If nothing appears in a couple of minutes: docker compose %s logs caddy --tail=50\n' "$FILES"
else
    printf '  The containers are up, but the panel does not answer from the inside yet.\n'
    printf '  Address: https://%s\n' "$DOMAIN"
    printf '  Check: docker compose %s logs --tail=50\n' "$FILES"
fi
printf '  Login:     \033[1madmin\033[0m\n'
printf '  Password:  \033[1m%s\033[0m\n' "$ADMIN_PW"
printf '  It is also stored in %s/.env\n' "$DIR"
if [ "$LOCAL_CA" = "1" ]; then
    printf '\n  The certificate is issued by caddy'"'"'s internal CA, which nobody trusts yet.\n'
    if [ -f "$CA_FILE" ]; then
        FP=$(openssl x509 -in "$CA_FILE" -noout -fingerprint -sha256 2>/dev/null | cut -d= -f2)
        printf '  Its root: %s\n' "$CA_FILE"
        [ -n "$FP" ] && printf '  SHA-256:  %s\n' "$FP"
        printf '  Agents: nothing to do by hand. The install command in the panel carries\n'
        printf '  this root, and install.sh adds it to the system trust store of the node.\n'
        printf '  Browser: import the root as a trusted authority, it is served at\n'
        printf '    https://%s/api/agent/ca.crt (or copy the file above).\n' "$DOMAIN"
        printf '  Any other Debian/Ubuntu machine (curl, scripts):\n'
        printf '    cp panel-ca.crt /usr/local/share/ca-certificates/kervax-panel.crt && update-ca-certificates\n'
    fi
    case "$DOMAIN" in
        *[!0-9.]*)
            printf '  The name %s must resolve on every agent machine and on your computer:\n' "$DOMAIN"
            printf '  local DNS or a line in /etc/hosts (see docs/install.md).\n' ;;
    esac
fi
if [ -z "$ALLOW_IPS" ] && [ "$LOCAL_CA" = "1" ]; then
    printf '\n\033[33m  WARNING: the whole local network reaches the panel, only the password\n'
    printf '  protects it. Restrict access with:\n'
    printf '    KERVAX_ALLOW_IPS="your.ip" in %s/.env, then\n' "$DIR"
    printf '    docker compose %s up -d frontend\033[0m\n' "$FILES"
elif [ -z "$ALLOW_IPS" ]; then
    printf '\n\033[33m  WARNING: the panel is open to the whole internet (the allow-list is\n'
    printf '  empty) — only the password protects it. Restrict access with:\n'
    printf '    KERVAX_ALLOW_IPS="your.ip" in %s/.env, then\n' "$DIR"
    printf '    docker compose %s up -d frontend\033[0m\n' "$FILES"
fi
printf '\033[32m════════════════════════════════════════════════════════\033[0m\n\n'
printf 'Next: change the password, enable 2FA in the ⚙ menu, and add your first server\n'
printf 'with the "Add server" button — the panel will show the agent install command.\n'
