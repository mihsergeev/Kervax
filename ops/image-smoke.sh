#!/bin/bash
# Smoke test of a built backend image: does it run the way production runs it.
#
#   ops/image-smoke.sh <image>
#
# CI runs it after "Build image" (job image-scan). Locally it needs Linux docker, openssl
# (3.x), gzip, sha256sum and the repository this script lives in: the signed variant below
# is built from it.
#
# 1. Read-only runtime. Since 1.8.163 code, migrations and the files served to agents in
#    /srv belong to root; the panel writes only to /data (KERVAX_DATA_DIR) and /srv/data.
#    The image runs with --read-only, tmpfs /tmp and a volume for /data against a real
#    PostgreSQL (same image as the migrations job): alembic upgrade head on an empty base,
#    then the image's CMD (alembic again + uvicorn), /api/health with the version of
#    backend/app/config.py, a clean stop, then the separate scheduler of compose.scale.yml
#    for a while. Logs must have no Traceback, Permission denied or
#    Read-only file system. A read-only root hides a writable /srv, so that is checked
#    without it: as kervax, find /srv -writable must give /srv/data and nothing else.
# 2. Served agent == signed manifest (the 1.90 incident: the panel served a binary whose
#    sha256 differed from the signed one). There is no signing key in CI, so a throwaway
#    Ed25519 key is made in a temp dir: the agent stage is built with its public key, a
#    manifest is signed for those binaries, and the image is built again with that
#    manifest in agent-dist/ next to stale decoy binaries (what broke 1.90). The agent
#    stage comes from the build cache, and the Go build is reproducible anyway. The panel
#    must serve the manifest (200: no release problem) and its signature, binaries with
#    the signed sha256 and the key built in, gz copies that unpack to the same bytes, and
#    must refuse the manifest (503) once a served binary is tampered with.
#
# Containers sit on an --internal docker network (no way out). Containers, volume,
# network, the variant images and the temp dir with the key are removed at exit.
# Exit code 0 - the image passed, 1 - it did not (the reason and container logs above).
set -euo pipefail

IMAGE=${1:?usage: ops/image-smoke.sh <image>}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# the same image as the migrations job in .github/workflows/ci.yml and compose.yml
PG_IMAGE=postgres:18.7-alpine@sha256:ff64c9e3bdf8e2120107d3559de8db314e8305f7e38c5105903c6295876738ff
ID=kervax-smoke-$$
NET=$ID-net
VOL=$ID-data
VARIANT=kervax-smoke-signed:$$
TMP=$(mktemp -d)
CTS=()
# a write near the code ends in EROFS under --read-only; uid trouble in EACCES
BAD='Traceback|Permission denied|PermissionError|Read-only file system'

say() { echo "image-smoke: $*"; }

