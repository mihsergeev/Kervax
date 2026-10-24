#!/bin/bash
# Nightly dump of the panel's own database, on the panel host, outside docker.
#
# For a panel host without the Kervax backup helper (no agent, or no backup server it can
# reach). Where the helper backs up the host, it already dumps pg@kervax-db-1 itself - this
# script is not needed there.
#
# What it does:
#   * pg_dump -Fc of the panel database from the running kervax-db-1 into $OUT;
#   * checks the dump before keeping it: the archive must list the data of the tables a panel
#     cannot live without, otherwise the file is not kept and the run fails;
#   * keeps .env next to the dumps (0600): without KERVAX_JWT_SECRET / KERVAX_BACKUP_KEY the
#     panel's own JSON backups cannot be decrypted, and the database password is there;
#   * optionally copies the result off the host with restic (KERVAX_DUMP_RESTIC_ENV - a file
#     with RESTIC_REPOSITORY and RESTIC_PASSWORD), and reports to a push monitor
#     (KERVAX_DUMP_PUSH_URL, e.g. an Uptime Kuma push URL: ?status=up|down&msg=... is added),
#     so a dump that stopped happening is noticed by something that is not this host.
#   * last.status in $OUT: "ok <unix time> <file> <bytes>" or "fail <unix time> <reason>".
#
# INSTALLATION (on the panel host, as root):
#   install -D -m755 ops/panel-pgdump.sh /lib65/scripts/kervax-panel-pgdump.sh
#   echo '17 3 * * * root /lib65/scripts/kervax-panel-pgdump.sh >> /var/log/kervax-panel-pgdump.log 2>&1' \
#       > /etc/cron.d/kervax-panel-pgdump && chmod 644 /etc/cron.d/kervax-panel-pgdump
# (the script lives in /lib65 because /usr is excluded from backups; the cron config is in /etc.)
set -euo pipefail

DIR="${KERVAX_DIR:-/app/kervax}"
OUT="${KERVAX_DUMP_DIR:-/backup/pg/kervax-db-1}"
KEEP="${KERVAX_DUMP_KEEP:-14}"
DB_CT="${KERVAX_DB_CONTAINER:-kervax-db-1}"
PUSH="${KERVAX_DUMP_PUSH_URL:-}"
RESTIC_ENV="${KERVAX_DUMP_RESTIC_ENV:-}"
NOW=$(date +%s)
umask 077

envval() { sed -n "s/^$1=//p" "$DIR/.env" | tail -1; }

push() {  # status, message
    [ -n "$PUSH" ] || return 0
    curl -fsS --max-time 15 -G "$PUSH" --data-urlencode "status=$1" --data-urlencode "msg=$2" >/dev/null 2>&1 || true
}

fail() {
    echo "$(date '+%F %T') FAIL: $*" >&2
    printf 'fail %s %s\n' "$NOW" "$*" > "$OUT/last.status" 2>/dev/null || true
    push down "$*"
    exit 1
}

mkdir -p "$OUT" || fail "cannot create $OUT"
[ -f "$DIR/.env" ] || fail "no $DIR/.env"
PW=$(envval KERVAX_DB_PASSWORD)
DBUSER=$(envval KERVAX_DB_USER); DBUSER=${DBUSER:-kervax}
DBNAME=$(envval KERVAX_DB_NAME); DBNAME=${DBNAME:-kervax}

file="$OUT/kervax-$(date +%Y%m%d-%H%M%S).dump"
docker exec -e PGPASSWORD="$PW" "$DB_CT" pg_dump -U "$DBUSER" -d "$DBNAME" -Fc > "$file.tmp" \
    || { rm -f "$file.tmp"; fail "pg_dump failed"; }

# The archive's table of contents, read by pg_restore of the same container (no client on the host)
toc=$(docker exec -i "$DB_CT" pg_restore --list < "$file.tmp") || { rm -f "$file.tmp"; fail "the dump is not a readable archive"; }
for t in alembic_version servers checks users app_settings; do
    printf '%s\n' "$toc" | grep -q "TABLE DATA public $t " || { rm -f "$file.tmp"; fail "no data of table $t in the dump"; }
done
mv "$file.tmp" "$file"
cp "$DIR/.env" "$OUT/dotenv"
size=$(stat -c %s "$file")

# the newest $KEEP dumps stay
ls -1t "$OUT"/kervax-*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while IFS= read -r old; do rm -f -- "$old"; done

if [ -n "$RESTIC_ENV" ]; then
    command -v restic >/dev/null || fail "restic is not installed"
    set -a; . "$RESTIC_ENV"; set +a
    restic backup --quiet --host "$(hostname)" --tag kervax-panel "$file" "$OUT/dotenv" \
        || fail "restic backup failed"
    restic forget --quiet --host "$(hostname)" --tag kervax-panel --keep-daily 14 --keep-weekly 8 \
        || echo "restic forget failed, the copy itself is saved" >&2
fi

printf 'ok %s %s %s\n' "$NOW" "$file" "$size" > "$OUT/last.status"
push up "$(basename "$file") $((size / 1024 / 1024)) MB"
echo "$(date '+%F %T') ok: $file ($size bytes)"
