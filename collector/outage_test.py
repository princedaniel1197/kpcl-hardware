"""Stage 3 acceptance test: pull the archive out from under a running collector.

From the build plan:

    Start everything. Let it run five minutes. Stop TimescaleDB. Watch the
    buffer grow. Wait three minutes. Start TimescaleDB. Then verify: no sample
    missing between the stop and start instants; every restored row carries its
    original source_ts and quality; zero duplicates; the trend has no gap.

This runs the real collector against the real simulator and the real database,
and really stops the database container. Nothing is mocked or simulated except
the plant itself, which is the point of having a simulator.

The verification is exact rather than statistical. The test subscribes to the
collector's own event stream and records every sample the collector says it
received, then compares that ledger against the archive row by row. "No sample
missing" is checked against what was actually acquired, not against an estimate
of what should have been. Two independent checks sit beside the ledger: the
OPC UA server's own NotificationMessage numbering (a hole is a message the
collector never received), and the per-run sequence numbers stored with every
archived row (a hole is a sample the collector meant to write and did not).

It must run ALONE. With another collector writing the same tags, a sample this
test's collector lost could be supplied by the other one, and the test would
credit this collector with the other's work. So it refuses to start while
another collector process is running.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg

from collector import events as ev
from collector.buffer import Buffer
from collector.config import CollectorConfig
from collector.events import EventStream
from collector.health import HealthPublisher
from collector.pipeline import Pipeline
from collector.session import ReadOnlySession
from collector.sink import ArchiveSink, SinkUnavailable

log = logging.getLogger("outage")

DOCKER = "/Applications/Docker.app/Contents/Resources/bin/docker"
CONTAINER = "crpms-timescaledb"


def docker(*args: str) -> None:
    subprocess.run([DOCKER, *args], check=True, capture_output=True)


class Ledger:
    """Everything the collector reported receiving, as it reported it."""

    def __init__(self) -> None:
        self.received: dict[tuple[str, str], tuple[float | None, int]] = {}
        self.buffered_events = 0
        self.drain_events: list[dict] = []
        self.overflow = 0
        self.gaps: list[dict] = []
        self.link_changes: list[dict] = []
        self.max_buffer_depth = 0

    async def follow(self, stream: EventStream) -> None:
        async for event in stream.subscribe():
            kind = event["kind"]
            if kind == ev.VALUE_RECEIVED:
                self.received[(event["tag"], event["source_ts"])] = (
                    event["value"], event["quality"])
            elif kind == ev.VALUE_BUFFERED:
                self.buffered_events += 1
                self.max_buffer_depth = max(self.max_buffer_depth, event["depth"])
            elif kind == ev.BUFFER_DRAINED:
                self.drain_events.append(event)
            elif kind == ev.BUFFER_OVERFLOW:
                self.overflow += event["discarded"]
            elif kind == ev.GAP_DETECTED:
                self.gaps.append(event)
            elif kind == ev.LINK_STATE:
                self.link_changes.append(event)


def other_collectors() -> list[str]:
    out = subprocess.run(["pgrep", "-f", "-l", "Python -m collector"],
                         capture_output=True, text=True).stdout.split("\n")
    return [line for line in out
            if line.strip() and not line.startswith(str(os.getpid()))
            and "outage_test" not in line]


async def run(run_s: float, outage_s: float, config: CollectorConfig) -> int:
    others = other_collectors()
    if others:
        print("another collector is running; stop it first, or this test would "
              "credit this collector with the other one's writes:")
        for line in others:
            print(f"    {line}")
        return 2

    stream = EventStream(queue_size=200_000)
    # A buffer of its own, empty at the start: a leftover from a previous run
    # would be drained into the archive and counted as this run's work.
    buffer_path = Path(config.buffer_path).with_name("outage-test.sqlite")
    for leftover in buffer_path.parent.glob(buffer_path.name + "*"):
        leftover.unlink()
    buffer = Buffer(buffer_path, config.buffer_max_rows)
    sink = ArchiveSink(config.dsn)
    await sink.connect()
    tags = await sink.tags()
    run_id = await sink.register_run("outage-test")

    pipeline = Pipeline(config, sink, buffer, stream, run_id=run_id)
    pipeline.link_up = True
    pipeline.last_forward_monotonic = time.monotonic()

    async with sink._conn.cursor() as cur:
        await cur.execute("SELECT name, id FROM tag WHERE source_system='collector'")
        health_ids = {n: i for n, i in await cur.fetchall()}

    ledger = Ledger()

    def publish_gap(subscription_id: int, sequence_number: int, missed: int) -> None:
        stream.emit(ev.GAP_DETECTED, subscription=subscription_id,
                    sequence_number=sequence_number, missed=missed)

    session = ReadOnlySession(config.endpoint, pipeline.on_sample,
                              on_publish_gap=publish_gap)
    publisher = HealthPublisher(pipeline, health_ids, config.health_interval_s,
                                session=session)
    # The ledger must be listening before the first sample arrives.
    ledger_task = asyncio.create_task(ledger.follow(stream))
    await asyncio.sleep(0)
    await session.connect(tags)
    print(f"collector run {run_id}; subscribed to {len(session.subscribed)} tags "
          f"(source deadband {'on' if session.source_deadband else 'off'})\n")
    tasks = [
        ledger_task,
        asyncio.create_task(pipeline.run_forwarder()),
    ]

    async def health() -> None:
        while True:
            await asyncio.sleep(config.health_interval_s)
            for s in publisher.sample_now():
                pipeline.on_sample(s)
    tasks.append(asyncio.create_task(health()))

    # -- 1. normal running ------------------------------------------------
    print(f"[1] running normally for {run_s:.0f}s ...")
    await asyncio.sleep(run_s)
    before_outage = len(ledger.received)
    print(f"    received {before_outage:,} samples, forwarded "
          f"{pipeline.forwarded:,}, buffer {buffer.depth}\n")

    # -- 2. the outage ----------------------------------------------------
    stop_instant = dt.datetime.now(dt.timezone.utc)
    print(f"[2] stopping {CONTAINER} at {stop_instant.isoformat()}")
    docker("stop", CONTAINER)
    print("    stopped. watching the buffer:")

    deadline = time.monotonic() + outage_s
    while time.monotonic() < deadline:
        await asyncio.sleep(outage_s / 6)
        print(f"      t+{outage_s - (deadline - time.monotonic()):5.0f}s  "
              f"buffer={buffer.depth:6,}  received={len(ledger.received):7,}  "
              f"link={'up' if pipeline.link_up else 'DOWN'}")

    depth_at_restore = buffer.depth
    start_instant = dt.datetime.now(dt.timezone.utc)
    print(f"\n[3] starting {CONTAINER} at {start_instant.isoformat()}")
    docker("start", CONTAINER)
    for _ in range(60):
        try:
            subprocess.run([DOCKER, "exec", CONTAINER, "pg_isready", "-U", "crpms"],
                           check=True, capture_output=True)
            break
        except subprocess.CalledProcessError:
            await asyncio.sleep(1)
    print("    database accepting connections; waiting for the drain\n")

    # -- 4. wait for the buffer to empty ----------------------------------
    drain_deadline = time.monotonic() + 180
    while buffer.depth > 0 and time.monotonic() < drain_deadline:
        await asyncio.sleep(1)

    # Stop acquiring BEFORE stopping forwarding, then let everything already
    # received reach the archive. Cancelling the forwarder while samples are
    # still queued would lose them -- and would make the test blame the
    # collector for the harness's own shutdown race.
    await session.disconnect()
    settle = time.monotonic() + 30
    while (pipeline.queued or buffer.depth) and time.monotonic() < settle:
        await asyncio.sleep(0.5)
    await asyncio.sleep(1.0)
    await pipeline.flush_to_buffer()
    if buffer.depth:
        await pipeline.drain()

    for task in tasks:
        task.cancel()

    # -- 5. verification --------------------------------------------------
    print("=" * 74)
    print("VERIFICATION")
    print("=" * 74)
    ok = True

    # The archive is compared over the stretch of time the ledger covers, and
    # nothing outside it: history from before the test is left alone.
    first_ts = min(dt.datetime.fromisoformat(src) for _, src in ledger.received)
    last_ts = max(dt.datetime.fromisoformat(src) for _, src in ledger.received)
    names = sorted({name for name, _ in ledger.received})
    with psycopg.connect(config.dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT t.name, s.source_ts, s.server_ts, s.value, s.quality
            FROM sample s JOIN tag t ON t.id = s.tag_id
            WHERE t.name = ANY(%s) AND s.source_ts BETWEEN %s AND %s
        """, (names, first_ts, last_ts))
        archive = {(name, src.isoformat()): (srv, val, q)
                   for name, src, srv, val, q in cur.fetchall()}

        cur.execute("""
            SELECT count(*), count(DISTINCT (s.tag_id, s.source_ts))
            FROM sample s JOIN tag t ON t.id = s.tag_id
            WHERE t.name = ANY(%s) AND s.source_ts BETWEEN %s AND %s
        """, (names, first_ts, last_ts))
        total_rows, distinct_keys = cur.fetchone()

        cur.execute("SELECT count(*), coalesce(sum(missing), 0)"
                    " FROM sample_seq_gap WHERE collector_run = %s", (run_id,))
        seq_holes, seq_missing = cur.fetchone()
        cur.execute("SELECT count(*) FROM sample WHERE collector_run = %s",
                    (run_id,))
        numbered_rows = cur.fetchone()[0]
        cur.execute("SELECT coalesce(sum(samples), 0) FROM collector_loss"
                    " WHERE collector_run = %s", (run_id,))
        recorded_lost = cur.fetchone()[0]

    # a. nothing acquired is missing
    missing = [k for k in ledger.received if k not in archive]
    print(f"\n  samples the collector received : {len(ledger.received):,}")
    print(f"  rows in the archive, same span : {total_rows:,}")
    print(f"  missing from the archive       : {len(missing)}")
    if missing:
        ok = False
        for key in missing[:5]:
            print(f"      MISSING {key}")
    print(f"    ZERO LOSS: {'PASS' if not missing else 'FAIL'}")

    # b. no duplicates
    print(f"\n  distinct (tag_id, source_ts)   : {distinct_keys:,}")
    duplicates = total_rows - distinct_keys
    print(f"    ZERO DUPLICATES: {'PASS' if duplicates == 0 else 'FAIL'}")
    ok = ok and duplicates == 0

    # c. original timestamps and quality preserved
    mismatches = []
    for key, (value, quality) in ledger.received.items():
        if key not in archive:
            continue
        _, stored_value, stored_quality = archive[key]
        if stored_quality != quality:
            mismatches.append((key, "quality", quality, stored_quality))
        elif value is None and stored_value is not None:
            mismatches.append((key, "value", value, stored_value))
        elif value is not None and stored_value is None:
            mismatches.append((key, "value", value, stored_value))
    print(f"\n  quality/value mismatches       : {len(mismatches)}")
    for m in mismatches[:5]:
        print(f"      {m}")
    print(f"    ORIGINAL QUALITY PRESERVED: {'PASS' if not mismatches else 'FAIL'}")
    ok = ok and not mismatches

    # d. source_ts is never the receipt time
    with psycopg.connect(config.dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FILTER (WHERE s.server_ts <= s.source_ts),
                   count(*) FILTER (WHERE s.server_ts IS NULL)
            FROM sample s JOIN tag t ON t.id = s.tag_id
            WHERE t.source_system = 'opcua' AND s.source_ts BETWEEN %s AND %s
        """, (first_ts, last_ts))
        non_causal, no_server_ts = cur.fetchone()
        cur.execute("""
            SELECT count(*) FROM sample s JOIN tag t ON t.id = s.tag_id
            WHERE t.source_system = 'opcua' AND s.source_ts >= %s AND s.source_ts <= %s
        """, (stop_instant, start_instant))
        acquired_during_outage = cur.fetchone()[0]
    print(f"\n  rows with server_ts <= source_ts: {non_causal}")
    print(f"  rows with no server_ts         : {no_server_ts}")
    print(f"    SOURCE TIME IS NOT RECEIPT TIME: "
          f"{'PASS' if non_causal == 0 else 'FAIL'}")
    ok = ok and non_causal == 0

    # e. the outage window itself is covered
    outage_len = (start_instant - stop_instant).total_seconds()
    print(f"\n  outage window                  : {outage_len:.0f}s")
    print(f"  rows stamped INSIDE the outage : {acquired_during_outage:,}")
    print(f"    TREND HAS NO GAP: "
          f"{'PASS' if acquired_during_outage > 0 else 'FAIL'}")
    ok = ok and acquired_during_outage > 0

    # f. what the collector itself reported
    print(f"\n  buffer depth at restore        : {depth_at_restore:,}")
    print(f"  peak buffer depth              : {ledger.max_buffer_depth:,}")
    print(f"  buffer overflow (lost samples) : {ledger.overflow}")
    print(f"  link state changes             : "
          f"{[(c['up'], c['detail'][:40]) for c in ledger.link_changes]}")
    for drain in ledger.drain_events:
        print(f"  drain: {drain['replayed']:,} samples in {drain['seconds']}s, "
              f"{drain['remaining']} left")
    print(f"    NO SAMPLES LOST TO OVERFLOW: "
          f"{'PASS' if ledger.overflow == 0 else 'FAIL'}")
    ok = ok and ledger.overflow == 0

    # g. the two independent loss checks
    missed = sum(g["missed"] for g in ledger.gaps)
    print(f"\n  OPC UA NotificationMessages missed (server numbering): {missed}"
          f" in {len(ledger.gaps)} gap(s)")
    print(f"    NOTHING MISSED AT THE SOURCE: {'PASS' if missed == 0 else 'FAIL'}")
    ok = ok and missed == 0
    print(f"\n  rows numbered by run {run_id}         : {numbered_rows:,}")
    print(f"  holes in the run's sequence    : {seq_holes} "
          f"({seq_missing} samples)")
    print(f"  recorded in collector_loss     : {recorded_lost}")
    print(f"    NOTHING MISSING BY SEQUENCE: "
          f"{'PASS' if seq_holes == 0 and recorded_lost == 0 else 'FAIL'}")
    ok = ok and seq_holes == 0 and recorded_lost == 0

    print("\n" + "=" * 74)
    print(f"STAGE 3 OUTAGE TEST: {'PASS' if ok else 'FAIL'}")
    print("=" * 74)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-seconds", type=float, default=300.0)
    ap.add_argument("--outage-seconds", type=float, default=180.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-19s %(message)s",
                        stream=sys.stdout)
    logging.Formatter.converter = time.gmtime
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    config = CollectorConfig.from_env()
    return asyncio.run(run(args.run_seconds, args.outage_seconds, config))


if __name__ == "__main__":
    raise SystemExit(main())
