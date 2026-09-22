"""Load alert rules from configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "alert_rules.json"


def seed(conn: psycopg.Connection, spec: dict, actor: str) -> int:
    count = 0
    with conn.cursor() as cur:
        for r in spec["rules"]:
            cur.execute(
                "INSERT INTO alert_rule (name, description, subject_kind,"
                " subject, condition, threshold, for_seconds, severity,"
                " recipients) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (name) DO UPDATE SET"
                " description=EXCLUDED.description,"
                " subject_kind=EXCLUDED.subject_kind, subject=EXCLUDED.subject,"
                " condition=EXCLUDED.condition, threshold=EXCLUDED.threshold,"
                " for_seconds=EXCLUDED.for_seconds, severity=EXCLUDED.severity,"
                " recipients=EXCLUDED.recipients RETURNING id",
                (r["name"], r.get("description"), r["subject_kind"],
                 r["subject"], r["condition"], r.get("threshold"),
                 r.get("for_seconds", 30), r.get("severity", "warning"),
                 r.get("recipients", [])))
            rule_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO audit_log (actor, entity, entity_id, field,"
                " old_value, new_value, reason) VALUES"
                " (%s,'alert_rule',%s,NULL,NULL,%s,'seeded from config')",
                (actor, str(rule_id), r["name"]))
            count += 1
    conn.commit()
    return count


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ops.seed_alerts")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--actor", default=os.environ.get("USER", "unknown"))
    args = ap.parse_args(argv)
    dsn = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
    with psycopg.connect(dsn) as conn:
        count = seed(conn, json.loads(args.config.read_text()), args.actor)
    print(f"{count} alert rules configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
