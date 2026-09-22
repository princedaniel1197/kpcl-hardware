#!/bin/bash
# Restore a CRPMS backup into a CLEAN environment. (§512-518, §651)
#
# Restores into a SEPARATE container and database, never over the live one.
# A restore procedure that has only ever been run over the top of a working
# system has not been tested: the failure mode it must survive is the system
# being gone.
set -euo pipefail

DOCKER="${DOCKER:-/Applications/Docker.app/Contents/Resources/bin/docker}"
DUMP="${1:?usage: restore.sh <dump-file> [container-name]}"
TARGET="${2:-crpms-restore-test}"
USER_NAME="${POSTGRES_USER:-crpms}"
DB="${POSTGRES_DB:-crpms}"
PORT="${RESTORE_PORT:-5433}"

[ -f "$DUMP" ] || { echo "no such dump: $DUMP"; exit 1; }

echo "restoring $DUMP into a clean container '$TARGET' on port $PORT"
started=$(date +%s)

"$DOCKER" rm -f "$TARGET" >/dev/null 2>&1 || true
"$DOCKER" run -d --name "$TARGET" \
    -e POSTGRES_USER="$USER_NAME" -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-crpms}" \
    -e POSTGRES_DB="$DB" -e TZ=UTC -e PGTZ=UTC \
    -p "$PORT:5432" timescale/timescaledb:latest-pg16 >/dev/null

printf "waiting for the clean instance "
for _ in $(seq 1 90); do
    if "$DOCKER" exec "$TARGET" pg_isready -U "$USER_NAME" -d "$DB" >/dev/null 2>&1; then
        echo " ready"; break
    fi
    printf "."; sleep 1
done

# TimescaleDB requires the extension before restoring its objects, AND it
# requires the restore to be bracketed by timescaledb_pre_restore() and
# timescaledb_post_restore(). Those switch off the chunk-routing machinery for
# the duration; without them, rows destined for hypertable chunks are re-routed
# during the load and silently go missing.
#
# Measured before this was added: a restore that reported success and finished
# in 22 s had 8,943,924 of 10,154,800 samples. 1.2 million rows lost, with
# nothing on the console to say so -- because this script also threw away
# pg_restore's stderr. Both were wrong, and the second is why the first was
# invisible.
"$DOCKER" exec "$TARGET" psql -U "$USER_NAME" -d "$DB" -q -c \
    "CREATE EXTENSION IF NOT EXISTS timescaledb" >/dev/null
"$DOCKER" exec "$TARGET" psql -U "$USER_NAME" -d "$DB" -q -c \
    "SELECT timescaledb_pre_restore()" >/dev/null

RESTORE_LOG=$(mktemp)
set +e
"$DOCKER" exec -i "$TARGET" pg_restore -U "$USER_NAME" -d "$DB" \
    --no-owner --no-privileges --jobs=1 < "$DUMP" 2>"$RESTORE_LOG"
RESTORE_STATUS=$?
set -e

"$DOCKER" exec "$TARGET" psql -U "$USER_NAME" -d "$DB" -q -c \
    "SELECT timescaledb_post_restore()" >/dev/null

# pg_restore's warnings are reported, never swallowed. A restore that hides its
# complaints is the reason a lost 12% of the archive went unnoticed.
if [ -s "$RESTORE_LOG" ]; then
    echo
    echo "pg_restore reported $(wc -l < "$RESTORE_LOG" | tr -d ' ') line(s):"
    head -20 "$RESTORE_LOG" | sed 's/^/    /'
fi
rm -f "$RESTORE_LOG"
[ "$RESTORE_STATUS" -eq 0 ] || echo "WARNING: pg_restore exited $RESTORE_STATUS"

finished=$(date +%s)
echo
echo "restore duration (RTO): $((finished - started))s"
echo
echo "verification (compare these against the source before trusting the backup):"
"$DOCKER" exec "$TARGET" psql -U "$USER_NAME" -d "$DB" -c \
  "SELECT (SELECT count(*) FROM sample) AS samples,
          (SELECT count(*) FROM tag) AS tags,
          (SELECT count(*) FROM element) AS elements,
          (SELECT count(*) FROM kpi_definition) AS kpi_definitions,
          (SELECT max(source_ts) FROM sample) AS newest_sample"
echo
echo "the clean instance is still running on port $PORT; remove it with:"
echo "  $DOCKER rm -f $TARGET"
