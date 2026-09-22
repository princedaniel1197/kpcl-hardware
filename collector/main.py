"""Run the collector: python -m collector"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from collector.buffer import Buffer
from collector.config import CollectorConfig
from collector.events import EventStream
from collector.health import HealthPublisher
from collector.pipeline import Pipeline
from collector.session import ReadOnlySession
from collector.sink import ArchiveSink, SinkUnavailable

log = logging.getLogger("collector")


async def _health_loop(publisher: HealthPublisher, pipeline: Pipeline) -> None:
    while True:
        await asyncio.sleep(publisher.interval_s)
        for sample in publisher.sample_now():
            pipeline.on_sample(sample)


async def _opcua_supervisor(session: ReadOnlySession, tags: list[dict],
                            delay_s: float) -> None:
    """Keep the source session up. A source outage is not an archive outage and
    must be recovered from independently."""
    while True:
        try:
            await session.connect(tags)
            log.info("subscribed to %d tags on %s",
                     len(session.subscribed), session.endpoint)
            heartbeat = asyncio.create_task(session.max_time_read())
            while True:
                await asyncio.sleep(5)
                if session._client is None:
                    break
                try:
                    await session._client.check_connection()
                except Exception as exc:
                    log.warning("source connection lost: %s", exc)
                    break
            heartbeat.cancel()
        except Exception as exc:
            log.warning("cannot reach source %s: %s", session.endpoint, exc)
        await session.disconnect()
        await asyncio.sleep(delay_s)


async def run(config: CollectorConfig) -> None:
    stream = EventStream()
    buffer = Buffer(config.buffer_path, config.buffer_max_rows,
                    config.buffer_warn_fraction)
    sink = ArchiveSink(config.dsn)

    # Tag configuration must be readable at least once to know what to
    # subscribe to. If the archive is down at startup there is nothing to
    # acquire against, so this is the one place we wait for it.
    tags: list[dict] = []
    while not tags:
        try:
            await sink.connect()
            tags = await sink.tags()
        except SinkUnavailable as exc:
            log.warning("waiting for the archive to read tag configuration: %s", exc)
            await asyncio.sleep(config.reconnect_delay_s)
    log.info("tag configuration: %d subscribable tags", len(tags))

    pipeline = Pipeline(config, sink, buffer, stream)
    pipeline.link_up = True
    pipeline.last_forward_monotonic = time.monotonic()
    if buffer.depth:
        log.info("%d samples were left buffered by a previous run", buffer.depth)
        await pipeline.drain()

    health_ids = {}
    async with sink._conn.cursor() as cur:  # noqa: SLF001 - read-only lookup
        await cur.execute("SELECT name, id FROM tag WHERE source_system='collector'")
        health_ids = {name: tag_id for name, tag_id in await cur.fetchall()}
    publisher = HealthPublisher(pipeline, health_ids, config.health_interval_s)

    session = ReadOnlySession(config.endpoint, pipeline.on_sample)

    await asyncio.gather(
        pipeline.run_forwarder(),
        _health_loop(publisher, pipeline),
        _opcua_supervisor(session, tags, config.reconnect_delay_s),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="collector")
    ap.add_argument("--endpoint")
    ap.add_argument("--dsn")
    ap.add_argument("--buffer")
    ap.add_argument("--instance")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)-20s %(message)s",
        stream=sys.stdout)
    logging.Formatter.converter = time.gmtime
    logging.getLogger("asyncua").setLevel(logging.ERROR)

    config = CollectorConfig.from_env()
    overrides = {k: v for k, v in
                 {"endpoint": args.endpoint, "dsn": args.dsn,
                  "buffer_path": args.buffer, "instance": args.instance}.items()
                 if v}
    if overrides:
        config = CollectorConfig(**{**config.__dict__, **overrides})

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        log.info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
