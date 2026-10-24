#!/bin/sh
# Deploy Kervax to production: ship the sources, rebuild, VERIFY the result.
#
#   ops/deploy.sh <host>                  deploy HEAD to one panel
#   ops/deploy.sh <host> v1.1.2           deploy a tag
#   ops/deploy.sh <host> --dry-run        show what would happen and exit
#   ops/deploy.sh --all                   deploy to every panel listed in ops/prods
#   ops/deploy.sh <host> --attach         show a deploy that is running (or ran) on <host>
#   ops/deploy.sh <host> --rollback       put back the version that was there before the last deploy
#
# ops/prods is one ssh host per line (blank lines and # comments ignored). It is not
# in the repository: which panels an installation has is nobody else's business. With
# --all the panels are done one after another and the run STOPS on the first failure -
# a half-deployed fleet is worse than one that was not touched.
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
#
# The work on production itself (unpack, build, start, verify, roll back) is ops/deploy-remote.sh:
# it is shipped with the sources and runs there as its own process, so a dropped ssh session
# or a closed terminal no longer leaves a panel without containers. See that file for details.
set -eu

HOST=""; REF="HEAD"; DIR=/app/kervax; DRY=0; ALL=0; ATTACH=0; ROLLBACK=0
# время на панель - в итоговой строке: видно, какая из них тормозит выкладку
T_HOST=$(date +%s)
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1; shift ;;
        --all) ALL=1; shift ;;
        --attach) ATTACH=1; shift ;;
        --rollback) ROLLBACK=1; shift ;;
        --dir) DIR="$2"; shift 2 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        -*) echo "unknown argument: $1" >&2; exit 2 ;;
        *) if [ -z "$HOST" ]; then HOST="$1"; else REF="$1"; fi; shift ;;
    esac
done

# --all: прогоняем этот же скрипт по каждой панели из списка. Именно рекурсией, а не
# циклом внутри: каждая панель проходит ПОЛНУЮ проверку после выкатки. Первая панель из
# списка - пробная: падение на ней останавливает остальные, парк не расходится по версиям.
# Прошла - остальные катятся параллельно (14.10.2026: три панели подряд шли ~6 минут, почти
# все время - сборка образов, которой нечего ждать друг друга). Вывод каждой - в свой файл и
# на экран целиком после, чтобы не перемешивался.
if [ "$ALL" = "1" ]; then
    LIST="$(dirname "$0")/prods"
    [ -f "$LIST" ] || { echo "нет списка панелей: $LIST (по ssh-хосту на строку)" >&2; exit 2; }
    HOSTS=$(grep -vE '^[[:space:]]*(#|$)' "$LIST")
    [ -n "$HOSTS" ] || { echo "список $LIST пуст" >&2; exit 2; }
    n=$(printf '%s\n' "$HOSTS" | grep -c .)
    printf '\n\033[1m→ Панелей к выкатке: %s\033[0m\n' "$n"
    FIRST=$(printf '%s\n' "$HOSTS" | head -1)
    REST=$(printf '%s\n' "$HOSTS" | tail -n +2)
    T0=$(date +%s)
    printf '\n\033[1m═══ [1/%s] %s (пробная) ═══\033[0m\n' "$n" "$FIRST"
    set -- "$FIRST" "$REF"
    [ "$DRY" = "1" ] && set -- "$@" --dry-run
    sh "$0" "$@" || {
        printf '\n\033[31m✗ остановились на %s — остальные панели не тронуты\033[0m\n' "$FIRST" >&2
        exit 1
    }
    if [ -n "$REST" ]; then
        LOGS=$(mktemp -d)
        printf '\n\033[1m═══ остальные параллельно: %s ═══\033[0m\n' "$(echo $REST)"
        PIDS=""
        for h in $REST; do
            set -- "$h" "$REF"
            [ "$DRY" = "1" ] && set -- "$@" --dry-run
            sh "$0" "$@" > "$LOGS/$h.log" 2>&1 &
            PIDS="$PIDS $h:$!"
        done
        failed=""
        for hp in $PIDS; do
            wait "${hp#*:}" || failed="$failed ${hp%%:*}"
        done
        for h in $REST; do
            printf '\n\033[1m═══ %s ═══\033[0m\n' "$h"
            cat "$LOGS/$h.log"
        done
        rm -rf "$LOGS"
        if [ -n "$failed" ]; then
            printf '\n\033[31m✗ не выкатилось на:%s — остальные уже на новой версии\033[0m\n' "$failed" >&2
            exit 1
        fi
    fi
    if [ "$DRY" != "1" ]; then
        # Сверка после всех: каждая панель еще раз отвечает версией с живого health. 07.10.2026
        # панель из середины списка молча осталась на старой версии, а цикл отчитался "выкачено
        # на все": дочерний прогон вернул успех, ничего не выкатив, и по выводу причину уже не
        # восстановить. Повторный прогон на нее прошел чисто.
        WANT=$(git show "$REF:backend/app/config.py" | sed -n 's/^\s*version: str = "\([0-9.]*\)".*/\1/p' | head -1)
        bad=""
        for h in $HOSTS; do
            got=$(ssh "$h" "sudo docker exec kervax-backend-1 python -c 'import urllib.request;print(urllib.request.urlopen(\"http://localhost:8000/api/health\").read().decode())'" 2>/dev/null \
                  | sed -n 's/.*"version": *"\([0-9.]*\)".*/\1/p')
            [ "$got" = "$WANT" ] || bad="$bad $h=${got:-?}"
        done
        if [ -n "$bad" ]; then
            printf '\n\033[31m✗ после выкатки не на %s:%s\033[0m\n' "$WANT" "$bad" >&2
            exit 1
        fi
    fi
    printf '\n\033[32m✓ выкачено на все панели: %s, за %s с\033[0m\n' "$n" "$(( $(date +%s) - T0 ))"
    exit 0
