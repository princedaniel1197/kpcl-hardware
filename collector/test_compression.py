"""Compression tests.

The reconstruction bound is the one that matters: whatever is discarded must be
recoverable from what is kept, to within CompDev. If that holds, compression is
not loss; if it does not, compression is exactly the silent substitution this
project refuses to do.
"""

from __future__ import annotations

import datetime as dt
import math

import pytest

from collector.compression import (
    BY_MAX_TIME,
    BY_NON_NUMERIC,
    BY_QUALITY_CHANGE,
    BY_TIMESTAMP,
    compress,
    reconstruction_error,
)
from collector.model import Sample

T0 = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
# max_time is required whenever CompDev is set. Tests about the corridor itself
# use one far longer than any series here, so it never fires; the max_time
# exception has its own test.
NO_MAX_TIME = 10 ** 9
GOOD = 0
BAD = 2156593152
UNCERTAIN = 1083375616


def series(values, quality=GOOD, step_s=1.0, start=T0):
    out = []
    for i, v in enumerate(values):
        q = quality[i] if isinstance(quality, (list, tuple)) else quality
        ts = start + dt.timedelta(seconds=i * step_s)
        out.append(Sample(tag_id=1, tag_name="T", source_ts=ts,
                          server_ts=ts + dt.timedelta(milliseconds=40),
                          value=v, quality=q, seq=i + 1))
    return out


# --- the reconstruction bound ------------------------------------------------
#
# Two bounds, and they differ in what they are measured against.
#
#   * The SWINGING DOOR alone guarantees CompDev against the series it is given.
#   * TEXTBOOK two-stage compression guarantees only ExcDev + CompDev against the
#     RAW series, because stage one discards points within ExcDev that the door
#     never sees -- that is what PI documents.
#
# This implementation holds CompDev end to end, against the raw series, because
# the door is shown every raw sample as a witness and verifies each archived
# segment against them (compression.py, SwingingDoor). The end-to-end test
# below is the one that proves it.

