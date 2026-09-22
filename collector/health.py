"""Collector health, published as tags in the same archive (§462).

The collector is monitored by the same machinery as the plant: its health is
rows in `sample`, keyed on the same primary key, carrying the same quality
semantics. Nothing special, no side channel.

One consequence is worth stating plainly rather than hiding: when the archive
is unreachable, health samples cannot be written either. They are buffered with
everything else and appear on recovery, back-dated to when they were measured.
The gap in the health trend is therefore real and is itself the evidence of the
outage — which is more honest than a monitoring path that magically survives
the failure it is meant to report.

CPU and memory come from `resource.getrusage`, not psutil: no dependency beyond
the set the build plan names.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import resource
import sys
import time

from collector.model import Sample
from collector.pipeline import Pipeline

log = logging.getLogger("collector.health")

HEALTH_TAGS = {
    "COLLECTOR_LINK_STATE":       ("Archive link state, 1 up 0 down", ""),
    "COLLECTOR_BUFFER_DEPTH":     ("Buffered samples awaiting forward", "samples"),
    "COLLECTOR_BUFFER_PCT":       ("Buffer fullness", "%"),
    "COLLECTOR_SAMPLES_PER_SEC":  ("Samples received per second", "1/s"),
    "COLLECTOR_LAST_FORWARD_AGE": ("Seconds since last successful forward", "s"),
    "COLLECTOR_CPU_PCT":          ("Collector process CPU", "%"),
    "COLLECTOR_MEM_MB":           ("Collector process resident memory", "MB"),
    "COLLECTOR_GAPS":             ("Sequence gaps detected since start", "count"),
}


class HealthPublisher:
    def __init__(self, pipeline: Pipeline, tag_ids: dict[str, int],
                 interval_s: float = 5.0) -> None:
        self.pipeline = pipeline
        self.tag_ids = tag_ids
        self.interval_s = interval_s
        self._seq = 0
        self._last_cpu = self._cpu_seconds()
        self._last_wall = time.monotonic()

    @staticmethod
    def _cpu_seconds() -> float:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_utime + usage.ru_stime

    def _memory_mb(self) -> float:
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS, kilobytes on Linux.
        return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024

    def _cpu_percent(self) -> float:
        now_cpu, now_wall = self._cpu_seconds(), time.monotonic()
        elapsed = now_wall - self._last_wall
        used = now_cpu - self._last_cpu
        self._last_cpu, self._last_wall = now_cpu, now_wall
        return (used / elapsed * 100.0) if elapsed > 0 else 0.0

    def sample_now(self) -> list[Sample]:
        """Health values, measured now and stamped now.

        The source timestamp is the instant of measurement, exactly as for a
        plant tag. If these end up buffered, they carry this timestamp into the
        archive on recovery — they are not re-stamped at write time.
        """
        source_ts = dt.datetime.now(dt.timezone.utc)
        server_ts = source_ts
        pipeline = self.pipeline
        age = pipeline.last_forward_age_s()

        readings = {
            "COLLECTOR_LINK_STATE": 1.0 if pipeline.link_up else 0.0,
            "COLLECTOR_BUFFER_DEPTH": float(pipeline.buffer.depth),
            "COLLECTOR_BUFFER_PCT": round(pipeline.buffer.fraction_full * 100, 3),
            "COLLECTOR_SAMPLES_PER_SEC": round(pipeline.samples_per_second(), 3),
            "COLLECTOR_LAST_FORWARD_AGE": None if age is None else round(age, 3),
            "COLLECTOR_CPU_PCT": round(self._cpu_percent(), 2),
            "COLLECTOR_MEM_MB": round(self._memory_mb(), 2),
            "COLLECTOR_GAPS": float(pipeline.gaps),
        }

        self._seq += 1
        out = []
        for name, value in readings.items():
            tag_id = self.tag_ids.get(name)
            if tag_id is None:
                continue
            out.append(Sample(
                tag_id=tag_id, tag_name=name,
                source_ts=source_ts, server_ts=server_ts,
                value=value,
                # A health value that could not be measured is not zero.
                # Nothing has ever been forwarded yet is a real state, and it
                # is Uncertain, not 0 seconds ago.
                quality=0 if value is not None else 1083375616,
                seq=self._seq))
        return out
