"""The asset framework. (§341, §392)

Rule 7: adding an asset is configuration, not code. A second unit is a new
element created from an existing template. If adding a unit requires editing
Python, the asset model is wrong.

So `instantiate()` is the whole story. Given a template and a context, it
creates the element, its children, their attributes, and any tag rows those
attributes need — recursively, from the template, with nothing hard-coded about
which unit it is. `test_assets.py` adds Unit 2 with one call and checks that
nothing had to be remapped by hand.

The hierarchy is KPCL -> Station -> Unit -> System -> Sub-system -> Equipment ->
Component -> Parameter (§392). Depth is data, not structure: `element.parent_id`
is self-referencing, so a level can be inserted without a migration.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import psycopg

from archive import audit

# ISA-95 levels, coarse to fine. Documented in engine/README.md (§385).
LEVELS = ("Enterprise", "Station", "Unit", "System", "SubSystem",
          "Equipment", "Component", "Parameter")


class AssetError(RuntimeError):
    pass


@dataclass(frozen=True)
class ElementRow:
    id: int
    asset_code: str
    name: str
    level: str | None
    parent_id: int | None
    template_id: int | None
    context: dict


def resolve(pattern: str, context: dict) -> str:
    """Substitute {placeholders} from an element's context.

    A pattern naming something the context does not carry is an error, not an
    empty string: silently producing "_MS_TEMP" would create an attribute
    pointing at a tag that will never exist, and nothing downstream would say so.
    """
    missing = [name for name in re.findall(r"\{(\w+)\}", pattern)
               if name not in context]
    if missing:
        raise AssetError(
            f"pattern {pattern!r} needs {missing} which the element context "
            f"{sorted(context)} does not provide")
    return pattern.format(**context)


# -- defining templates ------------------------------------------------------

def create_template(conn: psycopg.Connection, name: str, *, level: str | None = None,
                    description: str | None = None,
                    derives_from: str | None = None) -> int:
    with conn.cursor() as cur:
        parent_id = None
        if derives_from:
            cur.execute("SELECT id FROM element_template WHERE name = %s",
                        (derives_from,))
            row = cur.fetchone()
            if row is None:
                raise AssetError(f"no such base template: {derives_from}")
            parent_id = row[0]
        cur.execute(
            "INSERT INTO element_template (name, description, parent_template_id,"
            " level) VALUES (%s,%s,%s,%s)"
            " ON CONFLICT (name) DO UPDATE SET description = EXCLUDED.description,"
            " level = EXCLUDED.level RETURNING id",
            (name, description, parent_id, level))
        return cur.fetchone()[0]


def add_attribute_template(conn: psycopg.Connection, template: str, name: str, *,
                           tag_pattern: str | None = None,
                           engineering_unit: str | None = None,
                           range_low: float | None = None,
                           range_high: float | None = None,
                           scan_rate_ms: int | None = None,
                           exc_dev: float | None = None,
                           comp_dev: float | None = None,
                           max_time_ms: int | None = None,
                           source_system: str | None = None,
                           description: str | None = None,
                           static_default: str | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM element_template WHERE name = %s", (template,))
        row = cur.fetchone()
        if row is None:
            raise AssetError(f"no such template: {template}")
        cur.execute(
            "INSERT INTO attribute_template (element_template_id, name, description,"
            " engineering_unit, is_tag_reference, default_value, tag_pattern,"
            " range_low, range_high, scan_rate_ms, exc_dev, comp_dev, max_time_ms,"
            " source_system)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (element_template_id, name) DO UPDATE SET"
            " tag_pattern = EXCLUDED.tag_pattern,"
            " engineering_unit = EXCLUDED.engineering_unit,"
            " range_low = EXCLUDED.range_low, range_high = EXCLUDED.range_high,"
            " scan_rate_ms = EXCLUDED.scan_rate_ms, exc_dev = EXCLUDED.exc_dev,"
            " comp_dev = EXCLUDED.comp_dev, max_time_ms = EXCLUDED.max_time_ms,"
            " source_system = EXCLUDED.source_system"
            " RETURNING id",
            (row[0], name, description, engineering_unit, tag_pattern is not None,
             static_default, tag_pattern, range_low, range_high, scan_rate_ms,
             exc_dev, comp_dev, max_time_ms, source_system))
        return cur.fetchone()[0]


def compose(conn: psycopg.Connection, parent: str, child: str, *, name: str,
            code_suffix: str, level: str | None = None,
            sort_order: int = 0) -> None:
    """Say that elements of `parent` contain an element of `child`."""
    with conn.cursor() as cur:
        cur.execute("SELECT name, id FROM element_template WHERE name IN (%s,%s)",
                    (parent, child))
        ids = dict(cur.fetchall())
        for needed in (parent, child):
            if needed not in ids:
                raise AssetError(f"no such template: {needed}")
        cur.execute(
            "INSERT INTO template_composition (parent_template_id,"
            " child_template_id, name, code_suffix, level, sort_order)"
            " VALUES (%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (parent_template_id, name) DO UPDATE SET"
            " child_template_id = EXCLUDED.child_template_id,"
            " code_suffix = EXCLUDED.code_suffix, level = EXCLUDED.level,"
            " sort_order = EXCLUDED.sort_order",
            (ids[parent], ids[child], name, code_suffix, level, sort_order))


# -- instantiating -----------------------------------------------------------

def _audit(cur, actor: str, entity: str, entity_id: str, new_value: str,
           reason: str) -> None:
    cur.execute(
        "INSERT INTO audit_log (actor, entity, entity_id, field, old_value,"
        " new_value, reason) VALUES (%s,%s,%s,NULL,NULL,%s,%s)",
        (actor, entity, entity_id, new_value, reason))


def _ensure_tag(cur, name: str, spec: dict, actor: str) -> int:
    """Find the tag this attribute refers to, creating it if it does not exist.

    Creating tags here is what makes rule 7 true. If instantiating a unit left
    its tags to be written by hand, adding a unit would still be a manual
    remapping job wearing a template's clothes.

    Where the tag lives in the source address space comes from the element's
    context (`source_path`), like everything else that differs between units.
    A unit whose context gives none gets tags with no source path, which the
    collector refuses loudly rather than guessing -- the column default that
    used to guess filed Unit 2's tags under Unit1.
    """
    cur.execute("SELECT id FROM tag WHERE name = %s", (name,))
    row = cur.fetchone()
    if row is not None:
        return row[0]
    cur.execute(
        "INSERT INTO tag (name, description, engineering_unit, range_low,"
        " range_high, source_system, scan_rate_ms, exc_dev, comp_dev, max_time_ms,"
        " source_path)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (name, spec.get("description"), spec.get("engineering_unit"),
         spec.get("range_low"), spec.get("range_high"),
         spec.get("source_system") or "opcua",
         spec.get("scan_rate_ms") or 1000, spec.get("exc_dev"),
         spec.get("comp_dev"), spec.get("max_time_ms"),
         spec.get("source_path")))
    tag_id = cur.fetchone()[0]
    _audit(cur, actor, "tag", str(tag_id), name, "created by asset instantiation")
    return tag_id


def instantiate(conn: psycopg.Connection, template: str, *, asset_code: str,
                name: str, context: dict, parent_code: str | None = None,
                level: str | None = None, actor: str = "asset-model") -> int:
    """Create an element from a template, with everything beneath it.

    Recursive: child elements from `template_composition`, attributes from
    `attribute_template`, and tag rows for any attribute whose resolved tag does
    not exist yet. Returns the new element's id.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, level FROM element_template WHERE name = %s",
                    (template,))
        row = cur.fetchone()
        if row is None:
            raise AssetError(f"no such template: {template}")
        template_id, template_level = row

        parent_id = None
        if parent_code is not None:
            cur.execute("SELECT id FROM element WHERE asset_code = %s", (parent_code,))
            parent = cur.fetchone()
            if parent is None:
                raise AssetError(f"no such parent element: {parent_code}")
            parent_id = parent[0]

        cur.execute("SELECT id FROM element WHERE asset_code = %s", (asset_code,))
        if cur.fetchone() is not None:
            # Asset codes are permanent and never reused (§392). Reusing one
            # would silently re-point history at a different physical thing.
            raise AssetError(
                f"asset code {asset_code!r} already exists; codes are permanent "
                f"and are never reused")

        cur.execute(
            "INSERT INTO element (asset_code, name, template_id, parent_id, level,"
            " context) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (asset_code, name, template_id, parent_id, level or template_level,
             json.dumps(context)))
        element_id = cur.fetchone()[0]
        _audit(cur, actor, "element", str(element_id), asset_code,
               f"instantiated from template {template}")

        # Attributes, including inherited ones from the derivation chain.
        for attribute in _attribute_templates(cur, template_id):
            (attr_name, tag_pattern, unit, is_tag_ref, default_value, rlo, rhi,
             scan, exc, comp, maxt, source, description) = attribute
            tag_id = None
            static_value = None
            if is_tag_ref and tag_pattern:
                tag_name = resolve(tag_pattern, context)
                tag_id = _ensure_tag(cur, tag_name, {
                    "description": description or attr_name,
                    "engineering_unit": unit, "range_low": rlo, "range_high": rhi,
                    "scan_rate_ms": scan, "exc_dev": exc, "comp_dev": comp,
                    "max_time_ms": maxt, "source_system": source,
                    "source_path": context.get("source_path")}, actor)
                # Mapping a tag to an element is a configuration change, and
                # is audited like one.
                audit.change(cur, "tag", tag_id, "element_id", element_id,
                             actor=actor,
                             reason=f"mapped to {asset_code} by asset instantiation")
            else:
                static_value = default_value
            cur.execute(
                "INSERT INTO attribute (element_id, attribute_template_id, name,"
                " engineering_unit, tag_id, static_value)"
                " SELECT %s, at.id, %s, %s, %s, %s FROM attribute_template at"
                " WHERE at.element_template_id = %s AND at.name = %s"
                " ON CONFLICT (element_id, name) DO NOTHING",
                (element_id, attr_name, unit, tag_id, static_value,
                 _owning_template(cur, template_id, attr_name), attr_name))

        # Children.
        cur.execute(
            "SELECT t.name, c.name, c.code_suffix, c.level"
            " FROM template_composition c"
            " JOIN element_template t ON t.id = c.child_template_id"
            " WHERE c.parent_template_id = %s ORDER BY c.sort_order, c.name",
            (template_id,))
        children = cur.fetchall()

    for child_template, child_name, suffix, child_level in children:
        instantiate(conn, child_template,
                    asset_code=f"{asset_code}-{resolve(suffix, context)}",
                    name=resolve(child_name, context), context=context,
                    parent_code=asset_code, level=child_level, actor=actor)
    return element_id


