"""Load alert rules from configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg

from archive import audit

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "alert_rules.json"


FIELDS = ("description", "subject_kind", "subject", "condition", "threshold",
          "for_seconds", "severity", "recipients")
DEFAULTS = {"for_seconds": 30, "severity": "warning", "recipients": []}


def seed(conn: psycopg.Connection, spec: dict, actor: str) -> int:
    """Create or update alert rules. Every change is audited with the value it
    replaced (§433); a rule that is already as configured is left alone and
    writes no audit row."""
    count = 0
    with conn.cursor() as cur:
        for r in spec["rules"]:
            wanted = {f: r.get(f, DEFAULTS.get(f)) for f in FIELDS}
            cur.execute("SELECT id FROM alert_rule WHERE name = %s", (r["name"],))
            existing = cur.fetchone()
            if existing is None:
                cur.execute(
                    "INSERT INTO alert_rule (name, " + ", ".join(FIELDS) + ")"
                    " VALUES (" + ", ".join(["%s"] * (len(FIELDS) + 1)) + ")"
                    " RETURNING id", [r["name"], *wanted.values()])
                rule_id = cur.fetchone()[0]
                audit.record(cur, actor=actor, entity="alert_rule",
                             entity_id=rule_id, field=None, old=None,
                             new=r["name"], reason="seeded from config")
                count += 1
                continue
            changed = False
            for field, value in wanted.items():
                changed |= audit.change(cur, "alert_rule", existing[0], field,
                                        value, actor=actor,
                                        reason="seeded from config")
            count += changed
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
    print(f"{count} alert rules created or changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
