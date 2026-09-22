"""Event frames: a state machine over tag history. (§472, §475)

An event frame is a named window with milestones inside it — a thermal start-up
from boiler light-up to full load, a shutdown through coast-down. The frame is
what turns "a lot of samples" into "the unit started at 09:14 and synchronised
eleven minutes later", which is the question an engineer actually asks.

TWO DECISIONS THAT DECIDE WHETHER THIS IS USEFUL.

**Debounce.** Every trigger must hold true for N seconds before it counts. A
transmitter spike that momentarily reads 3000 rpm must not open a start-up, and
a single Bad sample must not close one. The debounce is per trigger, because how
long a condition must persist to be real depends on the condition.

**The milestone timestamp is when the condition BECAME true, not when the
debounce expired.** Those differ by N seconds, and N is an artefact of how
carefully we are watching rather than a fact about the plant. Reporting the
later one would make every start-up look slower than it was, consistently and
invisibly — and the whole point of §475 is comparing one start-up against
another.

Quality travels here too. A milestone reached on a Bad sample is not a
milestone: the trigger is evaluated only on Good samples, and a milestone
records the quality of the sample that satisfied it.
"""

from __future__ import annotations

import datetime as dt
import operator
from dataclasses import dataclass, field

import psycopg

GOOD = 0

_COMPARISONS = {
    ">": operator.gt, ">=": operator.ge,
    "<": operator.lt, "<=": operator.le,
    "==": operator.eq, "!=": operator.ne,
}


@dataclass(frozen=True)
class Trigger:
    """A condition on one tag that must hold for `debounce_s` to count."""
    attribute: str
    comparison: str
    threshold: float
    debounce_s: float = 2.0

    def satisfied_by(self, value: float | None) -> bool:
        if value is None:
            return False
        return _COMPARISONS[self.comparison](value, self.threshold)

    def describe(self) -> str:
        return (f"{self.attribute} {self.comparison} {self.threshold:g} "
                f"for {self.debounce_s:g}s")


@dataclass(frozen=True)
class MilestoneSpec:
    name: str
    trigger: Trigger
    sort_order: int


@dataclass(frozen=True)
class EventTemplate:
    """What an event of this kind looks like."""
    name: str
    start: Trigger                 # opens the frame
    end: Trigger                   # closes it
    milestones: tuple[MilestoneSpec, ...]
    summary_attributes: tuple[str, ...] = ()
    # An event that never reaches its end condition is abandoned rather than
    # left open for ever; a start-up that stalls is a real outcome.
    max_duration_s: float = 86400.0


@dataclass
class Milestone:
    name: str
    ts: dt.datetime
    value: float | None
    quality: int
    sort_order: int


@dataclass
class Frame:
    template: str
    element_id: int
    start_ts: dt.datetime
    end_ts: dt.datetime | None = None
    status: str = "open"
    milestones: list[Milestone] = field(default_factory=list)
    summary: dict[str, dict] = field(default_factory=dict)

    @property
    def duration_s(self) -> float | None:
        if self.end_ts is None:
            return None
        return (self.end_ts - self.start_ts).total_seconds()

    def milestone(self, name: str) -> Milestone | None:
        return next((m for m in self.milestones if m.name == name), None)


class _Debouncer:
    """Tracks when a condition first became continuously true.

    `require_edge` makes it a transition detector rather than a level detector:
    the condition must be observed FALSE before it can fire. That is what the
    start of an event needs. A start-up begins when light-up HAPPENS, not when
    somebody notices light-up is already true — and a level-triggered start
    opens a spurious frame every time detection is run over a window in which
    the unit was already running. Measured: scanning a steady-state window
    produced six frames, each 2.5 s long with every milestone at the same
    offset.

    Milestones inside a frame stay level-triggered, because a milestone is
    "the first moment this held during this event", and the start condition is
    frequently also the first milestone.
    """

    def __init__(self, trigger: Trigger, require_edge: bool = False) -> None:
        self.trigger = trigger
        self.require_edge = require_edge
        self._true_since: dt.datetime | None = None
        self._seen_false = not require_edge
        self.fired = False

    def feed(self, ts: dt.datetime, value: float | None,
             quality: int) -> dt.datetime | None:
        """Returns the instant the condition became true, once it has held for
        the debounce. Returns it once, not on every subsequent sample."""
        if self.fired:
            return None
        # A trigger is evaluated only on Good samples. A milestone reached on a
        # Bad reading is not a milestone. An unknown value (quality -1, nothing
        # received yet) is not evidence of anything either way.
        if quality != GOOD or not self.trigger.satisfied_by(value):
            if quality == GOOD:
                self._seen_false = True
            self._true_since = None
            return None
        if not self._seen_false:
            # Already true when we started looking: not a transition.
            return None
        if self._true_since is None:
            self._true_since = ts
        if (ts - self._true_since).total_seconds() >= self.trigger.debounce_s:
            self.fired = True
            # The instant it BECAME true, not the instant we became sure.
            return self._true_since
        return None

    def reset(self) -> None:
        self._true_since = None
        self._seen_false = not self.require_edge
        self.fired = False


