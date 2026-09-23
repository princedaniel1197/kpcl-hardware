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


def test_source_timestamp_is_required(conn, tag_id):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.NotNullViolation):
            cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value,"
                        " quality) VALUES (%s,NULL,%s,%s,%s)",
                        (tag_id, T0, 1.0, GOOD))


def test_an_absent_server_timestamp_is_stored_as_absent(conn, tag_id):
    """Migration 014. When no server stamped a value, the row says so with
    NULL. It is not filled from source_ts -- identical timestamps are the
    fingerprint of a substituted receipt time -- and there is no default that
    could fill it silently."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sample (tag_id, source_ts, value, quality)"
                    " VALUES (%s,%s,%s,%s)", (tag_id, T0, 1.0, GOOD))
        cur.execute("SELECT server_ts, transit FROM sample_decoded"
                    " WHERE tag_id = %s", (tag_id,))
        assert cur.fetchone() == (None, None)


def test_run_and_sequence_come_together(conn, tag_id):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO collector_run (instance) VALUES ('test')"
                    " RETURNING id")
        run = cur.fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value,"
                        " quality, collector_run) VALUES (%s,%s,%s,1,0,%s)",
                        (tag_id, T0, T0 + dt.timedelta(milliseconds=5), run))


def _numbered(cur, tag_id, run, seqs, offset_s=0):
    for n in seqs:
        ts = T0 + dt.timedelta(seconds=n + offset_s)
        cur.execute("INSERT INTO sample (tag_id, source_ts, server_ts, value,"
                    " quality, collector_run, seq) VALUES (%s,%s,%s,%s,0,%s,%s)"
                    " ON CONFLICT DO NOTHING",
                    (tag_id, ts, ts + dt.timedelta(milliseconds=30), float(n),
                     run, n))


def test_a_hole_in_a_runs_sequence_is_found(conn, tag_id):
    """B1: a sample the collector meant to write that is not in the archive."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO collector_run (instance) VALUES ('test')"
                    " RETURNING id")
        run = cur.fetchone()[0]
        _numbered(cur, tag_id, run, [1, 2, 3, 6, 7])
        cur.execute("SELECT prev_seq, next_seq, missing, held_by_other_runs,"
                    " recorded_as_lost FROM sample_seq_gap WHERE collector_run=%s",
                    (run,))
        assert cur.fetchall() == [(3, 6, 2, 0, 0)]


def test_a_hole_another_collector_filled_says_so(conn, tag_id):
    """Two collectors, first write wins: the primary's hole is the secondary's
    row. The view must say so rather than report a loss."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO collector_run (instance) VALUES ('secondary'),"
                    " ('primary') RETURNING id")
        secondary, primary = [r[0] for r in cur.fetchall()]
        _numbered(cur, tag_id, secondary, [4, 5])       # got there first
        _numbered(cur, tag_id, primary, [1, 2, 3, 4, 5, 6])
        cur.execute("SELECT missing, held_by_other_runs FROM sample_seq_gap"
                    " WHERE collector_run=%s", (primary,))
        assert cur.fetchall() == [(2, 2)]


def test_a_hole_the_loss_ledger_explains_says_so(conn, tag_id):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO collector_run (instance) VALUES ('test')"
                    " RETURNING id")
        run = cur.fetchone()[0]
        _numbered(cur, tag_id, run, [1, 2, 5])
        cur.execute("INSERT INTO collector_loss (collector_run, tag_id,"
                    " first_source_ts, last_source_ts, first_seq, last_seq,"
                    " samples, reason, detected_at) VALUES"
                    " (%s,%s,%s,%s,3,4,2,'buffer_overflow',now())",
                    (run, tag_id, T0 + dt.timedelta(seconds=3),
                     T0 + dt.timedelta(seconds=4)))
        cur.execute("SELECT missing, recorded_as_lost FROM sample_seq_gap"
                    " WHERE collector_run=%s", (run,))
        assert cur.fetchall() == [(2, 2)]


def test_a_tag_cannot_be_compressed_without_its_parameters(conn):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("INSERT INTO tag (name, comp_dev, compress) VALUES"
                        " ('TEST_COMPRESS_NO_MAXTIME', 1.0, true)")


def test_a_tag_has_no_default_source_path(conn):
    """C9: the column default quietly filed Unit 2's tags under Unit1."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO tag (name) VALUES ('TEST_NO_PATH')"
                    " RETURNING source_path")
        assert cur.fetchone()[0] is None


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


def test_the_audit_log_is_append_only(conn):
    """Migration 015: an audit row, once written, cannot be edited or removed."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO audit_log (actor, entity, entity_id, field,"
                    " old_value, new_value, reason) VALUES"
                    " ('pd','tag','1','comp_dev','1.0','0.5','tightened') RETURNING id")
        row_id = cur.fetchone()[0]
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("UPDATE audit_log SET new_value='0.4' WHERE id=%s", (row_id,))
        cur.execute("ROLLBACK TO SAVEPOINT s")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("DELETE FROM audit_log WHERE id=%s", (row_id,))
        cur.execute("ROLLBACK TO SAVEPOINT s")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            cur.execute("TRUNCATE audit_log")


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
