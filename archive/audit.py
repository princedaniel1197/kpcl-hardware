"""Configuration changes, with the audit row that goes with them. (§433)

Every configuration change is written to `audit_log` with actor, timestamp, old
value, new value and reason. That is easy to state and easy to break: a quick
UPDATE in a demonstration script, or an upsert that records "old value: NULL"
because it never looked, leaves a change nobody can explain later. The Stage 6
demonstration did exactly that -- it narrowed U1_MS_TEMP's EURange to force an
out-of-range condition, left 1,251 range flags behind, and the audit log had no
record that the range had ever been anything but 0-600.

So configuration is changed through here: the old value is read in the same
transaction, the row is updated only if the value actually changes, and the
audit row carries both.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

# Tables whose rows are configuration, and the column that identifies a row.
CONFIG_TABLES = {"tag": "id", "quality_rule": "id", "alert_rule": "id",
                 "kpi_definition": "id", "element": "id", "attribute": "id"}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _same(old: Any, new: Any) -> bool:
    numbers = (int, float, Decimal)
    if (isinstance(old, numbers) and isinstance(new, numbers)
            and not isinstance(old, bool) and not isinstance(new, bool)):
        return float(old) == float(new)
    return _text(old) == _text(new)


def record(cur: psycopg.Cursor, *, actor: str, entity: str, entity_id: Any,
           field: str | None, old: Any, new: Any, reason: str) -> None:
    cur.execute(
        "INSERT INTO audit_log (actor, entity, entity_id, field, old_value,"
        " new_value, reason) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (actor, entity, str(entity_id), field, _text(old), _text(new), reason))


def change(cur: psycopg.Cursor, table: str, row_id: Any, field: str, new: Any,
           *, actor: str, reason: str) -> bool:
    """Set one field of one configuration row, audited. Returns False, and
    writes nothing, if the value is already `new`."""
    key = CONFIG_TABLES.get(table)
    if key is None:
        raise ValueError(f"{table} is not a configuration table")
    cur.execute(sql.SQL("SELECT {f} FROM {t} WHERE {k} = %s FOR UPDATE").format(
        f=sql.Identifier(field), t=sql.Identifier(table), k=sql.Identifier(key)),
        (row_id,))
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"no {table} row {row_id}")
    old = row[0]
    if _same(old, new):
        return False
    # A dict is a jsonb document; a list is stored as the column's array type.
    stored = Jsonb(new) if isinstance(new, dict) else new
    cur.execute(sql.SQL("UPDATE {t} SET {f} = %s WHERE {k} = %s").format(
        f=sql.Identifier(field), t=sql.Identifier(table), k=sql.Identifier(key)),
        (stored, row_id))
    record(cur, actor=actor, entity=table, entity_id=row_id, field=field,
           old=old, new=new, reason=reason)
    return True
