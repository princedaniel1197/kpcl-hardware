"""The measurement behind config/sources.json: deadband at the source, on and off.

Two read-only sessions against the running simulator, subscribed to the same
Unit 1 tags at the same rates. One applies each tag's ExcDev as an OPC UA
DataChangeFilter at the source (Trigger = StatusValue, absolute deadband); the
other applies none. For a fixed window each counts what it receives; then one
tag is forced to BadDeviceFailure through the simulator's control API and each
counts the Bad notifications it is sent.

What it shows, on asyncua 2.0.1: the deadband cuts the traffic from the source,
which is what it is for (§317) -- and hides the tag going Bad, which a
StatusValue trigger must not do (Part 4). So for this server the setting is
off, and the cost is measured rather than assumed.

    python -m collector.deadband_evidence
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import urllib.request

import psycopg

from collector.model import Sample
from collector.session import ReadOnlySession

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
ENDPOINT = os.environ.get("SIM_OPCUA_ENDPOINT",
                          "opc.tcp://127.0.0.1:4840/orianode/crpms/")
CONTROL = os.environ.get("SIM_CONTROL", "http://127.0.0.1:8081")
FORCED = "U1_MS_PRESS"


def control(method: str, path: str, body: dict | None = None) -> None:
    data = json.dumps(body).encode() if body else None
    request = urllib.request.Request(
        CONTROL + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    urllib.request.urlopen(request, timeout=5).read()


async def run(window_s: float, forced_s: float) -> int:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT id, name, scan_rate_ms, exc_dev, comp_dev, max_time_ms,"
                    " source_path FROM tag WHERE source_system='opcua'"
                    " AND source_path='Unit1' ORDER BY name")
        tags = [{"id": r[0], "name": r[1], "scan_rate_ms": r[2], "exc_dev": r[3],
                 "comp_dev": r[4], "max_time_ms": r[5], "source_path": r[6]}
                for r in cur.fetchall()]

    received = {"on": [], "off": []}
    sessions = {
        "on": ReadOnlySession(ENDPOINT, received["on"].append, source_deadband=True),
        "off": ReadOnlySession(ENDPOINT, received["off"].append, source_deadband=False),
    }
    control("DELETE", "/quality")
    for session in sessions.values():
        await session.connect(tags)
    try:
        await asyncio.sleep(2.0)                  # initial values
        for series in received.values():
            series.clear()
        await asyncio.sleep(window_s)
        rates = {k: len(v) / window_s for k, v in received.items()}

        for series in received.values():
            series.clear()
        control("POST", f"/quality/{FORCED}", {"quality": "BadDeviceFailure"})
        await asyncio.sleep(forced_s)
    finally:
        control("DELETE", "/quality")
        for session in sessions.values():
            await session.disconnect()

    def bad(series: list[Sample]) -> int:
        return sum(1 for s in series
                   if s.tag_name == FORCED and (s.quality >> 30) & 3 == 2)

    print(f"server: {sessions['off'].server_uri}")
    print(f"{len(tags)} Unit 1 tags, each at its configured rate\n")
    print(f"  {'':34} {'deadband ON':>12} {'deadband OFF':>13}")
    print(f"  {'notifications per second':34} {rates['on']:>12.1f} "
          f"{rates['off']:>13.1f}")
    print(f"  {'Bad notifications for ' + FORCED:34} {bad(received['on']):>12} "
          f"{bad(received['off']):>13}")
    print(f"\n  window {window_s:.0f} s; {FORCED} forced BadDeviceFailure for "
          f"{forced_s:.0f} s")
    if rates["on"]:
        print(f"  cost of the deadband being off: {rates['off'] / rates['on']:.1f}x "
              f"the notifications")
    hidden = bad(received["on"]) == 0 and bad(received["off"]) > 0
    print(f"\n  the source deadband hid the tag going Bad: {hidden}")
    return 0 if hidden else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=float, default=30.0)
    ap.add_argument("--forced", type=float, default=5.0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("asyncua").setLevel(logging.ERROR)
    return asyncio.run(run(args.window, args.forced))


if __name__ == "__main__":
    raise SystemExit(main())