fi

[ -n "$HOST" ] || { echo "Usage: ops/deploy.sh <ssh-host> [ref] [--dry-run] | --all" >&2; exit 2; }

say()  { printf '\n\033[1m→ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$*"; }

# Watch a run on production. The log goes to the screen and to a local copy; if the connection
# drops, reconnect and continue from the line the screen stopped at. The run itself does not
# depend on this connection at all. Returns 0 - success, 1 - the run failed, 2 - no connection.
follow() {
    kind="$1"; tries=0; FL=$(mktemp); : > "$FL.rc"
    while :; do
        shown=$(( $(wc -l < "$FL") ))
        # код ssh - через файл: в конвейере его иначе не достать, а под set -e упавший ssh
        # оборвал бы подоболочку раньше записи
        { r=0; ssh -o ServerAliveInterval=15 -o ServerAliveCountMax=4 "$HOST" \
              "sudo sh '$RD/run.sh' follow $kind $shown" || r=$?; echo "$r" > "$FL.rc"; } | tee -a "$FL"
        rc=$(cat "$FL.rc")
        case "$rc" in
            0|1) rm -f "$FL" "$FL.rc"; return "$rc" ;;
        esac
        tries=$((tries + 1))
        if [ "$tries" -ge 6 ]; then
            rm -f "$FL" "$FL.rc"
            warn "no connection to $HOST - the run goes on there by itself; to watch it again:"
            warn "  ops/deploy.sh $HOST --attach"
            return 2
        fi
        warn "connection to $HOST lost, reconnecting ($tries)..."
        sleep 5
    done
}

# --attach / --rollback: the last run on the panel, without the repository
if [ "$ATTACH" = "1" ] || [ "$ROLLBACK" = "1" ]; then
    RD=$(ssh "$HOST" "readlink '$DIR/.deploy/current'" 2>/dev/null) || true
    [ -n "$RD" ] || die "$HOST: no deploy runs found in $DIR/.deploy"
    if [ "$ROLLBACK" = "1" ]; then
        say "Rolling $HOST back from the run $(basename "$RD")"
        ssh "$HOST" "sudo sh '$RD/run.sh' start rollback" || die "could not start the rollback"
        follow rollback || die "rollback failed - see above"
        printf '\n\033[32m✓ %s rolled back\033[0m\n' "$HOST"
    else
        say "Run $(basename "$RD") on $HOST"
        follow run || exit 1
    fi
    exit 0