SHAPES = {
    "ramp": [i * 0.1 for i in range(600)],
    "sine": [50 + 40 * math.sin(i / 25) for i in range(1000)],
    "step": [0.0] * 200 + [100.0] * 200 + [50.0] * 200,
    "flat": [42.0] * 500,
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("comp_dev", [0.05, 0.2, 1.0, 5.0])
def test_swinging_door_never_exceeds_comp_dev(shape, comp_dev):
    original = series(SHAPES[shape])
    archived, _ = compress(original, None, comp_dev, NO_MAX_TIME)
    worst, compared = reconstruction_error(original, archived)
    assert compared > len(original) * 0.9
    assert worst <= comp_dev + 1e-9, f"{shape}: {worst} > {comp_dev}"


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("comp_dev", [0.1, 0.5, 2.0])
def test_two_stage_end_to_end_never_exceeds_comp_dev(shape, comp_dev):
    """The stronger claim, and the one the build plan asks for.

    It only holds because the door verifies against the RAW samples, including
    those stage one discarded. Without that, a point dropped by exception
    reporting is never checked against the line finally drawn: measured on real
    U1_DRUM_PRESS data the worst error was 0.6247 against a 0.600 bound.
    """
    exc_dev = comp_dev / 2          # the PI convention (§442)
    original = series(SHAPES[shape])
    archived, _ = compress(original, exc_dev, comp_dev, NO_MAX_TIME)
    worst, _ = reconstruction_error(original, archived)
    assert worst <= comp_dev + 1e-9, f"{shape}: {worst} > {comp_dev}"


def test_the_door_alone_holds_on_noise_too():
    """Noise compresses badly -- that is correct -- but the bound still holds."""
    import random
    rng = random.Random(4)
    original = series([2.5 + rng.gauss(0, 0.4) for _ in range(800)])
    archived, _ = compress(original, None, 0.2, NO_MAX_TIME)
    worst, _ = reconstruction_error(original, archived)
    assert worst <= 0.2 + 1e-9


def _textbook_swinging_door(samples, comp_dev):
    """Bristol's method with nothing added: archive the previous point when the
    corridor closes. Written here, independently of compression.py, as the
    thing the verification step is being compared against."""
    kept = [samples[0]]
    anchor = samples[0]
    upper, lower = -math.inf, math.inf
    previous = samples[0]
    for point in samples[1:]:
        dt_s = (point.source_ts - anchor.source_ts).total_seconds()
        upper = max(upper, (point.value - anchor.value - comp_dev) / dt_s)
        lower = min(lower, (point.value - anchor.value + comp_dev) / dt_s)
        if upper > lower:
            kept.append(previous)
            anchor = previous
            dt_s = (point.source_ts - anchor.source_ts).total_seconds()
            upper = (point.value - anchor.value - comp_dev) / dt_s
            lower = (point.value - anchor.value + comp_dev) / dt_s
        previous = point
    kept.append(samples[-1])
    return kept


def test_the_textbook_cone_alone_would_not_hold_this_bound():
    """Why the verification step exists.

    The cone test proves a line within CompDev of every point exists; it does
    not prove the line actually drawn -- anchor to archived point -- is that
    one. On the same sine, textbook swinging door exceeds CompDev and this
    implementation does not."""
    from collector.compression import Archived
    original = series(SHAPES["sine"])
    textbook = [Archived(p, "textbook")
                for p in _textbook_swinging_door(original, 2.0)]
    textbook_worst, _ = reconstruction_error(original, textbook)
    assert textbook_worst > 2.0, (
        f"textbook error {textbook_worst:.3f} no longer exceeds CompDev; the "
        "comparison is not exercising the edge")

    archived, _ = compress(original, None, 2.0, NO_MAX_TIME)
    worst, _ = reconstruction_error(original, archived)
    assert worst <= 2.0 + 1e-9


def test_compdev_without_max_time_is_refused():
    """The open segment is bounded by max_time and by nothing else."""
    from collector.compression import SwingingDoor
    with pytest.raises(ValueError):
        SwingingDoor(1.0, None)


# --- the four exceptions -----------------------------------------------------

def test_quality_change_is_archived_even_though_the_value_did_not_move():
    """The case a deadband would silently swallow: a tag goes Bad while sitting
    perfectly flat."""
    flat = [100.0] * 20
    quality = [GOOD] * 10 + [BAD] * 10
    original = series(flat, quality=quality)
    # A Bad sample carries no value in reality; here the value is held constant
    # deliberately so the ONLY thing that changed is quality.
    archived, stats = compress(original, 1.0, 2.0, NO_MAX_TIME)
    reasons = [a.reason for a in archived]
    assert BY_QUALITY_CHANGE in reasons
    changed = next(a for a in archived if a.reason == BY_QUALITY_CHANGE)
    assert changed.sample.quality == BAD
    assert changed.sample.source_ts == T0 + dt.timedelta(seconds=10)


def test_every_quality_transition_is_archived():
    quality = ([GOOD] * 5 + [UNCERTAIN] * 5 + [BAD] * 5 + [GOOD] * 5)
    original = series([7.0] * 20, quality=quality)
    archived, _ = compress(original, 1.0, 2.0, NO_MAX_TIME)
    transitions = [a for a in archived if a.reason == BY_QUALITY_CHANGE]
    assert len(transitions) == 3
    assert [t.sample.quality for t in transitions] == [UNCERTAIN, BAD, GOOD]


def test_a_value_that_is_not_a_number_is_archived():
    """A Bad DataValue carries no value. 'No value' cannot be interpolated
    between, so it must be archived rather than discarded."""
    original = series([10.0, 10.0, 10.0, None, 10.0, 10.0],
                      quality=[GOOD, GOOD, GOOD, GOOD, GOOD, GOOD])
    archived, _ = compress(original, 1.0, 2.0, NO_MAX_TIME)
    assert BY_NON_NUMERIC in [a.reason for a in archived]
    kept = next(a for a in archived if a.reason == BY_NON_NUMERIC)
    assert kept.sample.value is None


def test_a_timestamp_that_does_not_advance_is_archived():
    original = series([1.0, 1.0, 1.0])
    frozen = original[1]
    backwards = Sample(tag_id=1, tag_name="T",
                       source_ts=frozen.source_ts - dt.timedelta(seconds=5),
                       server_ts=frozen.server_ts, value=1.0, quality=GOOD, seq=9)
    archived, _ = compress([original[0], original[1], backwards, original[2]],
                           1.0, 2.0, NO_MAX_TIME)
    assert BY_TIMESTAMP in [a.reason for a in archived]


def test_max_time_forces_an_archive_on_a_dead_flat_tag():
    """Without this a steady tag produces no rows for hours and is
    indistinguishable from a dead one."""
    original = series([42.0] * 600, step_s=1.0)
    archived, stats = compress(original, 1.0, 2.0, max_time_ms=60_000)
    forced = [a for a in archived if a.reason == BY_MAX_TIME]
    assert len(forced) >= 9, f"expected ~9 forced archives, got {len(forced)}"
    gaps = [(b.sample.source_ts - a.sample.source_ts).total_seconds()
            for a, b in zip(archived, archived[1:])]
    assert max(gaps) <= 60.0


# --- ratios ------------------------------------------------------------------

def test_a_flat_tag_compresses_hard():
    original = series([100.0 + (i % 2) * 0.01 for i in range(1000)])
    archived, stats = compress(original, 0.5, 1.0, NO_MAX_TIME)
    assert stats.ratio > 100


def test_noise_compresses_badly_and_that_is_correct():
    """A noisy tag has a low ratio because there is genuinely little redundancy
    in it. Reporting a high ratio here would mean throwing away real signal."""
    import random
    rng = random.Random(11)
    original = series([2.5 + rng.gauss(0, 0.5) for _ in range(1000)])
    archived, stats = compress(original, 0.05, 0.1, NO_MAX_TIME)
    assert stats.ratio < 3.0


def test_nothing_is_archived_twice_and_order_is_preserved():
    original = series([50 + 40 * math.sin(i / 30) for i in range(500)])
    archived, _ = compress(original, 0.25, 0.5, NO_MAX_TIME)
    times = [a.sample.source_ts for a in archived]
    assert times == sorted(times)
    assert len(times) == len(set(times))


def test_the_first_and_last_points_are_always_kept():
    original = series([i * 0.5 for i in range(200)])
    archived, _ = compress(original, 1.0, 2.0, NO_MAX_TIME)
    assert archived[0].sample.source_ts == original[0].source_ts
    assert archived[-1].sample.source_ts == original[-1].source_ts
