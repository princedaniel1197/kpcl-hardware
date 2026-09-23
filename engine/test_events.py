"""Event frame tests (§472, §475)."""

from __future__ import annotations

import datetime as dt


from engine import events
from engine.events import Frame, Milestone, Trigger, _Debouncer

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
GOOD = 0
BAD = 2156593152


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


# --- debounce ----------------------------------------------------------------

def test_a_spike_does_not_fire_a_trigger():
    """A transmitter momentarily reading 3000 rpm must not open a start-up."""
    d = _Debouncer(Trigger("Speed", ">=", 2950, debounce_s=4.0))
    assert d.feed(at(0), 100.0, GOOD) is None
    assert d.feed(at(1), 3000.0, GOOD) is None      # spike begins
    assert d.feed(at(2), 100.0, GOOD) is None       # and ends
    assert d.fired is False


def test_a_sustained_condition_fires_after_the_debounce():
    d = _Debouncer(Trigger("Speed", ">=", 2950, debounce_s=4.0))
    for t in (0, 1, 2, 3):
        assert d.feed(at(t), 3000.0, GOOD) is None
    assert d.feed(at(4), 3000.0, GOOD) == at(0)


def test_the_milestone_time_is_when_it_became_true_not_when_we_were_sure():
    """Those differ by the debounce, and the debounce is an artefact of how
    carefully we are watching rather than a fact about the plant. Reporting the
    later one makes every start-up look slower than it was."""
    d = _Debouncer(Trigger("Speed", ">=", 2950, debounce_s=10.0))
    for t in range(10):
        d.feed(at(t), 3000.0, GOOD)
    assert d.feed(at(10), 3000.0, GOOD) == at(0)


def test_a_trigger_ignores_bad_samples():
    """A milestone reached on a Bad reading is not a milestone."""
    d = _Debouncer(Trigger("Speed", ">=", 2950, debounce_s=2.0))
    for t in range(6):
        assert d.feed(at(t), 3000.0, BAD) is None
    assert d.fired is False


def test_a_trigger_fires_only_once():
    d = _Debouncer(Trigger("MW", ">=", 200, debounce_s=1.0))
    d.feed(at(0), 210.0, GOOD)
    assert d.feed(at(2), 210.0, GOOD) == at(0)
    assert d.feed(at(3), 210.0, GOOD) is None


def test_an_edge_trigger_will_not_fire_on_a_condition_already_true():
    """A start-up begins when light-up HAPPENS. A level-triggered start opens a
    spurious frame every time detection runs over a window in which the unit
    was already running."""
    d = _Debouncer(Trigger("LightUp", ">=", 1, debounce_s=2.0), require_edge=True)
    for t in range(10):
        assert d.feed(at(t), 1.0, GOOD) is None
    assert d.fired is False


def test_an_edge_trigger_fires_once_it_has_seen_the_condition_false():
    d = _Debouncer(Trigger("LightUp", ">=", 1, debounce_s=2.0), require_edge=True)
    d.feed(at(0), 0.0, GOOD)
    d.feed(at(1), 1.0, GOOD)
    assert d.feed(at(3), 1.0, GOOD) == at(1)


def test_a_level_trigger_fires_on_a_condition_already_true():
    """Milestones inside a frame stay level-triggered: a milestone is the first
    moment a condition held during this event."""
    d = _Debouncer(Trigger("LightUp", ">=", 1, debounce_s=2.0))
    d.feed(at(0), 1.0, GOOD)
    assert d.feed(at(2), 1.0, GOOD) == at(0)


# --- comparison --------------------------------------------------------------

def frame(start_s: float, milestones: dict[str, float]) -> Frame:
    f = Frame("ThermalStartup", 1, at(start_s), at(start_s + 400), "closed")
    f.milestones = [Milestone(n, at(start_s + off), 1.0, GOOD, i)
                    for i, (n, off) in enumerate(milestones.items())]
    return f


def test_comparison_reports_a_delta_per_milestone():
    fast = frame(0, {"lightup": 2, "sync": 100, "full_load": 300})
    slow = frame(1000, {"lightup": 2, "sync": 352, "full_load": 600})
    deltas = {d.milestone: d.delta_s for d in events.compare(slow, fast)}
    assert deltas == {"lightup": 0.0, "sync": 252.0, "full_load": 300.0}


def test_comparison_is_measured_from_each_events_own_start():
    """Two start-ups happen at different wall-clock times; only the offsets
    from their own beginnings are comparable."""
    a = frame(0, {"sync": 100})
    b = frame(99999, {"sync": 100})
    assert events.compare(a, b)[0].delta_s == 0.0


def test_comparison_wording_matches_the_clause():
    """§475 asks for 'synchronisation reached 4 minutes 12 seconds later'."""
    fast = frame(0, {"synchronisation": 100})
    slow = frame(0, {"synchronisation": 352})
    text = events.compare(slow, fast)[0].describe()
    assert "synchronisation reached" in text
    assert "later" in text
    assert "4m 12.0s" in text


def test_a_milestone_missing_from_one_event_is_reported_not_guessed():
    reached = frame(0, {"sync": 100, "full_load": 300})
    stalled = frame(0, {"sync": 120})
    deltas = {d.milestone: d for d in events.compare(stalled, reached)}
    assert deltas["full_load"].delta_s is None
    assert "not reached in this event" in deltas["full_load"].describe()


def test_comparison_covers_milestones_present_in_either_event():
    a = frame(0, {"sync": 100})
    b = frame(0, {"sync": 90, "full_load": 300})
    assert {d.milestone for d in events.compare(a, b)} == {"sync", "full_load"}


# --- templates ----------------------------------------------------------------

def test_templates_load_from_configuration():
    templates = events.load_templates("config/event_templates.json")
    assert "ThermalStartup" in templates and "Shutdown" in templates
    startup = templates["ThermalStartup"]
    assert [m.name for m in startup.milestones] == [
        "boiler_lightup", "steam_admission", "turbine_rolling", "rated_speed",
        "synchronisation", "full_load"]
    assert all(m.trigger.debounce_s > 0 for m in startup.milestones)


def test_the_shutdown_template_has_a_coast_down_milestone():
    """§472 names coast-down for shutdown."""
    templates = events.load_templates("config/event_templates.json")
    assert "coast_down" in [m.name for m in templates["Shutdown"].milestones]


def test_hms_formatting():
    assert events._hms(12.3) == "12.3s"
    assert events._hms(252.0) == "4m 12.0s"
    assert events._hms(3725.0) == "1h 2m 05.0s"
