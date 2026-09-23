"""Machine-readable export of data, KPI results and configuration. (§503)

CSV for series, JSON for configuration. Both are formats a customer can read
with tools they already have, which is what "machine-readable" is for.

THE EXPORT CARRIES QUALITY. Every sample row has its StatusCode and its decoded
class, and a Bad sample exports an EMPTY value field, never a zero. An export
that drops quality hands the recipient a file that looks authoritative and
cannot be checked — and once it has left this system, nobody can ever recover
which readings were trustworthy.

The manifest records what was exported, when, by whom and over what window, so
a file found later can be traced back.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import psycopg

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")


def _rows(conn, sql: str, params: tuple = ()) -> tuple[list[str], list[tuple]]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [d.name for d in cur.description], cur.fetchall()


def _csv(columns: list[str], rows: list[tuple]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return out.getvalue()


def export(conn: psycopg.Connection, destination: Path, *,
           start: dt.datetime, end: dt.datetime, tags: list[str] | None = None,
           actor: str = "unknown") -> dict:
    parts: dict[str, str] = {}

    # --- samples, with quality on every row --------------------------------
    # server_ts is empty where no server stamped the value; collector_run and
    # seq say which collector run archived the row and its place in that run's
    # numbering, so a recipient can reconcile loss without this system.
    sql = ("SELECT t.name AS tag, s.source_ts, s.server_ts, s.value, s.quality,"
           " quality_class(s.quality) AS quality_class, s.collector_run, s.seq"
           " FROM sample s JOIN tag t ON t.id = s.tag_id"
           " WHERE s.source_ts BETWEEN %s AND %s")
    params: list = [start, end]
    if tags:
        sql += " AND t.name = ANY(%s)"
        params.append(tags)
    sql += " ORDER BY t.name, s.source_ts"
    columns, rows = _rows(conn, sql, tuple(params))
    parts["samples.csv"] = _csv(columns, rows)
    sample_count = len(rows)

    # --- KPI results, with the version that produced each one --------------
    columns, rows = _rows(conn,
        "SELECT d.name AS kpi, k.kpi_version, e.asset_code, k.ts, k.value,"
        " k.quality, quality_class(k.quality) AS quality_class, k.reason,"
        " d.engineering_unit, d.equation"
        " FROM kpi_value k JOIN kpi_definition d ON d.id = k.kpi_definition_id"
        " JOIN element e ON e.id = k.element_id"
        " WHERE k.ts BETWEEN %s AND %s ORDER BY d.name, k.ts", (start, end))
    parts["kpi_values.csv"] = _csv(columns, rows)
    kpi_count = len(rows)

    # --- configuration ------------------------------------------------------
    config: dict = {}
    for name, sql in (
        ("tags", "SELECT id, name, description, engineering_unit, range_low,"
                 " range_high, source_system, source_path, scan_rate_ms,"
                 " exc_dev, comp_dev, max_time_ms, compress, element_id"
                 " FROM tag ORDER BY name"),
        ("elements", "SELECT id, asset_code, name, level, parent_id, template_id,"
                     " context FROM element ORDER BY asset_code"),
        ("element_templates", "SELECT id, name, description, parent_template_id,"
                              " level FROM element_template ORDER BY name"),
        ("attributes", "SELECT id, element_id, name, engineering_unit, tag_id,"
                       " static_value FROM attribute ORDER BY element_id, name"),
        ("kpi_definitions", "SELECT id, name, description, classification,"
                            " equation, inputs, constants, engineering_unit,"
                            " reference_value, validity_low, validity_high,"
                            " bad_data_treatment, version, valid_from, valid_to"
                            " FROM kpi_definition ORDER BY name, version"),
        ("quality_rules", "SELECT q.id, t.name AS tag, q.rule_type, q.params,"
                          " q.enabled FROM quality_rule q JOIN tag t ON t.id = q.tag_id"
                          " ORDER BY t.name, q.rule_type"),
        ("alert_rules", "SELECT id, name, description, subject_kind, subject,"
                        " condition, threshold, for_seconds, severity, enabled"
                        " FROM alert_rule ORDER BY name"),
        ("event_templates", "SELECT DISTINCT template FROM event_frame ORDER BY template"),
    ):
        columns, rows = _rows(conn, sql)
        config[name] = [dict(zip(columns, row)) for row in rows]
    sources = Path(__file__).parent.parent / "config" / "sources.json"
    if sources.exists():
        config["sources"] = json.loads(sources.read_text())
    parts["configuration.json"] = json.dumps(config, indent=2, default=str)

    # --- event frames --------------------------------------------------------
    columns, rows = _rows(conn,
        "SELECT f.id, f.template, e.asset_code, f.start_ts, f.end_ts, f.status,"
        " m.name AS milestone, m.ts AS milestone_ts, m.value, m.quality"
        " FROM event_frame f JOIN element e ON e.id = f.element_id"
        " LEFT JOIN event_milestone m ON m.event_frame_id = f.id"
        " WHERE f.start_ts BETWEEN %s AND %s"
        " ORDER BY f.start_ts, m.sort_order", (start, end))
    parts["event_frames.csv"] = _csv(columns, rows)

    # --- audit trail ----------------------------------------------------------
    columns, rows = _rows(conn,
        "SELECT id, ts, actor, entity, entity_id, field, old_value, new_value,"
        " reason FROM audit_log WHERE ts BETWEEN %s AND %s ORDER BY id",
        (start, end))
    parts["audit_log.csv"] = _csv(columns, rows)

    manifest = {
        "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "exported_by": actor,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "tags": tags or "all",
        "counts": {"samples": sample_count, "kpi_values": kpi_count,
                   "event_frame_rows": len(rows)},
        "notes": [
            "Quality is the numeric OPC UA StatusCode, with a decoded class "
            "alongside it.",
            "A sample that arrived Bad has an EMPTY value field. It is not a "
            "zero, and it must not be read as one.",
            "kpi_values carries the definition version that produced each "
            "result, and the equation as it stood.",
        ],
        "files": {},
    }
    for name, content in parts.items():
        manifest["files"][name] = {
            "bytes": len(content.encode()),
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ops.export")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--tags", default=None,
                    help="comma-separated; default is every tag")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--actor", default=os.environ.get("USER", "unknown"))
    args = ap.parse_args(argv)

    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(hours=args.hours)
    out = args.out or Path("exports") / (
        f"crpms-export-{end.strftime('%Y%m%dT%H%M%SZ')}.zip")
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None

    with psycopg.connect(DSN) as conn:
        manifest = export(conn, out, start=start, end=end, tags=tags,
                          actor=args.actor)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (actor, entity, entity_id, field,"
                " old_value, new_value, reason) VALUES"
                " (%s,'export',%s,NULL,NULL,%s,'machine-readable export §503')",
                (args.actor, out.name, json.dumps(manifest["counts"])))
        conn.commit()

    print(f"exported to {out} ({out.stat().st_size / 1048576:.1f} MB)")
    for name, info in manifest["files"].items():
        print(f"  {name:<22} {info['bytes']:>10,} bytes  "
              f"sha256 {info['sha256'][:16]}")
    print(f"  counts: {manifest['counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
