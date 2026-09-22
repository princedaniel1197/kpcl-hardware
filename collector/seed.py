"""Seed tag configuration into the archive from a config file.

Adding a tag is configuration, not code (§341). This reads a JSON file and
upserts rows into `tag`, writing every change to `audit_log` with actor,
old value, new value and reason (§433).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "unit1_tags.json"

FIELDS = ("description", "engineering_unit", "range_low", "range_high",
          "source_system", "scan_rate_ms", "exc_dev", "comp_dev", "max_time_ms",
          "source_path")


def seed(conn: psycopg.Connection, tags: list[dict], actor: str) -> tuple[int, int]:
    created = updated = 0
    with conn.cursor() as cur:
        for spec in tags:
            # Built from FIELDS rather than written out, so adding a column
            # to FIELDS cannot leave the query behind. It already did once:
            # source_path was added to FIELDS and the SELECT still returned the
            # old nine columns, which failed with an index error rather than a
            # useful message.
            cur.execute(
                "SELECT id, " + ", ".join(FIELDS) + " FROM tag WHERE name = %s",
                (spec["name"],))
            existing = cur.fetchone()

            if existing is None:
                columns = ", ".join(("name",) + FIELDS)
                placeholders = ", ".join(["%s"] * (len(FIELDS) + 1))
                cur.execute(
                    f"INSERT INTO tag ({columns}) VALUES ({placeholders}) RETURNING id",
                    [spec["name"]] + [spec.get(f) for f in FIELDS])
                tag_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO audit_log (actor, entity, entity_id, field,"
                    " old_value, new_value, reason) VALUES"
                    " (%s,'tag',%s,NULL,NULL,%s,'seeded from config')",
                    (actor, str(tag_id), json.dumps(spec)))
                created += 1
                continue

            tag_id, *current = existing
            for index, field in enumerate(FIELDS):
                want, have = spec.get(field), current[index]
                if want is None or str(want) == str(have):
                    continue
                cur.execute(f"UPDATE tag SET {field} = %s WHERE id = %s",
                            (want, tag_id))
                cur.execute(
                    "INSERT INTO audit_log (actor, entity, entity_id, field,"
                    " old_value, new_value, reason) VALUES"
                    " (%s,'tag',%s,%s,%s,%s,'seeded from config')",
                    (actor, str(tag_id), field, str(have), str(want)))
                updated += 1
    conn.commit()
    return created, updated


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collector.seed")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--actor", default=os.environ.get("USER", "unknown"))
    args = ap.parse_args(argv)

    tags = json.loads(args.config.read_text())["tags"]
    dsn = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
    with psycopg.connect(dsn) as conn:
        created, updated = seed(conn, tags, args.actor)
    print(f"{len(tags)} tags in {args.config.name}: "
          f"{created} created, {updated} field(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
