"""Store tests: per-unit rows reach the archive, and the unit-sum check judges
them honestly. Against the real database; skipped if it is not reachable.

write_reading commits, so these cannot run inside a rolled-back transaction.
They use a page timestamp in 2001 — long before the recorder existed, so it
cannot collide with a real reading — and delete what they wrote.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import psycopg
import pytest

from scraper import store
from scraper.parse import QUALITY_GOOD, QUALITY_UNPARSEABLE, parse

FIXTURE = Path(__file__).parent / "fixtures" / "StateGen_2026-09-22T2110IST.html"
PAGE_TS_RAW = "01/01/2001 05:30"          # 00:00 UTC
SOURCE_TS = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
FETCHED_AT = dt.datetime(2001, 1, 1, 0, 7, 12, tzinfo=dt.timezone.utc)
TABLES = ("sldc_unit_generation", "sldc_generation", "sldc_system")


def _page(html: str):
    return parse(html.replace("22/09/2026 21:10", PAGE_TS_RAW), FETCHED_AT)


@pytest.fixture
async def archive():
    a = store.Archive()
    try:
        await a.connection()
    except psycopg.OperationalError as exc:
        pytest.skip(f"archive not reachable: {exc}")
    await _cleanup(a)
    try:
        yield a
    finally:
        await _cleanup(a)
        await a.close()


async def _cleanup(a: store.Archive) -> None:
    conn = await a.connection()
    async with conn.cursor() as cur:
        for table in TABLES:
            await cur.execute(f"DELETE FROM {table} WHERE source_ts = %s", (SOURCE_TS,))
    await conn.commit()


async def _fetch(a: store.Archive, sql: str, *params):
    conn = await a.connection()
    async with conn.cursor() as cur:
        await cur.execute(sql, params)
        rows = await cur.fetchall()
    await conn.commit()
    return rows


@pytest.fixture
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8", errors="replace")


async def test_units_are_stored_with_both_timestamps(archive, html):
    written = await store.write_reading(archive, _page(html))
    assert (written.stations, written.units) == (6, 33)

    rows = await _fetch(archive, """
        SELECT station, unit, source_ts, server_ts, generation_mw, quality
        FROM sldc_unit_generation WHERE source_ts = %s ORDER BY station, unit""",
        SOURCE_TS)
    assert len(rows) == 33
    assert all(r[2] == SOURCE_TS and r[3] == FETCHED_AT for r in rows)
    assert ("YTPS", 2, SOURCE_TS, FETCHED_AT, 487.0, QUALITY_GOOD) in rows


async def test_rerecording_a_page_inserts_nothing(archive, html):
    await store.write_reading(archive, _page(html))
    again = await store.write_reading(archive, _page(html))
    assert again.total == 0


async def test_consistency_verdicts_on_the_captured_page(archive, html):
    """RTPS published 1190 MW with every unit at 0 MW: inconsistent. The other
    five differ from their unit sums by 1 to 13 MW: consistent."""
    await store.write_reading(archive, _page(html))
    rows = dict((s, (v, d)) for s, v, d in await _fetch(archive, """
        SELECT station, verdict, difference_mw FROM sldc_unit_consistency
        WHERE source_ts = %s""", SOURCE_TS))
    assert rows["RTPS"] == ("inconsistent", -1190.0)
    assert rows["BTPS"] == ("consistent", 5.0)
    assert rows["YTPS"] == ("consistent", 13.0)
    assert {s for s, (v, _) in rows.items() if v == "consistent"} == {
        "BTPS", "YTPS", "SHARAVATHI", "VARAHI", "ALMATTI"}


async def test_a_bad_unit_makes_the_check_incomplete_not_zero(archive, html):
    """One unreadable Varahi unit. Treating it as 0 MW would make the sum 42
    against a total of 57 — and flag an inconsistency the page never had."""
    broken = html.replace('<span id="lblvrh2">13</span>', '<span id="lblvrh2">x</span>')
    page = _page(broken)
    assert next(u for u in page.units
                if u.station == "VARAHI" and u.unit == 2).quality == QUALITY_UNPARSEABLE
    await store.write_reading(archive, page)
    (verdict, unit_sum, diff), = await _fetch(archive, """
        SELECT verdict, unit_sum_mw, difference_mw FROM sldc_unit_consistency
        WHERE station = 'VARAHI' AND source_ts = %s""", SOURCE_TS)
    assert verdict == "incomplete"
    assert unit_sum is None and diff is None


async def test_schema_refuses_a_bad_unit_with_a_number(archive):
    conn = await archive.connection()
    with pytest.raises(psycopg.errors.CheckViolation):
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO sldc_unit_generation
                    (station, unit, source_ts, server_ts, generation_mw, quality)
                VALUES ('VARAHI', 1, %s, %s, 0.0, %s)""",
                (SOURCE_TS, FETCHED_AT, QUALITY_UNPARSEABLE))
    await conn.rollback()