# -- running the state machine ------------------------------------------------

def _series(conn: psycopg.Connection, element_id: int, attribute: str,
            since: dt.datetime, until: dt.datetime
            ) -> list[tuple[dt.datetime, float | None, int]]:
    """One attribute's samples, resolved through the asset model."""
    with conn.cursor() as cur:
        cur.execute(
            "WITH RECURSIVE tree AS ("
            "  SELECT id FROM element WHERE id = %s"
            "  UNION ALL"
            "  SELECT e.id FROM element e JOIN tree ON e.parent_id = tree.id)"
            " SELECT t.id FROM attribute a JOIN tree ON tree.id = a.element_id"
            " JOIN tag t ON t.id = a.tag_id WHERE a.name = %s LIMIT 1",
            (element_id, attribute))
        found = cur.fetchone()
        if found is None:
            return []
        cur.execute(
            "SELECT source_ts, value, quality FROM sample"
            " WHERE tag_id = %s AND source_ts >= %s AND source_ts <= %s"
            " ORDER BY source_ts", (found[0], since, until))
        return cur.fetchall()


def detect(conn: psycopg.Connection, template: EventTemplate, element_id: int,
           since: dt.datetime, until: dt.datetime | None = None) -> list[Frame]:
    """Run the state machine over a window of history.

    Batch rather than streaming, deliberately: an event frame is a statement
    about a span of time, and it can only be made once that span exists. Running
    it over the archive is also how it stays re-runnable — the same history
    yields the same frames, which a streaming state machine with in-memory
    state would not guarantee across a restart.
    """
    until = until or dt.datetime.now(dt.timezone.utc)

    # Gather every attribute the template mentions, once.
    needed = {template.start.attribute, template.end.attribute}
    needed.update(m.trigger.attribute for m in template.milestones)
    needed.update(template.summary_attributes)
    series = {a: _series(conn, element_id, a, since, until) for a in needed}

    # Merge into one time-ordered stream of (ts, attribute, value, quality).
    stream: list[tuple[dt.datetime, str, float | None, int]] = []
    for attribute, rows in series.items():
        stream.extend((ts, attribute, value, quality)
                      for ts, value, quality in rows)
    stream.sort(key=lambda r: r[0])
    if not stream:
        return []

    latest: dict[str, tuple[float | None, int]] = {}
    frames: list[Frame] = []
    current: Frame | None = None
    # The START is edge-triggered; everything else is level-triggered.
    start_debounce = _Debouncer(template.start, require_edge=True)
    end_debounce = _Debouncer(template.end)
    milestone_debouncers: dict[str, _Debouncer] = {}

    def held(trigger: Trigger) -> tuple[float | None, int]:
        """The last known value of a trigger's attribute."""
        return latest.get(trigger.attribute, (None, -1))

    for ts, attribute, value, quality in stream:
        latest[attribute] = (value, quality)

        # EVERY debouncer is fed at EVERY step, against the stream clock and
        # the last known value of its own attribute.
        #
        # Feeding a debouncer only when its own tag reports cannot work for a
        # change-of-state tag. Digitals are written on transition and then send
        # nothing (§440), so a "true for 2 s" debounce on a digital would never
        # be confirmed -- the tag falls silent the instant the condition
        # becomes true. Measured: LightUp produced 4 samples across two
        # start-ups, and no frame was ever opened.
        #
        # Time passes whether or not a particular instrument says so, so the
        # clock that matters is the stream's.
        if current is None:
            held_value, held_quality = held(template.start)
            began = start_debounce.feed(ts, held_value, held_quality)
            if began is not None:
                current = Frame(template.name, element_id, began)
                milestone_debouncers = {
                    m.name: _Debouncer(m.trigger) for m in template.milestones}
                end_debounce.reset()
            else:
                continue

        for spec in template.milestones:
            held_value, held_quality = held(spec.trigger)
            reached = milestone_debouncers[spec.name].feed(
                ts, held_value, held_quality)
            if reached is not None:
                current.milestones.append(Milestone(spec.name, reached,
                                                    held_value, held_quality,
                                                    spec.sort_order))

        held_value, held_quality = held(template.end)
        ended = end_debounce.feed(ts, held_value, held_quality)
        if ended is not None and ended > current.start_ts:
            current.end_ts = ended
            current.status = "closed"
            current.summary = _summarise(series, current)
            current.milestones.sort(key=lambda m: (m.sort_order, m.ts))
            frames.append(current)
            current = None
            start_debounce.reset()
            continue

        if (ts - current.start_ts).total_seconds() > template.max_duration_s:
            current.end_ts = ts
            current.status = "aborted"
            current.summary = _summarise(series, current)
            current.milestones.sort(key=lambda m: (m.sort_order, m.ts))
            frames.append(current)
            current = None
            start_debounce.reset()

    if current is not None:
        current.summary = _summarise(series, current)
        current.milestones.sort(key=lambda m: (m.sort_order, m.ts))
        frames.append(current)          # still open
    return frames