fi

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
if [ -z "$CONFIG_FILES" ]; then
    # No frontend container - typically a run that died halfway. The set then comes from .env
    # (COMPOSE_FILE), so a broken panel can be repaired with the same command
    CONFIG_FILES=$(ssh "$HOST" "sudo sed -n 's/^COMPOSE_FILE=//p' '$DIR/.env'" 2>/dev/null | tr ':' ',' || true)
    [ -n "$CONFIG_FILES" ] || die "could not determine the compose file set on production (no running frontend and no COMPOSE_FILE in .env)"
    warn "the panel is not running - the compose set is taken from COMPOSE_FILE in .env"
fi
PREV_VERSION=$(printf '%s' "$VER_BEFORE" | sed -n 's/.*"version": *"\([0-9.]*\)".*/\1/p')

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

# The manifest only knows what earlier deploys shipped. Files in the source directories
# that no revision has - left from deploys before the manifest existed, or copied by
# hand - are invisible to it, yet the image build takes those directories whole. A live
# case: frontend/src/BrandingModal.tsx, removed from the repository in August, broke the
# 1.4.28 build on one panel once the signature of a function it called had changed.
# These directories belong to the repository entirely, so what it lacks goes too.
ORPHANS=$(ssh "$HOST" "cd '$DIR' && find frontend/src backend/app backend/alembic -type f \
            -not -path '*/__pycache__/*' -not -name '*.pyc' 2>/dev/null" | LC_ALL=C sort > /tmp/.kv_srv.$$
          printf '%s\n' "$NEW_LIST" | LC_ALL=C sort > /tmp/.kv_rev.$$
          LC_ALL=C comm -23 /tmp/.kv_srv.$$ /tmp/.kv_rev.$$
          rm -f /tmp/.kv_srv.$$ /tmp/.kv_rev.$$)
if [ -n "$ORPHANS" ]; then
    STALE=$(printf '%s\n%s\n' "$STALE" "$ORPHANS" | grep -v '^$' | LC_ALL=C sort -u)
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

# -- Shipping and the run on production -------------------------------------
# Everything the run needs goes in one archive through one connection; the run starts only if
# the whole archive arrived. A connection dropped before that changes nothing on production.
RUN="$(date -u +%Y%m%d-%H%M%S)-$REV"
RD="$DIR/.deploy/$RUN"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
git archive "$REF" > "$STAGE/src.tar"
printf '%s\n' "$NEW_LIST" > "$STAGE/manifest.txt"
printf '%s\n' "$STALE" | grep -v '^$' > "$STAGE/stale.txt" || true
cp "$(dirname "$0")/deploy-remote.sh" "$STAGE/run.sh"
# DEPS_EPOCH - номер недели: зависимости бэкенда в отдельном слое, и он пересобирается раз в
# неделю или при смене их списка, а не на каждом релизе (backend/Dockerfile)
cat > "$STAGE/params" <<EOF
FILES='$FILES'
SERVICES='$SERVICES'
EPOCH='$(date -u +%G-%V)'
VERSION='$VERSION'
PREV_VERSION='${PREV_VERSION:-unknown}'
REV='$REV'
EOF

# -- The signed agent release -------------------------------------------------
# agent-dist/ is not in the repository (the signature is made offline, off this machine's
# git), so `git archive` cannot carry it - and a panel deployed without it keeps serving
# the PREVIOUS release manifest: the new agent exists, is signed, and never reaches a
# single node. Ship the manifest explicitly. Only manifest.json and manifest.sig: the
# binaries the panel builds itself, and a stray binary here makes it refuse to serve
# updates at all.
if [ -f agent-dist/manifest.json ] && [ -f agent-dist/manifest.sig ]; then
    LOCAL_AGENT=$(sed -n 's/.*"version":"\([^"]*\)".*/\1/p' agent-dist/manifest.json)
    REMOTE_AGENT=$(ssh "$HOST" "sudo sed -n 's/.*\"version\":\"\([^\"]*\)\".*/\1/p' '$DIR/agent-dist/manifest.json' 2>/dev/null" || true)
    if [ "$LOCAL_AGENT" != "$REMOTE_AGENT" ]; then
        say "Shipping the signed agent release: ${REMOTE_AGENT:-none} -> $LOCAL_AGENT"
        tar -cf "$STAGE/agent.tar" -C agent-dist manifest.json manifest.sig
    else
        echo "  signed agent release: $LOCAL_AGENT, already there"
    fi