def _owning_template(cur, template_id: int, attribute_name: str) -> int:
    """Which template in the derivation chain declares this attribute."""
    cur.execute(
        "WITH RECURSIVE chain AS ("
        "  SELECT id, parent_template_id, 0 AS depth FROM element_template"
        "   WHERE id = %s"
        "  UNION ALL"
        "  SELECT t.id, t.parent_template_id, c.depth + 1 FROM element_template t"
        "  JOIN chain c ON t.id = c.parent_template_id)"
        " SELECT at.element_template_id FROM attribute_template at"
        " JOIN chain ON chain.id = at.element_template_id"
        " WHERE at.name = %s ORDER BY chain.depth LIMIT 1",
        (template_id, attribute_name))
    row = cur.fetchone()
    return row[0] if row else template_id


def _attribute_templates(cur, template_id: int) -> list[tuple]:
    """Attributes of a template, including those inherited by derivation.

    A derived template sees its base's attributes; a 210 MW unit does not
    restate what every unit has.
    """
    # depth 0 is the template itself, then each base in turn. DISTINCT ON keeps
    # the first row per name, so ordering by depth makes the MOST DERIVED
    # definition win. Without the depth ordering the winner is arbitrary, and a
    # derived template's override is silently ignored: ThermalUnit210's
    # Fuel = "coal" lost to GeneratingUnit's "unknown".
    cur.execute(
        "WITH RECURSIVE chain AS ("
        "  SELECT id, parent_template_id, 0 AS depth FROM element_template"
        "   WHERE id = %s"
        "  UNION ALL"
        "  SELECT t.id, t.parent_template_id, c.depth + 1 FROM element_template t"
        "  JOIN chain c ON t.id = c.parent_template_id)"
        " SELECT DISTINCT ON (at.name) at.name, at.tag_pattern,"
        " at.engineering_unit, at.is_tag_reference, at.default_value,"
        " at.range_low, at.range_high, at.scan_rate_ms, at.exc_dev, at.comp_dev,"
        " at.max_time_ms, at.source_system, at.description"
        " FROM attribute_template at JOIN chain ON chain.id = at.element_template_id"
        " ORDER BY at.name, chain.depth", (template_id,))
    return cur.fetchall()


