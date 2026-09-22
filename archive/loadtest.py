"""Stage 2 acceptance test: 10 million rows, and the queries over them.

From the build plan:

    insert 10 million synthetic rows; a 24-hour single-tag query returns in
    under 5 seconds (§528). Inserting the same (tag_id, source_ts) twice does
    not duplicate.

Run explicitly, not as part of the unit suite -- it writes gigabytes.

The synthetic data is not uniformly Good. Roughly 2% of rows are Bad and 3%
Uncertain, and every Bad row has a NULL value, because that is what the
protocol actually delivers (measured in Stage 1: a Bad DataValue carries
Value = None per OPC UA Part 4). A load test over perfectly clean data would
not exercise the index, the aggregate or the decode view the way real data does.
"""

from __future__ import annotations

import argparse
import os
import statistics
import time

import psycopg

DEFAULT_DSN = "postgresql://crpms:crpms@localhost:5432/crpms"

TOTAL_ROWS = 10_000_000
TAG_COUNT = 14
INTERVAL_S = 1

GOOD = 0
BAD_DEVICE_FAILURE = 2156593152
UNCERTAIN_SENSOR = 1083375616


def dsn() -> str:
    return os.environ.get("CRPMS_DSN", DEFAULT_DSN)


def ensure_tags(conn: psycopg.Connection, count: int) -> list[int]:
    ids = []
    with conn.cursor() as cur:
        for i in range(count):
            name = f"LOADTEST_TAG_{i:02d}"
            cur.execute(
                """
                INSERT INTO tag (name, description, engineering_unit,
                                 range_low, range_high, source_system,
                                 scan_rate_ms, exc_dev, comp_dev)
                VALUES (%s, 'Stage 2 load test', 'MW', 0, 250, 'loadtest',
                        1000, 0.5, 1.0)
                ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description
                RETURNING id
                """, (name,))
            ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def load(conn: psycopg.Connection, tag_ids: list[int], total: int) -> float:
    per_tag = total // len(tag_ids)
    started = time.monotonic()
    conn.autocommit = True
    with conn.cursor() as cur:
        for n, tag_id in enumerate(tag_ids, 1):
            cur.execute(
                """
                INSERT INTO sample (tag_id, source_ts, server_ts, value, quality)
                SELECT
                    %(tag)s,
                    ts,
                    -- server_ts is a genuinely different quantity: measurement
                    -- plus a transit delay that varies per row.
                    ts + (20 + (i %% 60)) * INTERVAL '1 millisecond',
                    CASE WHEN i %% 50 = 0 THEN NULL          -- Bad: no value
                         ELSE 100 + 40 * sin(i::float / 900)
                              + (i %% 17) * 0.3 END,
                    CASE WHEN i %% 50 = 0 THEN %(bad)s
                         WHEN i %% 33 = 0 THEN %(unc)s
                         ELSE 0 END
                FROM generate_series(0, %(n)s - 1) AS i,
                     LATERAL (SELECT TIMESTAMPTZ '2026-01-01 00:00:00+00'
                                     + i * %(step)s * INTERVAL '1 second') AS g(ts)
                ON CONFLICT (tag_id, source_ts) DO NOTHING
                """,
                {"tag": tag_id, "n": per_tag, "step": INTERVAL_S,
                 "bad": BAD_DEVICE_FAILURE, "unc": UNCERTAIN_SENSOR},
            )
            print(f"    tag {n}/{len(tag_ids)}: {per_tag:,} rows "
                  f"({time.monotonic() - started:.0f}s elapsed)", flush=True)
    conn.autocommit = False
    return time.monotonic() - started


def time_query(conn: psycopg.Connection, sql: str, params, runs: int = 5) -> list[float]:
    timings = []
    for _ in range(runs):
        started = time.monotonic()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cur.fetchall()
        timings.append(time.monotonic() - started)
    return timings