def _summarise(series: dict, frame: Frame) -> dict[str, dict]:
    """Min, max and mean per configured tag over the window.

    Computed over GOOD samples only, with the counts carried alongside. A mean
    over a window that was half Bad is not the same quantity as a mean over a
    clean one, and the summary says which it had.
    """
    out: dict[str, dict] = {}
    end = frame.end_ts or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
    for attribute, rows in series.items():
        window = [(v, q) for ts, v, q in rows if frame.start_ts <= ts <= end]
        good = [v for v, q in window if q == GOOD and v is not None]
        out[attribute] = {
            "min": min(good) if good else None,
            "max": max(good) if good else None,
            "mean": (sum(good) / len(good)) if good else None,
            "sample_count": len(window),
            "good_count": len(good),
        }
    return out


# -- persistence ---------------------------------------------------------------

def store(conn: psycopg.Connection, frame: Frame) -> int:
    """Persist a frame. Re-running detection over the same history updates the
    frame rather than creating a second one."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM event_frame WHERE element_id = %s AND template = %s"
            " AND start_ts = %s", (frame.element_id, frame.template,
                                   frame.start_ts))
        found = cur.fetchone()
        if found:
            frame_id = found[0]
            cur.execute("UPDATE event_frame SET end_ts=%s, status=%s WHERE id=%s",
                        (frame.end_ts, frame.status, frame_id))
        else:
            cur.execute(
                "INSERT INTO event_frame (template, element_id, start_ts, end_ts,"
                " status) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (frame.template, frame.element_id, frame.start_ts, frame.end_ts,
                 frame.status))
            frame_id = cur.fetchone()[0]

        for m in frame.milestones:
            cur.execute(
                "INSERT INTO event_milestone (event_frame_id, name, ts, value,"
                " quality, sort_order) VALUES (%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (event_frame_id, name) DO UPDATE SET"
                " ts = EXCLUDED.ts, value = EXCLUDED.value,"
                " quality = EXCLUDED.quality, sort_order = EXCLUDED.sort_order",
                (frame_id, m.name, m.ts, m.value, m.quality, m.sort_order))

        for attribute, stats in frame.summary.items():
            cur.execute(
                "WITH RECURSIVE tree AS ("
                "  SELECT id FROM element WHERE id = %s"
                "  UNION ALL SELECT e.id FROM element e JOIN tree"
                "    ON e.parent_id = tree.id)"
                " SELECT t.id FROM attribute a JOIN tree ON tree.id = a.element_id"
                " JOIN tag t ON t.id = a.tag_id WHERE a.name = %s LIMIT 1",
                (frame.element_id, attribute))
            tag = cur.fetchone()
            if tag is None:
                continue
            cur.execute(
                "INSERT INTO event_frame_summary (event_frame_id, tag_id,"
                " min_value, max_value, mean_value, sample_count, good_count)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (event_frame_id, tag_id) DO UPDATE SET"
                " min_value=EXCLUDED.min_value, max_value=EXCLUDED.max_value,"
                " mean_value=EXCLUDED.mean_value,"
                " sample_count=EXCLUDED.sample_count,"
                " good_count=EXCLUDED.good_count",
                (frame_id, tag[0], stats["min"], stats["max"], stats["mean"],
                 stats["sample_count"], stats["good_count"]))
    conn.commit()
    return frame_id


# -- comparison (§475) ---------------------------------------------------------

@dataclass(frozen=True)
class MilestoneDelta:
    milestone: str
    this_offset_s: float | None
    other_offset_s: float | None

    @property
    def delta_s(self) -> float | None:
        if self.this_offset_s is None or self.other_offset_s is None:
            return None
        return self.this_offset_s - self.other_offset_s

    def describe(self) -> str:
        if self.this_offset_s is None:
            return f"{self.milestone}: not reached in this event"
        if self.other_offset_s is None:
            return f"{self.milestone}: not reached in the comparison event"
        delta = self.delta_s
        word = "later" if delta > 0 else "earlier"
        return (f"{self.milestone} reached {_hms(abs(delta))} {word} "
                f"({_hms(self.this_offset_s)} against {_hms(self.other_offset_s)})")


def _hms(seconds: float) -> str:
    seconds = abs(float(seconds))
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {sec:04.1f}s" if minutes else f"{sec:.1f}s"
    hours, minutes = divmod(minutes, 60)
    return f"{int(hours)}h {int(minutes)}m {sec:04.1f}s"


def load_frame(conn: psycopg.Connection, frame_id: int) -> Frame | None:
    with conn.cursor() as cur:
        cur.execute("SELECT template, element_id, start_ts, end_ts, status"
                    " FROM event_frame WHERE id = %s", (frame_id,))
        row = cur.fetchone()
        if row is None:
            return None
        frame = Frame(row[0], row[1], row[2], row[3], row[4])
        cur.execute("SELECT name, ts, value, quality, sort_order"
                    " FROM event_milestone WHERE event_frame_id = %s"
                    " ORDER BY sort_order, ts", (frame_id,))
        frame.milestones = [Milestone(*r) for r in cur.fetchall()]
    return frame


def compare(this: Frame, other: Frame) -> list[MilestoneDelta]:
    """Per-milestone deltas, measured from each event's own start."""
    names = []
    for m in sorted(this.milestones + other.milestones,
                    key=lambda m: (m.sort_order, m.name)):
        if m.name not in names:
            names.append(m.name)
    out = []
    for name in names:
        mine, theirs = this.milestone(name), other.milestone(name)
        out.append(MilestoneDelta(
            name,
            (mine.ts - this.start_ts).total_seconds() if mine else None,
            (theirs.ts - other.start_ts).total_seconds() if theirs else None))
    return out


