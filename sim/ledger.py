"""What the simulator actually published: the source's own record.

A test of "no sample missing" is only as good as its idea of what should be
there. The strongest idea available is the source's: every value this server
put into its address space that an OPC UA subscription is obliged to report.

Which writes those are was measured against asyncua 2.0.1, not assumed
(fat/records/stage-14-fat.md): a subscription with the default StatusValue
trigger and no deadband reports a write exactly when the pair (value, status)
differs from the previous write, where a Bad write's value is dropped by the
server -- so two Bad writes in a row with different underlying numbers are one
notification, not two. A write that changes only the SourceTimestamp is not
reported. The ledger records exactly the writes that pass that test.

It is a real OPC UA server's view of its own output. A real DCS does not
expose one; this simulator does, because a demonstrator that claims zero loss
should be checked against the source rather than against itself.

Memory is bounded: per tag, an array of 8-byte microsecond timestamps, pruned
to the retention window.
"""

from __future__ import annotations

import datetime as dt
from array import array
from bisect import bisect_left, bisect_right

_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
BAD = 2


def to_us(when: dt.datetime) -> int:
    delta = when - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


class Ledger:
    def __init__(self, retention_s: float = 4 * 3600) -> None:
        self.retention_us = int(retention_s * 1_000_000)
        self.started_us = to_us(dt.datetime.now(dt.timezone.utc))
        self._reported: dict[str, array] = {}
        self._last: dict[str, tuple[object, int]] = {}
        self.writes = 0

    def record(self, tag: str, value: object, status: int,
               source_ts: dt.datetime) -> bool:
        """Note one write. Returns True when a subscription must report it."""
        self.writes += 1
        delivered = (None if (status >> 30) & 3 == BAD else value, status)
        if self._last.get(tag) == delivered:
            return False
        self._last[tag] = delivered
        series = self._reported.setdefault(tag, array("q"))
        series.append(to_us(source_ts))
        if len(series) % 1024 == 0:
            self._prune(series)
        return True

    def _prune(self, series: array) -> None:
        cutoff = series[-1] - self.retention_us
        keep = bisect_left(series, cutoff)
        if keep:
            del series[:keep]

    def window(self, since: dt.datetime, until: dt.datetime,
               tags: list[str] | None = None) -> dict[str, list[int]]:
        """Reported source timestamps, per tag, in [since, until], as
        microseconds since the epoch."""
        lo, hi = to_us(since), to_us(until)
        out = {}
        for tag, series in self._reported.items():
            if tags and tag not in tags:
                continue
            out[tag] = list(series[bisect_left(series, lo):bisect_right(series, hi)])
        return out

    @property
    def oldest_us(self) -> int | None:
        firsts = [s[0] for s in self._reported.values() if s]
        return min(firsts) if firsts else None
