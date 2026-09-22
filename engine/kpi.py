"""The KPI engine. (§320, §378, §379, §381, §383, §486)

THE RULE THIS MODULE EXISTS TO ENFORCE. A KPI with a Bad input does not return
zero, does not return the last good value, and does not return a number with a
footnote. It returns Bad, with a reason naming the offending input (§318).

That is the single most demonstrable difference between a team that understands
operational technology and a team that does not, so it is enforced structurally:
`compute()` cannot return a number unless every input it used was Good, and the
only way to get a value out of a KpiResult is to look at a field that is None
whenever the quality is not Good.

ON PLANT PHYSICS (§486). The three KPIs implemented here are definitional
ratios, taken from standard Indian utility practice:

  * gross unit heat rate   = coal flow x GCV / gross generation   [kcal/kWh]
  * auxiliary power        = aux power / gross generation x 100   [%]
  * specific coal          = coal flow / gross generation         [kg/kWh]

None of them requires a steam table, and none is invented here. Cylinder
efficiency and condenser performance DO require published steam tables, so under
§486 they are not implemented rather than approximated. A plausible-looking
number with no provenance is worse than an absent one.

EQUATIONS ARE DATA, and data from a database must not be executed. `safe_eval`
walks the parsed expression and permits arithmetic over named inputs and
constants and nothing else -- no attribute access, no calls except a short
whitelist, no names it was not given.
"""

from __future__ import annotations

import ast
import datetime as dt
import operator
from dataclasses import dataclass, field

import psycopg
from asyncua import ua

GOOD = int(ua.StatusCodes.Good)
BAD_INPUT = int(ua.StatusCodes.BadDependentValueChanged)
BAD_NO_DATA = int(ua.StatusCodes.BadNoData)
BAD_DIVIDE = int(ua.StatusCodes.BadOutOfRange)
UNCERTAIN_INPUT = int(ua.StatusCodes.UncertainSubstituteValue)
UNCERTAIN_VALIDITY = int(ua.StatusCodes.UncertainSensorNotAccurate)

CLASSIFICATIONS = ("measured", "calculated", "derived_statistical", "ai_ml")


def severity(code: int) -> int:
    return (code >> 30) & 3


class EquationError(ValueError):
    pass


# -- safe expression evaluation ----------------------------------------------

_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow,
    ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_CALLS = {"abs": abs, "min": min, "max": max, "round": round}


class DivisionByZero(ZeroDivisionError):
    """Raised so the caller can name it, rather than letting an infinity or a
    zero escape into a trend."""


