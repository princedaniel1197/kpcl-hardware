"""Stage 4 acceptance measurement: compression against real acquired data.

Subscribes to the live simulator at full scan rate with NO deadband, so what is
compressed is everything the DCS produced rather than something already thinned
at acquisition. Then applies each tag's configured ExcDev and CompDev and
reports the ratio and the worst reconstruction error.

The ratios are the point. A slow, smooth tag compresses hard. A noisy one does
not, and reporting a high ratio for it would mean discarding real signal --
which is why the noisy tag's low ratio is a correct result rather than a
disappointing one.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time

import psycopg

from collector.compression import compress, reconstruction_error
from collector.model import Sample
from collector.session import ReadOnlySession

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
ENDPOINT = os.environ.get("SIM_OPCUA_ENDPOINT",
                          "opc.tcp://127.0.0.1:4840/orianode/crpms/")


async def collect(seconds: float, tags: list[dict]) -> dict[str, list[Sample]]:
    captured: dict[str, list[Sample]] = {t["name"]: [] for t in tags}

    def on_sample(sample: Sample) -> None:
        captured[sample.tag_name].append(sample)

    # Full rate, no deadband: exc_dev is stripped so the subscription reports
    # every change the server produces.
    raw = [{**t, "exc_dev": None} for t in tags]
    session = ReadOnlySession(ENDPOINT, on_sample)
    await session.connect(raw)
    print(f"capturing {seconds:.0f}s at full rate from {len(session.subscribed)} tags ...")
    await asyncio.sleep(seconds)
    await session.disconnect()
    return captured


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=180.0)
    args = ap.parse_args()

    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, scan_rate_ms, exc_dev, comp_dev, max_time_ms "
                    "FROM tag WHERE source_system='opcua' ORDER BY name")
        tags = [{"id": r[0], "name": r[1], "scan_rate_ms": r[2], "exc_dev": r[3],
                 "comp_dev": r[4], "max_time_ms": r[5]} for r in cur.fetchall()]

    captured = asyncio.run(collect(args.seconds, tags))

    print(f"\n{'tag':<20} {'raw':>6} {'arch':>6} {'ratio':>8} "
          f"{'CompDev':>8} {'worst err':>10} {'bound':>8}  verdict")
    print("-" * 88)
    results = []
    for tag in tags:
        raw = captured[tag["name"]]
        if len(raw) < 10:
            print(f"{tag['name']:<20} {len(raw):>6}   (too few samples to judge)")
            continue
        exc, comp = tag["exc_dev"], tag["comp_dev"]
        archived, stats = compress(raw, exc, comp, tag["max_time_ms"])
        worst, _ = reconstruction_error(raw, archived)
        # The strict claim: CompDev end to end against the RAW series, not
        # ExcDev+CompDev. A lenient bound here hid a real violation.
        bound = comp or 0.0
        ok = worst <= bound + 1e-9
        results.append((tag["name"], stats.ratio, worst, bound, ok))
        print(f"{tag['name']:<20} {stats.received:>6} {stats.archived:>6} "
              f"{stats.ratio:>7.1f}: {comp if comp else 0:>8.3f} "
              f"{worst:>10.4f} {bound:>8.3f}  {'OK' if ok else 'OVER BOUND'}")

    print()
    within = [r for r in results if r[4]]
    print(f"  tags within CompDev        : {len(within)}/{len(results)}")
    if results:
        best = max(results, key=lambda r: r[1])
        worst_ratio = min(results, key=lambda r: r[1])
        print(f"  best ratio                 : {best[0]} at {best[1]:.1f}:1")
        print(f"  lowest ratio               : {worst_ratio[0]} at "
              f"{worst_ratio[1]:.1f}:1  (correct for a noisy tag)")
    return 0 if len(within) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
