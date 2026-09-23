"""Stage 4 acceptance measurement: compression in the live acquisition path.

Subscribes to the live simulator with no deadband at the source, and sends
every sample two ways at once:

  * into a raw record, untouched -- everything the DCS produced;
  * into the collector's own Pipeline, with two-stage compression switched on
    for every tag that has a CompDev, exactly as `python -m collector` does for
    a tag with compress = true, and out through the forwarder to a sink that
    records what would have been archived.

Then, per tag, it reports the ratio (raw in, archived out) and the worst
difference between the raw series and the trend reconstructed from what was
archived -- which must not exceed CompDev.

This measures the code path the collector runs, not a replay through the
library: the numbering, the absorption of repeated timestamps and the
forwarder are all in it. The archive itself is not written to, so running this
does not thin the stored history of a tag whose `compress` flag is off.

The ratios are the point. A slow, smooth tag compresses hard. A noisy one does
not, and reporting a high ratio for it would mean discarding real signal --
which is why the noisy tag's low ratio is a correct result rather than a
disappointing one.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import tempfile
from pathlib import Path

import psycopg

from collector.buffer import Buffer
from collector.compression import Archived, reconstruction_error
from collector.config import CollectorConfig
from collector.events import EventStream
from collector.model import Sample
from collector.pipeline import Pipeline
from collector.session import ReadOnlySession

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
ENDPOINT = os.environ.get("SIM_OPCUA_ENDPOINT",
                          "opc.tcp://127.0.0.1:4840/orianode/crpms/")


class RecordingSink:
    """Stands where the archive stands, and keeps what it is given."""

    def __init__(self) -> None:
        self.rows: list[Sample] = []

    async def connect(self) -> None:
        return None

    async def write(self, samples: list[Sample]) -> int:
        self.rows.extend(samples)
        return len(samples)

    async def write_losses(self, losses: list[dict]) -> None:
        raise AssertionError(f"the buffer overflowed during a measurement: {losses}")


async def measure(seconds: float, tags: list[dict], workdir: Path
                  ) -> tuple[dict[int, list[Sample]], dict[int, list[Sample]], Pipeline]:
    raw: dict[int, list[Sample]] = {t["id"]: [] for t in tags}
    compressed = {t["id"]: t for t in tags if t["comp_dev"] is not None}
    sink = RecordingSink()
    pipeline = Pipeline(CollectorConfig(), sink, Buffer(workdir / "buffer.sqlite"),
                        EventStream(), run_id=None, compress_tags=compressed)
    pipeline.link_up = True

    def on_sample(sample: Sample) -> None:
        raw[sample.tag_id].append(sample)
        pipeline.on_sample(sample)

    # No deadband at the source: compress what the DCS produced, not something
    # already thinned at acquisition.
    session = ReadOnlySession(ENDPOINT, on_sample, source_deadband=False)
    forwarder = asyncio.create_task(pipeline.run_forwarder())
    await session.connect(tags)
    print(f"capturing {seconds:.0f}s from {len(session.subscribed)} tags through "
          f"the live pipeline, {len(compressed)} compressed ...")
    await asyncio.sleep(seconds)
    await session.disconnect()
    # What a clean shutdown does: the compressors' open segments go out too.
    await asyncio.sleep(1.0)
    forwarder.cancel()
    await pipeline.flush_to_buffer()
    await pipeline.drain()

    archived: dict[int, list[Sample]] = {t["id"]: [] for t in tags}
    for row in sink.rows:
        archived[row.tag_id].append(row)
    return raw, archived, pipeline


def dedupe(series: list[Sample]) -> list[Sample]:
    """The raw record, less repeated deliveries of one source timestamp -- the
    pipeline absorbs those before compression, and the archive would too."""
    seen, out = set(), []
    for s in series:
        if s.source_ts not in seen:
            seen.add(s.source_ts)
            out.append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=180.0)
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, scan_rate_ms, exc_dev, comp_dev, max_time_ms,"
                    " source_path FROM tag WHERE source_system='opcua'"
                    " AND source_path IS NOT NULL ORDER BY name")
        tags = [{"id": r[0], "name": r[1], "scan_rate_ms": r[2], "exc_dev": r[3],
                 "comp_dev": r[4], "max_time_ms": r[5], "source_path": r[6]}
                for r in cur.fetchall()]

    with tempfile.TemporaryDirectory() as workdir:
        raw, archived, pipeline = asyncio.run(
            measure(args.seconds, tags, Path(workdir)))

    print(f"\n{'tag':<20} {'raw':>6} {'arch':>6} {'ratio':>8} "
          f"{'CompDev':>8} {'worst err':>10}  verdict")
    print("-" * 80)
    results = []
    for tag in tags:
        series = dedupe(raw[tag["id"]])
        kept = archived[tag["id"]]
        if tag["comp_dev"] is None or len(series) < 10:
            continue
        worst, _ = reconstruction_error(series, [Archived(s, "") for s in kept])
        ratio = len(series) / len(kept) if kept else 0.0
        bound = tag["comp_dev"]
        ok = worst <= bound + 1e-9
        results.append((tag["name"], ratio, worst, bound, ok))
        print(f"{tag['name']:<20} {len(series):>6} {len(kept):>6} "
              f"{ratio:>7.1f}: {bound:>8.3f} {worst:>10.4f}  "
              f"{'OK' if ok else 'OVER BOUND'}")

    print()
    within = [r for r in results if r[4]]
    print(f"  tags within CompDev        : {len(within)}/{len(results)}")
    print(f"  samples absorbed as repeats: {pipeline.duplicate_ts}")
    if results:
        total_raw = sum(len(dedupe(raw[t["id"]])) for t in tags if t["comp_dev"])
        total_kept = sum(len(archived[t["id"]]) for t in tags if t["comp_dev"])
        print(f"  overall                    : {total_raw:,} raw -> "
              f"{total_kept:,} archived, {total_raw / max(total_kept, 1):.1f}:1")
        best = max(results, key=lambda r: r[1])
        low = min(results, key=lambda r: r[1])
        print(f"  best ratio                 : {best[0]} at {best[1]:.1f}:1")
        print(f"  lowest ratio               : {low[0]} at {low[1]:.1f}:1  "
              f"(correct for a noisy tag)")
    return 0 if results and len(within) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
