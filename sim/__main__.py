"""Run the DCS simulator: python -m sim"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

import uvicorn

from sim.control import build_app
from sim.server import DEFAULT_ENDPOINT, SimulatorServer


async def _run(sim: SimulatorServer, host: str, port: int) -> None:
    await sim.init()
    config = uvicorn.Config(build_app(sim), host=host, port=port,
                            log_level="warning", access_log=False)
    control = uvicorn.Server(config)
    logging.getLogger("sim").info("control API on http://%s:%d/docs", host, port)
    await asyncio.gather(sim.run(), control.serve())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sim", description="CRPMS DCS simulator")
    ap.add_argument("--endpoint", default=os.environ.get("SIM_OPCUA_ENDPOINT",
                                                         DEFAULT_ENDPOINT))
    ap.add_argument("--startup-seconds", type=float,
                    default=float(os.environ.get("SIM_STARTUP_SECONDS", "90")),
                    help="90 for a demonstration, 10800 for realism")
    ap.add_argument("--scan-ms", type=float,
                    default=float(os.environ.get("SIM_SCAN_MS", "500")))
    ap.add_argument("--field-latency-ms", type=float,
                    default=float(os.environ.get("SIM_FIELD_LATENCY_MS", "40")))
    ap.add_argument("--control-host", default=os.environ.get("SIM_CONTROL_HOST",
                                                             "127.0.0.1"))
    ap.add_argument("--control-port", type=int,
                    default=int(os.environ.get("SIM_CONTROL_PORT", "8081")))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--log-level", default=os.environ.get("SIM_LOG_LEVEL", "INFO"))
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )
    logging.Formatter.converter = time.gmtime   # log in UTC, like the data

    sim = SimulatorServer(
        endpoint=args.endpoint,
        startup_seconds=args.startup_seconds,
        scan_interval_ms=args.scan_ms,
        field_latency_ms=args.field_latency_ms,
        seed=args.seed,
    )
    try:
        asyncio.run(_run(sim, args.control_host, args.control_port))
    except KeyboardInterrupt:
        logging.getLogger("sim").info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
