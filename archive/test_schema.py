"""Schema tests for Stage 2.

Fast: they assert the schema's guarantees, not its performance. The 10-million
row performance criterion lives in archive/loadtest.py and is run explicitly.

Each test runs in a transaction that is rolled back, so they leave no rows
behind and can run against the same database as everything else.
"""

from __future__ import annotations

import datetime as dt
import os

import psycopg
import pytest

GOOD = 0
BAD_DEVICE_FAILURE = 2156593152
UNCERTAIN_SENSOR = 1083375616

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")
T0 = dt.datetime(2030, 6, 1, tzinfo=dt.timezone.utc)


@pytest.fixture
def conn():
    try:
        connection = psycopg.connect(DSN, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"archive not reachable: {exc}")
    connection.autocommit = False
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def tag_id(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tag (name, engineering_unit, range_low, range_high) "
            "VALUES ('TEST_TAG_SCHEMA', 'MW', 0, 250) RETURNING id")
        return cur.fetchone()[0]


def test_quality_column_holds_a_real_statuscode(conn, tag_id):
    """The build plan specifies `quality smallint`. BadDeviceFailure is
    2156593152, which exceeds smallint (32767) and int4 (2147483647) alike, so
    a smallint column could not satisfy the plan's own rule that quality is the
    numeric StatusCode. This test is why the column is bigint."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sample (tag_id, source_ts, server_ts, value, quality) "
            "VALUES (%s, %s, %s, NULL, %s)",
            (tag_id, T0, T0 + dt.timedelta(milliseconds=30), BAD_DEVICE_FAILURE))
        cur.execute("SELECT quality FROM sample WHERE tag_id = %s", (tag_id,))
        assert cur.fetchone()[0] == BAD_DEVICE_FAILURE


def test_same_tag_and_source_ts_cannot_duplicate(conn, tag_id):
    """Idempotency is structural: replay cannot duplicate because the schema
    forbids it. (§382, §648)"""
    row = (tag_id, T0, T0 + dt.timedelta(milliseconds=30), 42.0, GOOD)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value, quality)"
                    " VALUES (%s,%s,%s,%s,%s)", row)
        # A second insert of the same key is absorbed, not duplicated.
        cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value, quality)"
                    " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tag_id, source_ts)"
                    " DO NOTHING", row)
        cur.execute("SELECT count(*) FROM sample WHERE tag_id = %s", (tag_id,))
        assert cur.fetchone()[0] == 1

        # And without ON CONFLICT it is an error, not a silent second row.
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value,"
                        " quality) VALUES (%s,%s,%s,%s,%s)", row)


def test_a_bad_sample_has_no_value(conn, tag_id):
    """Measured in Stage 1: a Bad DataValue carries Value = None per OPC UA
    Part 4. The column must therefore be nullable, and a Bad row storing 0
    would be the substitution §318 forbids."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value,"
                    " quality) VALUES (%s,%s,%s,NULL,%s)",
                    (tag_id, T0, T0, BAD_DEVICE_FAILURE))
        cur.execute("SELECT value, quality FROM sample WHERE tag_id=%s", (tag_id,))
        value, quality = cur.fetchone()
        assert value is None
        assert quality == BAD_DEVICE_FAILURE


def test_quality_class_decodes_severity(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT quality_class(%s), quality_class(%s), quality_class(%s)",
                    (GOOD, UNCERTAIN_SENSOR, BAD_DEVICE_FAILURE))
        assert cur.fetchone() == ("Good", "Uncertain", "Bad")


