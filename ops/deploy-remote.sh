#!/bin/sh
# Server-side half of ops/deploy.sh. Not run by hand: deploy.sh puts it into a run directory
# on production together with everything the run needs and starts it there.
#
#   sh run.sh start [run|rollback]    start the work detached from the ssh session, return at once
#   sh run.sh follow [run|rollback] [N]   print its log (from line N+1) until it is over, exit with its result
#   sh run.sh run | rollback          the work itself (what start launches)
#
# Why a separate process. 24.10.2026 a deploy started in the background died together with the
# shell that started it, and the panel was left in an unclear state: the build and the container
# recreation ran inside the ssh session, and a SIGHUP between "stop the old container" and
# "start the new one" leaves no container at all. Here the work runs as its own systemd unit
# (or setsid nohup where there is no systemd): a dropped connection only stops the log from
# reaching the screen, `ops/deploy.sh <host> --attach` picks it up again.
#
# What a run does:
#   lock          one deploy per panel at a time (flock on .deploy/lock)
#   snapshot      the images of the running containers are tagged :prev, the files of the
#                 previous delivery go into prev-src.tar, the container logs into logs/ -
#                 recreating a container used to take its whole log with it
#   unpack        the new sources, removal of stale files, the manifest
#   build         `compose build` separately from the start: a failed build rolls the sources
#                 back and leaves the running containers alone
#   up            `compose up -d --no-build`, seconds instead of minutes
#   verify        the live version from health, the scheduler version, code errors in the logs
#   on failure    the :prev images and the previous sources go back, unless the migration
#                 number changed (old code on a new schema is worse than a failed check)
#   on success    the previous :prev images and build cache older than 48 hours are removed,
#                 the last 10 run directories are kept
#
# Result: run.status in the run directory (ok, rolled-back, failed, build-failed, busy), the log
# is run.log. A rollback by hand (`ops/deploy.sh <host> --rollback`) uses the directory of the
# last run, with its own rollback.log and rollback.status.
set -u

RD=$(cd "$(dirname "$0")" && pwd)       # the run directory: <panel>/.deploy/<run>
DEPLOY_DIR=$(dirname "$RD")             # <panel>/.deploy
DIR=$(dirname "$DEPLOY_DIR")            # the panel directory
MODE="${1:-}"
case "$MODE" in start|follow) KIND="${2:-run}" ;; *) KIND="$MODE" ;; esac
case "$KIND" in run|rollback) ;; *) echo "usage: run.sh start|follow [run|rollback] | run | rollback" >&2; exit 2 ;; esac
LOG="$RD/$KIND.log"
STATUS="$RD/$KIND.status"

# shellcheck disable=SC1091
. "$RD/params"    # FILES SERVICES EPOCH VERSION PREV_VERSION REV

step() { printf '\n→ %s\n' "$*"; }
note() { printf '  %s\n' "$*"; }

finish() {
    # status is written last and atomically: follow treats its appearance as "the run is over"
    printf '%s\n' "$1" > "$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS"
    note "result: $1"
    exit "${2:-1}"
}

dc() { env KERVAX_DEPS_EPOCH="$EPOCH" docker compose $FILES "$@"; }

health() {
    docker exec kervax-backend-1 python -c 'import urllib.request;print(urllib.request.urlopen("http://localhost:8000/api/health", timeout=5).read().decode())' 2>/dev/null
}

migration() {
    pw=$(grep '^KERVAX_DB_PASSWORD=' "$DIR/.env" 2>/dev/null | cut -d= -f2-)
    docker exec -e PGPASSWORD="$pw" kervax-db-1 psql -U kervax -d kervax -tAc 'select version_num from alembic_version' 2>/dev/null | head -1
}

# wait until health answers with the wanted version and a reachable database
wait_version() {
    want="$1"; i=0
    while [ "$i" -lt 60 ]; do
        h=$(health)
        case "$h" in *"\"version\":\"$want\""*'"db":"ok"'*|*'"db":"ok"'*"\"version\":\"$want\""*) return 0 ;; esac
        sleep 3; i=$((i + 1))
    done
    note "health: ${h:-no answer}"
    return 1
}

image_ref() { docker inspect -f '{{.Config.Image}}' "kervax-$1-1" 2>/dev/null; }
image_name() { printf '%s' "$1" | sed 's/:[^:/]*$//'; }

# The files of the previous delivery back in place, the new ones it did not have removed.
restore_sources() {
    [ -f "$RD/prev-src.tar" ] || { note "no previous sources saved - the files stay new"; return 1; }
    cd "$DIR" || return 1
    tar xf "$RD/prev-src.tar"
    if [ -f "$RD/prev-manifest" ]; then
        LC_ALL=C sort "$RD/prev-manifest" > "$RD/.old"
        LC_ALL=C sort "$RD/manifest.txt" > "$RD/.new"
        LC_ALL=C comm -13 "$RD/.old" "$RD/.new" | while IFS= read -r f; do
            [ -n "$f" ] && rm -f -- "$f"
        done
        rm -f "$RD/.old" "$RD/.new"
        cp "$RD/prev-manifest" .kervax-deployed
    fi
    note "sources are back at the previous delivery"
}

