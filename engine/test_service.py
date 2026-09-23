"""The engine service: which elements a KPI or an event template applies to.

Derived from the asset model, so adding a unit stays configuration (rule 7).
Against the real, seeded asset model; read-only.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from engine import events, kpi
from engine.service import TEMPLATES, applicable_elements

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(DSN, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"archive not reachable: {exc}")
    yield connection
    connection.rollback()
    connection.close()


def codes(conn, ids):
    with conn.cursor() as cur:
        cur.execute("SELECT asset_code FROM element WHERE id = ANY(%s)"
                    " ORDER BY asset_code", (ids,))
        return [r[0] for r in cur.fetchall()]


def test_a_unit_kpi_applies_to_each_unit_not_the_boiler_or_the_station(conn):
    """Heat rate needs CoalFlow (boiler) and GrossGeneration (unit). The unit is
    the lowest element holding both; the station holds them only through it."""
    found = codes(conn, applicable_elements(conn, {"CoalFlow", "GrossGeneration"}))
    assert "KPCL-RTPS-U1" in found and "KPCL-RTPS-U2" in found
    assert "KPCL-RTPS" not in found and "KPCL-RTPS-U1-BLR" not in found


def test_the_rig_kpi_applies_to_the_rig_only(conn):
    found = codes(conn, applicable_elements(
        conn, {"HubTemperature", "AmbientTemperature"}))
    assert found == ["KPCL-RTPS-RIG"]


def test_an_attribute_nothing_has_applies_nowhere(conn):
    assert applicable_elements(conn, {"NoSuchAttribute"}) == []


def test_every_current_kpi_applies_somewhere(conn):
    """A definition that applies to no element would never be computed, and
    nothing would say so."""
    for definition in kpi.load_definitions(conn):
        assert applicable_elements(conn, set(definition.inputs.values())), definition.name


def test_every_event_template_applies_somewhere(conn):
    for template in events.load_templates(TEMPLATES).values():
        needed = {template.start.attribute, template.end.attribute}
        needed |= {m.trigger.attribute for m in template.milestones}
        assert applicable_elements(conn, needed), template.name
