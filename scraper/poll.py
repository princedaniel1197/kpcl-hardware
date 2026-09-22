"""The polling loop.

The schedule is absolute, not relative. Tick n happens at start + n*interval,
computed from a fixed origin, so a slow fetch, a retry, or a page that is down
for twenty minutes cannot make the schedule drift. A loop that sleeps for the
interval *after* finishing its work slowly walks away from the clock, and the
walk is invisible until you compare two days of data.

Retries happen inside a tick and are bounded by the tick: if the page cannot be
fetched before the next tick is due, this tick is abandoned and logged, and the
next one starts on time. We never queue up retries that would fire late.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import http.client
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import psycopg

from scraper import store
from scraper.parse import PageTimestampError, parse

log = logging.getLogger("scraper")

URL = "https://kptclsldc.in/StateGen.aspx"

# Identifies the client honestly rather than pretending to be a browser.
# Polling a public page once a minute is modest, but it should be attributable.
#
# The site does NOT block automated clients: an empty User-Agent, `curl/8.0` and
# our own name all return 200. It does have a crude filter that resets the
# connection on the single token "recorder" — `scraper/0.1` is served, while
# `recorder/0.1` is not. Measured against the live site on 2026-09-22. So this
# string avoids that one word and is otherwise entirely truthful. Nothing here
# is spoofed and no access control is being worked around; if that ever changes,
# the answer is to ask KPTCL for a feed, not to disguise the client.
USER_AGENT = (
    "orianode-crpms-sldc/0.1 "
    "(Karnataka SLDC generation logger; contact: princedanieljj@gmail.com)"
)


@dataclass(frozen=True)
class PollerConfig:
    url: str = URL
    interval_s: float = 60.0
    http_timeout_s: float = 20.0
    retry_backoff_s: tuple[float, ...] = (2.0, 5.0, 10.0)
    daily_report_hour_ist: int = 0


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _fetch_blocking(url: str, timeout: float) -> tuple[str, int]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace"), response.status
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} {exc.reason}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FetchError(f"timed out after {timeout}s") from exc
    except http.client.HTTPException as exc:
        # RemoteDisconnected and friends are NOT wrapped in URLError on every
        # path. Caught here because an uncaught one kills the recorder outright
        # instead of being retried — observed against this very site.
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    except OSError as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc


async def fetch(url: str, timeout: float) -> tuple[str, int]:
    """Fetch the page off the event loop. urllib is used rather than adding an
    async HTTP client: the dependency set is the one the build plan names."""
    return await asyncio.to_thread(_fetch_blocking, url, timeout)


async def poll_once(
    archive: store.Archive,
    config: PollerConfig,
    deadline: float,
) -> int:
    """One tick: fetch, parse, store. Retries until `deadline` (a monotonic
    clock reading), then gives up so the next tick is not delayed.

    Returns the number of generation rows inserted.
    """
    attempt_ts = dt.datetime.now(dt.timezone.utc)
    started = time.monotonic()
    last_error: str = "no attempt made"
    status: int | None = None

    for index, backoff in enumerate((0.0, *config.retry_backoff_s)):
        if backoff:
            remaining = deadline - time.monotonic()
            if remaining <= backoff:
                break
            await asyncio.sleep(backoff)

        try:
            html, status = await fetch(config.url, config.http_timeout_s)
        except FetchError as exc:
            last_error = str(exc)
            log.warning("fetch failed (attempt %d): %s", index + 1, last_error)
            continue

        # server_ts is when the page arrived. It is never used as source_ts.
        server_ts = dt.datetime.now(dt.timezone.utc)
        try:
            page = parse(html, server_ts)
        except PageTimestampError as exc:
            # No source timestamp means no reading. We do not fall back to the
            # fetch time; that would record a stale page as current data.
            duration = int((time.monotonic() - started) * 1000)
            await store.log_attempt(
                archive, attempt_ts=attempt_ts, outcome="parse_error",
                http_status=status, duration_ms=duration, detail=str(exc))
            log.error("page carried no usable timestamp: %s", exc)
            return 0

        try:
            rows = await store.write_reading(archive, page)
        except psycopg.Error as exc:
            # write_reading has already discarded the broken connection;
            # log_attempt is best-effort and reconnects if it can.
            duration = int((time.monotonic() - started) * 1000)
            await store.log_attempt(
                archive, attempt_ts=attempt_ts, outcome="db_error",
                http_status=status, page_source_ts=page.source_ts,
                duration_ms=duration, detail=str(exc))
            log.error("database write failed: %s", exc)
            return 0

        duration = int((time.monotonic() - started) * 1000)
        await store.log_attempt(
            archive, attempt_ts=attempt_ts, outcome="ok", http_status=status,
            rows_written=rows, page_source_ts=page.source_ts,
            duration_ms=duration,
            detail=f"page age {page.page_age.total_seconds():.0f}s")

        bad = [s for s in page.stations if not s.is_good]
        log.info(
            "ok: page %s (age %.0fs), %d new rows, %d/%d stations good%s",
            page.raw_page_timestamp, page.page_age.total_seconds(), rows,
            len(page.stations) - len(bad), len(page.stations),
            "" if not bad else "; BAD: " + ", ".join(f"{s.station} ({s.reason})" for s in bad),
        )
        return rows

    duration = int((time.monotonic() - started) * 1000)
    await store.log_attempt(
        archive, attempt_ts=attempt_ts, outcome="http_error", http_status=status,
        duration_ms=duration, detail=last_error)
    log.error("tick abandoned after retries: %s", last_error)
    return 0


async def report_daily(archive: store.Archive) -> None:
    """Log yesterday's and today's row counts, so a glance at the log says
    whether the recorder is alive and how much it captured."""
    try:
        rows = await store.daily_counts(archive, days=2)
    except psycopg.Error as exc:
        await archive.discard()
        log.warning("daily report unavailable: %s", exc)
        return
    for row in rows:
        ist_date, rows, good, readings, first, last = row
        log.info(
            "DAILY %s: %d rows (%d good) from %d distinct page timestamps, "
            "%s to %s",
            ist_date, rows, good, readings, first, last,
        )


async def run(config: PollerConfig | None = None) -> None:
    """Poll forever on a schedule that does not drift."""
    config = config or PollerConfig()
    log.info("recorder starting: %s every %.0fs", config.url, config.interval_s)

    archive = store.Archive()
    try:
        origin = time.monotonic()
        tick = 0
        last_report_date: dt.date | None = None

        while True:
            # Absolute schedule: tick n is due at origin + n*interval.
            due = origin + tick * config.interval_s
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            elif tick and delay < -config.interval_s:
                # We fell a whole interval behind; skip to the next due tick
                # rather than firing a burst to catch up.
                missed = int(-delay // config.interval_s)
                log.warning("running %d ticks behind; skipping ahead", missed)
                tick += missed

            next_due = origin + (tick + 1) * config.interval_s
            # A tick must never be able to kill the recorder. Anything that
            # escapes poll_once is logged and the schedule continues; the
            # database coming back is then just the next tick succeeding.
            try:
                await poll_once(archive, config, deadline=next_due)
            except Exception:
                log.exception("tick failed; continuing on schedule")
                await archive.discard()

            today_ist = dt.datetime.now(dt.timezone.utc).astimezone(
                dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
            if last_report_date != today_ist:
                await report_daily(archive)
                last_report_date = today_ist

            tick += 1
    finally:
        await archive.close()
