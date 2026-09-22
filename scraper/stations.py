"""Mapping from KPCL/KPTCL SLDC page element ids to station identities.

The page at kptclsldc.in/StateGen.aspx renders each station's generation into
span elements with fixed ids. The ids are not uniformly named: most carry an
`lbl` prefix, YTPS does not. A scraper that assumes the prefix silently records
nothing for Yermarus and never says so, which is the specific failure this
module exists to prevent.

Station codes here are the ones used in the rest of this project, so that a
scraped station and a modelled element can eventually refer to the same asset.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StationSpec:
    code: str          # our identifier, stable, used as the table key
    name: str          # full name as commissioned
    total_id: str      # page element id holding station total generation, MW
    unit_ids: tuple[str, ...]   # per-unit element ids, in page order
    fuel: str


STATIONS: tuple[StationSpec, ...] = (
    StationSpec(
        "RTPS", "Raichur Thermal Power Station",
        "lblrtptot",
        tuple(f"lblrtp{i}" for i in range(1, 9)),
        "coal",
    ),
    StationSpec(
        "BTPS", "Bellary Thermal Power Station",
        "lblbtptot",
        tuple(f"lblbtp{i}" for i in range(1, 4)),
        "coal",
    ),
    StationSpec(
        # NOTE: no `lbl` prefix on this station's ids. Verified against the page
        # on 2026-09-22; see scraper/fixtures/.
        "YTPS", "Yermarus Thermal Power Station",
        "ytptot",
        ("ytp1", "ytp2"),
        "coal",
    ),
    StationSpec(
        "SHARAVATHI", "Sharavathi Generating Station",
        "lblshvytot",
        tuple(f"lblshvty{i}" for i in range(1, 11)),
        "hydro",
    ),
    StationSpec(
        "VARAHI", "Varahi Underground Power House",
        "lblvrhtot",
        tuple(f"lblvrh{i}" for i in range(1, 5)),
        "hydro",
    ),
    StationSpec(
        "ALMATTI", "Almatti Dam Power House",
        "lblalmttot",
        tuple(f"lblalmt{i}" for i in range(1, 7)),
        "hydro",
    ),
)

BY_CODE: dict[str, StationSpec] = {s.code: s for s in STATIONS}

# System-wide values, not attributable to any one station.
FREQUENCY_ID = "lblfreq"
PAGE_TIMESTAMP_ID = "lbldate"
STATE_GEN_ID = "lblstategen"      # total KPCL state generation, MW
TOTAL_GEN_ID = "lbltotgen"        # state generation plus central share, MW

# The page renders its timestamp in Indian Standard Time with no zone marker.
PAGE_TIMEZONE = "Asia/Kolkata"
PAGE_TIMESTAMP_FORMAT = "%d/%m/%Y %H:%M"
