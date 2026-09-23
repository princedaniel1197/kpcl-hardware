"""Parser tests, against a real page captured on 2026-09-22.

The fixture is a genuine response from kptclsldc.in, not a hand-written sample.
Tests run without touching the network, which is what makes them usable in CI
and what stops a failing test being blamed on the site being down.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from scraper.parse import (
    QUALITY_GOOD,
    QUALITY_MISSING_ELEMENT,
    QUALITY_NO_DATA,
    QUALITY_UNPARSEABLE,
    PageTimestampError,
    parse,
)

FIXTURE = Path(__file__).parent / "fixtures" / "StateGen_2026-09-22T2110IST.html"
FETCHED_AT = dt.datetime(2026, 9, 22, 15, 52, 32, tzinfo=dt.timezone.utc)


@pytest.fixture
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8", errors="replace")


def test_all_six_stations_are_read(html: str) -> None:
    page = parse(html, FETCHED_AT)
    got = {s.station: s.value for s in page.stations}
    assert got == {
        "RTPS": 1190.0,
        "BTPS": 1321.0,
        "YTPS": 1064.0,        # the station whose ids carry no `lbl` prefix
        "SHARAVATHI": 362.0,
        "VARAHI": 57.0,
        "ALMATTI": 218.0,
    }
    assert all(s.quality == QUALITY_GOOD for s in page.stations)


def test_ytps_is_not_silently_missing(html: str) -> None:
    """YTPS uses ids without the `lbl` prefix every other station has. A
    scraper that assumes the prefix records nothing for it and says nothing."""
    page = parse(html, FETCHED_AT)
    ytps = next(s for s in page.stations if s.station == "YTPS")
    assert ytps.value == 1064.0
    assert ytps.quality == QUALITY_GOOD


def test_source_timestamp_is_the_pages_own_and_is_utc(html: str) -> None:
    page = parse(html, FETCHED_AT)
    assert page.raw_page_timestamp == "22/09/2026 21:10"
    # 21:10 IST is 15:40 UTC.
    assert page.source_ts == dt.datetime(2026, 9, 22, 15, 40, tzinfo=dt.timezone.utc)
    assert page.source_ts.tzinfo is not None


def test_source_and_server_timestamps_are_different_quantities(html: str) -> None:
    """The whole point. On this capture they are twelve and a half minutes
    apart; collapsing them would record a stale page as current data."""
    page = parse(html, FETCHED_AT)
    assert page.source_ts != page.server_ts
    assert page.server_ts == FETCHED_AT
    assert page.page_age == dt.timedelta(minutes=12, seconds=32)


def test_missing_page_timestamp_is_refused_not_substituted(html: str) -> None:
    """With no source timestamp there is no reading. Falling back to the fetch
    time is the failure this test exists to prevent."""
    broken = html.replace('id="lbldate"', 'id="lbldate_gone"')
    with pytest.raises(PageTimestampError):
        parse(broken, FETCHED_AT)


def test_unparseable_page_timestamp_is_refused(html: str) -> None:
    broken = html.replace("22/09/2026 21:10", "not a date")
    with pytest.raises(PageTimestampError):
        parse(broken, FETCHED_AT)


def test_missing_station_element_is_bad_not_zero(html: str) -> None:
    """Zero is a real generation figure — RTPS reads 0 MW on this very page for
    several units — so a parse failure must never become a zero."""
    broken = html.replace('id="lblbtptot"', 'id="lblbtptot_gone"')
    page = parse(broken, FETCHED_AT)
    btps = next(s for s in page.stations if s.station == "BTPS")
    assert btps.value is None
    assert btps.quality == QUALITY_MISSING_ELEMENT
    assert "lblbtptot" in btps.reason
    # every other station is unaffected
    assert all(s.quality == QUALITY_GOOD for s in page.stations if s.station != "BTPS")


def test_blank_station_value_is_bad_no_data(html: str) -> None:
    broken = html.replace('<span id="lblvrhtot">57</span>',
                          '<span id="lblvrhtot"></span>')
    page = parse(broken, FETCHED_AT)
    varahi = next(s for s in page.stations if s.station == "VARAHI")
    assert varahi.value is None
    assert varahi.quality == QUALITY_NO_DATA


def test_non_numeric_station_value_is_bad_decoding(html: str) -> None:
    broken = html.replace('<span id="lblalmttot">218</span>',
                          '<span id="lblalmttot">N/A</span>')
    page = parse(broken, FETCHED_AT)
    almatti = next(s for s in page.stations if s.station == "ALMATTI")
    assert almatti.value is None
    assert almatti.quality == QUALITY_UNPARSEABLE
    assert "'N/A'" in almatti.reason


def test_genuine_zero_is_good_not_bad(html: str) -> None:
    """A station that is genuinely shut down reads 0 MW with Good quality, and
    must stay distinguishable from a station we failed to read."""
    zeroed = html.replace('<span id="lblrtptot">1190</span>',
                          '<span id="lblrtptot">0</span>')
    page = parse(zeroed, FETCHED_AT)
    rtps = next(s for s in page.stations if s.station == "RTPS")
    assert rtps.value == 0.0
    assert rtps.quality == QUALITY_GOOD
    assert rtps.reason is None


def test_system_values(html: str) -> None:
    page = parse(html, FETCHED_AT)
    assert page.frequency.value == 50.05
    assert page.frequency.quality == QUALITY_GOOD
    assert page.state_gen.value == 5193.0
    assert page.total_gen.value == 11094.0


def test_naive_server_ts_is_rejected(html: str) -> None:
    with pytest.raises(ValueError):
        parse(html, dt.datetime(2026, 9, 22, 15, 52, 32))


# --- per-unit generation -----------------------------------------------------

EXPECTED_UNITS = {
    "RTPS": [0.0] * 8,
    "BTPS": [373.0, 371.0, 582.0],
    "YTPS": [590.0, 487.0],
    "SHARAVATHI": [0.0, 25.0, 80.0, 25.0, 24.0, 25.0, 30.0, 25.0, 39.0, 84.0],
    "VARAHI": [14.0, 13.0, 14.0, 15.0],
    "ALMATTI": [0.0, 31.0, 42.0, 48.0, 46.0, 48.0],
}


def test_every_unit_is_read(html: str) -> None:
    """33 units across the six stations, each by its UNIT n column."""
    page = parse(html, FETCHED_AT)
    got: dict[str, list[float | None]] = {}
    for u in page.units:
        got.setdefault(u.station, []).append(u.value)
        assert u.quality == QUALITY_GOOD
    assert got == EXPECTED_UNITS
    assert len(page.units) == 33
    assert [u.unit for u in page.units if u.station == "SHARAVATHI"] == list(range(1, 11))


def test_ytps_units_are_not_silently_missing(html: str) -> None:
    page = parse(html, FETCHED_AT)
    assert [(u.unit, u.value) for u in page.units if u.station == "YTPS"] == [
        (1, 590.0), (2, 487.0)]


def test_missing_unit_element_is_bad_not_zero(html: str) -> None:
    broken = html.replace('id="lblbtp2"', 'id="lblbtp2_gone"')
    page = parse(broken, FETCHED_AT)
    u2 = next(u for u in page.units if u.station == "BTPS" and u.unit == 2)
    assert u2.value is None
    assert u2.quality == QUALITY_MISSING_ELEMENT
    assert "lblbtp2" in u2.reason and "BTPS unit 2" in u2.reason
    # the station total and the other units are unaffected
    assert next(s for s in page.stations if s.station == "BTPS").quality == QUALITY_GOOD
    assert all(u.is_good for u in page.units if not (u.station == "BTPS" and u.unit == 2))


def test_non_numeric_unit_is_bad_decoding(html: str) -> None:
    broken = html.replace('<span id="ytp1">590</span>', '<span id="ytp1">--</span>')
    page = parse(broken, FETCHED_AT)
    u1 = next(u for u in page.units if u.station == "YTPS" and u.unit == 1)
    assert u1.value is None
    assert u1.quality == QUALITY_UNPARSEABLE


def test_unit_zero_is_good(html: str) -> None:
    """RTPS reads 0 MW on every unit of this page. Those are Good zeros."""
    page = parse(html, FETCHED_AT)
    rtps = [u for u in page.units if u.station == "RTPS"]
    assert all(u.value == 0.0 and u.quality == QUALITY_GOOD and u.reason is None
               for u in rtps)