fi

say "Shipping the sources and starting the run $RUN"
tar -cf - -C "$STAGE" . | ssh "$HOST" "sudo mkdir -p '$RD' && sudo tar xf - -C '$RD' \
    && { sudo sh '$RD/run.sh' start run || { rc=\$?; sudo rm -rf '$RD'; exit \$rc; }; }" \
    || die "the run did not start (another deploy on $HOST? ops/deploy.sh $HOST --attach) - production was not touched"

set +e
follow run
RC=$?
set -e
[ "$RC" = 2 ] && exit 1
[ "$RC" = 0 ] || die "the deploy failed on $HOST - see the log above (on the server: $RD/run.log)"

# -- What the live panel says -------------------------------------------------
say "Verifying that this is what got deployed"
AFTER=$(ssh "$HOST" "cd '$DIR'
    DBPW=\$(sudo grep '^KERVAX_DB_PASSWORD=' .env | cut -d= -f2-)
    VER=\$(sudo docker exec kervax-backend-1 python -c 'import urllib.request;print(urllib.request.urlopen(\"http://localhost:8000/api/health\").read().decode())' 2>/dev/null)
    MIG=\$(sudo docker exec -e PGPASSWORD=\"\$DBPW\" kervax-db-1 psql -U kervax -d kervax -tAc 'select version_num from alembic_version' 2>/dev/null | head -1)
    CNT=\$(sudo docker exec -e PGPASSWORD=\"\$DBPW\" kervax-db-1 psql -U kervax -d kervax -tAc 'select (select count(*) from servers), (select count(*) from checks), (select count(*) from users)' 2>/dev/null | head -1)
    AGT=\$(sudo docker exec kervax-backend-1 python -c 'import urllib.request;print(urllib.request.urlopen(\"http://localhost:8000/api/agent/manifest\").read().decode())' 2>/dev/null | sed -n 's/.*\"version\":\"\([^\"]*\)\".*/\1/p')
    printf '%s\n%s\n%s\n%s\n' \"\$VER\" \"\$MIG\" \"\$CNT\" \"\$AGT\"")

VER_AFTER=$(printf '%s' "$AFTER" | sed -n 1p)
MIG_AFTER=$(printf '%s' "$AFTER" | sed -n 2p)
CNT_AFTER=$(printf '%s' "$AFTER" | sed -n 3p)
AGT_AFTER=$(printf '%s' "$AFTER" | sed -n 4p)

BAD=0
echo "  health:    $VER_AFTER"
case "$VER_AFTER" in
    *"\"version\":\"$VERSION\""*) ;;
    *) echo "    ✗ version is not $VERSION - the code is NOT deployed"; BAD=1 ;;
esac
echo "  migration: $MIG_BEFORE -> $MIG_AFTER"
echo "  data:      $CNT_BEFORE -> $CNT_AFTER"
[ "$CNT_BEFORE" = "$CNT_AFTER" ] || warn "the counters changed - check that this is expected"
# What the LIVE panel hands to agents, not what lies in the directory: the manifest is
# baked into the image, so a file delivered but never built in reaches no one.
if [ -n "${LOCAL_AGENT:-}" ]; then
    echo "  agent release served: ${AGT_AFTER:-none}"
    [ "$AGT_AFTER" = "$LOCAL_AGENT" ] \
        || { echo "    ✗ the panel serves ${AGT_AFTER:-nothing}, not $LOCAL_AGENT - agents will not update"
             echo "      (a fresh panel usually misses KERVAX_AGENT_PUBKEY in its .env: the key is"
             echo "       compiled into the agent, so without it the built binary does not match"
             echo "       the signed one and the panel refuses to serve the release)"
             BAD=1; }
fi

if [ "$BAD" = "0" ]; then
    printf '\n\033[32m✓ %s: %s deployed and verified in %s s\033[0m\n' "$HOST" "$VERSION" "$(( $(date +%s) - T_HOST ))"
else
    die "verification failed - see the output above; to put the previous version back: ops/deploy.sh $HOST --rollback"
fi