rollback() {
    step "Rolling back to $PREV_VERSION"
    mig_now=$(migration)
    mig_before=$(cat "$RD/mig-before" 2>/dev/null)
    if [ -n "$mig_before" ] && [ "$mig_now" != "$mig_before" ]; then
        note "the migration changed: $mig_before -> $mig_now. The old code does not know the new schema,"
        note "so no automatic rollback. By hand, once it is clear what the migration did:"
        note "  cd $DIR && docker compose $FILES run --rm --no-deps backend alembic downgrade $mig_before"
        note "  then: sh $RD/run.sh start rollback"
        return 1
    fi
    ok=1; failed=""
    for svc in $SERVICES; do
        ref=$(grep "^$svc " "$RD/images" 2>/dev/null | cut -d' ' -f2)
        [ -n "$ref" ] || { note "$svc: no :prev image recorded"; ok=0; continue; }
        # the image that did not pass goes away after the rollback, it has no tag left
        failed="$failed $(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null)"
        docker tag "$(image_name "$ref"):prev" "$ref" || ok=0
    done
    restore_sources || ok=0
    dc up -d --no-build $SERVICES 2>&1 | tail -6
    if wait_version "$PREV_VERSION"; then
        note "the panel is back on $PREV_VERSION"
        for id in $failed; do docker rmi "$id" > /dev/null 2>&1; done
    else
        note "the panel did not come back on $PREV_VERSION - look at: docker compose $FILES logs --tail=50"
        ok=0
    fi
    [ "$ok" = 1 ]
}

case "$MODE" in
start)
    if ! flock -n "$DEPLOY_DIR/lock" true; then
        echo "another deploy is running on this panel: $(readlink "$DEPLOY_DIR/current" 2>/dev/null)" >&2
        exit 75
    fi
    rm -f "$STATUS" "$RD/$KIND.pid"
    : > "$LOG"
    if command -v systemd-run >/dev/null 2>&1; then
        # its own unit: not tied to the ssh session, its output does not go anywhere but the log
        systemd-run --quiet --collect --unit="kervax-deploy-$(basename "$RD")-$KIND" \
            --property=StandardOutput=append:"$LOG" --property=StandardError=append:"$LOG" \
            sh "$RD/run.sh" "$KIND" || exit 1
    else
        setsid nohup sh "$RD/run.sh" "$KIND" >> "$LOG" 2>&1 < /dev/null &
    fi
    exit 0
    ;;
follow)
    # tail until status appears; a dead runner without a status is a crash, not a hang
    # the third argument: lines already shown (a reconnect continues where the screen stopped)
    n=0; seen="${3:-0}"
    while :; do
        lines=$(wc -l < "$LOG" 2>/dev/null || echo 0)
        if [ "$lines" -gt "$seen" ]; then
            sed -n "$((seen + 1)),${lines}p" "$LOG"
            seen=$lines
        fi
        if [ -f "$STATUS" ]; then
            sed -n "$((seen + 1)),\$p" "$LOG"
            case "$KIND:$(cat "$STATUS")" in run:ok|rollback:rolled-back) exit 0 ;; *) exit 1 ;; esac
        fi
        pid=$(cat "$RD/$KIND.pid" 2>/dev/null)
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            sleep 1
            [ -f "$STATUS" ] && continue
            echo "the deploy process is gone without a result (killed or the host rebooted): see $LOG"
            exit 1
        fi
        n=$((n + 1))
        [ -z "$pid" ] && [ "$n" -gt 30 ] && { echo "the deploy did not start: see $LOG"; exit 1; }
        sleep 1
    done
    ;;
esac

# --- the work itself, under the lock ----------------------------------------------------
echo $$ > "$RD/$KIND.pid"
exec 9>"$DEPLOY_DIR/lock"
flock -n 9 || finish busy 75
ln -sfn "$RD" "$DEPLOY_DIR/current"
cd "$DIR" || finish failed 1
printf '%s  %s %s -> %s on %s\n' "$(date '+%F %T')" "$MODE" "$PREV_VERSION" "$VERSION" "$(hostname)"

if [ "$MODE" = rollback ]; then
    rollback && finish rolled-back 0
    finish failed 1
fi

step "Snapshot of what is running now"
tar tf "$RD/src.tar" > /dev/null 2>&1 || finish failed 1
migration > "$RD/mig-before"
note "migration: $(cat "$RD/mig-before")"
: > "$RD/images"
: > "$RD/old-prev"
mkdir -p "$RD/logs"
for svc in $SERVICES; do
    ref=$(image_ref "$svc")
    if [ -z "$ref" ]; then
        # no container (a run that died halfway before this one): keep whatever :prev there is
        note "$svc: no container, :prev stays as it was"
        continue
    fi
    name=$(image_name "$ref")
    case "${ref##*/}" in *:*) ;; *) ref="$ref:latest" ;; esac
    echo "$svc $ref" >> "$RD/images"
    old=$(docker image inspect -f '{{.Id}}' "$name:prev" 2>/dev/null)
    cur=$(docker inspect -f '{{.Image}}' "kervax-$svc-1")
    [ -n "$old" ] && [ "$old" != "$cur" ] && echo "$old" >> "$RD/old-prev"
    docker tag "$cur" "$name:prev" && note "$svc: $name:prev"
    # the log of the container about to be recreated, with timestamps - otherwise it is gone
    docker logs --timestamps "kervax-$svc-1" 2>&1 | gzip -1 > "$RD/logs/$svc.log.gz"
