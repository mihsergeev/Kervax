#!/bin/sh
# Deploy Kervax to production: ship the sources, rebuild, VERIFY the result.
#
#   ops/deploy.sh kervax-build            deploy HEAD to kervax.acdev.pro
#   ops/deploy.sh fi-hz-ms2 v1.1.2        deploy a tag to kervax.msergeev.ru
#   ops/deploy.sh kervax-build --dry-run  show what would happen and exit
#
# Production builds from source (it has its own agent signing key) and /app/kervax is NOT
# a git repository: files arrive as an archive. Hence the two things this script exists
# for.
#
# 1. `git archive | tar x` UNPACKS over the top but deletes nothing. A file removed from
#    the repository stays on production forever - and one day breaks the build or, worse,
#    keeps running as live code. So every deploy leaves a manifest of what it delivered
#    (.kervax-deployed), and the next one removes whatever was in the previous manifest and
#    is gone from the new one. The script touches nothing but its own past files: .env,
#    data/, agent-dist/ and agent-signing/ never enter the manifest because they are not in
#    the archive.
#
# 2. "The container is up" is not "the code is deployed". What is checked is not the fact
#    of starting but the version from the LIVE health endpoint, the migration number, the
#    data counters, and that the scheduler was built from the same version as the backend:
#    it is the one sending alerts, and a forgotten scheduler silently leaves them on the
#    old code.
set -eu

HOST=""; REF="HEAD"; DIR=/app/kervax; DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        --dir) DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; exit 2 ;;
        *) if [ -z "$HOST" ]; then HOST="$1"; else REF="$1"; fi; shift ;;
    esac
done
[ -n "$HOST" ] || { echo "Usage: ops/deploy.sh <ssh-host> [ref] [--dry-run]" >&2; exit 2; }

