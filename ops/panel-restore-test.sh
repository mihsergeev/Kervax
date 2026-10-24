#!/bin/bash
# Restore test of the panel's database dump: does the dump really come back as a working base.
#
#   ops/panel-restore-test.sh [dump]
#
# The dump is either a pg_dump -Fc archive (ops/panel-pgdump.sh, *.dump) or a directory of the
# Kervax backup helper (<date>/kervax.sql.gz + globals.sql.gz). Without an argument the newest
# one in /backup/pg/kervax-db-1 is taken.
#
# It is restored into a throwaway postgres container of the same image as kervax-db-1: no
# network, data in an anonymous volume on disk (a 2 GB base in tmpfs would eat the memory of a
# small panel host), container and volume removed at the end whatever happens. The live
# database is only read. Then the restored base is compared with the live one: the counts of
# servers, monitors and users must be the same or close (the dump is hours old).
#
# Run it on the panel host as root, monthly or after anything changed in how dumps are made.
# Exit code 0 - the dump restores and matches, 1 - it does not.
set -uo pipefail

DIR="${KERVAX_DIR:-/app/kervax}"
SRC="${1:-}"
DB_CT="${KERVAX_DB_CONTAINER:-kervax-db-1}"
T=kervax-restore-test
[ -z "$SRC" ] && SRC=$(ls -1td /backup/pg/kervax-db-1/kervax-*.dump /backup/pg/kervax-db-1/*/ 2>/dev/null | head -1)
[ -n "$SRC" ] && [ -e "$SRC" ] || { echo "no dump found (give one as an argument)"; exit 1; }

envval() { sed -n "s/^$1=//p" "$DIR/.env" | tail -1; }
PW=$(envval KERVAX_DB_PASSWORD)
DBUSER=$(envval KERVAX_DB_USER); DBUSER=${DBUSER:-kervax}
DBNAME=$(envval KERVAX_DB_NAME); DBNAME=${DBNAME:-kervax}
IMAGE=$(docker inspect -f '{{.Config.Image}}' "$DB_CT" 2>/dev/null || echo postgres:18-alpine)

Q="select (select version_num from alembic_version), (select count(*) from servers), (select count(*) from checks), (select count(*) from users), (select count(*) from server_metrics)"

cleanup() { docker rm -fv "$T" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

echo "dump:  $SRC"
echo "image: $IMAGE"
docker run -d --name "$T" --network none -v /var/lib/postgresql \
    -e POSTGRES_PASSWORD=restore-test -e POSTGRES_USER="$DBUSER" -e POSTGRES_DB=postgres \
    "$IMAGE" >/dev/null || { echo "could not start $IMAGE"; exit 1; }
for _ in $(seq 60); do
    docker exec "$T" pg_isready -U "$DBUSER" -d postgres >/dev/null 2>&1 && break
    sleep 1
done
# the entrypoint restarts postgres once after init: wait for the final one
sleep 3
for _ in $(seq 30); do
    docker exec "$T" psql -U "$DBUSER" -d postgres -tAc 'select 1' >/dev/null 2>&1 && break
    sleep 1
done

t0=$(date +%s)
if [ -d "$SRC" ]; then
    # the helper's pg_dumpall-style set: roles first, then the database itself
    [ -f "$SRC/globals.sql.gz" ] && zcat "$SRC/globals.sql.gz" | docker exec -i "$T" psql -q -U "$DBUSER" -d postgres >/dev/null 2>&1
    f="$SRC/$DBNAME.sql.gz"
    [ -f "$f" ] || { echo "no $DBNAME.sql.gz in $SRC"; exit 1; }
    if zcat "$f" | head -c 20000 | grep -q '^CREATE DATABASE'; then
        zcat "$f" | docker exec -i "$T" psql -q -v ON_ERROR_STOP=1 -U "$DBUSER" -d postgres > /tmp/$T.log 2>&1
    else
        docker exec "$T" createdb -U "$DBUSER" "$DBNAME"
        zcat "$f" | docker exec -i "$T" psql -q -v ON_ERROR_STOP=1 -U "$DBUSER" -d "$DBNAME" > /tmp/$T.log 2>&1
    fi
    rc=$?
else
    docker exec "$T" createdb -U "$DBUSER" "$DBNAME"
    docker exec -i "$T" pg_restore -U "$DBUSER" -d "$DBNAME" --no-owner --exit-on-error < "$SRC" > /tmp/$T.log 2>&1
    rc=$?
fi
if [ "$rc" != 0 ]; then
    echo "✗ the restore failed (rc=$rc):"; tail -20 /tmp/$T.log
    exit 1
fi
echo "restored in $(( $(date +%s) - t0 )) s"

got=$(docker exec "$T" psql -U "$DBUSER" -d "$DBNAME" -tAc "$Q" 2>&1)
live=$(docker exec -e PGPASSWORD="$PW" "$DB_CT" psql -U "$DBUSER" -d "$DBNAME" -tAc "$Q" 2>&1)
echo "             migration|servers|monitors|users|metric rows"
echo "  restored:  $got"
echo "  live:      $live"

IFS='|' read -r m1 s1 c1 u1 x1 <<< "$got"
IFS='|' read -r m2 s2 c2 u2 x2 <<< "$live"
bad=0
[ -n "$m1" ] || { echo "✗ no migration number in the restored base"; bad=1; }
# a release with a migration since the dump is normal, the counts are compared all the same
[ "$m1" = "$m2" ] || echo "! the migration differs: $m1 in the dump, $m2 live"
for pair in "servers $s1 $s2" "monitors $c1 $c2" "users $u1 $u2"; do
    set -- $pair
    [ -n "$2" ] || { echo "✗ $1: not readable in the restored base"; bad=1; continue; }
    [ "$2" -gt 0 ] || [ "$3" = 0 ] || { echo "✗ $1: none restored, $3 live"; bad=1; continue; }
    # a few added or removed since the dump is normal
    d=$(( $2 > $3 ? $2 - $3 : $3 - $2 ))
    [ "$d" -le $(( $3 / 10 + 2 )) ] || { echo "✗ $1: $2 restored against $3 live"; bad=1; }
done
[ "${x1:-0}" -gt 0 ] || [ "${x2:-0}" = 0 ] || { echo "✗ no metrics restored"; bad=1; }
rm -f /tmp/$T.log
if [ "$bad" = 0 ]; then echo "✓ the dump restores and matches the live panel"; else exit 1; fi