# -- the query an asset framework exists for ---------------------------------

def attribute_across_template(conn: psycopg.Connection, template: str,
                              attribute: str) -> list[dict]:
    """Give me this attribute across every element of this template.

    Every bearing vibration on every unit; every main steam temperature on every
    unit in the fleet. That single capability is what an asset framework is for:
    without it, "compare this across the fleet" is a hand-written query per unit.
    """
    # Elements of the named template AND of anything derived from it. Asking
    # for GeneratingUnit.GrossGeneration must find the 210 MW units and the
    # 500 MW ones alike; matching the template name exactly returns nothing at
    # all for a base template, which is the query a mixed fleet most wants.
    with conn.cursor() as cur:
        cur.execute(
            "WITH RECURSIVE family AS ("
            "  SELECT id FROM element_template WHERE name = %s"
            "  UNION ALL"
            "  SELECT t.id FROM element_template t"
            "  JOIN family f ON t.parent_template_id = f.id)"
            " SELECT e.asset_code, e.name, a.name, t.id, t.name,"
            "       t.engineering_unit, a.static_value"
            " FROM element e"
            " JOIN family ON family.id = e.template_id"
            " JOIN attribute a ON a.element_id = e.id"
            " LEFT JOIN tag t ON t.id = a.tag_id"
            " WHERE a.name = %s"
            " ORDER BY e.asset_code", (template, attribute))
        return [
            {"asset_code": r[0], "element": r[1], "attribute": r[2],
             "tag_id": r[3], "tag_name": r[4], "engineering_unit": r[5],
             "static_value": r[6]}
            for r in cur.fetchall()
        ]