def reference_frame(conn: psycopg.Connection, template: str,
                    element_id: int) -> Frame | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM event_frame WHERE template=%s"
                    " AND element_id=%s AND is_reference", (template, element_id))
        row = cur.fetchone()
    return load_frame(conn, row[0]) if row else None


def best_frame(conn: psycopg.Connection, template: str, element_id: int,
               exclude_id: int | None = None) -> Frame | None:
    """The previous best event: the shortest completed one.

    "Best" is shortest here because for a start-up that is what best means —
    the unit on load soonest. It is stated rather than assumed, since for some
    templates the fastest is emphatically not the best.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM event_frame WHERE template=%s AND element_id=%s"
            " AND status='closed' AND end_ts IS NOT NULL"
            " AND (%s::bigint IS NULL OR id <> %s)"
            " ORDER BY (end_ts - start_ts) ASC LIMIT 1",
            (template, element_id, exclude_id, exclude_id))
        row = cur.fetchone()
    return load_frame(conn, row[0]) if row else None


def frames_for_element(conn: psycopg.Connection, element_id: int,
                       template: str | None = None) -> list[dict]:
    """All event frames for an element with their milestone tables."""
    with conn.cursor() as cur:
        sql = ("SELECT id, template, start_ts, end_ts, status, is_reference"
               " FROM event_frame WHERE element_id = %s")
        params: list = [element_id]
        if template:
            sql += " AND template = %s"
            params.append(template)
        sql += " ORDER BY start_ts DESC"
        cur.execute(sql, params)
        frames = cur.fetchall()
        out = []
        for fid, tmpl, start, end, status, is_ref in frames:
            cur.execute("SELECT name, ts, value, quality, sort_order"
                        " FROM event_milestone WHERE event_frame_id=%s"
                        " ORDER BY sort_order, ts", (fid,))
            milestones = [
                {"name": n, "ts": ts, "offset_s": (ts - start).total_seconds(),
                 "value": v, "quality": q}
                for n, ts, v, q, _ in cur.fetchall()]
            out.append({
                "id": fid, "template": tmpl, "start_ts": start, "end_ts": end,
                "status": status, "is_reference": is_ref,
                "duration_s": (end - start).total_seconds() if end else None,
                "milestones": milestones,
            })
    return out


# -- loading templates from configuration -------------------------------------

def load_templates(path) -> dict[str, EventTemplate]:
    """Event templates are configuration, not code (§341)."""
    import json
    from pathlib import Path
    spec = json.loads(Path(path).read_text())
    out: dict[str, EventTemplate] = {}
    for t in spec["templates"]:
        out[t["name"]] = EventTemplate(
            name=t["name"],
            start=Trigger(**t["start"]),
            end=Trigger(**t["end"]),
            milestones=tuple(
                MilestoneSpec(m["name"], Trigger(**m["trigger"]),
                              m["sort_order"])
                for m in t["milestones"]),
            summary_attributes=tuple(t.get("summary_attributes", ())),
            max_duration_s=float(t.get("max_duration_s", 86400)),
        )
    return out
