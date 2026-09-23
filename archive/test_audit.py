"""§433: every configuration change is recorded with actor, timestamp, old
value, new value and reason -- and only real changes are recorded.

Against the real database, in transactions that are rolled back (the audit log
is append-only, so nothing here may commit).
"""

from __future__ import annotations

import os

import psycopg
import pytest

from archive import audit

DSN = os.environ.get("CRPMS_DSN", "postgresql://crpms:crpms@localhost:5432/crpms")


class _Cursor:
    """A cursor with the test tag's id beside it."""

    def __init__(self, cursor, tag_id: int) -> None:
        self._cursor = cursor
        self.tag_id = tag_id

    def __getattr__(self, name):
        return getattr(self._cursor, name)


@pytest.fixture
def cur():
    try:
        conn = psycopg.connect(DSN, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.skip(f"archive not reachable: {exc}")
    try:
        with conn.cursor() as c:
            c.execute("INSERT INTO tag (name, range_low, range_high)"
                      " VALUES ('TEST_AUDIT_TAG', 0, 600) RETURNING id")
            yield _Cursor(c, c.fetchone()[0])
    finally:
        conn.rollback()
        conn.close()


def _rows(cur, tag_id):
    cur.execute("SELECT actor, field, old_value, new_value, reason, ts IS NOT NULL"
                " FROM audit_log WHERE entity='tag' AND entity_id=%s ORDER BY id",
                (str(tag_id),))
    return cur.fetchall()


def test_a_change_records_all_five(cur):
    assert audit.change(cur._cursor, "tag", cur.tag_id, "range_high", 300.0,
                        actor="inspector", reason="narrowed for a test")
    assert _rows(cur, cur.tag_id) == [
        ("inspector", "range_high", "600.0", "300.0", "narrowed for a test", True)]
    cur.execute("SELECT range_high FROM tag WHERE id=%s", (cur.tag_id,))
    assert cur.fetchone()[0] == 300.0


def test_setting_a_value_to_itself_records_nothing(cur):
    """An audit trail full of non-changes hides the real ones. Numbers are
    compared as numbers: 600 and 600.0 are the same range."""
    assert not audit.change(cur._cursor, "tag", cur.tag_id, "range_high", 600,
                            actor="inspector", reason="no-op")
    assert _rows(cur, cur.tag_id) == []


def test_a_json_field_records_old_and_new_documents(cur):
    cur.execute("INSERT INTO quality_rule (tag_id, rule_type, params)"
                " VALUES (%s, 'range', '{\"a\": 1}') RETURNING id", (cur.tag_id,))
    rule_id = cur.fetchone()[0]
    assert audit.change(cur._cursor, "quality_rule", rule_id, "params", {"a": 2},
                        actor="inspector", reason="tightened")
    cur.execute("SELECT old_value, new_value FROM audit_log"
                " WHERE entity='quality_rule' AND entity_id=%s", (str(rule_id),))
    assert cur.fetchone() == ('{"a": 1}', '{"a": 2}')


def test_only_configuration_tables_can_be_changed_this_way(cur):
    with pytest.raises(ValueError):
        audit.change(cur._cursor, "sample", 1, "value", 0.0, actor="x", reason="no")
