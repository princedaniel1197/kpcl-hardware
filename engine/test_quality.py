"""Quality rule tests (§384) and tag health (§439).

The assertion running through all of these: a value that arrived Good but failed
a check is not the same thing as a value that arrived Bad, and the system can
always say which.
"""

from __future__ import annotations

import datetime as dt

import pytest
from asyncua import ua

from engine import quality as q

T0 = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
GOOD = 0
BAD_DEVICE = int(ua.StatusCodes.BadDeviceFailure)


# --- rule 1: range -----------------------------------------------------------

def test_range_check_flags_above_and_below():
    assert q.check_range(650.0, 0.0, 600.0)[0] == q.OUT_OF_RANGE
    assert q.check_range(-5.0, 0.0, 600.0)[0] == q.OUT_OF_RANGE
    assert q.check_range(300.0, 0.0, 600.0) is None


def test_range_check_reports_which_limit_and_by_what():
    verdict = q.check_range(650.0, 0.0, 600.0)
    assert "above" in verdict[1] and "600" in verdict[1] and "650" in verdict[1]


def test_range_check_is_bad_not_uncertain():
    """A reading outside what the instrument can represent is not a measurement
    at all, so it is Bad rather than merely doubted."""
    assert q.severity(q.OUT_OF_RANGE) == 2


def test_range_check_says_nothing_about_a_value_that_is_absent():
    """A Bad sample carries no value. There is nothing to range check, and
    inventing a verdict would be worse than having none."""
    assert q.check_range(None, 0.0, 600.0) is None


# --- rule 2: rate of change --------------------------------------------------

def test_rate_of_change_flags_an_implausible_step():
    verdict = q.check_rate_of_change(500.0, 100.0, 1.0, max_per_second=50.0)
    assert verdict[0] == q.RATE_EXCEEDED
    assert "400" in verdict[1]


def test_rate_of_change_accepts_a_plausible_ramp():
    assert q.check_rate_of_change(105.0, 100.0, 1.0, max_per_second=50.0) is None


def test_rate_of_change_is_uncertain_not_bad():
    """The reading may be perfectly real; what is doubted is whether to believe
    it. Calling it Bad would discard data that is probably fine."""
    assert q.severity(q.RATE_EXCEEDED) == 1


def test_rate_of_change_scales_with_the_interval():
    # Same step, ten times longer: a tenth of the rate.
    assert q.check_rate_of_change(500.0, 100.0, 10.0, 50.0) is None
    assert q.check_rate_of_change(500.0, 100.0, 1.0, 50.0) is not None


# --- rule 3: cross-tag consistency -------------------------------------------

def test_cross_tag_flags_a_divergence():
    # 670 t/h feedwater is consistent with 210 MW at 3.19 t/h per MW.
    assert q.check_cross_tag(670.0, 210.0, other_name="U1_MW", ratio=3.19,
                             tolerance=0.20) is None
    # 300 t/h at 210 MW is not.
    verdict = q.check_cross_tag(300.0, 210.0, other_name="U1_MW", ratio=3.19,
                                tolerance=0.20)
    assert verdict[0] == q.INCONSISTENT
    assert "U1_MW" in verdict[1] and "%" in verdict[1]


def test_cross_tag_is_uncertain():
    """Two instruments disagree. Which one is wrong is not yet known, so
    neither is declared Bad."""
    assert q.severity(q.INCONSISTENT) == 1


def test_cross_tag_needs_both_values():
    assert q.check_cross_tag(None, 210.0, other_name="x", ratio=1,
                             tolerance=0.1) is None
    assert q.check_cross_tag(670.0, None, other_name="x", ratio=1,
                             tolerance=0.1) is None


# --- rule 4: frozen and stale ------------------------------------------------

def _stamped(values, step_s=5.0, start=T0):
    return [(start + dt.timedelta(seconds=i * step_s), v)
            for i, v in enumerate(values)]


