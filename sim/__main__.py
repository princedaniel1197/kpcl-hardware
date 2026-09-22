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


async def _run(sim: SimulatorServer, host: str, port: int,
               modbus_host: str | None = None, modbus_port: int = 502,
               modbus_unit: int = 1) -> None:
    await sim.init()

    # Stage 10: the bench rig is republished into THIS server's address space,
    # so the collector needs no change -- it already subscribes to whatever the
    # tag table lists.
    bridge = None
    if modbus_host:
        from sim.bridge import ModbusBridge
        bridge = ModbusBridge(sim._server, sim.namespace_index, modbus_host,
                              modbus_port, modbus_unit)
        await bridge.build_address_space()
    config = uvicorn.Config(build_app(sim), host=host, port=port,
                            log_level="warning", access_log=False)
    control = uvicorn.Server(config)
    logging.getLogger("sim").info("control API on http://%s:%d/docs", host, port)
    tasks = [sim.run(), control.serve()]
    if bridge is not None:
        tasks.append(bridge.run())
    await asyncio.gather(*tasks)


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
    ap.add_argument("--modbus-host", default=os.environ.get("RIG_MODBUS_HOST"),
                    help="bench rig Modbus TCP host; enables the Stage 10 bridge")
    ap.add_argument("--modbus-port", type=int,
                    default=int(os.environ.get("RIG_MODBUS_PORT", "502")))
    ap.add_argument("--modbus-unit", type=int, default=1)
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
        asyncio.run(_run(sim, args.control_host, args.control_port,
                         args.modbus_host, args.modbus_port, args.modbus_unit))
    except KeyboardInterrupt:
        logging.getLogger("sim").info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