def safe_eval(expression: str, names: dict[str, float]) -> float:
    """Evaluate an arithmetic expression over `names`. Nothing else is allowed."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise EquationError(f"cannot parse {expression!r}: {exc}") from exc

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return float(node.value)
            raise EquationError(f"literal {node.value!r} is not a number")
        if isinstance(node, ast.Name):
            if node.id not in names:
                raise EquationError(
                    f"equation references {node.id!r}, which is not an input or "
                    f"a constant of this KPI (has: {sorted(names)})")
            return names[node.id]
        if isinstance(node, ast.BinOp):
            op = _BINOPS.get(type(node.op))
            if op is None:
                raise EquationError(f"operator {type(node.op).__name__} not allowed")
            left, right = walk(node.left), walk(node.right)
            if op in (operator.truediv, operator.mod, operator.floordiv) and right == 0:
                raise DivisionByZero(
                    f"division by zero evaluating {ast.unparse(node)}")
            return op(left, right)
        if isinstance(node, ast.UnaryOp):
            op = _UNARY.get(type(node.op))
            if op is None:
                raise EquationError(f"unary {type(node.op).__name__} not allowed")
            return op(walk(node.operand))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _CALLS:
                raise EquationError("only abs, min, max and round may be called")
            if node.keywords:
                raise EquationError("keyword arguments are not allowed")
            return float(_CALLS[node.func.id](*[walk(a) for a in node.args]))
        raise EquationError(
            f"{type(node).__name__} is not permitted in a KPI equation")

    return float(walk(tree))


# -- definitions and results --------------------------------------------------

@dataclass(frozen=True)
class KpiDefinition:
    id: int
    name: str
    description: str | None
    classification: str
    equation: str
    inputs: dict          # variable name -> attribute name
    constants: dict       # variable name -> number
    engineering_unit: str | None
    reference_value: float | None
    validity_low: float | None
    validity_high: float | None
    calculation_freq_ms: int
    version: int


@dataclass(frozen=True)
class InputValue:
    variable: str
    attribute: str
    tag_name: str | None
    value: float | None
    quality: int
    source_ts: dt.datetime | None

    @property
    def is_good(self) -> bool:
        return self.quality == GOOD and self.value is not None


@dataclass(frozen=True)
class KpiResult:
    definition: KpiDefinition
    element_id: int
    ts: dt.datetime
    quality: int
    reason: str | None = None
    # None whenever quality is not Good. There is deliberately no way to obtain
    # a number from a Bad result: the absence is the point.
    value: float | None = None
    inputs: tuple[InputValue, ...] = field(default_factory=tuple)

    @property
    def is_good(self) -> bool:
        return self.quality == GOOD

    def __post_init__(self) -> None:
        if self.quality != GOOD and self.value is not None:
            raise AssertionError(
                "a KPI result that is not Good must carry no value; returning a "
                "number with a bad quality is the substitution §318 forbids")


# -- computing ----------------------------------------------------------------

def _resolve_inputs(conn: psycopg.Connection, definition: KpiDefinition,
                    element_id: int, at: dt.datetime | None,
                    max_age_s: float) -> list[InputValue]:
    """Find each input's latest value, searching the element's subtree.

    A unit-level KPI draws on attributes that live on the boiler and on the
    generator, so resolution walks down from the element rather than looking
    only at it.
    """
    at = at or dt.datetime.now(dt.timezone.utc)
    out: list[InputValue] = []
    with conn.cursor() as cur:
        for variable, attribute in definition.inputs.items():
            cur.execute(
                "WITH RECURSIVE tree AS ("
                "  SELECT id FROM element WHERE id = %s"
                "  UNION ALL"
                "  SELECT e.id FROM element e JOIN tree ON e.parent_id = tree.id)"
                " SELECT t.name, t.id FROM attribute a"
                " JOIN tree ON tree.id = a.element_id"
                " JOIN tag t ON t.id = a.tag_id"
                " WHERE a.name = %s LIMIT 1", (element_id, attribute))
            found = cur.fetchone()
            if found is None:
                out.append(InputValue(variable, attribute, None, None,
                                      BAD_NO_DATA, None))
                continue
            tag_name, tag_id = found
            cur.execute(
                "SELECT value, quality, source_ts FROM sample"
                " WHERE tag_id = %s AND source_ts <= %s AND source_ts > %s"
                " ORDER BY source_ts DESC LIMIT 1",
                (tag_id, at, at - dt.timedelta(seconds=max_age_s)))
            sample = cur.fetchone()
            if sample is None:
                # No recent sample is not a zero and not the last good value
                # from an hour ago. It is an absence, and it is Bad.
                out.append(InputValue(variable, attribute, tag_name, None,
                                      BAD_NO_DATA, None))
                continue
            value, quality, source_ts = sample
            out.append(InputValue(variable, attribute, tag_name, value,
                                  quality, source_ts))
    return out


def compute(conn: psycopg.Connection, definition: KpiDefinition,
            element_id: int, *, at: dt.datetime | None = None,
            max_age_s: float = 120.0) -> KpiResult:
    """Compute one KPI for one element.

    Quality propagation, in order:
      * any input Bad or absent  -> Bad, naming it
      * any input Uncertain      -> Uncertain, naming it
      * division by zero         -> Bad, naming the expression
      * outside validity range   -> flagged Uncertain, value withheld
    Never a substitution, at any step.
    """
    at = at or dt.datetime.now(dt.timezone.utc)
    inputs = _resolve_inputs(conn, definition, element_id, at, max_age_s)

    bad = [i for i in inputs if severity(i.quality) == 2 or i.value is None]
    if bad:
        names = ", ".join(
            f"{i.attribute}"
            + (f" ({i.tag_name})" if i.tag_name else " (no tag mapped)")
            + (" is " + ua.StatusCode(i.quality).name if i.value is not None
               else (" has no value, " + ua.StatusCode(i.quality).name))
            for i in bad)
        return KpiResult(definition, element_id, at, quality=BAD_INPUT,
                         reason=f"input {names}", inputs=tuple(inputs))

    uncertain = [i for i in inputs if severity(i.quality) == 1]
    if uncertain:
        names = ", ".join(f"{i.attribute} ({i.tag_name}) is "
                          f"{ua.StatusCode(i.quality).name}" for i in uncertain)
        return KpiResult(definition, element_id, at, quality=UNCERTAIN_INPUT,
                         reason=f"input {names}", inputs=tuple(inputs))

    names = {i.variable: float(i.value) for i in inputs}
    names.update({k: float(v) for k, v in definition.constants.items()})
    try:
        value = safe_eval(definition.equation, names)
    except DivisionByZero as exc:
        # At zero load a heat rate is undefined. Not infinity, and emphatically
        # not zero -- a zero heat rate would read as perfect efficiency.
        return KpiResult(definition, element_id, at, quality=BAD_DIVIDE,
                         reason=str(exc), inputs=tuple(inputs))
    except EquationError as exc:
        return KpiResult(definition, element_id, at, quality=BAD_NO_DATA,
                         reason=f"equation error: {exc}", inputs=tuple(inputs))

    low, high = definition.validity_low, definition.validity_high
    if (low is not None and value < low) or (high is not None and value > high):
        return KpiResult(
            definition, element_id, at, quality=UNCERTAIN_VALIDITY,
            reason=(f"result {value:.4g} outside validity range "
                    f"[{low}, {high}] {definition.engineering_unit or ''}".strip()),
            inputs=tuple(inputs))

    return KpiResult(definition, element_id, at, quality=GOOD, value=value,
                     inputs=tuple(inputs))


# -- persistence --------------------------------------------------------------

def store(conn: psycopg.Connection, result: KpiResult) -> None:
    """Record the result, with the definition version that produced it.

    The version is stored on the value row, not looked up through the
    definition, so a later edit to the definition can never silently restate
    history (§320, §379, §381).
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO kpi_value (kpi_definition_id, kpi_version, element_id,"
            " ts, value, quality, reason) VALUES (%s,%s,%s,%s,%s,%s,%s)"
            " ON CONFLICT (kpi_definition_id, element_id, ts) DO UPDATE SET"
            " value = EXCLUDED.value, quality = EXCLUDED.quality,"
            " reason = EXCLUDED.reason, kpi_version = EXCLUDED.kpi_version",
            (result.definition.id, result.definition.version, result.element_id,
             result.ts, result.value, result.quality, result.reason))
    conn.commit()