say()  { printf '\n\033[1m→ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }

git rev-parse --git-dir >/dev/null 2>&1 || die "run this from the Kervax repository"
git rev-parse --verify --quiet "$REF" >/dev/null || die "no such revision: $REF"
# Exactly what is committed gets deployed: git archive does not see the working tree, and
# "fixed it, deployed, still broken" is almost always this.
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    warn "the working tree has uncommitted changes - they will NOT be deployed"
fi
# ^{commit}: for an annotated tag rev-parse otherwise returns the SHA of the tag object
REV=$(git rev-parse --short "$REF^{commit}")
VERSION=$(git show "$REF:backend/app/config.py" | sed -n 's/^\s*version: str = "\([0-9.]*\)".*/\1/p' | head -1)
[ -n "$VERSION" ] || die "could not read the version from backend/app/config.py at $REF"

say "Deploying $REF ($REV), version $VERSION -> $HOST:$DIR"

# -- Snapshot before ----------------------------------------------------------
# The set of compose files is read from production itself (a label on the running
# container) rather than assumed: installations differ, and an extra or missing -f recreates
# the containers without whatever the skipped file describes.
BEFORE=$(ssh "$HOST" "cd '$DIR' 2>/dev/null || exit 7
    CF=\$(sudo docker inspect kervax-frontend-1 --format '{{index .Config.Labels \"com.docker.compose.project.config_files\"}}' 2>/dev/null)
    DBPW=\$(sudo grep '^KERVAX_DB_PASSWORD=' .env 2>/dev/null | cut -d= -f2-)
    VER=\$(sudo docker exec kervax-backend-1 python -c 'import urllib.request;print(urllib.request.urlopen(\"http://localhost:8000/api/health\").read().decode())' 2>/dev/null)
    MIG=\$(sudo docker exec -e PGPASSWORD=\"\$DBPW\" kervax-db-1 psql -U kervax -d kervax -tAc 'select version_num from alembic_version' 2>/dev/null | head -1)
    CNT=\$(sudo docker exec -e PGPASSWORD=\"\$DBPW\" kervax-db-1 psql -U kervax -d kervax -tAc 'select (select count(*) from servers), (select count(*) from checks), (select count(*) from users)' 2>/dev/null | head -1)
    printf '%s\n%s\n%s\n%s\n' \"\$CF\" \"\$VER\" \"\$MIG\" \"\$CNT\"") || die "could not reach $HOST:$DIR"

CONFIG_FILES=$(printf '%s' "$BEFORE" | sed -n 1p)
VER_BEFORE=$(printf '%s' "$BEFORE" | sed -n 2p)
MIG_BEFORE=$(printf '%s' "$BEFORE" | sed -n 3p)
CNT_BEFORE=$(printf '%s' "$BEFORE" | sed -n 4p)
[ -n "$CONFIG_FILES" ] || die "could not determine the compose file set on production (is the panel running?)"

FILES=$(printf '%s' "$CONFIG_FILES" | tr ',' '\n' | sed 's|.*/||' | sed 's/^/-f /' | tr '\n' ' ')
# the scheduler is rebuilt ONLY if this set contains it (scale mode)
case "$CONFIG_FILES" in
    *compose.scale.yml*) SERVICES="backend scheduler frontend" ;;
    *) SERVICES="backend frontend" ;;
esac

echo "  currently on production: $VER_BEFORE"
echo "  migration:              $MIG_BEFORE"
echo "  data:                   $CNT_BEFORE (servers|monitors|users)"
echo "  compose:                $FILES"
echo "  to rebuild:             $SERVICES"

# -- What will be shipped and what will be removed ----------------------------
NEW_LIST=$(git ls-tree -r --name-only "$REF")
OLD_LIST=$(ssh "$HOST" "cat '$DIR/.kervax-deployed' 2>/dev/null" || true)
if [ -z "$OLD_LIST" ]; then
    warn "no manifest from a previous deploy - nothing is removed this time,"
    warn "cleanup starts working from the next one"
    STALE=""
else
    # what we shipped before and what is gone from the new revision
    STALE=$(printf '%s\n' "$OLD_LIST" | sort > /tmp/.kv_old.$$
             printf '%s\n' "$NEW_LIST" | sort > /tmp/.kv_new.$$
             comm -23 /tmp/.kv_old.$$ /tmp/.kv_new.$$
             rm -f /tmp/.kv_old.$$ /tmp/.kv_new.$$)
fi

if [ -n "$STALE" ]; then
    say "Files removed from the repository (they will be deleted on production)"
    printf '%s\n' "$STALE" | sed 's/^/  − /'
else
    echo "  no stale files"
fi

if [ "$DRY" = "1" ]; then
    printf '\n\033[33mDry run: nothing was changed.\033[0m\n'
    n_new=$(printf '%s\n' "$NEW_LIST" | grep -c . || true)
    n_stale=$(printf '%s\n' "$STALE" | grep -c . || true)
    printf 'Would ship: %s files, would remove: %s\n' "$n_new" "$n_stale"
    exit 0
fi

# -- Shipping -----------------------------------------------------------------
say "Shipping the sources"
git archive "$REF" | ssh "$HOST" "sudo tar x -C '$DIR'" || die "unpacking failed"

if [ -n "$STALE" ]; then
    say "Removing stale files"
    printf '%s\n' "$STALE" | ssh "$HOST" "cd '$DIR' && while IFS= read -r f; do
        [ -n \"\$f\" ] && sudo rm -f -- \"\$f\"
    done
    # directories left empty by the removal go too - but only if they are empty
    sudo find . -type d -empty -not -path './data/*' -not -path './.git/*' -delete 2>/dev/null || true"
fi

# the manifest is written AFTER a successful delivery - otherwise an interrupted deploy
# would leave a list of files that are not on production, and the next cleanup would miss
printf '%s\n' "$NEW_LIST" | ssh "$HOST" "sudo tee '$DIR/.kervax-deployed' >/dev/null"

# -- Build --------------------------------------------------------------------
say "Building and starting: $SERVICES"
ssh "$HOST" "cd '$DIR' && sudo docker compose $FILES up -d --build $SERVICES 2>&1 | tail -6" \
    || die "the build failed - production stayed on its previous containers"

# -- Verification -------------------------------------------------------------
say "Verifying that this is what got deployed"
sleep 12
AFTER=$(ssh "$HOST" "cd '$DIR'
    DBPW=\$(sudo grep '^KERVAX_DB_PASSWORD=' .env | cut -d= -f2-)
    VER=\$(sudo docker exec kervax-backend-1 python -c 'import urllib.request;print(urllib.request.urlopen(\"http://localhost:8000/api/health\").read().decode())' 2>/dev/null)
    MIG=\$(sudo docker exec -e PGPASSWORD=\"\$DBPW\" kervax-db-1 psql -U kervax -d kervax -tAc 'select version_num from alembic_version' 2>/dev/null | head -1)
    CNT=\$(sudo docker exec -e PGPASSWORD=\"\$DBPW\" kervax-db-1 psql -U kervax -d kervax -tAc 'select (select count(*) from servers), (select count(*) from checks), (select count(*) from users)' 2>/dev/null | head -1)
    SCH=\$(sudo docker exec kervax-scheduler-1 sed -n 's/^\s*version: str = \"\([0-9.]*\)\".*/\1/p' /srv/app/config.py 2>/dev/null | head -1)
    ERR=\$(sudo docker logs kervax-backend-1 --since 3m 2>&1 | grep -ciE 'traceback|critical' || true)
    printf '%s\n%s\n%s\n%s\n%s\n' \"\$VER\" \"\$MIG\" \"\$CNT\" \"\$SCH\" \"\$ERR\"")

VER_AFTER=$(printf '%s' "$AFTER" | sed -n 1p)
MIG_AFTER=$(printf '%s' "$AFTER" | sed -n 2p)
CNT_AFTER=$(printf '%s' "$AFTER" | sed -n 3p)
SCH_AFTER=$(printf '%s' "$AFTER" | sed -n 4p)
ERR_AFTER=$(printf '%s' "$AFTER" | sed -n 5p)

BAD=0
echo "  health:    $VER_AFTER"
case "$VER_AFTER" in
    *"\"version\":\"$VERSION\""*) ;;
    *) echo "    ✗ version is not $VERSION - the code is NOT deployed"; BAD=1 ;;
esac
case "$VER_AFTER" in
    *'"db":"ok"'*) ;;
    *) echo "    ✗ the database is unreachable for the panel"; BAD=1 ;;
esac
echo "  migration: $MIG_BEFORE -> $MIG_AFTER"
echo "  data:      $CNT_BEFORE -> $CNT_AFTER"
[ "$CNT_BEFORE" = "$CNT_AFTER" ] || warn "the counters changed - check that this is expected"
if [ "$SERVICES" != "${SERVICES#*scheduler}" ]; then
    echo "  scheduler: $SCH_AFTER"
    [ "$SCH_AFTER" = "$VERSION" ] || { echo "    ✗ scheduler is not on $VERSION - alerts run old code"; BAD=1; }
fi
echo "  tracebacks in the backend log over 3 minutes: ${ERR_AFTER:-0}"
[ "${ERR_AFTER:-0}" = "0" ] || BAD=1

if [ "$BAD" = "0" ]; then
    printf '\n\033[32m✓ %s: %s deployed and verified\033[0m\n' "$HOST" "$VERSION"
else
    die "verification failed - see the output above (docker compose $FILES logs --tail=50)"
fi
