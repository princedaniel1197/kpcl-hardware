"""Central capacity monitoring. (§344)

Measures what actually runs out: disk, rows, chunks, and the rate of growth. A
capacity figure without a growth rate answers "how full is it" but not "when
does it stop working", and the second question is the one that matters.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil

import psycopg

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")

METRICS_SQL = {
    "database_bytes": "SELECT pg_database_size(current_database())",
    "sample_bytes": "SELECT hypertable_size('sample')",
    "sample_rows": "SELECT count(*) FROM sample",
    "kpi_value_rows": "SELECT count(*) FROM kpi_value",
    "quality_flag_rows": "SELECT count(*) FROM quality_flag",
    "chunks": "SELECT count(*) FROM timescaledb_information.chunks",
    "tags": "SELECT count(*) FROM tag",
    "elements": "SELECT count(*) FROM element",
    "open_alerts": "SELECT count(*) FROM alert WHERE cleared_at IS NULL",
}


def collect(conn: psycopg.Connection, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    values: dict[str, float] = {}
    with conn.cursor() as cur:
        for metric, sql in METRICS_SQL.items():
            try:
                cur.execute(sql)
                row = cur.fetchone()
                values[metric] = float(row[0]) if row and row[0] is not None else 0.0
            except psycopg.Error:
                conn.rollback()
                continue
    usage = shutil.disk_usage("/")
    values["host_disk_free_bytes"] = float(usage.free)
    values["host_disk_total_bytes"] = float(usage.total)
    values["host_disk_used_pct"] = round(
        (usage.total - usage.free) / usage.total * 100, 3)

    with conn.cursor() as cur:
        for metric, value in values.items():
            cur.execute(
                "INSERT INTO capacity_sample (ts, metric, value) VALUES (%s,%s,%s)"
                " ON CONFLICT (ts, metric) DO UPDATE SET value = EXCLUDED.value",
                (now, metric, value))
    conn.commit()
    return values


def growth(conn: psycopg.Connection, metric: str, over_days: float = 1.0
           ) -> dict | None:
    """Rate of change, and the projection that makes it useful."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, value FROM capacity_sample WHERE metric = %s"
            " AND ts > now() - (%s || ' days')::interval ORDER BY ts",
            (metric, str(over_days)))
        rows = cur.fetchall()
    if len(rows) < 2:
        return None
    (t0, v0), (t1, v1) = rows[0], rows[-1]
    seconds = (t1 - t0).total_seconds()
    if seconds <= 0:
        return None
    per_day = (v1 - v0) / seconds * 86400
    return {"metric": metric, "from": v0, "to": v1, "per_day": per_day,
            "window_s": seconds}


def days_until_full(conn: psycopg.Connection) -> float | None:
    """How long the host disk lasts at the current rate.

    None when growth is zero or negative — an honest "not applicable" rather
    than a reassuring large number.
    """
    rate = growth(conn, "database_bytes")
    if not rate or rate["per_day"] <= 0:
        return None
    usage = shutil.disk_usage("/")
    return usage.free / rate["per_day"]


def report(conn: psycopg.Connection) -> str:
    values = collect(conn)
    lines = ["Capacity", "=" * 60]
    def human(n: float) -> str:
        for unit in ("B", "kB", "MB", "GB", "TB"):
            if abs(n) < 1024:
                return f"{n:,.1f} {unit}"
            n /= 1024
        return f"{n:,.1f} PB"
    lines.append(f"  database            {human(values['database_bytes'])}")
    lines.append(f"  sample hypertable   {human(values['sample_bytes'])}")
    lines.append(f"  sample rows         {values['sample_rows']:,.0f}")
    lines.append(f"  chunks              {values['chunks']:,.0f}")
    lines.append(f"  tags / elements     {values['tags']:,.0f} / "
                 f"{values['elements']:,.0f}")
    lines.append(f"  host disk used      {values['host_disk_used_pct']:.1f}%")
    lines.append(f"  host disk free      {human(values['host_disk_free_bytes'])}")
    rate = growth(conn, "database_bytes")
    if rate:
        lines.append(f"  growth              {human(rate['per_day'])}/day "
                     f"(over {rate['window_s'] / 3600:.1f} h)")
    remaining = days_until_full(conn)
    lines.append(f"  disk exhausted in   "
                 + (f"{remaining:,.0f} days at the current rate"
                    if remaining else "not growing — no projection"))
    return "\n".join(lines)


def main() -> int:
    with psycopg.connect(DSN) as conn:
        print(report(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
