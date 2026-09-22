"""Writing SLDC readings to TimescaleDB.

Idempotency is structural. Both inserts are ON CONFLICT DO NOTHING against a
primary key that includes source_ts, so re-recording a page we already hold
cannot duplicate a row. That matters here more than it might look: the page
carries a one-minute timestamp and we poll every sixty seconds, so the same
reading is routinely seen more than once. The schema absorbs that. No
de-duplication logic exists in the poller, and none should be added.
"""

from __future__ import annotations

import datetime as dt
import os

import psycopg

from scraper.parse import PageReading

DEFAULT_DSN = "postgresql://crpms:crpms@localhost:5432/crpms"


def dsn() -> str:
    return os.environ.get("CRPMS_DSN", DEFAULT_DSN)


async def write_reading(conn: psycopg.AsyncConnection, page: PageReading) -> int:
    """Write one page reading. Returns the number of generation rows inserted
    (rows already present are not counted, and are not an error)."""
    async with conn.cursor() as cur:
        await cur.executemany(
            """
            INSERT INTO sldc_generation
                (station, source_ts, server_ts, generation_mw, quality, reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (station, source_ts) DO NOTHING
            """,
            [
                (s.station, page.source_ts, page.server_ts,
                 s.value, s.quality, s.reason)
                for s in page.stations
            ],
        )
        inserted = cur.rowcount

        await cur.execute(
            """
            INSERT INTO sldc_system
                (source_ts, server_ts, frequency_hz, frequency_quality,
                 state_gen_mw, total_gen_mw, raw_page_timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_ts) DO NOTHING
            """,
            (page.source_ts, page.server_ts,
             page.frequency.value, page.frequency.quality,
             page.state_gen.value, page.total_gen.value,
             page.raw_page_timestamp),
        )
    await conn.commit()
    return max(inserted, 0)


async def log_attempt(
    conn: psycopg.AsyncConnection,
    *,
    attempt_ts: dt.datetime,
    outcome: str,
    http_status: int | None = None,
    rows_written: int = 0,
    page_source_ts: dt.datetime | None = None,
    duration_ms: int | None = None,
    detail: str | None = None,
) -> None:
    """Record that a poll happened, whatever came of it. A silent scraper and a
    dead scraper must not look the same."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO sldc_poll_log
                (attempt_ts, outcome, http_status, rows_written,
                 page_source_ts, duration_ms, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (attempt_ts) DO NOTHING
            """,
            (attempt_ts, outcome, http_status, rows_written,
             page_source_ts, duration_ms, detail),
        )
    await conn.commit()


async def daily_counts(conn: psycopg.AsyncConnection, days: int = 1) -> list[tuple]:
    """Per-day row counts, newest first — the liveness figure."""
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT ist_date, rows_written, good_rows, distinct_readings,
                   first_reading, last_reading
            FROM sldc_daily_rows
            LIMIT %s
            """,
            (days,),
        )
        return await cur.fetchall()