done
note "container logs saved: $(du -sh "$RD/logs" | cut -f1)"
if [ -f .kervax-deployed ]; then
    cp .kervax-deployed "$RD/prev-manifest"
    while IFS= read -r f; do [ -f "$f" ] && printf '%s\n' "$f"; done < .kervax-deployed > "$RD/prev-list"
    for f in agent-dist/manifest.json agent-dist/manifest.sig; do [ -f "$f" ] && echo "$f" >> "$RD/prev-list"; done
    tar cf "$RD/prev-src.tar" -T "$RD/prev-list" && note "previous sources saved: $(wc -l < "$RD/prev-list") files"
fi

step "Unpacking $REV"
tar xf "$RD/src.tar" -C "$DIR" || finish failed 1
if [ -s "$RD/stale.txt" ]; then
    while IFS= read -r f; do [ -n "$f" ] && rm -f -- "$f"; done < "$RD/stale.txt"
    find . -type d -empty -not -path './data/*' -not -path './.git/*' -not -path './.deploy/*' -delete 2>/dev/null || true
    note "stale files removed: $(wc -l < "$RD/stale.txt")"
fi
[ -f "$RD/agent.tar" ] && tar xf "$RD/agent.tar" -C "$DIR/agent-dist" && note "signed agent release delivered"
cp "$RD/manifest.txt" .kervax-deployed

step "Building: $SERVICES"
if ! dc build $SERVICES > "$RD/build.log" 2>&1; then
    tail -20 "$RD/build.log"
    note "the build failed - the containers were not touched"
    restore_sources
    finish build-failed 1
fi
tail -3 "$RD/build.log"

step "Starting: $SERVICES"
UP_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
dc up -d --no-build $SERVICES 2>&1 | tail -6

step "Verifying"
BAD=0
if wait_version "$VERSION"; then
    note "health: $VERSION, db ok"
else
    note "✗ health does not answer with $VERSION"; BAD=1
fi
case " $SERVICES " in
*" scheduler "*)
    # the scheduler is the one sending alerts: a forgotten one leaves them on the old code.
    # Its first tick goes right after the start - give it time to fall over if it is going to
    sleep 30
    sch=$(docker exec kervax-scheduler-1 sed -n 's/^\s*version: str = "\([0-9.]*\)".*/\1/p' /srv/app/config.py 2>/dev/null | head -1)
    note "scheduler: ${sch:-not running}"
    [ "$sch" = "$VERSION" ] || { note "✗ the scheduler is not on $VERSION"; BAD=1; }
    ;;
esac
err=$(docker logs --since "$UP_AT" kervax-backend-1 2>&1 | grep -ciE 'traceback|critical')
note "tracebacks in the backend log: $err"
[ "$err" = 0 ] || BAD=1
if docker inspect kervax-scheduler-1 >/dev/null 2>&1; then
    # in the scheduler a traceback can be a network error of one stage (Telegram is down), that
    # is no reason to roll back; an error in the code itself is
    code=$(docker logs --since "$UP_AT" kervax-scheduler-1 2>&1 \
        | grep -E '^[A-Za-z_.]*(NameError|AttributeError|ImportError|ModuleNotFoundError|TypeError|SyntaxError|UnboundLocalError|KeyError|IndentationError)\b' | sort | uniq -c)
    tb=$(docker logs --since "$UP_AT" kervax-scheduler-1 2>&1 | grep -ci traceback)
    note "tracebacks in the scheduler log: $tb"
    [ -n "$code" ] && { note "✗ code errors in the scheduler:"; printf '%s\n' "$code" | sed 's/^/      /'; BAD=1; }
fi

if [ "$BAD" != 0 ]; then
    rollback && finish rolled-back 1
    finish failed 1
fi

step "Cleanup"
# the :prev of the previous deploy is replaced by this one - the old image goes, otherwise every
# deploy leaves three images behind (a panel had 1201 of them, 57 GB)
while IFS= read -r id; do
    [ -n "$id" ] && docker rmi "$id" > /dev/null 2>&1 && note "old :prev removed: $(printf '%s' "${id#sha256:}" | cut -c1-12)"
done < "$RD/old-prev"
freed=$(docker builder prune -f --filter until=48h 2>/dev/null | sed -n 's/^Total reclaimed space:[[:space:]]*//p; s/^Total:[[:space:]]*//p' | tail -1)
note "build cache older than 48 hours freed: ${freed:-0B}"
# the last 10 runs with their logs, the rest go
ls -1d "$DEPLOY_DIR"/*/ 2>/dev/null | sort | head -n -10 | while IFS= read -r d; do rm -rf -- "$d"; done
finish ok 0
