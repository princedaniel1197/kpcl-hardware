"""Seed tag configuration into the archive from a config file.

Adding a tag is configuration, not code (§341). This reads a JSON file and
upserts rows into `tag`, writing every change to `audit_log` with actor,
old value, new value and reason (§433).

The collector's own health tags are the exception, and deliberately: what they
measure is defined by the code that measures it (collector/health.py), so they
are generated from that code rather than listed by hand beside it. A health
measurement added to the code gets its tag; one retired from the code keeps its
tag and its history, with a description saying it is retired.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg

from archive import audit

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "unit1_tags.json"

FIELDS = ("description", "engineering_unit", "range_low", "range_high",
          "source_system", "scan_rate_ms", "exc_dev", "comp_dev", "max_time_ms",
          "source_path", "compress", "quality_note")


def health_tags() -> list[dict]:
    """One tag per health measurement per collector instance."""
    from collector.health import (HEALTH_MEASUREMENTS, INSTANCES,
                                  RETIRED_MEASUREMENTS, tag_name)
    tags = []
    for instance in INSTANCES:
        for measurement, (description, unit) in HEALTH_MEASUREMENTS.items():
            tags.append({
                "name": tag_name(instance, measurement),
                "description": f"{description} ({instance} collector)",
                "engineering_unit": unit,
                "source_system": "collector",
                "scan_rate_ms": 5000,
                "max_time_ms": 60000,
            })
        for measurement, description in RETIRED_MEASUREMENTS.items():
            tags.append({
                "name": tag_name(instance, measurement),
                "description": f"{description} ({instance} collector)",
                "source_system": "collector",
            })
    return tags


def seed(conn: psycopg.Connection, tags: list[dict], actor: str) -> tuple[int, int]:
    created = updated = 0
    with conn.cursor() as cur:
        for spec in tags:
            cur.execute("SELECT id FROM tag WHERE name = %s", (spec["name"],))
            existing = cur.fetchone()

            if existing is None:
                # Only the fields the spec gives: a field it omits takes the
                # column's default, not an explicit NULL.
                given = [f for f in FIELDS if spec.get(f) is not None]
                columns = ", ".join(["name", *given])
                placeholders = ", ".join(["%s"] * (len(given) + 1))
                cur.execute(
                    f"INSERT INTO tag ({columns}) VALUES ({placeholders}) RETURNING id",
                    [spec["name"]] + [spec[f] for f in given])
                tag_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO audit_log (actor, entity, entity_id, field,"
                    " old_value, new_value, reason) VALUES"
                    " (%s,'tag',%s,NULL,NULL,%s,'seeded from config')",
                    (actor, str(tag_id), json.dumps(spec)))
                created += 1
                continue

            tag_id = existing[0]
            for field in FIELDS:
                want = spec.get(field)
                if want is None:
                    continue
                # Compared as numbers where both are numbers: comparing the
                # strings "1" and "1.0" once wrote an audit row, and an UPDATE,
                # for every digital tag on every seed.
                updated += audit.change(cur, "tag", tag_id, field, want,
                                        actor=actor, reason="seeded from config")
    conn.commit()
    return created, updated


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collector.seed")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--actor", default=os.environ.get("USER", "unknown"))
    args = ap.parse_args(argv)

    tags = json.loads(args.config.read_text())["tags"] + health_tags()
    dsn = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
    with psycopg.connect(dsn) as conn:
        created, updated = seed(conn, tags, args.actor)
    print(f"{len(tags)} tags ({args.config.name} plus collector health): "
          f"{created} created, {updated} field(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
