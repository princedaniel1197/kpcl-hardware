"""Parse the KPTCL SLDC StateGen page into readings.

Two rules from CLAUDE.md govern everything here.

Measurement time is not arrival time (§335). The page publishes its own
timestamp, and that is the source timestamp. The moment we fetched the page is
a different quantity and is carried separately. Observed on 2026-09-22, the two
differed by about twelve minutes. If the page timestamp cannot be read, the
reading has no source timestamp and is rejected outright — substituting the
fetch time would silently turn a stale page into fresh data.

Never silently substitute (§318). A station whose value is absent, blank or
non-numeric is recorded as Bad with a reason naming what was wrong. It is not
recorded as zero. Zero is a real generation figure — several of these stations
legitimately read 0 MW — so a parse failure that becomes a zero is indis-
tinguishable from a station that is genuinely shut down.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from asyncua import ua

from scraper import stations as st

# Quality is the numeric OPC UA StatusCode throughout this project, never a
# boolean or a string.
QUALITY_GOOD = int(ua.StatusCodes.Good)
QUALITY_NO_DATA = int(ua.StatusCodes.BadNoData)                # element blank
QUALITY_MISSING_ELEMENT = int(ua.StatusCodes.BadNodeIdUnknown)  # id not on page
QUALITY_UNPARSEABLE = int(ua.StatusCodes.BadDecodingError)      # not a number

IST = ZoneInfo(st.PAGE_TIMEZONE)


class PageTimestampError(ValueError):
    """The page carried no readable timestamp, so the reading has no source
    time. Callers must not fall back to the fetch time."""


class _IdTextParser(HTMLParser):
    """Collect the text of every element carrying an id."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[str] = []
        self.text: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element_id = dict(attrs).get("id")
        if element_id:
            self._stack.append(element_id)
            self.text.setdefault(element_id, "")

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        chunk = data.strip()
        if chunk and self._stack:
            self.text[self._stack[-1]] += chunk


@dataclass(frozen=True)
class Reading:
    """One measured quantity, with the quality that goes with it."""

    value: float | None
    quality: int
    reason: str | None = None

    @property
    def is_good(self) -> bool:
        return self.quality == QUALITY_GOOD


@dataclass(frozen=True)
class StationReading(Reading):
    station: str = ""


@dataclass(frozen=True)
class PageReading:
    """Everything one fetch of the page yielded."""

    source_ts: dt.datetime            # the page's own timestamp, in UTC
    server_ts: dt.datetime            # when we fetched it, in UTC
    raw_page_timestamp: str           # exactly as printed, for the record
    frequency: Reading
    state_gen: Reading
    total_gen: Reading
    stations: tuple[StationReading, ...]

    @property
    def page_age(self) -> dt.timedelta:
        """How far behind the fetch the page's own timestamp was. Reported, not
        corrected."""
        return self.server_ts - self.source_ts


def _number(text: dict[str, str], element_id: str, label: str) -> Reading:
    """Read one numeric element, or say precisely why it could not be read."""
    if element_id not in text:
        return Reading(None, QUALITY_MISSING_ELEMENT,
                       f"{label}: element id '{element_id}' not present on page")
    raw = text[element_id].strip()
    if not raw:
        return Reading(None, QUALITY_NO_DATA,
                       f"{label}: element id '{element_id}' was empty")
    cleaned = raw.replace(",", "")
    try:
        return Reading(float(cleaned), QUALITY_GOOD)
    except ValueError:
        return Reading(None, QUALITY_UNPARSEABLE,
                       f"{label}: element id '{element_id}' held {raw!r}, not a number")


def parse_page_timestamp(text: dict[str, str]) -> tuple[dt.datetime, str]:
    """Return the page's own timestamp as UTC, plus the raw string.

    Raises PageTimestampError rather than returning a fallback: a reading
    without a source timestamp is not a reading.
    """
    raw = text.get(st.PAGE_TIMESTAMP_ID, "").strip()
    if not raw:
        raise PageTimestampError(
            f"element id '{st.PAGE_TIMESTAMP_ID}' absent or empty; the page "
            "published no timestamp"
        )
    try:
        naive = dt.datetime.strptime(raw, st.PAGE_TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise PageTimestampError(
            f"page timestamp {raw!r} does not match "
            f"{st.PAGE_TIMESTAMP_FORMAT!r}: {exc}"
        ) from exc
    # The page prints IST with no zone marker. Attaching the zone explicitly is
    # the only way the stored instant is unambiguous.
    return naive.replace(tzinfo=IST).astimezone(dt.timezone.utc), raw


def parse(html: str, server_ts: dt.datetime) -> PageReading:
    """Parse a fetched StateGen page. `server_ts` is when it was fetched."""
    if server_ts.tzinfo is None:
        raise ValueError("server_ts must be timezone-aware")

    parser = _IdTextParser()
    parser.feed(html)
    text = parser.text

    source_ts, raw = parse_page_timestamp(text)

    station_readings = tuple(
        StationReading(
            value=(r := _number(text, spec.total_id, spec.code)).value,
            quality=r.quality,
            reason=r.reason,
            station=spec.code,
        )
        for spec in st.STATIONS
    )

    return PageReading(
        source_ts=source_ts,
        server_ts=server_ts.astimezone(dt.timezone.utc),
        raw_page_timestamp=raw,
        frequency=_number(text, st.FREQUENCY_ID, "system frequency"),
        state_gen=_number(text, st.STATE_GEN_ID, "state generation"),
        total_gen=_number(text, st.TOTAL_GEN_ID, "total generation"),
        stations=station_readings,
    )
