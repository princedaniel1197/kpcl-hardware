"""Entry point: python -m scraper"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from scraper.poll import URL, PollerConfig, run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scraper",
                                 description="Karnataka SLDC generation recorder")
    ap.add_argument("--url", default=os.environ.get("SLDC_URL", URL))
    ap.add_argument("--interval", type=float,
                    default=float(os.environ.get("SLDC_INTERVAL_S", "60")))
    ap.add_argument("--timeout", type=float,
                    default=float(os.environ.get("SLDC_TIMEOUT_S", "20")))
    ap.add_argument("--once", action="store_true",
                    help="poll a single time and exit (for checking setup)")
    ap.add_argument("--log-level", default=os.environ.get("SLDC_LOG_LEVEL", "INFO"))
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # Log in UTC. The page publishes IST; conflating the two in the log is how
    # a twelve-minute discrepancy becomes invisible.
    logging.Formatter.converter = __import__("time").gmtime

    config = PollerConfig(url=args.url, interval_s=args.interval,
                          http_timeout_s=args.timeout)

    if args.once:
        import psycopg
        from scraper import store
        from scraper.poll import poll_once
        import time as _time

        async def one() -> int:
            async with await psycopg.AsyncConnection.connect(store.dsn()) as conn:
                rows = await poll_once(conn, config,
                                       deadline=_time.monotonic() + config.interval_s)
                await __import__("scraper.poll", fromlist=["report_daily"]).report_daily(conn)
                return rows

        asyncio.run(one())
        return 0

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logging.getLogger("scraper").info("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
