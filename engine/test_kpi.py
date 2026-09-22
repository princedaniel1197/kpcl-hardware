"""KPI engine tests.

Everything here circles one rule: a KPI with a Bad input returns Bad, with a
reason naming the offending input. Not zero, not the last good value, not a
number with a footnote (§318).
"""

from __future__ import annotations

import datetime as dt

import pytest
from asyncua import ua

from engine import kpi

T0 = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
GOOD = 0
BAD_DEVICE = int(ua.StatusCodes.BadDeviceFailure)
UNCERTAIN = int(ua.StatusCodes.UncertainSensorNotAccurate)


def definition(**over) -> kpi.KpiDefinition:
    base = dict(
        id=1, name="GrossUnitHeatRate", description="",
        classification="calculated", equation="coal_flow * gcv / mw",
        inputs={"coal_flow": "CoalFlow", "mw": "GrossGeneration"},
        constants={"gcv": 3500.0}, engineering_unit="kcal/kWh",
        reference_value=2450.0, validity_low=1800.0, validity_high=5000.0,
        calculation_freq_ms=60000, version=1)
    base.update(over)
    return kpi.KpiDefinition(**base)


# --- equations are data, and data is not executed ----------------------------

def test_arithmetic_works():
    assert kpi.safe_eval("coal * gcv / mw",
                         {"coal": 142.0, "gcv": 3500.0, "mw": 210.0}) \
        == pytest.approx(2366.67, abs=0.01)


@pytest.mark.parametrize("attack", [
    "__import__('os').system('id')",
    "open('/etc/passwd').read()",
    "mw.__class__.__mro__",
    "(lambda: 1)()",
    "[x for x in range(3)]",
    "globals()",
])
def test_an_equation_cannot_execute_anything(attack):
    """Equations come out of a database. A database row must never be able to
    run code."""
    with pytest.raises(Exception) as exc:
        kpi.safe_eval(attack, {"mw": 1.0})
    assert isinstance(exc.value, (kpi.EquationError, SyntaxError))


def test_an_equation_cannot_reference_an_unknown_name():
    with pytest.raises(kpi.EquationError, match="not an input or a constant"):
        kpi.safe_eval("coal * secret", {"coal": 1.0})


def test_division_by_zero_is_raised_not_returned():
    """At zero load a heat rate is undefined. Not infinity, and emphatically
    not zero, which would read as perfect efficiency."""
    with pytest.raises(kpi.DivisionByZero):
        kpi.safe_eval("coal / mw", {"coal": 142.0, "mw": 0.0})


# --- a result that is not Good carries no number -----------------------------

def test_a_result_cannot_be_both_bad_and_numeric():
    """Enforced in the type, not by convention. There is no path by which a bad
    result can be read as a figure."""
    with pytest.raises(AssertionError, match="must carry no value"):
        kpi.KpiResult(definition(), 1, T0, quality=BAD_DEVICE, value=2400.0)


def test_a_good_result_carries_its_number():
    r = kpi.KpiResult(definition(), 1, T0, quality=GOOD, value=2400.0)
    assert r.is_good and r.value == 2400.0


# --- quality propagation ------------------------------------------------------

class FakeConn:
    """Stands in for the archive so propagation can be tested exhaustively
    without needing every combination to occur in a live plant."""
    def __init__(self, values: dict[str, tuple]):
        self.values = values
    def cursor(self): return _FakeCur(self.values)


class _FakeCur:
    def __init__(self, values): self.values = values; self.mode = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        if "WITH RECURSIVE tree" in sql:
            self.mode = ("tag", params[1])
        elif "FROM sample" in sql:
            self.mode = ("sample", self.last_tag)
    def fetchone(self):
        kind, key = self.mode
        if kind == "tag":
            self.last_tag = key
            return (f"U1_{key}", hash(key) % 1000)
        value, quality = self.values[self.last_tag]
        return (value, quality, T0)


def compute_with(values: dict[str, tuple], **over) -> kpi.KpiResult:
    return kpi.compute(FakeConn(values), definition(**over), 1, at=T0)


def test_a_bad_input_makes_the_kpi_bad_and_names_it():
    """The build plan's test, in miniature: coal flow goes Bad, heat rate goes
    Bad, and the reason says coal flow."""
    r = compute_with({"CoalFlow": (None, BAD_DEVICE),
                      "GrossGeneration": (210.0, GOOD)})
    assert not r.is_good
    assert kpi.severity(r.quality) == 2
    assert r.value is None
    assert "CoalFlow" in r.reason
    assert "BadDeviceFailure" in r.reason


def test_a_bad_input_does_not_make_the_kpi_zero():
    """The specific failure this project exists to prevent. A zero heat rate
    would read as perfect efficiency."""
    r = compute_with({"CoalFlow": (None, BAD_DEVICE),
                      "GrossGeneration": (210.0, GOOD)})
    assert r.value is None


def test_an_uncertain_input_makes_the_kpi_uncertain_not_bad():
    r = compute_with({"CoalFlow": (142.0, UNCERTAIN),
                      "GrossGeneration": (210.0, GOOD)})
    assert kpi.severity(r.quality) == 1
    assert r.value is None
    assert "CoalFlow" in r.reason


def test_bad_wins_over_uncertain():
    r = compute_with({"CoalFlow": (142.0, UNCERTAIN),
                      "GrossGeneration": (None, BAD_DEVICE)})
    assert kpi.severity(r.quality) == 2
    assert "GrossGeneration" in r.reason


def test_all_good_inputs_give_a_number():
    r = compute_with({"CoalFlow": (142.0, GOOD),
                      "GrossGeneration": (210.0, GOOD)})
    assert r.is_good
    assert r.value == pytest.approx(2366.67, abs=0.01)


def test_zero_load_is_bad_not_infinite():
    r = compute_with({"CoalFlow": (142.0, GOOD),
                      "GrossGeneration": (0.0, GOOD)})
    assert kpi.severity(r.quality) == 2
    assert r.value is None
    assert "division by zero" in r.reason


def test_a_result_outside_its_validity_range_is_flagged_and_withheld():
    """The computation succeeded; the answer is implausible. It is flagged
    Uncertain and the number is not published as if it were trustworthy."""
    r = compute_with({"CoalFlow": (142.0, GOOD), "GrossGeneration": (5.0, GOOD)})
    assert kpi.severity(r.quality) == 1
    assert r.value is None
    assert "outside validity range" in r.reason


def test_the_reason_names_every_offending_input_not_just_the_first():
    r = compute_with({"CoalFlow": (None, BAD_DEVICE),
                      "GrossGeneration": (None, BAD_DEVICE)})
    assert "CoalFlow" in r.reason and "GrossGeneration" in r.reason


# --- classification and versioning -------------------------------------------

def test_classification_is_one_of_the_four_the_tender_names():
    assert set(kpi.CLASSIFICATIONS) == {"measured", "calculated",
                                        "derived_statistical", "ai_ml"}


def test_every_computed_quality_is_a_real_statuscode():
    for code in (kpi.BAD_INPUT, kpi.BAD_NO_DATA, kpi.BAD_DIVIDE,
                 kpi.UNCERTAIN_INPUT, kpi.UNCERTAIN_VALIDITY):
        assert ua.StatusCode(code).name
