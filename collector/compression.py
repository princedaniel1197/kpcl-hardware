"""Two-stage compression, as PI does it. (§442)

Stage one is exception reporting against ExcDev: a value that has not moved
further than ExcDev from the last reported value is not passed on at all.
Stage two is swinging-door compression against CompDev, following Bristol's
method (US4669097A): hold a corridor from the last archived point and archive
the previous point when a new one falls outside it.

Convention is ExcDev ~ CompDev / 2.

FOUR EXCEPTIONS, and they are the point of this module. Regardless of any
deadband, a value is archived when:

  1. quality changes -- a tag going Bad is the most important thing that can
     happen to it, and it usually happens while the value is flat, which is
     exactly when a deadband would discard it;
  2. the timestamp does not advance -- out-of-order or repeated timestamps mean
     the assumptions under both algorithms no longer hold, so compress nothing;
  3. the value is not a number -- a Bad DataValue carries no value at all
     (OPC UA Part 4), and "no value" cannot be interpolated between;
  4. max_time has elapsed since the last archived point -- otherwise a genuinely
     steady tag would produce no rows for hours and be indistinguishable from a
     dead one.

These four are where naive implementations lose the data that matters. Each has
a test.

NOTE ON SCOPE. Compression deliberately discards samples. That is the opposite
of the guarantee Stage 3 tests -- that nothing acquired is lost between the
source and the archive. The two are reconciled by *what* is discarded: a sample
inside the deadband is one whose value can be reconstructed from its
neighbours to within CompDev, and the reconstruction test proves it. Nothing
that cannot be reconstructed is ever discarded. This module is not wired into
the live pipeline by default; Stage 3's zero-loss result was measured with it
off, and turning it on changes what "loss" means.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from collector.model import Sample

# Why a sample was archived, carried so a trend can explain itself.
BY_EXCEPTION = "exception"
BY_SWINGING_DOOR = "swinging_door"
BY_QUALITY_CHANGE = "quality_change"
BY_TIMESTAMP = "timestamp_not_advancing"
BY_NON_NUMERIC = "non_numeric"
BY_MAX_TIME = "max_time"
BY_FIRST = "first"
BY_FLUSH = "flush"

FORCED_REASONS = frozenset({BY_QUALITY_CHANGE, BY_TIMESTAMP, BY_NON_NUMERIC,
                            BY_MAX_TIME})


@dataclass(frozen=True)
class Archived:
    """A sample that survived compression, and why."""
    sample: Sample
    reason: str

    @property
    def forced(self) -> bool:
        return self.reason in FORCED_REASONS


@dataclass
class Stats:
    received: int = 0
    reported: int = 0     # survived stage one
    archived: int = 0     # survived stage two
    forced: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def ratio(self) -> float:
        """Received per archived. 10.0 means one row kept in ten."""
        return self.received / self.archived if self.archived else 0.0

    def count(self, reason: str) -> None:
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1


def _forced_reason(sample: Sample, previous: Sample | None) -> str | None:
    """The four exceptions. Checked before any deadband."""
    if previous is None:
        return None
    if sample.quality != previous.quality:
        return BY_QUALITY_CHANGE
    if sample.source_ts <= previous.source_ts:
        return BY_TIMESTAMP
    if sample.value is None:
        return BY_NON_NUMERIC
    return None


class ExceptionFilter:
    """Stage one: report only what has moved further than ExcDev."""

    def __init__(self, exc_dev: float | None, max_time_ms: int | None = None) -> None:
        self.exc_dev = exc_dev
        self.max_time = (dt.timedelta(milliseconds=max_time_ms)
                         if max_time_ms else None)
        self._last_reported: Sample | None = None
        self._held: Sample | None = None        # seen, not yet reported
        self._previous: Sample | None = None    # the one before this

    def push(self, sample: Sample) -> list[tuple[Sample, str]]:
        previous, self._previous = self._previous, sample

        if self._last_reported is None:
            self._last_reported = sample
            self._held = None
            return [(sample, BY_FIRST)]

        forced = _forced_reason(sample, previous)
        if forced is None and self.max_time is not None:
            if sample.source_ts - self._last_reported.source_ts >= self.max_time:
                forced = BY_MAX_TIME

        if forced is None and self.exc_dev is not None:
            if (sample.value is not None and self._last_reported.value is not None
                    and abs(sample.value - self._last_reported.value) <= self.exc_dev):
                # Inside the deadband: hold it, do not report it. If the next
                # value breaks out, this one is reported first so the corner is
                # not rounded off.
                self._held = sample
                return []

        out: list[tuple[Sample, str]] = []
        if self._held is not None and self._held.source_ts < sample.source_ts:
            # PI reports the held value alongside the breakout, preserving the
            # shape of the excursion rather than just its end.
            out.append((self._held, BY_EXCEPTION))
        self._held = None
        out.append((sample, forced or BY_EXCEPTION))
        self._last_reported = sample
        return out

    def release_held(self) -> Sample | None:
        """Hand back a sample being held inside the deadband, so the end of a
        series is not lost to it."""
        held, self._held = self._held, None
        return held


class SwingingDoor:
    """Stage two: Bristol's method (US4669097A), with the bound made true.

    A corridor is held open from the last archived point: an upper door pivoted
    at (t0, v0 + CompDev) and a lower door at (t0, v0 - CompDev). Each new point
    swings the doors further open and never back. When the upper door's slope
    passes the lower door's, no straight line from the anchor can still pass
    within CompDev of every point, so a point is archived and the corridor
    restarts there.

    WHY THERE IS A VERIFICATION STEP. The cone test proves that *a* line within
    CompDev of every point exists. It does not prove that the line actually
    drawn -- anchor to archived point -- is that line, and in general it is not.
    Measured on a sine with the cone test alone, the realised interpolation
    error reached 1.6 to 1.9 times CompDev. The textbook algorithm bounds the
    error at roughly 2*CompDev, not CompDev.

    The build plan asks for reconstruction error never to exceed CompDev, and a
    trend that claims CompDev while being out by twice that is precisely the
    quiet dishonesty this project exists to avoid. So the cone decides *when* to
    archive, and then the chosen point is checked against the segment it would
    represent; if interpolating to it would put any discarded point further than
    CompDev from the line, an earlier point is chosen instead. The cost is
    holding the current segment in memory, which max_time bounds.
    """

    def __init__(self, comp_dev: float | None, max_time_ms: int | None = None) -> None:
        self.comp_dev = comp_dev
        self.max_time = (dt.timedelta(milliseconds=max_time_ms)
                         if max_time_ms else None)
        self._anchor: Sample | None = None
        self._segment: list[Sample] = []   # reached the door since the anchor
        # Every RAW sample since the anchor, including those stage one
        # discarded. The bound is verified against these, not merely against
        # what survived exception reporting -- otherwise a point dropped by
        # stage one is never checked against the line finally drawn, and the
        # end-to-end error is not bounded by anything. Measured on real
        # U1_DRUM_PRESS data, that gap put the worst error at 0.6247 against a
        # claimed 0.600.
        self._witness: list[Sample] = []
        self._upper = float("-inf")
        self._lower = float("inf")
        self._previous: Sample | None = None

    def observe_raw(self, sample: Sample) -> None:
        """Record a sample the door may never be given, so the bound can still
        be verified against it."""
        self._witness.append(sample)

    # -- cone ---------------------------------------------------------------

    def _slopes(self, anchor: Sample, point: Sample) -> tuple[float, float] | None:
        if anchor.value is None or point.value is None:
            return None
        elapsed = (point.source_ts - anchor.source_ts).total_seconds()
        if elapsed <= 0:
            return None
        assert self.comp_dev is not None
        return ((point.value - anchor.value - self.comp_dev) / elapsed,
                (point.value - anchor.value + self.comp_dev) / elapsed)

    def _rebuild_cone(self) -> None:
        self._upper, self._lower = float("-inf"), float("inf")
        if self._anchor is None:
            return
        for point in self._segment:
            slopes = self._slopes(self._anchor, point)
            if slopes is None:
                continue
            self._upper = max(self._upper, slopes[0])
            self._lower = min(self._lower, slopes[1])

    # -- the bound ----------------------------------------------------------

    def _witnesses_between(self, anchor: Sample, target: Sample) -> list[Sample]:
        return [w for w in self._witness
                if anchor.source_ts < w.source_ts < target.source_ts]

    def _within_bound(self, anchor: Sample, target: Sample,
                      discarded: list[Sample]) -> bool:
        """Would interpolating anchor->target keep every discarded point within
        CompDev of the line? Checked against the raw samples too."""
        discarded = list(discarded) + self._witnesses_between(anchor, target)
        if anchor.value is None or target.value is None:
            return False
        span = (target.source_ts - anchor.source_ts).total_seconds()
        if span <= 0:
            return False
        slope = (target.value - anchor.value) / span
        assert self.comp_dev is not None
        for point in discarded:
            if point.value is None:
                return False
            offset = (point.source_ts - anchor.source_ts).total_seconds()
            if abs(point.value - (anchor.value + slope * offset)) > self.comp_dev + 1e-12:
                return False
        return True

    def _choose(self) -> int:
        """Index in the segment of the point to archive.

        Starts at the last point that still fitted the cone and walks back until
        the interpolation bound actually holds. Index 0 always holds: a line
        from the anchor to the very next point discards nothing.
        """
        assert self._anchor is not None
        index = len(self._segment) - 2
        while index > 0:
            if self._within_bound(self._anchor, self._segment[index],
                                  self._segment[:index]):
                return index
            index -= 1
        return 0

    # -- driving ------------------------------------------------------------

    def _reset(self, anchor: Sample, remaining: list[Sample] | None = None) -> None:
        self._anchor = anchor
        self._segment = list(remaining or [])
        self._witness = [w for w in self._witness
                         if w.source_ts > anchor.source_ts]
        self._rebuild_cone()

    def push(self, sample: Sample) -> list[tuple[Sample, str]]:
        previous, self._previous = self._previous, sample

        if self._anchor is None:
            self._reset(sample)
            return [(sample, BY_FIRST)]

        forced = _forced_reason(sample, previous)
        if forced is None and self.max_time is not None:
            if sample.source_ts - self._anchor.source_ts >= self.max_time:
                forced = BY_MAX_TIME

        if forced is not None:
            # Points between the anchor and a forced archive are real data, and
            # the span to them must satisfy the bound like any other -- so the
            # segment is drained with verification, not just topped off with its
            # last point.
            out = self._drain_segment(BY_SWINGING_DOOR)
            out.append((sample, forced))
            self._reset(sample)
            return out

        if self.comp_dev is None:
            self._reset(sample)
            return [(sample, BY_SWINGING_DOOR)]

        slopes = self._slopes(self._anchor, sample)
        if slopes is None:
            self._reset(sample)
            return [(sample, BY_NON_NUMERIC if sample.value is None
                     else BY_TIMESTAMP)]

        self._segment.append(sample)
        upper, lower = self._upper, self._lower
        for witness in (*self._witnesses_between(self._anchor, sample), sample):
            pair = self._slopes(self._anchor, witness)
            if pair is None:
                continue
            upper = max(upper, pair[0])
            lower = min(lower, pair[1])

        if upper <= lower:
            self._upper, self._lower = upper, lower
            return []

        # Corridor closed. Pick a point the interpolation bound actually holds
        # for, archive it, and restart the corridor there.
        index = self._choose()
        archived = self._segment[index]
        self._reset(archived, self._segment[index + 1:])
        return [(archived, BY_SWINGING_DOOR)]

    def _drain_segment(self, final_reason: str) -> list[tuple[Sample, str]]:
        """Archive the open segment, inserting intermediate points until the
        interpolation bound holds all the way to its final point.

        Used by flush() and by the forced-archive path. Emitting only the last
        point leaves everything before it unverified -- which is how
        U1_CONDENSER_VAC came out at 3.7645 against a claimed 3.300 on real
        data, via the max_time forced archives.
        """
        out: list[tuple[Sample, str]] = []
        while self._segment:
            anchor = self._anchor
            last = self._segment[-1]
            if (anchor is None or len(self._segment) == 1
                    or self._within_bound(anchor, last, self._segment[:-1])):
                out.append((last, final_reason))
                self._segment = []
                self._anchor = last
                break
            index = self._choose()
            archived = self._segment[index]
            out.append((archived, BY_SWINGING_DOOR))
            self._reset(archived, self._segment[index + 1:])
        return out

    def flush(self) -> list[tuple[Sample, str]]:
        """Archive whatever is still held, so the last real point of a series is
        never silently dropped."""
        return self._drain_segment(BY_FLUSH)


class TwoStageCompressor:
    """Exception reporting, then swinging door. One per tag."""

    def __init__(self, exc_dev: float | None, comp_dev: float | None,
                 max_time_ms: int | None = None) -> None:
        self.exception = ExceptionFilter(exc_dev, max_time_ms)
        self.door = SwingingDoor(comp_dev, max_time_ms)
        self.stats = Stats()

    def push(self, sample: Sample) -> list[Archived]:
        self.stats.received += 1
        # The door witnesses every raw sample even when stage one discards it,
        # so the reconstruction bound is verified end to end.
        self.door.observe_raw(sample)
        out: list[Archived] = []
        for reported, _ in self.exception.push(sample):
            self.stats.reported += 1
            for archived, reason in self.door.push(reported):
                self.stats.archived += 1
                self.stats.count(reason)
                if reason in FORCED_REASONS:
                    self.stats.forced += 1
                out.append(Archived(archived, reason))
        return out

    def flush(self) -> list[Archived]:
        out: list[Archived] = []
        # The exception filter may still be holding a sample inside the
        # deadband that never reached the door. Push it through first, or the
        # last point of a series is lost.
        held = self.exception.release_held()
        if held is not None:
            for archived, reason in self.door.push(held):
                self.stats.archived += 1
                self.stats.count(reason)
                out.append(Archived(archived, reason))
        for archived, reason in self.door.flush():
            self.stats.archived += 1
            self.stats.count(reason)
            out.append(Archived(archived, reason))
        return out


def compress(samples: list[Sample], exc_dev: float | None,
             comp_dev: float | None,
             max_time_ms: int | None = None) -> tuple[list[Archived], Stats]:
    """Compress a whole series. Returns what was archived, and the statistics."""
    compressor = TwoStageCompressor(exc_dev, comp_dev, max_time_ms)
    archived: list[Archived] = []
    for sample in samples:
        archived.extend(compressor.push(sample))
    archived.extend(compressor.flush())
    return archived, compressor.stats


def reconstruct(archived: list[Sample], at: list[dt.datetime]) -> list[float | None]:
    """Rebuild the original series by linear interpolation between archived
    points -- which is exactly how a trend display draws it.

    Returns None where no value can be honestly produced: before the first
    archived point, after the last, or across a span bounded by a point that
    carried no value. An interpolation across a Bad sample would be inventing
    data, which is the whole thing this project refuses to do.
    """
    points = [p for p in archived]
    out: list[float | None] = []
    index = 0
    for when in at:
        while index + 1 < len(points) and points[index + 1].source_ts <= when:
            index += 1
        if not points or when < points[0].source_ts or when > points[-1].source_ts:
            out.append(None)
            continue
        left = points[index]
        right = points[index + 1] if index + 1 < len(points) else left
        if left.value is None or right.value is None:
            out.append(None)
            continue
        if right.source_ts == left.source_ts:
            out.append(left.value)
            continue
        span = (right.source_ts - left.source_ts).total_seconds()
        offset = (when - left.source_ts).total_seconds()
        out.append(left.value + (right.value - left.value) * (offset / span))
    return out


def reconstruction_error(original: list[Sample],
                         archived: list[Archived]) -> tuple[float, int]:
    """Largest absolute difference between the original series and its
    reconstruction, and how many points could be compared."""
    points = [a.sample for a in archived]
    times = [s.source_ts for s in original]
    rebuilt = reconstruct(points, times)
    worst = 0.0
    compared = 0
    for sample, value in zip(original, rebuilt):
        if sample.value is None or value is None:
            continue
        worst = max(worst, abs(sample.value - value))
        compared += 1
    return worst, compared
