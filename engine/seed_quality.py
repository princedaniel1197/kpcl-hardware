"""Load quality rule thresholds from configuration into the archive."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg

from archive import audit

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "quality_rules.json"


def seed(conn: psycopg.Connection, spec: dict, actor: str) -> int:
    count = 0
    with conn.cursor() as cur:
        for entry in spec["rules"]:
            cur.execute("SELECT id FROM tag WHERE name = %s", (entry["tag"],))
            row = cur.fetchone()
            if row is None:
                print(f"  skipped {entry['tag']}: no such tag")
                continue
            for rule_type, params in entry["rules"].items():
                # Read before writing, so the audit row can say what the rule
                # was -- an upsert that records "old value: NULL" whenever it
                # overwrites is an audit trail that cannot answer the one
                # question it exists for (§433). Unchanged rules write nothing.
                cur.execute("SELECT id, params, enabled FROM quality_rule"
                            " WHERE tag_id = %s AND rule_type = %s",
                            (row[0], rule_type))
                existing = cur.fetchone()
                if existing is None:
                    cur.execute(
                        "INSERT INTO quality_rule (tag_id, rule_type, params)"
                        " VALUES (%s,%s,%s) RETURNING id",
                        (row[0], rule_type, json.dumps(params)))
                    rule_id = cur.fetchone()[0]
                    audit.record(cur, actor=actor, entity="quality_rule",
                                 entity_id=rule_id, field=rule_type, old=None,
                                 new=params, reason="seeded from config")
                    count += 1
                    continue
                rule_id, _, enabled = existing
                changed = audit.change(cur, "quality_rule", rule_id, "params",
                                       params, actor=actor,
                                       reason="seeded from config")
                if not enabled:
                    audit.change(cur, "quality_rule", rule_id, "enabled", True,
                                 actor=actor, reason="seeded from config")
                    changed = True
                count += changed
    conn.commit()
    return count


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engine.seed_quality")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--actor", default=os.environ.get("USER", "unknown"))
    args = ap.parse_args(argv)
    spec = json.loads(args.config.read_text())
    dsn = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
    with psycopg.connect(dsn) as conn:
        count = seed(conn, spec, args.actor)
    print(f"{count} quality rules created or changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