def test_decoded_view_exposes_transit(conn, tag_id):
    """server_ts - source_ts is the transit the system must never collapse."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value,"
                    " quality) VALUES (%s,%s,%s,%s,%s)",
                    (tag_id, T0, T0 + dt.timedelta(milliseconds=40), 10.0, GOOD))
        cur.execute("SELECT tag_name, transit, quality_class FROM sample_decoded"
                    " WHERE tag_id = %s", (tag_id,))
        name, transit, klass = cur.fetchone()
        assert name == "TEST_TAG_SCHEMA"
        assert transit == dt.timedelta(milliseconds=40)
        assert klass == "Good"


def test_source_and_server_timestamps_are_both_required(conn, tag_id):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.NotNullViolation):
            cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value,"
                        " quality) VALUES (%s,%s,NULL,%s,%s)",
                        (tag_id, T0, 1.0, GOOD))


def test_quality_is_required(conn, tag_id):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.NotNullViolation):
            cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value,"
                        " quality) VALUES (%s,%s,%s,%s,NULL)",
                        (tag_id, T0, T0, 1.0))


def test_attribute_cannot_be_both_a_tag_and_a_static_value(conn, tag_id):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO element (asset_code, name) VALUES"
                    " ('TEST-EL-1','Test element') RETURNING id")
        element_id = cur.fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("INSERT INTO attribute (element_id, name, tag_id,"
                        " static_value) VALUES (%s,'Ambiguous',%s,'7')",
                        (element_id, tag_id))


def test_asset_code_is_unique(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO element (asset_code, name) VALUES ('TEST-EL-2','a')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO element (asset_code, name) VALUES"
                        " ('TEST-EL-2','b')")


def test_only_one_event_frame_may_be_open_per_element_and_template(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO element (asset_code, name) VALUES"
                    " ('TEST-EL-3','u') RETURNING id")
        element_id = cur.fetchone()[0]
        cur.execute("INSERT INTO event_frame (template, element_id, start_ts)"
                    " VALUES ('ThermalStartup',%s,%s)", (element_id, T0))
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO event_frame (template, element_id, start_ts)"
                        " VALUES ('ThermalStartup',%s,%s)", (element_id, T0))


def test_only_one_kpi_version_may_be_current(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO kpi_definition (name, classification, equation,"
                    " version) VALUES ('TestKPI','calculated','a/b',1)")
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute("INSERT INTO kpi_definition (name, classification,"
                        " equation, version) VALUES ('TestKPI','calculated',"
                        "'a/b',2)")


def test_superseding_a_kpi_version_is_allowed(conn):
    """Versioning works the way §320 needs: close the old one, open the new."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO kpi_definition (name, classification, equation,"
                    " version) VALUES ('TestKPI2','calculated','a/b',1)")
        cur.execute("UPDATE kpi_definition SET valid_to = now() WHERE"
                    " name='TestKPI2' AND version=1")
        cur.execute("INSERT INTO kpi_definition (name, classification, equation,"
                    " version) VALUES ('TestKPI2','calculated','a/c',2)")
        cur.execute("SELECT count(*) FROM kpi_definition WHERE name='TestKPI2'")
        assert cur.fetchone()[0] == 2


def test_kpi_bad_data_treatment_cannot_be_substitution(conn):
    """The column exists because the tender asks for it to be declared, not
    because substituting is an option. (§318)"""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("INSERT INTO kpi_definition (name, classification,"
                        " equation, version, bad_data_treatment) VALUES"
                        " ('TestKPI3','calculated','a/b',1,'use_last_good')")


def test_a_bad_kpi_value_has_no_number_but_has_a_reason(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO element (asset_code, name) VALUES"
                    " ('TEST-EL-4','u') RETURNING id")
        element_id = cur.fetchone()[0]
        cur.execute("INSERT INTO kpi_definition (name, classification, equation,"
                    " version) VALUES ('TestKPI4','calculated','a/b',1) RETURNING id")
        kpi_id = cur.fetchone()[0]
        cur.execute("INSERT INTO kpi_value (kpi_definition_id, kpi_version,"
                    " element_id, ts, value, quality, reason) VALUES"
                    " (%s,1,%s,%s,NULL,%s,'U1_COAL_FLOW is BadDeviceFailure')",
                    (kpi_id, element_id, T0, BAD_DEVICE_FAILURE))
        cur.execute("SELECT value, quality_class, reason FROM kpi_value_decoded"
                    " WHERE kpi_name='TestKPI4'")
        value, klass, reason = cur.fetchone()
        assert value is None
        assert klass == "Bad"
        assert "U1_COAL_FLOW" in reason


def test_audit_log_records_before_and_after(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO audit_log (actor, entity, entity_id, field,"
                    " old_value, new_value, reason) VALUES"
                    " ('pd','tag','1','comp_dev','1.0','0.5','tightened') RETURNING id")
        assert cur.fetchone()[0] is not None


def test_tag_range_must_be_ordered(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("INSERT INTO tag (name, range_low, range_high) VALUES"
                        " ('BAD_RANGE', 100, 10)")