def latest_across_template(conn: psycopg.Connection, template: str,
                           attribute: str) -> list[dict]:
    """The same query, with each tag's most recent sample and its quality.

    Quality travels with the value here as everywhere else: a fleet comparison
    that cannot say which units are reporting Bad is not a comparison.
    """
    rows = attribute_across_template(conn, template, attribute)
    tag_ids = [r["tag_id"] for r in rows if r["tag_id"]]
    if not tag_ids:
        return rows
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT ON (tag_id) tag_id, source_ts, value, quality,"
            " quality_class(quality) FROM sample WHERE tag_id = ANY(%s)"
            " ORDER BY tag_id, source_ts DESC", (tag_ids,))
        latest = {r[0]: r[1:] for r in cur.fetchall()}
    for row in rows:
        found = latest.get(row["tag_id"])
        row["source_ts"], row["value"], row["quality"], row["quality_class"] = (
            found if found else (None, None, None, None))
    return rows


def descendants(conn: psycopg.Connection, asset_code: str) -> list[ElementRow]:
    """The whole subtree below an element, in hierarchy order."""
    with conn.cursor() as cur:
        cur.execute(
            "WITH RECURSIVE tree AS ("
            "  SELECT id, asset_code, name, level, parent_id, template_id, context,"
            "         0 AS depth FROM element WHERE asset_code = %s"
            "  UNION ALL"
            "  SELECT e.id, e.asset_code, e.name, e.level, e.parent_id,"
            "         e.template_id, e.context, tree.depth + 1"
            "  FROM element e JOIN tree ON e.parent_id = tree.id)"
            " SELECT id, asset_code, name, level, parent_id, template_id, context"
            " FROM tree ORDER BY depth, asset_code", (asset_code,))
        return [ElementRow(*r) for r in cur.fetchall()]