def test_frozen_flags_a_value_that_arrives_but_never_moves():
    """The dangerous failure, because it looks healthy: samples are Good, on
    time, and identical."""
    verdict = q.check_frozen(_stamped([42.0] * 30), max_seconds=120.0)
    assert verdict[0] == q.FROZEN
    assert "unchanged" in verdict[1]


def test_frozen_will_not_claim_more_than_the_history_supports():
    """Twenty seconds of identical samples does not establish that a value has
    been unchanged for five minutes. The earlier implementation could not tell
    those apart -- and, filtering to the window and then demanding the slice
    span the window, could never fire at all."""
    short = _stamped([42.0] * 5, step_s=5.0)        # 20 s of history
    assert q.check_frozen(short, max_seconds=300.0) is None
    assert q.check_frozen(short, max_seconds=15.0) is not None


def test_frozen_fires_when_the_history_covers_the_window():
    samples = _stamped([7.0] * 13, step_s=10.0)      # 120 s of history
    assert q.check_frozen(samples, max_seconds=60.0) is not None


def test_frozen_does_not_fire_on_a_moving_value():
    values = [42.0 + i * 0.01 for i in range(30)]
    assert q.check_frozen(_stamped(values), 120.0, tolerance=0.001) is None


def test_frozen_ignores_samples_that_carry_no_value():
    """A Bad sample has no number. It is not evidence of movement, and it is
    not evidence of stillness either."""
    samples = _stamped([5.0, None, 5.0, None, 5.0] * 6)
    verdict = q.check_frozen(samples, max_seconds=60.0)
    assert verdict is not None and verdict[0] == q.FROZEN


def test_frozen_tolerance_decides_how_much_dither_still_counts_as_stuck():
    """The tolerance is how much movement still counts as not moving, so a
    LARGER tolerance flags more readily, not less.

    That direction is worth stating because it is the opposite of a range or
    rate limit, where a larger threshold is more permissive."""
    dither = _stamped([42.0 + (i % 2) * 0.0005 for i in range(30)])
    assert q.check_frozen(dither, 120.0, tolerance=0.0001) is None
    assert q.check_frozen(dither, 120.0, tolerance=0.001) is not None


def test_stale_flags_silence():
    verdict = q.check_stale(T0, T0 + dt.timedelta(seconds=300), max_seconds=60)
    assert verdict[0] == q.STALE
    assert "300" in verdict[1]


def test_stale_is_bad_because_there_is_no_measurement_to_judge():
    assert q.severity(q.STALE) == 2


def test_stale_does_not_fire_inside_its_limit():
    assert q.check_stale(T0, T0 + dt.timedelta(seconds=30), 60) is None


# --- the distinction that matters --------------------------------------------

def test_every_computed_code_is_a_real_opcua_statuscode():
    """Quality is the numeric StatusCode everywhere, including when this module
    computes it. Not a string, not an enum of our own."""
    for code in (q.OUT_OF_RANGE, q.RATE_EXCEEDED, q.INCONSISTENT, q.FROZEN,
                 q.STALE):
        assert ua.StatusCode(code).name


def test_arrived_bad_and_failed_a_check_are_different_codes():
    """The heart of §318. BadDeviceFailure means the instrument disowns the
    reading; BadOutOfRange means we do. They must not be the same value."""
    assert BAD_DEVICE != q.OUT_OF_RANGE
    assert ua.StatusCode(BAD_DEVICE).name != ua.StatusCode(q.OUT_OF_RANGE).name


def test_health_reports_the_worst_state_without_losing_the_others():
    health = q.Health(1, "T", T0, T0, is_bad=False, is_frozen=True,
                      is_out_of_range=True)
    assert health.worst_state == "frozen"
    # The others are still there to be read.
    assert health.is_out_of_range is True


def test_missing_and_stale_are_different_states():
    """Never seen is not the same as seen and gone quiet."""
    never = q.Health(1, "T", T0, None, is_missing=True)
    quiet = q.Health(1, "T", T0, T0, is_stale=True)
    assert never.worst_state == "missing"
    assert quiet.worst_state == "stale"
