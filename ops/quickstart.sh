#!/bin/sh
# Kervax: deploy the panel on a clean server with one command.
#
#   curl -fsSL https://raw.githubusercontent.com/mihsergeev/Kervax/main/ops/quickstart.sh | sudo sh
#
# What it does: installs Docker (if missing), brings up caddy-docker-proxy for TLS,
# clones the repository, generates the secrets, starts the panel and prints its
# address together with the admin password.
#
# The default domain is <public-IP>.sslip.io. sslip.io is a public DNS service that
# answers with the address written in the name itself: 203.0.113.10.sslip.io →
# 203.0.113.10. Such a name needs no registration, so Let's Encrypt issues an
# ordinary certificate for it and the panel opens over https right away. For a
# permanent installation prefer your own domain:
#   ... | sudo sh -s -- --domain kervax.example.com
#
# Main options:
#   --domain <name>      domain instead of <IP>.sslip.io
#   --allow-ips "A B"    let only these addresses reach the panel (see below)
#   --build              build images from source instead of pulling from GHCR
#   --dir <path>         install directory (default /opt/kervax)
#
# Russian version of this script: ops/quickstart-ru.sh
set -eu

DOMAIN=""
ALLOW_IPS=""
BUILD=0
DIR=/opt/kervax
REPO=https://github.com/mihsergeev/Kervax.git

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --allow-ips) ALLOW_IPS="$2"; shift 2 ;;
        --build) BUILD=1; shift ;;
        --dir) DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
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
if [ -z "$DOMAIN" ]; then
    IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)
    [ -n "$IP" ] || IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
    [ -n "$IP" ] || die "could not determine the public IP — pass --domain"
    DOMAIN="$IP.sslip.io"
    say "No domain given, using $DOMAIN"
    echo "  (sslip.io resolves to $IP — Let's Encrypt will issue a certificate for that name)"
else
    say "Domain: $DOMAIN"
fi

# ── 4. Caddy ─────────────────────────────────────────────────────────────────
docker network inspect caddy >/dev/null 2>&1 || docker network create caddy >/dev/null
if docker ps --format '{{.Image}}' | grep -q caddy-docker-proxy; then
    say "caddy-docker-proxy is already running"
else
    say "Starting caddy-docker-proxy (TLS and certificates)"
    mkdir -p /srv/caddy
    cat > /srv/caddy/compose.yml <<'YML'
services:
  caddy:
    image: lucaslorentz/caddy-docker-proxy:2.10-alpine
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    environment:
      CADDY_INGRESS_NETWORKS: caddy
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./data:/data
    networks: [caddy]
networks:
  caddy:
    external: true
YML
    (cd /srv/caddy && docker compose up -d >/dev/null) || die "caddy failed to start"
fi

# ── 5. Repository ────────────────────────────────────────────────────────────
if [ -d "$DIR/.git" ]; then
    say "Updating $DIR"
    git -C "$DIR" pull --ff-only >/dev/null || die "git pull failed"
elif [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    die "directory $DIR is not empty and is not a Kervax repository.
  Remove it (rm -rf $DIR) or install the panel elsewhere: --dir /path"
else
    say "Cloning into $DIR"
    command -v git >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq git) >/dev/null 2>&1
    # Clone next to the target and move the finished copy into place: a clone
    # interrupted halfway otherwise leaves a non-empty directory without .git, after
    # which a retry is impossible — it can neither clone (occupied) nor pull (not a
    # repository).
    rm -rf "$DIR.tmp"
    git clone -q "$REPO" "$DIR.tmp" || { rm -rf "$DIR.tmp"; die "could not clone $REPO"; }
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
FILES=$(printf -- '-f %s ' $(echo "$COMPOSE_LIST" | tr ':' ' '))

if [ -f .env ]; then
    say ".env already exists — leaving it as is"
    ADMIN_PW=$(grep '^KERVAX_ADMIN_PASSWORD=' .env | cut -d= -f2-)
else
    say "Writing .env with random secrets"
    cp .env.example .env
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

# ── 7. Start ─────────────────────────────────────────────────────────────────
if [ "$BUILD" = "1" ]; then
    say "Building images from source (a few minutes, needs about 2 GB of RAM)"
    docker compose $FILES up -d --build || die "the build failed"
else
    say "Pulling released images from GHCR"
    # --pull always is required: without it a repeated run starts the image already
    # on disk, and the "upgrade" only updates the repository files. The panel stays
    # on the old version — silently.
    docker compose $FILES up -d --pull always || die "startup failed"
fi

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
code=""
if [ -z "$ALLOW_IPS" ]; then
    i=0
    while [ $i -lt 30 ]; do
        code=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/api/health" --max-time 5 || true)
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
CADDY_CT=$(docker ps --format '{{.Names}} {{.Image}}' | grep caddy-docker-proxy | head -1 | cut -d' ' -f1)
RATE_LIMITED=0
if [ -n "$CADDY_CT" ] && docker logs "$CADDY_CT" --since 10m 2>&1 | grep -q rateLimited; then
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
if [ -z "$ALLOW_IPS" ]; then
    printf '\n\033[33m  WARNING: the panel is open to the whole internet (the allow-list is\n'
    printf '  empty) — only the password protects it. Restrict access with:\n'
    printf '    KERVAX_ALLOW_IPS="your.ip" in %s/.env, then\n' "$DIR"
    printf '    docker compose %s up -d frontend\033[0m\n' "$FILES"
fi
printf '\033[32m════════════════════════════════════════════════════════\033[0m\n\n'
printf 'Next: change the password, enable 2FA in the ⚙ menu, and add your first server\n'
printf 'with the "Add server" button — the panel will show the agent install command.\n'
