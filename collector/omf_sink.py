"""An OMF sink with the same interface as the direct archive sink.

Selected by setting CRPMS_OMF_URL. The collector then emits OMF and nothing
else, which is what makes "the collector's only output format is OMF" true
rather than aspirational. Everything upstream — buffering, ordered drain,
idempotency — is unchanged, because the sink is the only thing that differs.
"""

from __future__ import annotations

import logging

import psycopg

from collector import omf
from collector.model import Sample
from collector.sink import SinkUnavailable

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
                " description, engineering_unit, source_path FROM tag"
                " WHERE source_system = ANY(%s) ORDER BY name", (list(systems),))
            rows = await cur.fetchall()
        return [{"id": r[0], "name": r[1], "scan_rate_ms": r[2],
                 "exc_dev": r[3], "comp_dev": r[4], "max_time_ms": r[5],
                 "description": r[6], "engineering_unit": r[7],
                 "source_path": r[8]} for r in rows]

    async def tags(self) -> list[dict]:
        """Tags to SUBSCRIBE to: the ones coming from the source."""
        return await self._tags_where(("opcua",))

    async def output_tags(self) -> list[dict]:
        """Tags this collector will SEND: source tags plus its own health."""
        return await self._tags_where(("opcua", "collector"))
