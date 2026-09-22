"""Acquisition pipeline: receive, forward, buffer, drain.

The rule that shapes all of this: losing the uplink must not stop acquisition
(§319). Samples keep arriving whatever the archive is doing. When a forward
fails they go to the local buffer; when the archive returns they are replayed in
ascending source-timestamp order, with their original timestamps and quality,
through an upsert that cannot duplicate (§382, §648).

One subtlety worth stating, because getting it wrong is easy and invisible:
while the buffer is draining, LIVE samples are buffered too. If live samples
went straight to the archive while older buffered ones were still being
replayed, the archive would receive them out of order — which is precisely what
the ordered drain exists to prevent. Live forwarding resumes only once the
buffer is empty.
"""

from __future__ import annotations

import asyncio
import logging
import time

from collector import events as ev
from collector.buffer import Buffer
from collector.config import CollectorConfig
from collector.events import EventStream
from collector.model import Sample
from collector.sink import ArchiveSink, SinkUnavailable

log = logging.getLogger("collector.pipeline")


class Pipeline:
    def __init__(self, config: CollectorConfig, sink: ArchiveSink,
                 buffer: Buffer, stream: EventStream) -> None:
        self.config = config
        self.sink = sink
        self.buffer = buffer
        self.stream = stream

        self._incoming: asyncio.Queue[Sample] = asyncio.Queue()
        self.link_up = False
        self.draining = False

        # Counters, published as health tags (§462).
        self.received = 0
        self.forwarded = 0
        self.buffered = 0
        self.drained = 0
        self.gaps = 0
        self.last_forward_monotonic: float | None = None

        self._last_seq: dict[int, int] = {}
        self._last_quality: dict[int, int] = {}
        self._rate_window: list[float] = []

    # -- ingress -----------------------------------------------------------

    def on_sample(self, sample: Sample) -> None:
        """Called from the OPC UA handler. Must not block: acquisition is not
        allowed to wait on the archive."""
        self.received += 1
        self._rate_window.append(time.monotonic())

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

        self._detect_gap(sample)

        self.stream.emit(ev.VALUE_RECEIVED, tag=sample.tag_name,
                         value=sample.value, quality=sample.quality,
                         source_ts=sample.source_ts.isoformat(),
                         server_ts=sample.server_ts.isoformat(), seq=sample.seq)
        self._incoming.put_nowait(sample)

    def _detect_gap(self, sample: Sample) -> None:
        """Sequence numbers are per-tag and monotonic, so a hole is a fact
        rather than an inference."""
        last = self._last_seq.get(sample.tag_id)
        if last is not None and sample.seq != last + 1:
            missing = sample.seq - last - 1
            self.gaps += 1
            self.stream.emit(ev.GAP_DETECTED, tag=sample.tag_name,
                             expected=last + 1, got=sample.seq, missing=missing)
            log.warning("gap on %s: expected seq %d, got %d (%d missing)",
                        sample.tag_name, last + 1, sample.seq, missing)
        self._last_seq[sample.tag_id] = sample.seq

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

    def _buffer(self, batch: list[Sample], reason: str) -> None:
        discarded = self.buffer.append(batch)
        self.buffered += len(batch)
        self.stream.emit(ev.VALUE_BUFFERED, count=len(batch),
                         depth=self.buffer.depth, reason=reason)
        if discarded:
            self.stream.emit(ev.BUFFER_OVERFLOW, discarded=discarded,
                             total_lost=self.buffer.overflowed)
            log.error("buffer full: discarded %d oldest samples (%d lost in "
                      "total). The loss is real and is visible as a sequence "
                      "gap downstream.", discarded, self.buffer.overflowed)
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

            # While draining, live samples go to the buffer as well, so that
            # the archive never sees a new sample before an older one.
            if self.draining or not self.link_up:
                self._buffer(batch, "draining" if self.draining else "link down")
                if not self.link_up:
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

        Returns only when the buffer is empty, so live forwarding cannot resume
        while older samples are still waiting.
        """
        if self.buffer.depth == 0:
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
        elapsed = time.monotonic() - started
        self.stream.emit(ev.BUFFER_DRAINED, replayed=replayed,
                         seconds=round(elapsed, 2),
                         remaining=self.buffer.depth)
        log.info("drained %d samples in %.1fs; buffer now %d",
                 replayed, elapsed, self.buffer.depth)

    async def flush_to_buffer(self) -> int:
        """Move anything still queued in memory into the durable buffer.

        The queue between `on_sample` and the forwarder is in memory, so a
        collector stopped mid-flight would otherwise lose whatever was sitting
        in it. Called on shutdown: the samples land in SQLite with their
        original timestamps and quality, and the next start drains them.
        """
        pending: list[Sample] = []
        while True:
            try:
                pending.append(self._incoming.get_nowait())
            except asyncio.QueueEmpty:
                break
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
