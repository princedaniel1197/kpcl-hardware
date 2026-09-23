"""Pipeline tests: numbering, absorption of repeats, compression in the live
path, the drain's handling of live samples, and the loss ledger.

The sink here is a fake, because the thing under test is the pipeline's own
ordering and bookkeeping; the real archive is exercised by the Stage 3 outage
test (collector/outage_test.py) and by T-04 against the simulator's ledger.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from collector.buffer import Buffer
from collector.config import CollectorConfig
from collector.events import EventStream
from collector.model import Sample
from collector.pipeline import Pipeline
from collector.sink import SinkUnavailable

T0 = dt.datetime(2026, 9, 23, 2, 0, tzinfo=dt.timezone.utc)


class FakeSink:
    def __init__(self) -> None:
        self.rows: list[Sample] = []
        self.losses: list[dict] = []
        self.up = True
        self.on_write = None

    async def connect(self) -> None:
        if not self.up:
            raise SinkUnavailable("down")

    async def write(self, samples: list[Sample]) -> int:
        if not self.up:
            raise SinkUnavailable("down")
        if self.on_write is not None:
            self.on_write()
        self.rows.extend(samples)
        return len(samples)

    async def write_losses(self, losses: list[dict]) -> None:
        if not self.up:
            raise SinkUnavailable("down")
        self.losses.extend(losses)


def s(n: float, tag: int = 1, value: float | None = None, quality: int = 0) -> Sample:
    return Sample(tag, f"T{tag}", T0 + dt.timedelta(seconds=n),
                  T0 + dt.timedelta(seconds=n, milliseconds=30),
                  float(n) if value is None else value, quality)


def pipeline(tmp_path, sink=None, **kw) -> Pipeline:
    config = CollectorConfig(batch_linger_s=0.01, batch_size=50,
                             reconnect_delay_s=0.01)
    p = Pipeline(config, sink or FakeSink(), Buffer(tmp_path / "b.sqlite", **kw),
                 EventStream(), run_id=42)
    p.link_up = True
    return p


def test_samples_are_numbered_per_tag_within_the_run(tmp_path):
    p = pipeline(tmp_path)
    for n in range(3):
        p.on_sample(s(n, tag=1))
        p.on_sample(s(n, tag=2))
    queued = p._take_queued()
    assert [(q.tag_id, q.seq) for q in queued] == [
        (1, 1), (2, 1), (1, 2), (2, 2), (1, 3), (2, 3)]
    assert {q.run for q in queued} == {42}


def test_the_same_source_timestamp_twice_is_absorbed_and_counted(tmp_path):
    """A max-time read of a value the source has not re-stamped delivers the
    same sample again. It must not take a sequence number -- the archive would
    discard the row and leave a hole that looked like a loss."""
    p = pipeline(tmp_path)
    p.on_sample(s(1))
    p.on_sample(s(1))
    p.on_sample(s(2))
    queued = p._take_queued()
    assert [q.seq for q in queued] == [1, 2]
    assert p.duplicate_ts == 1


def test_a_compressed_tag_passes_only_what_compression_keeps(tmp_path):
    p = pipeline(tmp_path)
    p.compressors = {}
    from collector.compression import TwoStageCompressor
    p.compressors[1] = TwoStageCompressor(0.05, 0.1, 60_000)
    for n in range(200):                       # a slow straight ramp
        p.on_sample(s(n * 0.5, value=10.0 + n * 0.01))
    kept = p._take_queued()
    assert 0 < len(kept) < 20
    assert p.compressed_out > 150
    # Numbered consecutively all the same: a sequence number counts what the
    # collector decided to archive, so compression leaves no holes.
    assert [q.seq for q in kept] == list(range(1, len(kept) + 1))


def test_shutdown_flushes_a_compressors_open_segment(tmp_path):
    p = pipeline(tmp_path)
    from collector.compression import TwoStageCompressor
    p.compressors[1] = TwoStageCompressor(0.05, 0.1, 60_000)
    for n in range(50):
        p.on_sample(s(n * 0.5, value=10.0 + n * 0.01))
    asyncio.run(p.flush_to_buffer())
    last = max(q.source_ts for _, q in p.buffer.take(1000))
    assert last == T0 + dt.timedelta(seconds=49 * 0.5)


def test_live_samples_during_a_drain_go_to_the_buffer_in_order(tmp_path):
    """While the drain replays, new samples keep arriving. They must not sit
    only in memory for the whole drain, and must not reach the archive before
    older buffered ones."""
    sink = FakeSink()
    p = pipeline(tmp_path, sink)
    # An outage's worth of buffered samples.
    for n in range(120):
        p.on_sample(s(n))
    p.buffer.append(p._take_queued())

    arrivals = iter(range(120, 140))

    def live_sample_arrives() -> None:
        # Every write the drain makes, a new live sample turns up.
        for n in (next(arrivals, None),):
            if n is not None:
                p.on_sample(s(n))
                assert p.queued <= 1, "live samples piled up in memory"

    sink.on_write = live_sample_arrives
    asyncio.run(p.drain())
    times = [r.source_ts for r in sink.rows]
    assert times == sorted(times)
    assert p.buffer.depth == 0
    assert p.queued == 0
    # Everything numbered, everything delivered, nothing twice.
    assert len({r.seq for r in sink.rows}) == len(sink.rows)


def test_overflow_losses_reach_the_archive(tmp_path):
    sink = FakeSink()
    p = pipeline(tmp_path, sink, max_rows=10)
    for n in range(15):
        p.on_sample(s(n))
    p._buffer(p._take_queued(), "link down")
    asyncio.run(p.drain())
    (loss,) = sink.losses
    assert (loss["run"], loss["tag_id"], loss["samples"]) == (42, 1, 5)
    assert (loss["first_seq"], loss["last_seq"]) == (1, 5)
    assert p.buffer.pending_losses() == 0
    # The sequence numbers the archive holds pick up exactly where the loss
    # record says the loss ended.
    assert [r.seq for r in sink.rows] == list(range(6, 16))


def test_losses_wait_in_the_buffer_while_the_archive_is_down(tmp_path):
    sink = FakeSink()
    p = pipeline(tmp_path, sink, max_rows=5)
    for n in range(8):
        p.on_sample(s(n))
    p._buffer(p._take_queued(), "link down")
    sink.up = False
    asyncio.run(p._forward_losses())
    assert p.buffer.pending_losses() == 1 and sink.losses == []
    sink.up = True
    asyncio.run(p._forward_losses())
    assert p.buffer.pending_losses() == 0 and len(sink.losses) == 1


def test_a_quality_change_is_an_event_even_with_the_value_unchanged(tmp_path):
    p = pipeline(tmp_path)
    seen = []
    p.stream.emit = lambda kind, **f: seen.append(kind)
    p.on_sample(s(1, value=5.0))
    p.on_sample(s(2, value=5.0, quality=2156593152))
    assert "quality_changed" in seen


def test_nothing_is_lost_between_on_sample_and_the_archive(tmp_path):
    """End to end through the forwarder, with an outage in the middle."""
    sink = FakeSink()
    p = pipeline(tmp_path, sink)

    async def go() -> None:
        forwarder = asyncio.create_task(p.run_forwarder())
        for n in range(30):
            p.on_sample(s(n))
            await asyncio.sleep(0.002)
        sink.up = False
        for n in range(30, 60):
            p.on_sample(s(n))
            await asyncio.sleep(0.002)
        sink.up = True
        for n in range(60, 90):
            p.on_sample(s(n))
            await asyncio.sleep(0.002)
        for _ in range(200):
            if len(sink.rows) >= 90 and p.buffer.depth == 0:
                break
            await asyncio.sleep(0.01)
        forwarder.cancel()

    asyncio.run(go())
    assert sorted(r.seq for r in sink.rows) == list(range(1, 91))
    # And the archive never received a sample before an older one.
    times = [r.source_ts for r in sink.rows]
    assert times == sorted(times)
