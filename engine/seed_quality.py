"""Load quality rule thresholds from configuration into the archive."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg

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
                cur.execute(
                    "INSERT INTO quality_rule (tag_id, rule_type, params)"
                    " VALUES (%s,%s,%s) ON CONFLICT (tag_id, rule_type)"
                    " DO UPDATE SET params = EXCLUDED.params, enabled = true"
                    " RETURNING id", (row[0], rule_type, json.dumps(params)))
                rule_id = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO audit_log (actor, entity, entity_id, field,"
                    " old_value, new_value, reason) VALUES"
                    " (%s,'quality_rule',%s,%s,NULL,%s,'seeded from config')",
                    (actor, str(rule_id), rule_type, json.dumps(params)))
                count += 1
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
    print(f"{count} quality rules configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
