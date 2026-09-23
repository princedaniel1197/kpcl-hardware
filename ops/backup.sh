#!/bin/bash
# Automatic backup of the CRPMS archive. (§512-518, §651)
#
# pg_dump inside the container, compressed, to a dated file. Custom format so a
# restore can be parallel and selective.
#
# WHAT THIS DOES NOT DO, said plainly: there is no off-site copy, no encryption
# at rest, and no retention enforcement beyond --keep. A backup that lives on
# the machine it is backing up protects against a bad migration, not against
# the machine. Off-siting it is a deployment decision and is not simulated here.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/docker.sh"
CONTAINER="${CONTAINER:-crpms-timescaledb}"
USER_NAME="${POSTGRES_USER:-crpms}"
DB="${POSTGRES_DB:-crpms}"
DEST="${BACKUP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/backups}"
KEEP="${KEEP:-7}"

mkdir -p "$DEST"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
FILE="$DEST/crpms-$STAMP.dump"

# Written under a temporary name and renamed only when pg_dump has succeeded. A
# dump that failed half way must never be mistaken for a backup -- and must
# never count towards retention, where it would push a good one out.
PARTIAL="$FILE.partial"
trap 'rm -f "$PARTIAL"' EXIT
started=$(date +%s)
"$DOCKER" exec "$CONTAINER" pg_dump -U "$USER_NAME" -d "$DB" \
    --format=custom --compress=6 > "$PARTIAL"
mv "$PARTIAL" "$FILE"
finished=$(date +%s)

SIZE=$(wc -c < "$FILE" | tr -d ' ')
echo "backup   : $FILE"
echo "size     : $(printf '%.1f' "$(echo "$SIZE/1048576" | bc -l)") MB"
echo "duration : $((finished - started))s"

# Record the backup in the archive itself, so "when was the last good backup"
# is answerable from the same place as everything else.
"$DOCKER" exec "$CONTAINER" psql -U "$USER_NAME" -d "$DB" -q -c \
  "INSERT INTO audit_log (actor, entity, entity_id, field, old_value, new_value, reason)
   VALUES ('backup','database','$DB','dump',NULL,'$FILE','automatic backup, $((finished-started))s, $SIZE bytes')" \
  >/dev/null || echo "note: could not record the backup in audit_log"

# Retention.
ls -1t "$DEST"/crpms-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    echo "retention: removing $old"
    rm -f "$old"
done
