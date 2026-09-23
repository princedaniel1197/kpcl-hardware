"""Forwarding samples to the archive.

The insert is ON CONFLICT (tag_id, source_ts) DO NOTHING. That is the whole of
the de-duplication strategy: replay cannot duplicate because the schema forbids
it, not because this code remembers what it already sent (§382, §648).

Original timestamps and quality are written exactly as received. Nothing here
re-stamps, rounds, defaults or interpolates anything. A missing server_ts is
written as NULL. The collector run and the per-tag sequence number go with every
row, so a sample the collector meant to write and the archive lacks can be
found by query (`sample_seq_gap`).
"""

from __future__ import annotations

import os
import socket

import psycopg

from collector.model import Sample

INSERT = """
INSERT INTO sample (tag_id, source_ts, server_ts, value, quality,
                    collector_run, seq)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (tag_id, source_ts) DO NOTHING
"""

INSERT_LOSS = """
INSERT INTO collector_loss (collector_run, tag_id, first_source_ts,
                            last_source_ts, first_seq, last_seq, samples,
                            reason, detected_at)
VALUES (%(run)s, %(tag_id)s, %(first_source_ts)s, %(last_source_ts)s,
        %(first_seq)s, %(last_seq)s, %(samples)s, %(reason)s, %(detected_at)s)
ON CONFLICT (tag_id, first_source_ts) DO NOTHING
"""


class SinkUnavailable(RuntimeError):
    """The archive could not be reached or the write failed. The caller buffers
    and keeps acquiring; it does not stop and it does not discard."""


class ArchiveSink:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._conn.closed

    async def connect(self) -> None:
        if self.connected:
            return
        try:
            self._conn = await psycopg.AsyncConnection.connect(
                self.dsn, connect_timeout=5)
        except psycopg.Error as exc:
            self._conn = None
            raise SinkUnavailable(str(exc).strip()) from exc

    async def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()
        self._conn = None

    async def write(self, samples: list[Sample]) -> int:
        """Write a batch. Raises SinkUnavailable on any failure, having left
        nothing half-committed."""
        if not samples:
            return 0
        if not self.connected:
            await self.connect()
        assert self._conn is not None
        try:
            async with self._conn.cursor() as cur:
                await cur.executemany(
                    INSERT,
                    [(s.tag_id, s.source_ts, s.server_ts, s.value, s.quality,
                      s.run, s.seq)
                     for s in samples])
            await self._conn.commit()
            return len(samples)
        except psycopg.Error as exc:
            try:
                await self._conn.rollback()
            except psycopg.Error:
                pass
            # A broken connection must not be reused; the next attempt
            # reconnects. Otherwise recovery silently never happens.
            await self.close()
            raise SinkUnavailable(str(exc).strip()) from exc

    async def write_losses(self, losses: list[dict]) -> None:
        """Record buffer-overflow losses in `collector_loss`. Idempotent on
        (tag_id, first_source_ts), so re-sending after a crash is harmless."""
        if not losses:
            return
        if not self.connected:
            await self.connect()
        assert self._conn is not None
        try:
            async with self._conn.cursor() as cur:
                await cur.executemany(INSERT_LOSS, losses)
            await self._conn.commit()
        except psycopg.Error as exc:
            try:
                await self._conn.rollback()
            except psycopg.Error:
                pass
            await self.close()
            raise SinkUnavailable(str(exc).strip()) from exc

    async def register_run(self, instance: str) -> int:
        """Open a collector run: every sample this process archives is
        numbered within it."""
        if not self.connected:
            await self.connect()
        assert self._conn is not None
        async with self._conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO collector_run (instance, host, pid)"
                " VALUES (%s, %s, %s) RETURNING id",
                (instance, socket.gethostname(), os.getpid()))
            run_id = (await cur.fetchone())[0]
        await self._conn.commit()
        return run_id

    async def tags(self) -> list[dict]:
        """Tag configuration, read from the archive.

        Scan rates and deadbands are configuration, not code: adding a tag or
        retuning one is a row change, not an edit here (§317, §341).
        """
        if not self.connected:
            await self.connect()
        assert self._conn is not None
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT id, name, scan_rate_ms, exc_dev, comp_dev, max_time_ms, "
                "source_path, compress "
                "FROM tag WHERE source_system = %s ORDER BY name", ("opcua",))
            return [
                {"id": r[0], "name": r[1], "scan_rate_ms": r[2],
                 "exc_dev": r[3], "comp_dev": r[4], "max_time_ms": r[5],
                 "source_path": r[6], "compress": r[7]}
                for r in await cur.fetchall()
            ]