TWENTY_FOUR_HOURS = """
SELECT source_ts, value, quality
FROM sample
WHERE tag_id = %(tag)s
  AND source_ts >= %(start)s
  AND source_ts <  %(start)s + INTERVAL '24 hours'
ORDER BY source_ts
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=TOTAL_ROWS)
    ap.add_argument("--skip-load", action="store_true")
    args = ap.parse_args()

    with psycopg.connect(dsn()) as conn:
        print(f"Stage 2 load test — target {args.rows:,} rows\n")
        tag_ids = ensure_tags(conn, TAG_COUNT)

        if not args.skip_load:
            print("  loading ...")
            seconds = load(conn, tag_ids, args.rows)
            print(f"\n  load: {seconds:.1f}s "
                  f"({args.rows / seconds:,.0f} rows/s)\n")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sample")
            total = cur.fetchone()[0]
            cur.execute("SELECT pg_size_pretty(hypertable_size('sample'))")
            size = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM timescaledb_information.chunks "
                        "WHERE hypertable_name = 'sample'")
            chunks = cur.fetchone()[0]
        print(f"  rows in sample : {total:,}")
        print(f"  on disk        : {size}")
        print(f"  chunks         : {chunks}\n")

        # --- the criterion: 24-hour single-tag query under 5 s -------------
        # Pick the window from the data actually present, not a hard-coded
        # date. A window outside the data returns nothing in no time and would
        # report PASS while proving nothing.
        with conn.cursor() as cur:
            cur.execute("SELECT min(source_ts), max(source_ts) FROM sample "
                        "WHERE tag_id = %s", (tag_ids[0],))
            first, last = cur.fetchone()
            cur.execute("SELECT %s::timestamptz + (%s::timestamptz - %s::timestamptz)/2",
                        (first, last, first))
            midpoint = cur.fetchone()[0]
        print(f"  data spans {first} .. {last}")
        params = {"tag": tag_ids[0], "start": midpoint}
        with conn.cursor() as cur:
            cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + TWENTY_FOUR_HOURS, params)
            plan = [r[0] for r in cur.fetchall()]
        print("  query plan:")
        for line in plan[:6]:
            print(f"    {line}")

        timings = time_query(conn, TWENTY_FOUR_HOURS, params)
        with conn.cursor() as cur:
            cur.execute(TWENTY_FOUR_HOURS, params)
            returned = len(cur.fetchall())
        print(f"\n  24-hour single-tag query: {returned:,} rows")
        print(f"    runs   : {', '.join(f'{t*1000:.0f}ms' for t in timings)}")
        print(f"    median : {statistics.median(timings)*1000:.0f} ms")
        print(f"    max    : {max(timings)*1000:.0f} ms")
        # A query that returned nothing proves nothing, however fast it was.
        expected = int(24 * 3600 / INTERVAL_S)
        substantive = returned > expected * 0.9
        criterion = max(timings) < 5.0 and substantive
        print(f"    expected ~{expected:,} rows in 24 h at {INTERVAL_S}s: "
              f"{'ok' if substantive else 'TOO FEW — test window is wrong'}")
        print(f"    CRITERION (< 5 s over a full window): "
              f"{'PASS' if criterion else 'FAIL'}\n")

        # --- idempotency ---------------------------------------------------
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sample WHERE tag_id = %s", (tag_ids[0],))
            before = cur.fetchone()[0]
            cur.execute(
                "SELECT source_ts, server_ts, value, quality FROM sample "
                "WHERE tag_id = %s ORDER BY source_ts LIMIT 1000", (tag_ids[0],))
            rows = cur.fetchall()
            cur.executemany(
                "INSERT INTO sample (tag_id, source_ts, server_ts, value, quality) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (tag_id, source_ts) DO NOTHING",
                [(tag_ids[0], *r) for r in rows])
            conn.commit()
            cur.execute("SELECT count(*) FROM sample WHERE tag_id = %s", (tag_ids[0],))
            after = cur.fetchone()[0]
        print(f"  re-inserted 1,000 existing rows: {before:,} -> {after:,}")
        idempotent = before == after
        print(f"    CRITERION (no duplicates): {'PASS' if idempotent else 'FAIL'}\n")

        return 0 if (criterion and idempotent) else 1


if __name__ == "__main__":
    raise SystemExit(main())
