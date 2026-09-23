"""An OMF sink with the same interface as the direct archive sink.

Selected by setting CRPMS_OMF_URL. The collector then emits OMF for every
sample, which is what makes "the collector's only output format is OMF" true
for data rather than aspirational. Everything upstream — buffering, ordered
drain, idempotency — is unchanged, because the sink is the only thing that
differs.

WHAT OMF DOES NOT CARRY, stated rather than implied. The per-tag sequence
number and the collector run travel with every row on the SQL path; the OMF
sample type has no property for them, and PI has no field for them either, so
on this path they are not archived and `sample_seq_gap` has nothing to check.
Three things that are not data still use SQL against the configuration
database, exactly as reading tag configuration always has: registering the
collector run, recording buffer-overflow losses in `collector_loss`, and
reading which tags to subscribe to.
"""

from __future__ import annotations

import logging
import os
import socket

import psycopg

from collector import omf
from collector.model import Sample
from collector.sink import INSERT_LOSS, SinkUnavailable

log = logging.getLogger("collector.omf_sink")


class OmfSink:
    def __init__(self, config: omf.OmfConfig, dsn: str) -> None:
        self.client = omf.OmfClient(config)
        # Tag configuration is still read from the archive: it is configuration,
        # not data flow, and OMF has no way to ask for it.
        self.dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None
        self._registered = False

    @property
    def connected(self) -> bool:
        return self._registered

    async def connect(self) -> None:
        """Register the types and containers, which is what an OMF producer
        must do before sending any data.

        Containers are created for EVERY tag this collector will send, not only
        the ones it subscribes to. The collector publishes its own health as
        tags in the same archive (§462), and those need containers too —
        without them the health stream is rejected by the endpoint, silently in
        the sense that the plant data keeps flowing and only the monitoring
        disappears. Measured before the fix: 60 rejected messages in 30 s, all
        of them health.
        """
        tags = await self.output_tags()
        try:
            self.client.post("type", "create", omf.type_message())
            self.client.post("container", "create", omf.container_message(tags))
        except omf.OmfError as exc:
            raise SinkUnavailable(str(exc)) from exc
        self._registered = True
        log.info("registered %d containers with %s", len(tags),
                 self.client.config.url)

    async def close(self) -> None:
        self._registered = False
        if self._conn is not None and not self._conn.closed:
            await self._conn.close()
        self._conn = None

    async def write(self, samples: list[Sample]) -> int:
        if not samples:
            return 0
        if not self._registered:
            await self.connect()
        try:
            self.client.post("data", "create", omf.data_message(samples))
        except omf.OmfError as exc:
            # A rejected data message does NOT mean the types and containers
            # are gone. Re-registering on every rejection turned one bad batch
            # into a re-registration storm: 120 type messages and 1680
            # container messages in 30 seconds.
            raise SinkUnavailable(str(exc)) from exc
        return len(samples)

    async def write_losses(self, losses: list[dict]) -> None:
        if not losses:
            return
        conn = await self._db()
        try:
            async with conn.cursor() as cur:
                await cur.executemany(INSERT_LOSS, losses)
            await conn.commit()
        except psycopg.Error as exc:
            await self.close()
            raise SinkUnavailable(str(exc)) from exc

    async def register_run(self, instance: str) -> int:
        conn = await self._db()
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO collector_run (instance, host, pid)"
                " VALUES (%s, %s, %s) RETURNING id",
                (instance, socket.gethostname(), os.getpid()))
            run_id = (await cur.fetchone())[0]
        await conn.commit()
        return run_id

    async def _db(self) -> psycopg.AsyncConnection:
        if self._conn is None or self._conn.closed:
            try:
                self._conn = await psycopg.AsyncConnection.connect(
                    self.dsn, connect_timeout=5)
            except psycopg.Error as exc:
                raise SinkUnavailable(str(exc)) from exc
        return self._conn

    async def _tags_where(self, systems: tuple[str, ...]) -> list[dict]:
        conn = await self._db()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, name, scan_rate_ms, exc_dev, comp_dev, max_time_ms,"
                " description, engineering_unit, source_path, compress FROM tag"
                " WHERE source_system = ANY(%s) ORDER BY name", (list(systems),))
            rows = await cur.fetchall()
        return [{"id": r[0], "name": r[1], "scan_rate_ms": r[2],
                 "exc_dev": r[3], "comp_dev": r[4], "max_time_ms": r[5],
                 "description": r[6], "engineering_unit": r[7],
                 "source_path": r[8], "compress": r[9]} for r in rows]

    async def tags(self) -> list[dict]:
        """Tags to SUBSCRIBE to: the ones coming from the source."""
        return await self._tags_where(("opcua",))

    async def output_tags(self) -> list[dict]:
        """Tags this collector will SEND: source tags plus its own health."""
        return await self._tags_where(("opcua", "collector"))
