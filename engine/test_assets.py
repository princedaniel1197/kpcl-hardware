"""Asset framework tests.

The one that decides Stage 5: adding a second unit must be one call against a
template, with no tag remapped by hand and no Python edited.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
import pytest

from engine import assets
from engine.seed_assets import seed

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
CONFIG = Path(__file__).parent.parent / "config" / "asset_model.json"


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(DSN, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"archive not reachable: {exc}")
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def model(conn):
    """The seeded model, in a transaction that is rolled back afterwards."""
    seed(conn, json.loads(CONFIG.read_text()), actor="test")
    return conn


# --- the Stage 5 criterion ---------------------------------------------------

def test_adding_a_second_unit_is_one_call_and_remaps_nothing(model):
    """Rule 7. A new unit is created from the same template Unit 1 came from, with
    a different context. No tag is remapped, no Python is edited, and the only
    input is the two lines below."""
    conn = model
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM element")
        before_elements = cur.fetchone()[0]

    element_id = assets.instantiate(
        conn, "ThermalUnit210",
        asset_code="KPCL-RTPS-U9", name="RTPS Unit 9",
        context={"station": "RTPS", "unit": "U9"},
        parent_code="KPCL-RTPS", actor="test")

    assert element_id
    with conn.cursor() as cur:
        # The whole subtree appeared: unit, boiler, turbine, generator, bearing.
        cur.execute("SELECT asset_code FROM element WHERE asset_code LIKE"
                    " 'KPCL-RTPS-U9%' ORDER BY asset_code")
        codes = [r[0] for r in cur.fetchall()]
    assert codes == ["KPCL-RTPS-U9", "KPCL-RTPS-U9-BLR", "KPCL-RTPS-U9-GEN",
                     "KPCL-RTPS-U9-TUR", "KPCL-RTPS-U9-TUR-BRG1"]

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM element")
        assert cur.fetchone()[0] == before_elements + len(codes)

        # Every attribute resolved to a U2 tag. Nothing points at a U1 tag.
        cur.execute(
            "SELECT a.name, t.name FROM element e JOIN attribute a"
            " ON a.element_id = e.id JOIN tag t ON t.id = a.tag_id"
            " WHERE e.asset_code LIKE 'KPCL-RTPS-U9%'")
        mapped = dict(cur.fetchall())
    assert mapped, "no attributes resolved to tags"
    assert all(tag.startswith("U9_") for tag in mapped.values()), mapped
    assert mapped["MainSteamTemperature"] == "U9_MS_TEMP"
    assert mapped["Vibration"] == "U9_BEARING_VIB"


def test_the_new_units_tags_were_created_with_their_configuration(model):
    """The tags the new unit needs did not exist. They were created from the
    attribute templates, carrying units, span and deadbands -- otherwise adding
    a unit would still be a manual tag-writing job."""
    conn = model
    assets.instantiate(conn, "ThermalUnit210", asset_code="KPCL-RTPS-U9",
                       name="RTPS Unit 9",
                       context={"station": "RTPS", "unit": "U9"},
                       parent_code="KPCL-RTPS", actor="test")
    with conn.cursor() as cur:
        cur.execute("SELECT name, engineering_unit, range_low, range_high,"
                    " scan_rate_ms, exc_dev, comp_dev FROM tag"
                    " WHERE name = 'U9_MS_TEMP'")
        row = cur.fetchone()
    assert row is not None, "U9_MS_TEMP was not created"
    name, unit, lo, hi, scan, exc, comp = row
    assert unit == "degC"
    assert (lo, hi) == (0.0, 600.0)
    assert scan == 500
    assert comp == 2.4 and exc == 1.2


def test_the_fleet_query_now_returns_both_units(model):
    """The capability an asset framework exists for: this attribute, across
    every element of this template."""
    conn = model
    before = [r["tag_name"] for r in
              assets.attribute_across_template(conn, "Bearing", "Vibration")]
    assert "U1_BEARING_VIB" in before
    assert "U9_BEARING_VIB" not in before

    assets.instantiate(conn, "ThermalUnit210", asset_code="KPCL-RTPS-U9",
                       name="RTPS Unit 9",
                       context={"station": "RTPS", "unit": "U9"},
                       parent_code="KPCL-RTPS", actor="test")

    after = assets.attribute_across_template(conn, "Bearing", "Vibration")
    names = [r["tag_name"] for r in after]
    assert names == sorted(before + ["U9_BEARING_VIB"])
    assert "KPCL-RTPS-U9-TUR-BRG1" in [r["asset_code"] for r in after]


def test_adding_a_unit_is_recorded_in_the_audit_log(model):
    """Configuration changes are audited with actor and reason (§433)."""
    conn = model
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM audit_log")
        before = cur.fetchone()[0]
    assets.instantiate(conn, "ThermalUnit210", asset_code="KPCL-RTPS-U9",
                       name="RTPS Unit 9",
                       context={"station": "RTPS", "unit": "U9"},
                       parent_code="KPCL-RTPS", actor="prince")
    with conn.cursor() as cur:
        cur.execute("SELECT entity, new_value, reason FROM audit_log"
                    " WHERE actor = 'prince' ORDER BY id")
        rows = cur.fetchall()
    assert len(rows) > before or rows
    assert any(e == "element" and v == "KPCL-RTPS-U9" for e, v, _ in rows)
    assert any(e == "tag" and v == "U9_MS_TEMP" for e, v, _ in rows)


# --- templates ---------------------------------------------------------------

def test_a_derived_template_overrides_its_base(model):
    """ThermalUnit210 derives from GeneratingUnit and restates Fuel and
    RatedCapacity. The derived value must win -- without ordering by derivation
    depth the winner is arbitrary, and Fuel came out 'unknown' instead of
    'coal'."""
    conn = model
    with conn.cursor() as cur:
        cur.execute("SELECT a.name, a.static_value FROM element e"
                    " JOIN attribute a ON a.element_id = e.id"
                    " WHERE e.asset_code = 'KPCL-RTPS-U1'"
                    "   AND a.static_value IS NOT NULL ORDER BY a.name")
        values = dict(cur.fetchall())
    assert values == {"Fuel": "coal", "RatedCapacity": "210"}


def test_a_derived_template_inherits_what_it_does_not_restate(model):
    """GrossGeneration is declared once, on the base."""
    conn = model
    with conn.cursor() as cur:
        cur.execute("SELECT t.name FROM element e JOIN attribute a"
                    " ON a.element_id = e.id JOIN tag t ON t.id = a.tag_id"
                    " WHERE e.asset_code = 'KPCL-RTPS-U1'"
                    "   AND a.name = 'GrossGeneration'")
        assert cur.fetchone()[0] == "U1_MW"


# --- asset codes -------------------------------------------------------------

def test_an_asset_code_is_never_reused(model):
    """Codes are permanent (§392). Reusing one would silently re-point history
    at a different physical thing."""
    conn = model
    with pytest.raises(assets.AssetError, match="never reused"):
        assets.instantiate(conn, "ThermalUnit210", asset_code="KPCL-RTPS-U1",
                           name="Something else",
                           context={"station": "RTPS", "unit": "U9"},
                           parent_code="KPCL-RTPS")


def test_the_hierarchy_is_navigable_from_the_top(model):
    conn = model
    tree = assets.descendants(conn, "KPCL")
    codes = [e.asset_code for e in tree]
    assert codes[0] == "KPCL"
    assert "KPCL-RTPS-U1-TUR-BRG1" in codes
    levels = {e.asset_code: e.level for e in tree}
    assert levels["KPCL-RTPS"] == "Station"
    assert levels["KPCL-RTPS-U1"] == "Unit"
    assert levels["KPCL-RTPS-U1-BLR"] == "System"
    assert levels["KPCL-RTPS-U1-TUR-BRG1"] == "Equipment"


# --- pattern resolution ------------------------------------------------------

def test_a_pattern_the_context_cannot_satisfy_is_an_error():
    """Silently producing '_MS_TEMP' would create an attribute pointing at a
    tag that will never exist, and nothing downstream would say so."""
    with pytest.raises(assets.AssetError, match="does not provide"):
        assets.resolve("{unit}_MS_TEMP", {"station": "RTPS"})


def test_a_pattern_resolves_against_the_context():
    assert assets.resolve("{unit}_MS_TEMP", {"unit": "U7"}) == "U7_MS_TEMP"
    assert assets.resolve("{station}-{unit}", {"station": "BTPS", "unit": "U3"}) \
        == "BTPS-U3"


def test_the_fleet_query_spans_derived_templates(model):
    """Asking a base template must find elements built from anything derived
    from it. A mixed fleet is the normal case -- 210 MW units beside 500 MW
    ones -- and matching the template name exactly returns nothing at all for
    the base, which is the query you would most want to ask."""
    conn = model
    assets.instantiate(conn, "ThermalUnit210", asset_code="KPCL-RTPS-U9",
                       name="RTPS Unit 9",
                       context={"station": "RTPS", "unit": "U9"},
                       parent_code="KPCL-RTPS", actor="test")
    # The elements are ThermalUnit210; the query names its base.
    base = [r["tag_name"] for r in
            assets.attribute_across_template(conn, "GeneratingUnit",
                                             "GrossGeneration")]
    assert "U1_MW" in base and "U9_MW" in base

    # And the derived template still answers for itself, identically here
    # because every unit in this model is a ThermalUnit210.
    derived = [r["tag_name"] for r in
               assets.attribute_across_template(conn, "ThermalUnit210",
                                                "GrossGeneration")]
    assert derived == base