die() {
    echo "image-smoke: FAIL: $*" >&2
    # the step runs one container at a time: the failing one is the last started
    if [ ${#CTS[@]} -gt 0 ] && docker inspect "${CTS[-1]}" >/dev/null 2>&1; then
        echo "----- last 60 log lines of ${CTS[-1]}" >&2
        docker logs --tail 60 "${CTS[-1]}" >&2 2>&1 || true
        echo "----- end of ${CTS[-1]} log" >&2
    fi
    exit 1
}

cleanup() {
    rc=$?
    for c in "${CTS[@]}"; do docker rm -f -v "$c" >/dev/null 2>&1 || true; done
    docker volume rm "$VOL" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    docker rmi -f "$VARIANT" "$VARIANT-agent" >/dev/null 2>&1 || true
    rm -rf "$TMP"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

for tool in docker openssl gzip sha256sum timeout; do
    command -v "$tool" >/dev/null || die "$tool is not installed"
done
docker image inspect "$IMAGE" >/dev/null 2>&1 || die "no image $IMAGE (build it first)"
VERSION=$(sed -n 's/^    version: str = "\([^"]*\)"$/\1/p' "$ROOT/backend/app/config.py")
AGENT_VERSION=$(sed -n 's/^const version = "\([^"]*\)"$/\1/p' "$ROOT/agent/main.go")
if [ -z "$VERSION" ] || [ -z "$AGENT_VERSION" ]; then die "cannot read the panel or agent version"; fi

# GET inside the container: the body to stdout, the status to stderr (000 - no answer)
PYGET='import sys, urllib.request as u
try:
    r = u.urlopen("http://127.0.0.1:8000" + sys.argv[1], timeout=20)
except u.HTTPError as e:
    r = e
except OSError as e:
    sys.stderr.write("000 %s\n" % e)
    sys.exit(0)
sys.stdout.buffer.write(r.read())
sys.stderr.write("%d\n" % r.getcode())'

# http <container> <path> <file>: body into the file, status into $CODE
http() {
    CODE=$(timeout 60 docker exec -u kervax "$1" python -c "$PYGET" "$2" 2>&1 >"$3" | tail -1) || CODE=000
    CODE=${CODE%% *}
}

sha() { sha256sum "$1" | cut -d' ' -f1; }

# the panel as compose.yml runs it, but with a read-only root; throwaway secrets
PG_PASSWORD=$(openssl rand -hex 16)
RUN=(--network "$NET" --read-only --tmpfs /tmp --security-opt no-new-privileges:true
     -v "$VOL:/data" -e KERVAX_DATA_DIR=/data -e KERVAX_BIND=127.0.0.1:8080
     -e "KERVAX_DB_URL=postgresql+asyncpg://kervax:$PG_PASSWORD@pg:5432/kervax"
     -e "KERVAX_JWT_SECRET=$(openssl rand -hex 32)"
     -e "KERVAX_ADMIN_PASSWORD=$(openssl rand -hex 16)")

start() { # start <name> <image> [docker run options...] [-- command...]
    local name=$ID-$1 image=$2 opts=() cmd=()
    shift 2
    while [ $# -gt 0 ] && [ "$1" != -- ]; do opts+=("$1"); shift; done
    [ $# -gt 0 ] && { shift; cmd=("$@"); }
    CTS+=("$name")
    docker run -d --name "$name" "${RUN[@]}" "${opts[@]}" "$image" "${cmd[@]}" >/dev/null \
        || die "cannot start $name"
}

running() { [ "$(docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null)" = running ]; }

wait_health() { # wait_health <container>: /api/health 200 with this version
    local c=$1 i
    for i in $(seq 60); do
        running "$c" || die "$c exited before /api/health answered (alembic upgrade head or startup failed)"
        http "$c" /api/health "$TMP/health"
        [ "$CODE" = 200 ] && break
        sleep 2
    done
    [ "$CODE" = 200 ] || die "$c: /api/health did not answer 200 in 120 s (last: $CODE $(head -c 300 "$TMP/health"))"
    grep -qF "\"version\":\"$VERSION\"" "$TMP/health" \
        || die "$c: /api/health does not report version $VERSION: $(head -c 300 "$TMP/health")"
}

check_logs() { # check_logs <container>
    local hits
    hits=$(docker logs "$1" 2>&1 | grep -E -A 12 "$BAD" | head -60) || true
    [ -z "$hits" ] || die "$1 logged an error:
$hits"
}

stop_clean() { # stop_clean <container>: SIGTERM, exit code 0, clean log
    timeout 90 docker stop -t 60 "$1" >/dev/null || die "cannot stop $1"
    code=$(docker inspect -f '{{.State.ExitCode}}' "$1")
    [ "$code" = 0 ] || die "$1 exited with code $code after SIGTERM"
    check_logs "$1"
}

started=$(date +%s)

# --- 1. read-only runtime -------------------------------------------------------------
say "$IMAGE: what kervax (uid 10001) can write in /srv"
writable=$(timeout 60 docker run --rm --network none --entrypoint find -u 10001 "$IMAGE" /srv -writable) \
    || die "find /srv -writable failed in $IMAGE"
extra=$(printf '%s\n' "$writable" | grep -v -e '^/srv/data$' -e '^/srv/data/' -e '^$' || true)
[ -z "$extra" ] || die "kervax can write $(printf '%s\n' "$extra" | wc -l) paths outside /srv/data, the panel process could replace its code or the agent binaries:
$(printf '%s\n' "$extra" | head -10)"
printf '%s\n' "$writable" | grep -qx /srv/data || die "/srv/data is not writable by kervax"

docker network create --internal "$NET" >/dev/null
docker volume create "$VOL" >/dev/null
CTS+=("$ID-pg")
docker run -d --name "$ID-pg" --network "$NET" --network-alias pg --tmpfs /var/lib/postgresql \
    -e POSTGRES_DB=kervax -e POSTGRES_USER=kervax -e "POSTGRES_PASSWORD=$PG_PASSWORD" \
    "$PG_IMAGE" >/dev/null || die "cannot start PostgreSQL ($PG_IMAGE)"
for i in $(seq 60); do
    if docker logs "$ID-pg" 2>&1 | grep -q 'PostgreSQL init process complete' \
            && docker exec "$ID-pg" pg_isready -q -U kervax -d kervax; then
        break
    fi
    running "$ID-pg" || die "PostgreSQL exited"
    [ "$i" = 60 ] && die "PostgreSQL is not ready in 60 s"
    sleep 1
done

say "$IMAGE: read-only alembic upgrade head on an empty PostgreSQL"
start migrate "$IMAGE" -- alembic upgrade head
code=$(timeout 300 docker wait "$ID-migrate") || die "alembic upgrade head did not finish in 300 s"
[ "$code" = 0 ] || die "alembic upgrade head exited with code $code"
check_logs "$ID-migrate"
# First start looks up the panel's location on ip-api.com (app/bootstrap.py) and logs the
# failure with a traceback; the network here is internal, so the lookup is marked as done.
docker exec "$ID-pg" psql -q -U kervax -d kervax -v ON_ERROR_STOP=1 \
    -c "insert into app_settings (key, value) values ('default_location_seeded', '1')" \
    || die "cannot seed app_settings"

say "$IMAGE: read-only start (CMD: alembic upgrade head + uvicorn), /api/health, version $VERSION"
start backend "$IMAGE"
wait_health "$ID-backend"
sleep 5 # the in-process scheduler gets to its first tick
stop_clean "$ID-backend"

say "$IMAGE: read-only scheduler (python -m app.scheduler_run) for 20 s"
start scheduler "$IMAGE" -e KERVAX_RUN_SCHEDULER=1 -- python -m app.scheduler_run
sleep 20
running "$ID-scheduler" || die "the scheduler exited"
docker logs "$ID-scheduler" 2>&1 | grep -q 'kervax.scheduler' || die "the scheduler did not report its start"
stop_clean "$ID-scheduler"

# --- 2. served agent == signed manifest -----------------------------------------------
say "signed variant: throwaway Ed25519 key, agent stage, manifest, image"
openssl genpkey -algorithm ed25519 -out "$TMP/test.key" 2>/dev/null
openssl pkey -in "$TMP/test.key" -pubout -out "$TMP/test.pub"
PUB=$(openssl pkey -pubin -in "$TMP/test.pub" -outform DER | tail -c 32 | base64 -w0)

# build context: the repository without .git, node_modules and the real agent-dist/
mkdir "$TMP/ctx"
tar -C "$ROOT" --exclude=.git --exclude=node_modules --exclude=./agent-dist -cf - . | tar -C "$TMP/ctx" -xf -
mkdir "$TMP/ctx/agent-dist"
build() { # build <log> <docker build args...>
    local log=$TMP/$1.log
    shift
    docker build --progress=plain -f "$TMP/ctx/backend/Dockerfile" --build-arg "AGENT_PUBKEY=$PUB" "$@" "$TMP/ctx" >"$log" 2>&1 \
        || { tail -40 "$log" >&2; die "docker build failed ($*)"; }
}
build agent --target agent -t "$VARIANT-agent"
cid=$(docker create "$VARIANT-agent" true)
docker cp "$cid:/out" "$TMP/out" >/dev/null
docker rm "$cid" >/dev/null

# canonical JSON as agent-signing/release.py writes it: sorted keys, no spaces, no newline
art=""
for arch in amd64 arm64; do
    f=$TMP/out/kervax-agent-$arch
    [ -s "$f" ] || die "the agent stage did not build $arch"
    art="$art${art:+,}\"$arch\":{\"sha256\":\"$(sha "$f")\",\"size\":$(stat -c %s "$f")}"
done
printf '{"artifacts":{%s},"min_agent":"1.0","released_at":"%s","version":"%s"}' \
    "$art" "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" "$AGENT_VERSION" >"$TMP/manifest.json"
openssl pkeyutl -sign -rawin -inkey "$TMP/test.key" -in "$TMP/manifest.json" -out "$TMP/manifest.bin"
{ base64 -w0 "$TMP/manifest.bin"; echo; } >"$TMP/manifest.sig"
cp "$TMP/manifest.json" "$TMP/manifest.sig" "$TMP/ctx/agent-dist/"
# stale binaries of a previous release left in agent-dist/: the image must not serve them
for arch in amd64 arm64; do
    echo "stale kervax-agent-$arch of an older release" >"$TMP/ctx/agent-dist/kervax-agent-$arch"
    echo "stale" | gzip -c >"$TMP/ctx/agent-dist/kervax-agent-$arch.gz"
done
build variant -t "$VARIANT"

say "signed variant: manifest, signature and binaries served by the panel"
start signed "$VARIANT"
wait_health "$ID-signed"
http "$ID-signed" /api/agent/manifest "$TMP/got.json"
[ "$CODE" = 200 ] || die "/api/agent/manifest answered $CODE, not 200: $(head -c 400 "$TMP/got.json")"
cmp -s "$TMP/got.json" "$TMP/manifest.json" || die "/api/agent/manifest is not the signed manifest"
openssl pkeyutl -verify -pubin -rawin -inkey "$TMP/test.pub" -in "$TMP/got.json" -sigfile "$TMP/manifest.bin" >/dev/null \
    || die "the signature does not verify over the served manifest"
http "$ID-signed" /api/agent/manifest.sig "$TMP/got.sig"
[ "$CODE" = 200 ] || die "/api/agent/manifest.sig answered $CODE"
cmp -s "$TMP/got.sig" "$TMP/manifest.sig" || die "/api/agent/manifest.sig is not the signature made here"
for arch in amd64 arm64; do
    want=$(sha "$TMP/out/kervax-agent-$arch")
    http "$ID-signed" "/api/agent/download/$arch" "$TMP/got-$arch"
    [ "$CODE" = 200 ] || die "/api/agent/download/$arch answered $CODE"
    got=$(sha "$TMP/got-$arch")
    [ "$got" = "$want" ] || die "/api/agent/download/$arch serves sha256 $got, the manifest signs $want"
    grep -qF "$PUB" "$TMP/got-$arch" || die "the served $arch binary has no update key built in (AGENT_PUBKEY / -X main.updatePubKeyB64)"
    http "$ID-signed" "/api/agent/download-gz/$arch" "$TMP/got-$arch.gz"
    [ "$CODE" = 200 ] || die "/api/agent/download-gz/$arch answered $CODE (gz copy does not unpack to the signed binary)"
    got=$(gzip -dc "$TMP/got-$arch.gz" | sha256sum | cut -d' ' -f1)
    [ "$got" = "$want" ] || die "/api/agent/download-gz/$arch unpacks to sha256 $got, the manifest signs $want"
done
stop_clean "$ID-signed"

say "signed variant: a tampered binary must be refused"
mkdir -m 755 "$TMP/tampered"
cp "$TMP/out"/kervax-agent-* "$TMP/manifest.json" "$TMP/manifest.sig" "$TMP/tampered/"
printf 'x' >>"$TMP/tampered/kervax-agent-amd64"
chmod 644 "$TMP/tampered"/*
start tampered "$VARIANT" -v "$TMP/tampered:/srv/agent:ro"
wait_health "$ID-tampered"
http "$ID-tampered" /api/agent/manifest "$TMP/tampered.out"
if [ "$CODE" != 503 ] || ! grep -qF amd64 "$TMP/tampered.out"; then
    die "with a tampered amd64 binary /api/agent/manifest answered $CODE: $(head -c 300 "$TMP/tampered.out")"
fi
stop_clean "$ID-tampered"

say "OK: $IMAGE passed in $(( $(date +%s) - started )) s"
