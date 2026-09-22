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
import logging
import os

import psycopg

from scraper.parse import PageReading

DEFAULT_DSN = "postgresql://crpms:crpms@localhost:5432/crpms"

log = logging.getLogger("scraper.store")


def dsn() -> str:
    return os.environ.get("CRPMS_DSN", DEFAULT_DSN)


class Archive:
    """A database connection that survives the database going away.

    The recorder originally held one connection for its whole life. When the
    database was stopped, that connection died and never came back; worse, the
    error handler tried to log the failure through the same dead connection, so
    the second exception escaped the poll loop and killed the process. It
    appeared to survive only because launchd restarted it.

    So: the connection is acquired on demand, a broken one is discarded rather
    than reused, and recording an attempt is best-effort -- failing to write the
    log of a failure must never be what takes the recorder down.
    """

    def __init__(self, dsn_: str | None = None) -> None:
        self.dsn = dsn_ or dsn()
        self._conn: psycopg.AsyncConnection | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    async def connection(self) -> psycopg.AsyncConnection:
        if not self.connected:
            self._conn = await psycopg.AsyncConnection.connect(
                self.dsn, connect_timeout=10)
        assert self._conn is not None
        return self._conn

    async def discard(self) -> None:
        """Drop a connection we can no longer trust."""
        if self._conn is not None and not self._conn.closed:
            try:
                await self._conn.close()
            except psycopg.Error:
                pass
        self._conn = None

    async def close(self) -> None:
        await self.discard()


async def write_reading(archive: "Archive", page: PageReading) -> int:
    """Write one page reading. Returns the number of generation rows inserted
    (rows already present are not counted, and are not an error).

    Raises psycopg.Error on failure, having discarded the connection so the
    next attempt reconnects rather than reusing a dead one.
    """
    conn = await archive.connection()
    try:
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
    except psycopg.Error:
        await archive.discard()
        raise
    return max(inserted, 0)


async def log_attempt(
    archive: "Archive",
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
    dead scraper must not look the same.

    Best-effort by design: this is most often called BECAUSE the database just
    failed, so it must not raise. Failing to record a failure is bad; crashing
    while trying to record it is worse.
    """
    try:
        conn = await archive.connection()
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
    except psycopg.Error as exc:
        await archive.discard()
        log.warning("could not record poll attempt (%s): %s", outcome, exc)


async def daily_counts(archive: "Archive", days: int = 1) -> list[tuple]:
    """Per-day row counts, newest first — the liveness figure."""
    conn = await archive.connection()
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
