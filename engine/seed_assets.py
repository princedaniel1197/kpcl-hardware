"""Seed the asset hierarchy from a configuration file.

KPCL -> RTPS -> Unit 1 -> Boiler / Turbine / Generator -> equipment ->
parameters, with the Stage 1 tags mapped onto attributes (§392).

Everything here reads `config/asset_model.json`. Adding a station, a unit or a
piece of equipment is an edit to that file, not to this one.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg

from engine import assets

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "asset_model.json"


def seed(conn: psycopg.Connection, model: dict, actor: str) -> dict:
    counts = {"templates": 0, "attributes": 0, "compositions": 0, "elements": 0}

    for spec in model["templates"]:
        assets.create_template(conn, spec["name"], level=spec.get("level"),
                               description=spec.get("description"),
                               derives_from=spec.get("derives_from"))
        counts["templates"] += 1
        for attribute in spec.get("attributes", []):
            assets.add_attribute_template(conn, spec["name"], attribute["name"],
                                          **{k: v for k, v in attribute.items()
                                             if k != "name"})
            counts["attributes"] += 1

    for link in model.get("composition", []):
        assets.compose(conn, link["parent"], link["child"], name=link["name"],
                       code_suffix=link["code_suffix"], level=link.get("level"),
                       sort_order=link.get("sort_order", 0))
        counts["compositions"] += 1
    conn.commit()

    for element in model.get("elements", []):
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM element WHERE asset_code = %s",
                        (element["asset_code"],))
            if cur.fetchone():
                continue
        assets.instantiate(conn, element["template"],
                           asset_code=element["asset_code"], name=element["name"],
                           context=element.get("context", {}),
                           parent_code=element.get("parent_code"), actor=actor)
        counts["elements"] += 1
        conn.commit()
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="engine.seed_assets")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--actor", default=os.environ.get("USER", "unknown"))
    args = ap.parse_args(argv)
    model = json.loads(args.config.read_text())
    dsn = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
    with psycopg.connect(dsn) as conn:
        counts = seed(conn, model, args.actor)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM element")
            total = cur.fetchone()[0]
    print(f"templates {counts['templates']}, attributes {counts['attributes']}, "
          f"compositions {counts['compositions']}, new element trees "
          f"{counts['elements']}; {total} elements now exist")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
