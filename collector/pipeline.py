"""Acquisition pipeline: receive, compress, number, forward, buffer, drain.

The rule that shapes all of this: losing the uplink must not stop acquisition
(§319). Samples keep arriving whatever the archive is doing. When a forward
fails they go to the local buffer; when the archive returns they are replayed in
ascending source-timestamp order, with their original timestamps and quality,
through an upsert that cannot duplicate (§382, §648).

WHAT HAPPENS TO LIVE SAMPLES DURING A DRAIN, stated exactly because an earlier
version of this docstring got it wrong. The drain runs inside the forwarder, so
while it runs nothing takes samples off the in-memory queue. Between every
drained batch the drain moves whatever has queued up into the durable buffer,
where the ascending-source_ts read puts it after the older rows it is replaying.
So live samples never reach the archive ahead of older buffered ones, and they
are never held only in memory for longer than one drained batch -- a SIGKILL
mid-drain costs at most that, not the whole drain's worth. The drain ends only
when both the buffer and the queue are empty.

SEQUENCE NUMBERS. A sample is numbered at the moment the pipeline decides it
will be archived -- after compression and after a non-advancing timestamp is
absorbed -- per tag, within this collector run. The number and the run travel
into the archive with the row. A hole in a run's numbers is therefore a sample
this collector meant to write and the archive does not hold; the view
`sample_seq_gap` finds every one. Buffer overflow, the one loss the collector
itself can cause, is additionally written to `collector_loss` with the exact
sequence numbers discarded. Loss before the collector -- between the source and
the subscription -- is detected from the server's NotificationMessage numbering
in session.py.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time

from collector import events as ev
from collector.buffer import Buffer
from collector.compression import TwoStageCompressor
from collector.config import CollectorConfig
from collector.events import EventStream
from collector.model import Sample
from collector.sink import ArchiveSink, SinkUnavailable

log = logging.getLogger("collector.pipeline")


class Pipeline:
    def __init__(self, config: CollectorConfig, sink: ArchiveSink,
                 buffer: Buffer, stream: EventStream, run_id: int | None = None,
                 compress_tags: dict[int, dict] | None = None) -> None:
        self.config = config
        self.sink = sink
        self.buffer = buffer
        self.stream = stream
        self.run_id = run_id

        self._incoming: asyncio.Queue[Sample] = asyncio.Queue()
        self.link_up = False
        self.draining = False

        # Counters, published as health tags (§462).
        self.received = 0
        self.forwarded = 0
        self.buffered = 0
        self.drained = 0
        self.duplicate_ts = 0          # absorbed: same tag, same source_ts
        self.compressed_out = 0        # discarded by compression, by design
        self.last_forward_monotonic: float | None = None

        self._seq: dict[int, int] = {}
        self._last_ts: dict[int, object] = {}
        self._last_quality: dict[int, int] = {}
        self._rate_window: list[float] = []

        # Two-stage compression, for tags configured with compress = true.
        # Off for every tag by default (see archive migration 014).
        self.compressors: dict[int, TwoStageCompressor] = {}
        for tag_id, spec in (compress_tags or {}).items():
            self.compressors[tag_id] = TwoStageCompressor(
                spec["exc_dev"], spec["comp_dev"], spec["max_time_ms"])

    # -- ingress -----------------------------------------------------------

    def on_sample(self, sample: Sample) -> None:
        """Called from the OPC UA handler. Must not block: acquisition is not
        allowed to wait on the archive."""
        self.received += 1
        self._rate_window.append(time.monotonic())

        # The same sample delivered again -- a max-time read of a value the
        # source has not re-stamped -- or a second value at a timestamp already
        # taken. The archive's primary key would absorb it anyway; absorbing it
        # here keeps it out of compression and out of the sequence numbering,
        # and counts it instead of losing it in an ON CONFLICT.
        if self._last_ts.get(sample.tag_id) == sample.source_ts:
            self.duplicate_ts += 1
            return
        self._last_ts[sample.tag_id] = sample.source_ts

        # A quality change is a reportable event in its own right, whether or
        # not the value changed (§318).
        previous = self._last_quality.get(sample.tag_id)
        if previous is not None and previous != sample.quality:
            self.stream.emit(ev.QUALITY_CHANGED, tag=sample.tag_name,
                             was=previous, now=sample.quality,
                             quality_class=sample.quality_class,
                             source_ts=sample.source_ts.isoformat())
            log.info("%s quality %s -> %s (%s)", sample.tag_name, previous,
                     sample.quality, sample.quality_class)
        self._last_quality[sample.tag_id] = sample.quality

        self.stream.emit(ev.VALUE_RECEIVED, tag=sample.tag_name,
                         value=sample.value, quality=sample.quality,
                         source_ts=sample.source_ts.isoformat(),
                         server_ts=(sample.server_ts.isoformat()
                                    if sample.server_ts else None))

        compressor = self.compressors.get(sample.tag_id)
        if compressor is None:
            self._admit(sample)
            return
        archived = compressor.push(sample)
        if not archived:
            self.compressed_out += 1
        for kept in archived:
            self._admit(kept.sample)

    def _admit(self, sample: Sample) -> None:
        """Number a sample the collector has decided to archive, and queue it."""
        seq = self._seq.get(sample.tag_id, 0) + 1
        self._seq[sample.tag_id] = seq
        self._incoming.put_nowait(
            dataclasses.replace(sample, seq=seq, run=self.run_id))

    # -- egress ------------------------------------------------------------

    async def _collect_batch(self) -> list[Sample]:
        """Wait for at least one sample, then take whatever else is ready."""
        first = await self._incoming.get()
        batch = [first]
        deadline = time.monotonic() + self.config.batch_linger_s
        while len(batch) < self.config.batch_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(self._incoming.get(),
                                                    timeout=remaining))
            except asyncio.TimeoutError:
                break
        return batch

    def _take_queued(self) -> list[Sample]:
        pending: list[Sample] = []
        while True:
            try:
                pending.append(self._incoming.get_nowait())
            except asyncio.QueueEmpty:
                return pending

    def _buffer(self, batch: list[Sample], reason: str) -> None:
        discarded = self.buffer.append(batch)
        self.buffered += len(batch)
        self.stream.emit(ev.VALUE_BUFFERED, count=len(batch),
                         depth=self.buffer.depth, reason=reason)
        if discarded:
            self.stream.emit(ev.BUFFER_OVERFLOW, discarded=discarded,
                             total_lost=self.buffer.overflowed)
            log.error("buffer full: discarded %d oldest samples (%d lost in "
                      "total). The loss is real: each discarded range is "
                      "recorded with its sequence numbers and is forwarded to "
                      "collector_loss.", discarded, self.buffer.overflowed)
        if self.buffer.over_high_water():
            self.stream.emit(ev.BUFFER_HIGH_WATER,
                             depth=self.buffer.depth,
                             fraction=round(self.buffer.fraction_full, 3))
            log.warning("buffer above %.0f%%: %d of %d rows",
                        self.config.buffer_warn_fraction * 100,
                        self.buffer.depth, self.buffer.max_rows)

    async def _set_link(self, up: bool, detail: str = "") -> None:
        if up != self.link_up:
            self.link_up = up
            self.stream.emit(ev.LINK_STATE, up=up, detail=detail)
            log.info("archive link %s%s", "up" if up else "down",
                     f": {detail}" if detail else "")

    async def run_forwarder(self) -> None:
        """Forward live samples, or buffer them, forever."""
        while True:
            batch = await self._collect_batch()

            if not self.link_up:
                self._buffer(batch, "link down")
                await self._try_recover()
                continue

            try:
                written = await self.sink.write(batch)
            except SinkUnavailable as exc:
                await self._set_link(False, str(exc)[:120])
                self._buffer(batch, "forward failed")
                continue

            self.forwarded += written
            self.last_forward_monotonic = time.monotonic()
            self.stream.emit(ev.VALUE_FORWARDED, count=written)
            await self._forward_losses()

    async def _try_recover(self) -> None:
        """Attempt to reach the archive; on success, drain before resuming."""
        try:
            await self.sink.connect()
        except SinkUnavailable:
            await asyncio.sleep(self.config.reconnect_delay_s)
            return
        await self._set_link(True, "reconnected")
        await self.drain()

    async def drain(self) -> None:
        """Replay the buffer in ascending source-timestamp order.

        Returns only when the buffer and the in-memory queue are both empty, so
        live forwarding cannot resume while older samples are still waiting.
        Live samples that arrive meanwhile are moved into the buffer between
        batches (see the module docstring).
        """
        if self.buffer.depth == 0:
            await self._forward_losses()
            return
        self.draining = True
        started = time.monotonic()
        depth_at_start = self.buffer.depth
        self.stream.emit(ev.BUFFER_DRAIN_STARTED, depth=depth_at_start)
        log.info("draining %d buffered samples in source-timestamp order",
                 depth_at_start)
        replayed = 0
        try:
            while True:
                live = self._take_queued()
                if live:
                    self._buffer(live, "arrived while draining")
                rows = self.buffer.take(self.config.batch_size)
                if not rows:
                    break
                ids = [row_id for row_id, _ in rows]
                samples = [s for _, s in rows]
                try:
                    await self.sink.write(samples)
                except SinkUnavailable as exc:
                    await self._set_link(False, str(exc)[:120])
                    log.warning("drain interrupted after %d samples: %s",
                                replayed, exc)
                    return
                # Only forget once the archive has committed them. A crash
                # mid-drain replays; it does not lose.
                self.buffer.forget(ids)
                replayed += len(samples)
                self.drained += len(samples)
                self.last_forward_monotonic = time.monotonic()
        finally:
            self.draining = False
        await self._forward_losses()
        elapsed = time.monotonic() - started
        self.stream.emit(ev.BUFFER_DRAINED, replayed=replayed,
                         seconds=round(elapsed, 2),
                         remaining=self.buffer.depth)
        log.info("drained %d samples in %.1fs; buffer now %d",
                 replayed, elapsed, self.buffer.depth)

    async def _forward_losses(self) -> None:
        """Send any recorded buffer-overflow losses to the archive. Kept in the
        buffer until the archive has them, like samples."""
        if not self.buffer.pending_losses():
            return
        losses = self.buffer.losses()
        try:
            await self.sink.write_losses([loss for _, loss in losses])
        except SinkUnavailable:
            return
        self.buffer.forget_losses([row_id for row_id, _ in losses])

    async def flush_to_buffer(self) -> int:
        """Move anything still held in memory into the durable buffer.

        Called on shutdown. The queue between `on_sample` and the forwarder is
        in memory, and so is any compressor's open segment; a collector stopped
        mid-flight would otherwise lose both. They land in SQLite with their
        original timestamps and quality, and the next start drains them.
        """
        for compressor in self.compressors.values():
            for kept in compressor.flush():
                self._admit(kept.sample)
        pending = self._take_queued()
        if pending:
            self._buffer(pending, "shutdown flush")
            log.info("flushed %d in-flight samples to the buffer", len(pending))
        return len(pending)

    @property
    def queued(self) -> int:
        """Samples received but not yet forwarded or buffered."""
        return self._incoming.qsize()

    # -- health ------------------------------------------------------------

    def samples_per_second(self, window_s: float = 10.0) -> float:
        cutoff = time.monotonic() - window_s
        self._rate_window = [t for t in self._rate_window if t >= cutoff]
        return len(self._rate_window) / window_s

    def last_forward_age_s(self) -> float | None:
        if self.last_forward_monotonic is None:
            return None
        return time.monotonic() - self.last_forward_monotonic
