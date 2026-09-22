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

# Measurement names. The tag name is COLLECTOR_<INSTANCE>_<MEASUREMENT>.
HEALTH_MEASUREMENTS = {
    "LINK_STATE":       ("Archive link state, 1 up 0 down", ""),
    "BUFFER_DEPTH":     ("Buffered samples awaiting forward", "samples"),
    "BUFFER_PCT":       ("Buffer fullness", "%"),
    "SAMPLES_PER_SEC":  ("Samples received per second", "1/s"),
    "LAST_FORWARD_AGE": ("Seconds since last successful forward", "s"),
    "CPU_PCT":          ("Collector process CPU", "%"),
    "MEM_MB":           ("Collector process resident memory", "MB"),
    "GAPS":             ("Sequence gaps detected since start", "count"),
}

INSTANCES = ("primary", "secondary")


def tag_name(instance: str, measurement: str) -> str:
    """Health tag names carry the instance, so two collectors running against
    the same archive are distinguishable rather than overwriting each other's
    story (§455)."""
    return f"COLLECTOR_{instance.upper()}_{measurement}"


class HealthPublisher:
    def __init__(self, pipeline: Pipeline, tag_ids: dict[str, int],
                 interval_s: float = 5.0, instance: str = "primary") -> None:
        self.pipeline = pipeline
        self.tag_ids = tag_ids
        self.interval_s = interval_s
        self.instance = instance
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
            "LINK_STATE": 1.0 if pipeline.link_up else 0.0,
            "BUFFER_DEPTH": float(pipeline.buffer.depth),
            "BUFFER_PCT": round(pipeline.buffer.fraction_full * 100, 3),
            "SAMPLES_PER_SEC": round(pipeline.samples_per_second(), 3),
            "LAST_FORWARD_AGE": None if age is None else round(age, 3),
            "CPU_PCT": round(self._cpu_percent(), 2),
            "MEM_MB": round(self._memory_mb(), 2),
            "GAPS": float(pipeline.gaps),
        }

        self._seq += 1
        out = []
        for measurement, value in readings.items():
            name = tag_name(self.instance, measurement)
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