def load_definitions(conn: psycopg.Connection, *, name: str | None = None,
                     current_only: bool = True) -> list[KpiDefinition]:
    sql = ("SELECT id, name, description, classification, equation, inputs,"
           " constants, engineering_unit, reference_value, validity_low,"
           " validity_high, calculation_freq_ms, version FROM kpi_definition")
    clauses, params = [], []
    if current_only:
        clauses.append("valid_to IS NULL")
    if name:
        clauses.append("name = %s")
        params.append(name)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY name"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [KpiDefinition(*r) for r in cur.fetchall()]


def run_once(conn: psycopg.Connection, *, element_template: str = "GeneratingUnit",
             at: dt.datetime | None = None) -> list[KpiResult]:
    """Compute every current KPI for every element of a template."""
    from engine import assets
    with conn.cursor() as cur:
        cur.execute(
            "WITH RECURSIVE family AS ("
            "  SELECT id FROM element_template WHERE name = %s"
            "  UNION ALL"
            "  SELECT t.id FROM element_template t JOIN family f"
            "    ON t.parent_template_id = f.id)"
            " SELECT e.id, e.asset_code FROM element e"
            " JOIN family ON family.id = e.template_id ORDER BY e.asset_code",
            (element_template,))
        elements = cur.fetchall()

    results = []
    for definition in load_definitions(conn):
        for element_id, _ in elements:
            result = compute(conn, definition, element_id, at=at)
            store(conn, result)
            results.append(result)
    return results


# -- the KPI dictionary (§383) ------------------------------------------------

def dictionary_markdown(conn: psycopg.Connection) -> str:
    """Generate the KPI dictionary from the database.

    Generated, never hand-written, so it cannot drift from what the engine
    actually computes.
    """
    lines = [
        "# KPI dictionary",
        "",
        "Generated from `kpi_definition` by `engine.kpi.dictionary_markdown`.",
        "Do not edit by hand: the database is the source of truth, and a",
        "hand-edited copy would describe equations that are not the ones being",
        "computed.",
        "",
        f"Generated {dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}.",
        "",
    ]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, description, classification, equation, inputs,"
            " constants, engineering_unit, reference_value, validity_low,"
            " validity_high, bad_data_treatment, calculation_freq_ms, version,"
            " valid_from, valid_to FROM kpi_definition ORDER BY name, version")
        rows = cur.fetchall()

    for (name, description, classification, equation, inputs, constants, unit,
         reference, low, high, bad_treatment, freq, version, valid_from,
         valid_to) in rows:
        lines += [
            f"## {name}  (v{version})",
            "",
            description or "",
            "",
            f"| | |",
            f"|---|---|",
            f"| Classification | `{classification}` |",
            f"| Equation | `{equation}` |",
            f"| Engineering unit | {unit or '—'} |",
            f"| Inputs | " + ", ".join(f"`{k}` = {v}" for k, v in inputs.items()) + " |",
            f"| Constants | " + (", ".join(f"`{k}` = {v}" for k, v in constants.items())
                                 or "—") + " |",
            f"| Reference value | {reference if reference is not None else '—'} |",
            f"| Validity range | [{low}, {high}] |",
            f"| Bad-data treatment | `{bad_treatment}` |",
            f"| Calculation frequency | {freq} ms |",
            f"| Valid from | {valid_from.isoformat(timespec='seconds')} |",
            f"| Valid to | {valid_to.isoformat(timespec='seconds') if valid_to else 'current'} |",
            "",
        ]
    lines += [
        "## Bad-data treatment",
        "",
        "Every definition declares `propagate`, and the schema permits no other",
        "value. A KPI with a Bad input returns Bad with a reason naming the",
        "offending input. It does not return zero, the last good value, or an",
        "interpolation (§318).",
        "",
        "A result that is not Good carries no number at all. `KpiResult` raises",
        "if constructed with both, so there is no path by which a bad result",
        "can be read as a figure.",
        "",
        "## Not implemented, deliberately",
        "",
        "Cylinder efficiency and condenser performance require published steam",
        "tables. Under §486 they are implemented from those tables or not at",
        "all, and they are not implemented here. An approximation carrying no",
        "provenance would be worse than their absence.",
        "",
    ]
    return "\n".join(lines)
