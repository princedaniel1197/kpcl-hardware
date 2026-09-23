"""Show that T-12 can fail.

T-12's criterion is at most 0.5 % of one core of source CPU per subscribed tag,
and scan work P95 within 10 % (or 1 ms) of the source's figure with no client.
This runs the same measurement -- the same source-side CPU accounting, the same
scan timing -- against a client that behaves the way T-12 exists to catch: it
POLLS every Unit 1 tag as fast as the server answers, with ordinary OPC UA
reads. It writes nothing.

If the criterion is real, this fails it. The result is recorded in
fat/records/stage-14-fat.md.

    python -m fat.intrusion_demo
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading

from asyncua import Client

from fat import tests as T

ENDPOINT = "opc.tcp://127.0.0.1:4840/orianode/crpms/"
NAMESPACE = "urn:orianode:crpms:sim"
TAGS = ["U1_MW", "U1_TURB_SPEED", "U1_DRUM_PRESS", "U1_MS_TEMP", "U1_MS_PRESS",
        "U1_FEEDWATER_FLOW", "U1_COAL_FLOW", "U1_AUX_POWER", "U1_CONDENSER_VAC",
        "U1_GEN_STATOR_TEMP", "U1_BEARING_VIB", "U1_BOILER_LIGHTUP",
        "U1_TURB_ROLLING", "U1_BREAKER_CLOSED"]


class Poller:
    """Reads every tag, in a loop, from a thread of its own."""

    def __init__(self) -> None:
        self.reads = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._run()),
                                        daemon=True)

    async def _run(self) -> None:
        client = Client(ENDPOINT)
        await client.connect()
        try:
            idx = await client.get_namespace_index(NAMESPACE)
            nodes = [await client.nodes.objects.get_child(
                [f"{idx}:Unit1", f"{idx}:{t}"]) for t in TAGS]
            while not self._stop.is_set():
                await client.read_values(nodes)
                self.reads += len(nodes)
        finally:
            await client.disconnect()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=30.0)
    args = ap.parse_args()
    logging.getLogger("asyncua").setLevel(logging.ERROR)

    sim_pid = T._pids("Python -m sim ")[0]
    baseline = T._source_window(sim_pid, args.seconds)
    poller = Poller()
    poller.start()
    try:
        loaded = T._source_window(sim_pid, args.seconds)
    finally:
        poller.stop()

    per_tag = (loaded["cpu_pct"] - baseline["cpu_pct"]) / len(TAGS)
    allowed = max(baseline["work_p95"] * (1 + T.NI_SCAN_P95_TOLERANCE),
                  baseline["work_p95"] + T.NI_SCAN_P95_FLOOR_MS)
    print(f"polling client: {poller.reads / args.seconds:,.0f} reads/s over "
          f"{len(TAGS)} tags")
    print(f"source CPU without the poller: {baseline['cpu_pct']:.2f}%   "
          f"with: {loaded['cpu_pct']:.2f}%")
    print(f"attributable per tag: {per_tag:.3f}% of one core "
          f"(T-12 budget {T.NI_CPU_PER_TAG_PCT}%)")
    print(f"scan work P95: without {baseline['work_p95']:.3f} ms, with "
          f"{loaded['work_p95']:.3f} ms (T-12 allows {allowed:.3f} ms)")
    print(f"scan start lateness P95: without {baseline['late_p95']:.3f} ms, "
          f"with {loaded['late_p95']:.3f} ms")
    fails = per_tag > T.NI_CPU_PER_TAG_PCT or loaded["work_p95"] > allowed
    print(f"\nT-12's criterion would {'FAIL' if fails else 'PASS'} this client")
    return 0 if fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
