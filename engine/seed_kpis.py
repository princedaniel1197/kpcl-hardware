"""Load KPI definitions from configuration.

A definition is never edited in place once it has produced values. Changing an
equation closes the current version and opens the next, so every stored result
still points at the equation that produced it (§320, §379, §381).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "kpi_definitions.json"


def seed(conn: psycopg.Connection, spec: dict, actor: str) -> tuple[int, int]:
    created = superseded = 0
    with conn.cursor() as cur:
        for d in spec["definitions"]:
            cur.execute(
                "SELECT id, version, equation, inputs, constants, validity_low,"
                " validity_high FROM kpi_definition WHERE name = %s"
                " AND valid_to IS NULL", (d["name"],))
            current = cur.fetchone()

            # Anything that changes the meaning of the number.
            FIELDS = ("equation", "inputs", "constants", "validity_low",
                      "validity_high")
            incoming = {"equation": d["equation"], "inputs": d["inputs"],
                        "constants": d["constants"],
                        "validity_low": d.get("validity_low"),
                        "validity_high": d.get("validity_high")}
            if current is not None:
                existing = dict(zip(FIELDS, current[2:7]))
                changed = {f: (existing[f], incoming[f]) for f in FIELDS
                           if existing[f] != incoming[f]}
                if not changed:
                    continue
                # Close the old version rather than editing it: results already
                # stored were produced by it and must keep pointing at it.
                cur.execute("UPDATE kpi_definition SET valid_to = now()"
                            " WHERE id = %s", (current[0],))
                # One audit row per field that ACTUALLY changed. Recording
                # "equation" with identical old and new values, because the
                # change was really in the constants, is worse than no audit
                # row at all -- it sends the reader looking in the wrong place.
                for field, (was, now_) in changed.items():
                    cur.execute(
                        "INSERT INTO audit_log (actor, entity, entity_id,"
                        " field, old_value, new_value, reason) VALUES"
                        " (%s,'kpi_definition',%s,%s,%s,%s,%s)",
                        (actor, str(current[0]), field, json.dumps(was),
                         json.dumps(now_),
                         f"superseded by v{current[1] + 1}"))
                superseded += 1
                version = current[1] + 1
            else:
                version = d.get("version", 1)

            cur.execute(
                "INSERT INTO kpi_definition (name, description, classification,"
                " equation, inputs, constants, engineering_unit,"
                " reference_value, validity_low, validity_high,"
                " bad_data_treatment, calculation_freq_ms, version, created_by)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'propagate',%s,%s,%s)"
                " RETURNING id",
                (d["name"], d.get("description"), d["classification"],
                 d["equation"], json.dumps(d["inputs"]),
                 json.dumps(d["constants"]), d.get("engineering_unit"),
                 d.get("reference_value"), d.get("validity_low"),
                 d.get("validity_high"), d.get("calculation_freq_ms", 60000),
                 version, actor))
            new_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO audit_log (actor, entity, entity_id, field,"
                " old_value, new_value, reason) VALUES"
                " (%s,'kpi_definition',%s,NULL,NULL,%s,'seeded from config')",
                (actor, str(new_id), f"{d['name']} v{version}"))
            created += 1
    conn.commit()
    return created, superseded


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engine.seed_kpis")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--actor", default=os.environ.get("USER", "unknown"))
    ap.add_argument("--dictionary", type=Path,
                    default=Path(__file__).parent.parent / "docs" / "kpi-dictionary.md")
    args = ap.parse_args(argv)
    spec = json.loads(args.config.read_text())
    dsn = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
    with psycopg.connect(dsn) as conn:
        created, superseded = seed(conn, spec, args.actor)
        from engine.kpi import dictionary_markdown
        args.dictionary.parent.mkdir(parents=True, exist_ok=True)
        args.dictionary.write_text(dictionary_markdown(conn))
    print(f"{created} definition(s) written, {superseded} superseded; "
          f"dictionary regenerated at {args.dictionary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
