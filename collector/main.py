"""Run the collector: python -m collector"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time

import psycopg

from collector import events as ev
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


async def _register_instance(dsn: str, instance: str) -> None:
    """Announce this collector, for reporting only.

    Nothing consults the registry before writing. Both collectors write freely
    because the (tag_id, source_ts) primary key makes a duplicate impossible —
    so redundancy here needs no consensus, no fencing and no split-brain
    handling, and a wrong leader indication costs a label rather than data.
    """
    import socket
    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=5) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO collector_instance (instance, host, pid,"
                " started_at, last_seen) VALUES (%s,%s,%s,now(),now())"
                " ON CONFLICT (instance) DO UPDATE SET host=EXCLUDED.host,"
                " pid=EXCLUDED.pid, started_at=now(), last_seen=now(),"
                # A restarted instance starts its counts again; leaving the
                # previous process's figures showed a collector that had not
                # yet acquired anything as having acquired thousands.
                " samples=0, link_up=false, buffer_depth=0",
                (instance, socket.gethostname(), os.getpid()))
        await conn.commit()


async def _heartbeat(dsn: str, instance: str, pipeline, interval_s: float = 5.0):
    while True:
        await asyncio.sleep(interval_s)
        try:
            async with await psycopg.AsyncConnection.connect(
                    dsn, connect_timeout=5) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE collector_instance SET last_seen=now(),"
                        " samples=%s, link_up=%s, buffer_depth=%s"
                        " WHERE instance=%s",
                        (pipeline.received, pipeline.link_up,
                         pipeline.buffer.depth, instance))
                await conn.commit()
        except psycopg.Error:
            # The registry is for reporting. Losing it must not disturb
            # acquisition, which is the thing that actually matters.
            pass


async def _health_tag_ids(dsn: str) -> dict[str, int]:
    """Health tag ids, read directly. The sink may be OMF, which has no way to
    ask a question."""
    async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=5) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, id FROM tag WHERE source_system='collector'")
            return {name: tag_id for name, tag_id in await cur.fetchall()}


async def run(config: CollectorConfig) -> None:
    stream = EventStream()
    buffer = Buffer(config.resolved_buffer_path(), config.buffer_max_rows,
                    config.buffer_warn_fraction)

    # Output format is configuration. Setting CRPMS_OMF_URL makes OMF the
    # collector's only output for data; pointing it at a real PI Web API OMF
    # endpoint is that URL plus credentials and no code change (Stage 9).
    omf_url = os.environ.get("CRPMS_OMF_URL")
    if omf_url:
        from collector import omf as omf_mod
        from collector.omf_sink import OmfSink
        sink = OmfSink(omf_mod.OmfConfig(
            url=omf_url,
            producer_token=os.environ.get("CRPMS_OMF_TOKEN", "orianode-crpms"),
            username=os.environ.get("CRPMS_OMF_USER"),
            password=os.environ.get("CRPMS_OMF_PASSWORD"),
            verify_tls=os.environ.get("CRPMS_OMF_VERIFY_TLS", "1") != "0",
        ), config.dsn)
        log.info("output format: OMF -> %s", omf_url)
    else:
        sink = ArchiveSink(config.dsn)
        log.info("output format: direct SQL")

    # Tag configuration must be readable at least once to know what to
    # subscribe to, and the run must be registered so every sample this
    # process archives can be numbered within it. If the archive is down at
    # startup there is nothing to acquire against, so this is the one place we
    # wait for it.
    tags: list[dict] = []
    run_id: int | None = None
    while not tags or run_id is None:
        try:
            await sink.connect()
            tags = await sink.tags()
            run_id = await sink.register_run(config.instance)
        except (SinkUnavailable, psycopg.Error) as exc:
            log.warning("waiting for the archive to read tag configuration: %s", exc)
            await asyncio.sleep(config.reconnect_delay_s)
    compress = {t["id"]: t for t in tags if t.get("compress")}
    log.info("tag configuration: %d subscribable tags, %d compressed; "
             "collector run %d", len(tags), len(compress), run_id)

    pipeline = Pipeline(config, sink, buffer, stream, run_id=run_id,
                        compress_tags=compress)
    pipeline.link_up = True
    pipeline.last_forward_monotonic = time.monotonic()
    if buffer.depth:
        log.info("%d samples were left buffered by a previous run", buffer.depth)
        await pipeline.drain()

    def publish_gap(subscription_id: int, sequence_number: int, missed: int) -> None:
        stream.emit(ev.GAP_DETECTED, subscription=subscription_id,
                    sequence_number=sequence_number, missed=missed)

    def refused(tag: str, why: str) -> None:
        stream.emit(ev.VALUE_REFUSED, tag=tag, reason=why)

    session = ReadOnlySession(config.endpoint, pipeline.on_sample,
                              on_publish_gap=publish_gap, on_drop=refused)

    health_ids = await _health_tag_ids(config.dsn)
    publisher = HealthPublisher(pipeline, health_ids, config.health_interval_s,
                                instance=config.instance, session=session)
    try:
        await _register_instance(config.dsn, config.instance)
    except psycopg.Error as exc:
        log.warning("could not register instance %s: %s", config.instance, exc)

    log.info("collector instance: %s", config.instance)
    from collector.event_server import serve as serve_events
    stop = asyncio.Event()
    events_server = asyncio.create_task(serve_events(
        stream, pipeline, config.instance, config.event_host, config.event_port,
        session=session, stop=stop))
    tasks = [
        asyncio.create_task(pipeline.run_forwarder()),
        asyncio.create_task(_health_loop(publisher, pipeline)),
        asyncio.create_task(_heartbeat(config.dsn, config.instance, pipeline)),
        asyncio.create_task(_opcua_supervisor(session, tags,
                                              config.reconnect_delay_s)),
    ]

    # Stop cleanly on SIGTERM or SIGINT: whatever is still in memory -- the
    # queue between the subscription and the forwarder, and any compressor's
    # open segment -- goes to the durable buffer and is drained by the next
    # start. Without this, `kill <pid>` lost it. (A SIGKILL cannot be caught;
    # what it costs is bounded by the batch linger and, for compressed tags, by
    # max_time. See pipeline.py.)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    stopper = asyncio.create_task(stop.wait())
    # Only a stop request or a failed acquisition task ends the collector. The
    # event stream ending -- its port taken, say -- does not.
    done, _ = await asyncio.wait([stopper, *tasks],
                                 return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        if (task is not stopper and not task.cancelled()
                and task.exception() is not None):
            log.error("collector task failed: %r", task.exception())
    log.info("stopping")
    stop.set()
    try:
        await asyncio.wait_for(events_server, timeout=5)
    except (asyncio.TimeoutError, Exception):
        events_server.cancel()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await session.disconnect()
    flushed = await pipeline.flush_to_buffer()
    log.info("stopped; %d in-flight samples flushed to the buffer, buffer depth "
             "%d", flushed, buffer.depth)
    await sink.close()
    buffer.close()


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

    asyncio.run(run(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
